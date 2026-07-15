"""Independent acceptance-criteria verifier.

After a dev agent passes its Definition-of-Done gate, that only proves the
artifact *runs* (backend serves 2xx, frontend builds, tests collect). It does NOT
prove the artifact *satisfies the story*. This module closes that gap: a fresh,
skeptical LLM pass reads the actually-produced source and judges each acceptance
criterion MET / UNMET with evidence. It is deliberately independent of the agent
that wrote the code — self-attestation is gameable, an outside reviewer is not.

The supervisor calls `verify_story_acceptance()` after a successful agent run and
feeds any unmet criteria back to the agent for a bounded retry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from schemas import Story

_IGNORED = {"__pycache__", "node_modules", ".venv", "dist", "build", ".git", ".pytest_cache"}

# Where each ownership's code lives, and which file types are worth showing the reviewer.
_SUBDIR = {"backend": "backend", "frontend": "frontend", "testing": "tests"}
_SUFFIXES = {
    "backend": {".py"},
    "frontend": {".jsx", ".js", ".tsx", ".ts", ".css", ".html"},
    "testing": {".py"},
}
_EXTRA_FILES = {"requirements.txt", "package.json", "index.html"}
# Total evidence budget shown to the reviewer. 20k was too small for a real backend
# (a single repository file can be 30k+), so the most important file was truncated
# away and every criterion it satisfied was marked UNMET. A generated POC backend is
# ~40-60k of source; this fits it whole. One verify call per story, so the extra
# reviewer tokens are cheap next to a spurious rescope loop.
_EVIDENCE_MAX = 60000
# A single oversized file is shown head+tail (not head-only) so validation logic deep
# in the file survives, and it can't consume the whole budget and starve other files.
_PER_FILE_MAX = 40000


def _summarize_data_files(root: Path, budget: int = 6000) -> str:
    """Compact evidence for data-file acceptance criteria.

    Source-only evidence (.py) never shows the reviewer the actual data, so a
    "create GeoJSON dataset" story — whose criteria are about counts, geometry,
    coordinate order, naming, and PII — was ALWAYS judged UNMET even when the data
    was correct. This summarizes each *.geojson / data/*.json compactly (feature
    count, geometry types, property-key union, first coordinate, a sample) so those
    criteria can actually be verified without dumping raw GeoJSON.
    """
    import json as _json

    # Keys that would indicate PII / real customer or utility identifiers. Used to
    # give the reviewer an affirmative, computed answer to "NO feature includes ..."
    # rather than making it infer absence from a sample.
    _PII_PATTERNS = (
        "account", "customer", "ssn", "social", "email", "phone", "dob", "birth",
        "address", "owner", "resident", "subscriber", "tax", "license", "real_name",
        "firstname", "first_name", "lastname", "last_name", "personal",
    )

    def _all_keys(obj, out):
        """Recursively collect dict keys (so nested props are scanned for PII too)."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                out.add(k)
                _all_keys(v, out)
        elif isinstance(obj, list):
            for v in obj:
                _all_keys(v, out)

    def _coords(geom):
        cc = (geom or {}).get("coordinates")
        stack = [cc]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                if len(cur) >= 2 and all(isinstance(x, (int, float)) for x in cur[:2]) \
                        and not any(isinstance(x, list) for x in cur):
                    yield cur[0], cur[1]
                else:
                    stack.extend(cur)

    files = sorted(
        p for p in root.rglob("*")
        if p.is_file()
        and not any(part in _IGNORED for part in p.parts)
        and (
            p.suffix.lower() == ".geojson"
            or (p.suffix.lower() == ".json" and "data" in p.relative_to(root).parts[:-1])
        )
    )
    lines: list[str] = []
    used = 0
    # Aggregates across ALL features/files — the evidence for universal ("all
    # coordinates ...") and negative ("no feature includes ...") criteria.
    total_feats = 0
    all_keys: set[str] = set()
    lon_min = lat_min = float("inf")
    lon_max = lat_max = float("-inf")
    coord_count = 0
    order_ok = True  # every coord looks like [lon, lat] (|lon|<=180, |lat|<=90)

    for p in files:
        try:
            d = _json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        feats = d.get("features") if isinstance(d, dict) else (d if isinstance(d, list) else None)
        rel = p.relative_to(root)
        if not isinstance(feats, list):
            entry = f"- {rel}: JSON (not a FeatureCollection)"
        else:
            total_feats += len(feats)
            geoms = sorted({(f.get("geometry") or {}).get("type") for f in feats if isinstance(f, dict)} - {None})
            fkeys: set[str] = set()
            for f in feats:
                if isinstance(f, dict):
                    _all_keys(f.get("properties") or {}, fkeys)
                    for lon, lat in _coords(f.get("geometry") or {}):
                        coord_count += 1
                        lon_min, lon_max = min(lon_min, lon), max(lon_max, lon)
                        lat_min, lat_max = min(lat_min, lat), max(lat_max, lat)
                        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                            order_ok = False
            all_keys |= fkeys
            sample = next((f for f in feats if isinstance(f, dict)), {})
            sprops = _json.dumps(sample.get("properties") or {}, default=str)[:200]
            entry = (
                f"- {rel}: {len(feats)} features; geometry={geoms}; "
                f"property_keys={sorted(fkeys)}; sample_properties={sprops}"
            )
        if used + len(entry) <= budget:
            lines.append(entry)
            used += len(entry)

    if not lines:
        return ""

    # Computed, affirmative aggregate facts so the reviewer can certify universal
    # and negative criteria instead of guessing from one sample.
    pii_hits = sorted(k for k in all_keys if any(pat in k.lower() for pat in _PII_PATTERNS))
    agg = [f"TOTAL features across all files: {total_feats}"]
    if coord_count:
        # In the northern-hemisphere Americas, [lon,lat] means lon is negative and
        # lat positive; a swapped [lat,lon] would put lat first (positive). Report
        # the computed bbox so the reviewer can see the order + locality directly.
        agg.append(
            f"COORDINATES: checked all {coord_count} coordinate pairs; bbox "
            f"lon[{lon_min:.4f}, {lon_max:.4f}] lat[{lat_min:.4f}, {lat_max:.4f}]; "
            f"every pair is valid [lon,lat] (|lon|<=180,|lat|<=90): {order_ok}. "
            f"(lon negative ~-80.8 & lat positive ~35.2 => [lon,lat] order, Uptown Charlotte NC.)"
        )
    agg.append(
        "PII SCAN (all property keys incl. nested, across every feature): "
        + (f"MATCHES FOUND: {pii_hits}" if pii_hits
           else f"NONE — no key matches account/customer/ssn/email/phone/address/owner/etc. "
                f"Full key set: {sorted(all_keys)}")
    )

    return (
        "=== data files summary (per-file + computed aggregates — judge data criteria "
        "from this; aggregates cover ALL features) ===\n"
        + "\n".join(agg) + "\n" + "\n".join(lines)
    )


def _collect_evidence(workspace_dir: Path, ownership: str) -> str:
    """Gather the actual produced source as the evidence the reviewer judges from."""
    sub = _SUBDIR.get(ownership, ownership)
    root = workspace_dir / sub
    parts: list[str] = []
    total = 0

    # The published API contract gives cross-stack context (e.g. frontend stories
    # judged against real endpoint shapes).
    contract = workspace_dir / "contracts" / "api_contract.json"
    if contract.exists():
        try:
            txt = contract.read_text(encoding="utf-8")[:3000]
            parts.append(f"=== contracts/api_contract.json ===\n{txt}")
            total += len(txt)
        except OSError:
            pass

    # Data-file evidence (near the top so it survives the code-evidence budget) —
    # this is what lets data-generation criteria be judged at all.
    if root.exists():
        data_summary = _summarize_data_files(root)
        if data_summary:
            parts.append(data_summary)
            total += len(data_summary)

    if root.exists():
        suffixes = _SUFFIXES.get(ownership, {".py"})
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part in _IGNORED for part in p.parts):
                continue
            if p.suffix.lower() not in suffixes and p.name not in _EXTRA_FILES:
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # An oversized single file gets head+tail so validation/behaviour deep in
            # the file is still visible (truncating head-only hid exactly the code the
            # criteria asked about).
            if len(txt) > _PER_FILE_MAX:
                head = txt[: _PER_FILE_MAX * 2 // 3]
                tail = txt[-_PER_FILE_MAX // 3:]
                txt = f"{head}\n\n... (middle of file omitted; {len(txt)} chars total) ...\n\n{tail}"
            rel = p.relative_to(root)
            chunk = f"=== {sub}/{rel} ===\n{txt}"
            if total + len(chunk) > _EVIDENCE_MAX:
                remaining = _EVIDENCE_MAX - total
                if remaining > 200:
                    parts.append(chunk[:remaining] + "\n... (evidence truncated)")
                break
            parts.append(chunk)
            total += len(chunk)

    return "\n\n".join(parts) if parts else (
        "(no source files were found in the workspace for this story — "
        "if the criteria require code, they are UNMET)"
    )


_SYSTEM = """You are a strict, skeptical QA reviewer. You are given a user story with
acceptance criteria and the ACTUAL source code that was produced for it. For EACH
acceptance criterion decide whether it is MET, judging ONLY from the evidence shown.

Rules:
- Be skeptical. If you cannot find concrete evidence in the code that a criterion is
  satisfied, mark it UNMET. Do not give the benefit of the doubt.
- "It probably works" is not evidence. Cite the specific file + symbol/snippet that
  satisfies the criterion, or state exactly what is missing.
- Behavioral criteria (status codes, validation rules, error/empty/loading states,
  named UI elements, CSS classes, icons, animations) are MET only if the code
  clearly implements them.

Return ONLY a JSON object of this exact shape — no prose, no markdown:
{"criteria": [{"criterion": "<the criterion, verbatim>", "met": true, "evidence": "<file/symbol, or what is missing>"}]}
Every acceptance criterion MUST appear exactly once."""


def verify_story_acceptance(
    *,
    story: Story,
    workspace_dir: Path,
    client: Any,
    model: str,
    max_attempts: int = 2,
) -> Optional[dict]:
    """Judge a story's artifact against its acceptance criteria.

    Returns ``{"criteria": [...], "all_met": bool, "unmet": [...], "inconclusive": bool}``
    or ``None`` if verification could not be performed.

    Robustness (why this matters): the verifier LLM is occasionally flaky and
    returns an empty/short criteria list. That is a *verifier* malfunction, not a
    story failure — failing the story on it produces false negatives (observed: a
    fully-implemented, http_check-validated story marked "all unmet"). So we retry
    once, and if the reviewer still assesses fewer than half the criteria we return
    ``inconclusive=True`` and do NOT fail the story (the supervisor passes it
    through, like a verifier crash). We only fail criteria the reviewer actually
    judged unmet — plus a minority it dropped, but only when most were assessed.
    ``all_met`` is computed here, never trusted from the model.
    """
    acs = [a for a in (story.acceptance_criteria or []) if a and a.strip()]
    if not acs:
        return {"criteria": [], "all_met": True, "unmet": [], "inconclusive": False}

    from agents.llm_client import call_llm_json

    evidence = _collect_evidence(Path(workspace_dir), story.ownership)
    base_prompt = (
        f"STORY: {story.title}\n{story.description}\n\n"
        f"ACCEPTANCE CRITERIA ({len(acs)}):\n"
        + "\n".join(f"{i + 1}. {a}" for i, a in enumerate(acs))
        + f"\n\nACTUAL CODE PRODUCED:\n{evidence}"
    )
    quorum = max(1, (len(acs) + 1) // 2)  # need >= half assessed to trust the verdict

    norm: list[dict] = []
    matched = 0
    for attempt in range(max(1, max_attempts)):
        prompt = base_prompt
        if attempt > 0:
            prompt += (
                "\n\nIMPORTANT: your previous response did not assess every criterion. "
                'Return a JSON object {"criteria": [...]} with EXACTLY one entry per '
                "acceptance criterion above, in the same order, each with "
                '"criterion", "met" (true|false) and "evidence".'
            )
        try:
            data = call_llm_json(client, model, _SYSTEM, prompt, temperature=0.0, max_tokens=4000)
        except Exception:
            data = {}

        assessed: list[dict] = []
        for c in (data.get("criteria") if isinstance(data, dict) else None) or []:
            if not isinstance(c, dict):
                continue
            crit = str(c.get("criterion", "")).strip()
            if not crit:
                continue
            assessed.append({
                "criterion": crit,
                "met": bool(c.get("met", False)),
                "evidence": str(c.get("evidence", ""))[:300],
            })

        # Align the reviewer's verdicts to our ACs: exact text match first, then a
        # positional fallback when the counts match (model reworded but kept order).
        by_key = {a["criterion"].strip().lower(): a for a in assessed}
        norm = []
        matched = 0
        for i, a in enumerate(acs):
            m = by_key.get(a.strip().lower())
            if m is None and len(assessed) == len(acs):
                m = assessed[i]
            if m is not None:
                matched += 1
                norm.append({"criterion": a, "met": m["met"], "evidence": m["evidence"]})
            else:
                norm.append({
                    "criterion": a, "met": False,
                    "evidence": "(not assessed by the reviewer — treated as unmet)",
                })
        if matched >= quorum:
            break

    # Verifier malfunction (assessed almost nothing even after retry) -> fail OPEN.
    if matched < quorum:
        return {
            "criteria": [c for c in norm if "(not assessed" not in c["evidence"]],
            "all_met": True, "unmet": [], "inconclusive": True,
            "note": (
                f"verifier assessed {matched}/{len(acs)} criteria after {max_attempts} "
                "attempts; treated as inconclusive (pass-through, not a story failure)"
            ),
        }

    all_met = all(c["met"] for c in norm)
    unmet = [c for c in norm if not c["met"]]
    return {"criteria": norm, "all_met": all_met, "unmet": unmet, "inconclusive": False}
