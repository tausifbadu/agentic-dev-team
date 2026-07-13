"""Contract-driven dummy-data generation for the test harness.

Produces deterministic, contract-valid sample instances from a published
`api_contract.json` (field types + enum vocabularies). Two uses:

  * `sample_model()` / `contract_sample` tool — one (or a few) valid instances of a
    named model, for the agent to build request payloads that never 400 on an enum.
  * `generate_seed_data()` / `ensure_seed_data()` — a persistent `tests/seed_data.json`
    the harness loads to populate list/filter/pagination screens and as demo data.

Determinism is the point: a fixed seed + index-cycled enum spread means the same
inputs always yield the same data, so run-to-run comparisons in the ledger are
meaningful. Generated values are contract-valid by construction (enum fields draw
only from the backend's accepted vocabulary), reusing the same enum machinery that
`check_contract` lints against.

User ownership: `ensure_seed_data` is create-if-missing and honors a
`"_managed": "manual"` marker — it never overwrites a hand-edited seed. Regeneration
is explicit and backs up the previous file first.
"""

from __future__ import annotations

import json
import random
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_FIXED_SEED = 1337
_DEFAULT_PER_MODEL = 6
_FIXED_TS = "2026-01-01T12:00:00+00:00"  # stable timestamp for reproducible diffs


# --------------------------------------------------------------------------- #
# Type parsing
# --------------------------------------------------------------------------- #

def _unwrap(type_str: str) -> tuple[str, bool, bool]:
    """Return (base_type, is_optional, is_list) for a contract field type string
    like 'Optional[float]', 'List[CustomerResponse]', 'Union[str, None]'."""
    t = (type_str or "str").strip()
    is_optional = False
    is_list = False
    # Optional[X] / Union[X, None]
    m = re.match(r"Optional\[(.+)\]$", t)
    if m:
        is_optional, t = True, m.group(1).strip()
    m = re.match(r"Union\[(.+)\]$", t)
    if m:
        parts = [p.strip() for p in _split_top(m.group(1))]
        non_none = [p for p in parts if p not in ("None", "NoneType")]
        if len(non_none) != len(parts):
            is_optional = True
        t = non_none[0] if non_none else "str"
    m = re.match(r"(?:List|list|Sequence)\[(.+)\]$", t)
    if m:
        is_list, t = True, m.group(1).strip()
    return t, is_optional, is_list


def _split_top(s: str) -> list[str]:
    """Split a comma list at top bracket depth ('str, List[Any]' -> ['str','List[Any]'])."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


# --------------------------------------------------------------------------- #
# Value generation
# --------------------------------------------------------------------------- #

def _str_value(field: str, rng: random.Random, idx: int) -> str:
    f = field.lower()
    if "email" in f:
        return f"user{idx + 1}@example.com"
    if f in ("name", "full_name", "customer_name", "username"):
        first = ["Ava", "Ben", "Cara", "Dan", "Eve", "Finn", "Gia", "Hugo"][idx % 8]
        return f"{first} {['Ng','Lee','Diaz','Kaur','Roy','Vega'][idx % 6]}"
    if "title" in f or "subject" in f:
        return f"Sample {field} {idx + 1}"
    if "description" in f or "body" in f or "message" in f or "notes" in f:
        return f"Auto-generated {field} for testing (#{idx + 1})."
    if "address" in f:
        return f"{100 + idx} Main St"
    if "city" in f:
        return ["Austin", "Denver", "Miami", "Boston", "Seattle", "Reno"][idx % 6]
    if "country" in f:
        return ["USA", "Canada", "UK", "India", "Germany", "Japan"][idx % 6]
    if "region" in f or "state" in f:
        return ["TX", "CO", "FL", "MA", "WA", "NV"][idx % 6]
    if "phone" in f:
        return f"+1-555-01{idx:02d}"
    if "url" in f or "link" in f:
        return f"https://example.com/{idx + 1}"
    if f.endswith("_id") or f == "id":
        return f"{field}-{idx + 1}"
    return f"{field}_{idx + 1}"


def _field_value(
    field: str, type_str: str, enums: dict[str, list[str]],
    rng: random.Random, idx: int, *, optional_fill: bool = True,
) -> Any:
    base, is_optional, is_list = _unwrap(type_str)

    def scalar() -> Any:
        # Enum field: draw from the backend's accepted vocabulary, cycled for spread.
        if base in enums and enums[base]:
            vals = enums[base]
            return vals[idx % len(vals)]
        b = base.lower()
        if b in ("str", "string", "uuid"):
            return _str_value(field, rng, idx)
        if b in ("int", "integer"):
            if "lat" in field.lower():
                return 30 + idx
            return idx + 1
        if b in ("float", "number", "decimal"):
            fl = field.lower()
            if "lat" in fl:
                return round(30.0 + idx * 0.5, 4)
            if "lon" in fl or "lng" in fl:
                return round(-97.0 - idx * 0.5, 4)
            return round(1.5 * (idx + 1), 2)
        if b in ("bool", "boolean"):
            return idx % 2 == 1
        if b in ("datetime", "date"):
            return _FIXED_TS
        if b.startswith("dict") or b == "any" or b.startswith("mapping"):
            return {}
        # Unknown / nested model type -> a small stub object.
        return {}

    if is_list:
        # A single-element list keeps validators that expect a non-empty list happy
        # while staying deterministic.
        return [scalar()]
    if is_optional and not optional_fill:
        return None
    return scalar()


# --------------------------------------------------------------------------- #
# Public generators
# --------------------------------------------------------------------------- #

def _models(contract: dict) -> dict[str, dict]:
    return {m.get("name", ""): (m.get("fields") or {})
            for m in (contract.get("models") or []) if m.get("name")}


def _enums(contract: dict) -> dict[str, list[str]]:
    return {k: list(v) for k, v in (contract.get("enums") or {}).items()}


def sample_model(model_name: str, contract: dict, count: int = 1,
                 seed: int = _FIXED_SEED) -> list[dict]:
    """Return `count` deterministic, contract-valid instances of a model.
    Skips `id`/`created_at`/server-set fields so the result is a valid *input* payload."""
    fields = _models(contract).get(model_name)
    if fields is None:
        return []
    enums = _enums(contract)
    rng = random.Random(seed)
    skip = {"id", "created_at", "updated_at", "submitted_at"}
    out: list[dict] = []
    for idx in range(max(1, count)):
        inst: dict[str, Any] = {}
        for fname, ftype in fields.items():
            if fname in skip:
                continue
            inst[fname] = _field_value(fname, ftype, enums, rng, idx)
        out.append(inst)
    return out


def validate_seed_payload(payload: Any, contract: dict) -> list[str]:
    """Return a list of human-readable problems with a seed payload (empty = valid).

    Enum fields must use the backend's accepted vocabulary — the same drift that
    `check_contract` guards against, applied to seed rows so a hand-edit can't quietly
    introduce a value the backend 400s on."""
    issues: list[str] = []
    if not isinstance(payload, dict):
        return ["seed must be a JSON object with a 'data' map"]
    data = payload.get("data")
    if not isinstance(data, dict):
        return ["seed must contain a 'data' object mapping model -> [rows]"]
    models, enums = _models(contract), _enums(contract)
    for model, rows in data.items():
        fields = models.get(model, {})
        if not isinstance(rows, list):
            issues.append(f"{model}: rows must be a list")
            continue
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                issues.append(f"{model}[{i}]: not an object")
                continue
            for fname, ftype in fields.items():
                base, _, is_list = _unwrap(ftype)
                if base in enums and fname in row and row[fname] is not None:
                    vals = row[fname] if is_list and isinstance(row[fname], list) else [row[fname]]
                    for v in vals:
                        if v not in enums[base]:
                            issues.append(
                                f"{model}[{i}].{fname}='{v}' is not in {base} "
                                f"{enums[base]} (backend would reject it)")
    return issues


def _input_models(models: dict[str, dict]) -> list[str]:
    """Prefer request/create-shaped models (what you POST). Fall back to all models."""
    inputs = [
        name for name in models
        if re.search(r"(Create|Request|Input|Payload|New|In)$", name)
        or "Create" in name
    ]
    if inputs:
        return inputs
    # Drop obvious read/response shapes if any input-like remain; else keep everything.
    return [n for n in models if not re.search(r"(Response|List|Query|Params|Out)$", n)] or list(models)


def generate_seed_data(contract: dict, per_model: int = _DEFAULT_PER_MODEL,
                       seed: int = _FIXED_SEED) -> dict:
    """Build the full seed structure: N valid instances per input-shaped model."""
    models = _models(contract)
    data = {name: sample_model(name, contract, per_model, seed)
            for name in _input_models(models)}
    return {
        "_managed": "auto",
        "_seed": seed,
        "_per_model": per_model,
        "data": data,
    }


# --------------------------------------------------------------------------- #
# Workspace file management (create-if-missing / explicit regenerate)
# --------------------------------------------------------------------------- #

def _backup(seed_path: Path) -> Optional[str]:
    if not seed_path.exists():
        return None
    backups = seed_path.parent / ".backups"
    try:
        backups.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = backups / f"seed_data_{ts}.json"
        shutil.copy2(seed_path, dest)
        # keep last 10
        olds = sorted(backups.glob("seed_data_*.json"))
        for f in olds[:-10]:
            try:
                f.unlink()
            except OSError:
                pass
        return str(dest)
    except OSError:
        return None


def ensure_seed_data(tests_dir, contract: dict, *, regenerate: bool = False,
                     per_model: int = _DEFAULT_PER_MODEL,
                     seed: int = _FIXED_SEED) -> dict:
    """Create `tests/seed_data.json` from the contract if missing.

    Returns {"status": created|exists|regenerated|locked|skipped, "path", "backup"}.
    Never overwrites unless `regenerate=True`; a `"_managed": "manual"` file is
    always left untouched (locked). Regeneration backs up the previous file first.
    """
    tests_dir = Path(tests_dir)
    seed_path = tests_dir / "seed_data.json"

    if seed_path.exists():
        try:
            existing = json.loads(seed_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if str(existing.get("_managed", "")).lower() == "manual":
            return {"status": "locked", "path": str(seed_path), "backup": None}
        if not regenerate:
            return {"status": "exists", "path": str(seed_path), "backup": None}

    if not (contract.get("models") or []):
        return {"status": "skipped", "path": str(seed_path),
                "backup": None, "reason": "no models in contract"}

    backup = _backup(seed_path) if seed_path.exists() else None
    payload = generate_seed_data(contract, per_model=per_model, seed=seed)
    try:
        tests_dir.mkdir(parents=True, exist_ok=True)
        seed_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "path": str(seed_path), "backup": backup, "reason": str(exc)}
    return {
        "status": "regenerated" if backup else "created",
        "path": str(seed_path), "backup": backup,
        "models": list(payload["data"].keys()),
    }
