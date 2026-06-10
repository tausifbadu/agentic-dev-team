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
_EVIDENCE_MAX = 20000


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
) -> Optional[dict]:
    """Judge a story's artifact against its acceptance criteria.

    Returns a dict ``{"criteria": [...], "all_met": bool, "unmet": [...]}`` or
    ``None`` if verification could not be performed (caller should not block on a
    verifier failure). ``all_met`` is computed here, not trusted from the model.
    """
    acs = [a for a in (story.acceptance_criteria or []) if a and a.strip()]
    if not acs:
        return {"criteria": [], "all_met": True, "unmet": []}

    from agents.llm_client import call_llm_json

    evidence = _collect_evidence(Path(workspace_dir), story.ownership)
    user_prompt = (
        f"STORY: {story.title}\n{story.description}\n\n"
        f"ACCEPTANCE CRITERIA ({len(acs)}):\n"
        + "\n".join(f"{i + 1}. {a}" for i, a in enumerate(acs))
        + f"\n\nACTUAL CODE PRODUCED:\n{evidence}"
    )

    data = call_llm_json(
        client, model, _SYSTEM, user_prompt,
        temperature=0.0, max_tokens=4000,
    )

    raw = data.get("criteria") if isinstance(data, dict) else None
    norm: list[dict] = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        norm.append({
            "criterion": str(c.get("criterion", "")).strip(),
            "met": bool(c.get("met", False)),
            "evidence": str(c.get("evidence", ""))[:300],
        })

    # Fail-safe: any criterion the reviewer dropped is treated as UNMET so the
    # gate can never pass by the model simply omitting a hard criterion.
    covered = {c["criterion"].strip().lower() for c in norm}
    for a in acs:
        if a.strip().lower() not in covered:
            norm.append({
                "criterion": a,
                "met": False,
                "evidence": "(not assessed by the reviewer — treated as unmet)",
            })

    all_met = all(c["met"] for c in norm) if norm else True
    unmet = [c for c in norm if not c["met"]]
    return {"criteria": norm, "all_met": all_met, "unmet": unmet}
