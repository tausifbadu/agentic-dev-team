"""Shared reasoning helpers for code agents."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


IGNORED_PARTS = {
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    "dist",
    "build",
    ".git",
}


def load_json(path: Path, default: dict | list | None = None):
    """Load JSON with a safe default."""
    if not path.exists():
        return {} if default is None else default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: dict | list) -> None:
    """Save JSON with UTF-8 encoding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def normalize_scope_entries(raw_entries: list) -> list[dict]:
    """Support old string-based scope entries and new dict-based entries."""
    normalized: list[dict] = []
    for entry in raw_entries:
        if isinstance(entry, str):
            normalized.append({"id": entry})
        elif isinstance(entry, dict) and entry.get("id"):
            normalized.append(entry)
    return normalized


def update_scope(scope_path: Path, entry: dict) -> dict:
    """Add or update a scope entry keyed by id."""
    scope = load_json(scope_path, {"implemented_stories": [], "last_updated": None})
    entries = normalize_scope_entries(scope.get("implemented_stories", []))
    by_id = {item["id"]: item for item in entries}
    by_id[entry["id"]] = entry
    scope["implemented_stories"] = list(by_id.values())
    return scope


def important_keywords(text: str) -> set[str]:
    """Extract lightweight keywords for file relevance ranking."""
    tokens = []
    current = []
    for ch in text.lower():
        if ch.isalnum() or ch in {"_", "-"}:
            current.append(ch)
        else:
            if len(current) >= 3:
                tokens.append("".join(current))
            current = []
    if len(current) >= 3:
        tokens.append("".join(current))
    stop = {
        "with",
        "from",
        "that",
        "this",
        "have",
        "show",
        "page",
        "story",
        "backend",
        "frontend",
        "using",
        "create",
        "build",
        "display",
    }
    return {tok for tok in tokens if tok not in stop}


def select_relevant_files(
    root: Path,
    story_text: str,
    include_suffixes: set[str],
    max_files: int = 12,
) -> list[Path]:
    """Select a focused subset of source files for prompt context."""
    if not root.exists():
        return []

    keywords = important_keywords(story_text)
    scored: list[tuple[int, Path]] = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORED_PARTS for part in path.parts):
            continue
        if path.suffix.lower() not in include_suffixes and path.name not in {
            "requirements.txt",
            "package.json",
            "index.html",
        }:
            continue

        rel = str(path.relative_to(root)).lower()
        score = 0
        score += 4 if "main.py" in rel or "app.jsx" in rel else 0
        score += 3 if "router" in rel or "test" in rel else 0
        score += 2 if "service" in rel or "model" in rel else 0
        keyword_hits = sum(1 for kw in keywords if kw in rel)
        score += keyword_hits * 5
        scored.append((score, path))

    scored.sort(key=lambda item: (-item[0], str(item[1])))
    picked = [path for score, path in scored[:max_files] if score > 0]

    if not picked:
        fallbacks = []
        for path in sorted(root.rglob("*")):
            if (
                path.is_file()
                and not any(part in IGNORED_PARTS for part in path.parts)
                and (
                    path.suffix.lower() in include_suffixes
                    or path.name in {"requirements.txt", "package.json", "index.html"}
                )
            ):
                fallbacks.append(path)
        picked = fallbacks[:max_files]

    return picked


def workspace_file_tree(root: Path, include_suffixes: set[str] | None = None) -> str:
    """Return a flat listing of all source files in the workspace directory.

    This is injected into agent prompts so the LLM knows exactly which
    modules and files exist — preventing hallucinated imports.
    """
    if not root.exists():
        return "(directory does not exist)"

    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED_PARTS for part in path.parts):
            continue
        if include_suffixes and path.suffix.lower() not in include_suffixes:
            if path.name not in {"requirements.txt", "package.json", "index.html",
                                 "vite.config.js", "tailwind.config.js", "postcss.config.js"}:
                continue
        rel = path.relative_to(root)
        lines.append(str(rel))

    return "\n".join(lines) if lines else "(empty directory)"


_PY_DEF_RE = re.compile(r'^(?:def|class|async def)\s+(\w+)', re.MULTILINE)
_JS_EXPORT_RE = re.compile(
    r'(?:^|\s)export\s+(?:default\s+)?(?:function|const|class|let|var)\s+(\w+)',
    re.MULTILINE,
)


def workspace_export_map(root: Path, suffixes: set[str]) -> str:
    """Scan workspace files and return a map of file -> exported symbols.

    This prevents the LLM from rewriting a file and accidentally dropping
    functions/classes that other files depend on.
    """
    if not root.exists():
        return ""

    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED_PARTS for part in path.parts):
            continue
        if path.suffix.lower() not in suffixes:
            continue

        rel = str(path.relative_to(root))
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        if path.suffix.lower() == ".py":
            names = _PY_DEF_RE.findall(content)
            public = [n for n in names if not n.startswith("_")]
        else:
            public = _JS_EXPORT_RE.findall(content)

        if public:
            lines.append(f"  {rel}: {', '.join(public)}")

    if not lines:
        return ""
    return "\n".join(lines)


def render_context(root: Path, paths: list[Path]) -> str:
    """Render source file snippets for prompting."""
    sections = []
    for path in paths:
        rel = path.relative_to(root)
        sections.append(f"=== {rel} ===\n{path.read_text(encoding='utf-8')}\n")
    return "\n".join(sections) if sections else "(empty workspace)"


_COPY_IGNORE = shutil.ignore_patterns("node_modules", ".venv", "__pycache__", "dist")

_BACKUPS_DIR = Path(__file__).parent.parent / "workspace" / ".backups"


def backup_workspace(workspace_dir: Path, label: str = "") -> str:
    """Create a timestamped backup of the workspace before an enhancement.

    Returns the absolute path to the backup directory.
    """
    _BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    tag = f"_{label}" if label else ""
    backup_name = f"backup_{ts}{tag}"
    backup_path = _BACKUPS_DIR / backup_name

    for subdir_name in ("backend", "frontend"):
        src = workspace_dir / subdir_name
        if src.exists():
            shutil.copytree(src, backup_path / subdir_name, dirs_exist_ok=True, ignore=_COPY_IGNORE)

    return str(backup_path)


def restore_backup(backup_path: str, workspace_dir: Path) -> None:
    """Restore a workspace from a previously created backup."""
    bp = Path(backup_path)
    if not bp.exists():
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    for subdir_name in ("backend", "frontend"):
        backup_sub = bp / subdir_name
        dest_sub = workspace_dir / subdir_name
        if backup_sub.exists():
            if dest_sub.exists():
                try:
                    shutil.rmtree(dest_sub)
                except OSError:
                    pass
            shutil.copytree(backup_sub, dest_sub, dirs_exist_ok=True, ignore=_COPY_IGNORE)


def list_backups() -> list[dict]:
    """Return available backups sorted newest first."""
    if not _BACKUPS_DIR.exists():
        return []
    result = []
    for p in sorted(_BACKUPS_DIR.iterdir(), reverse=True):
        if p.is_dir() and p.name.startswith("backup_"):
            subdirs = [d.name for d in p.iterdir() if d.is_dir()]
            result.append({"name": p.name, "path": str(p), "contents": subdirs})
    return result


def create_scratch_copy(source_dir: Path, prefix: str) -> Path:
    """Create an isolated scratch copy for an agent attempt.

    Skips node_modules, .venv, __pycache__, and dist since they are
    regenerated during validation (npm install / pip install).
    """
    scratch_root = Path(tempfile.mkdtemp(prefix=f"{prefix}_"))
    target = scratch_root / source_dir.name
    if source_dir.exists():
        shutil.copytree(source_dir, target, dirs_exist_ok=True, ignore=_COPY_IGNORE)
    else:
        target.mkdir(parents=True, exist_ok=True)
    return target


# Foundational / shared files whose modification has wide blast radius — a narrow
# story that changes these is a scope-creep smell worth surfacing on promote (#3).
_SHARED_FILES = {"App.jsx", "App.tsx", "main.jsx", "main.py", "index.html",
                 "package.json", "package-lock.json", "requirements.txt"}
_SHARED_SUFFIXES = (".config.js", ".config.ts")
_MANIFEST_IGNORE = {"node_modules", "dist", "build", ".venv", "__pycache__", ".git"}


def _list_rel_files(root: Path) -> dict:
    out: dict = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in _MANIFEST_IGNORE for part in rel.parts):
            continue
        out[str(rel)] = p
    return out


def _promote_manifest(scratch_dir: Path, destination_dir: Path) -> dict:
    """What a promote would change: added / modified / deleted files, plus any
    SHARED/foundational files touched (high blast radius)."""
    src = _list_rel_files(scratch_dir)
    dst = _list_rel_files(destination_dir)
    added, modified, deleted = [], [], []
    for rel, sp in src.items():
        if rel not in dst:
            added.append(rel)
        else:
            try:
                if sp.read_bytes() != dst[rel].read_bytes():
                    modified.append(rel)
            except OSError:
                modified.append(rel)
    deleted = [rel for rel in dst if rel not in src]
    changed = added + modified + deleted
    shared = [r for r in changed
              if Path(r).name in _SHARED_FILES or Path(r).name.endswith(_SHARED_SUFFIXES)]
    return {"added": sorted(added), "modified": sorted(modified),
            "deleted": sorted(deleted), "shared_changed": sorted(shared)}


def _backup_destination(destination_dir: Path, label: str = "") -> str | None:
    """Snapshot the live destination before it is overwritten, so a promote is
    reversible. Keeps the most recent 10 backups per destination."""
    if not destination_dir.exists():
        return None
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    tag = f"{destination_dir.name}_{ts}" + (f"_{label}" if label else "")
    backup_root = destination_dir.parent / ".backups"
    backup_dir = backup_root / tag
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(destination_dir, backup_dir, dirs_exist_ok=True, ignore=_COPY_IGNORE)
    except OSError:
        return None
    try:  # prune old backups for this destination
        peers = sorted(d for d in backup_root.glob(f"{destination_dir.name}_*") if d.is_dir())
        for old in peers[:-10]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
    return str(backup_dir)


def promote_scratch_copy(
    scratch_dir: Path, destination_dir: Path, *, backup: bool = True, label: str = "",
) -> dict:
    """Replace destination with validated scratch output.

    #3 safety: before overwriting, snapshot the destination to a timestamped backup
    and compute a change manifest (added/modified/deleted + any SHARED/foundational
    files touched) so every promote is reversible and attributable. Returns
    {backup_path, added, modified, deleted, shared_changed}.

    Skips node_modules/dist/etc. Uses dirs_exist_ok to merge into an existing
    directory when rmtree fails (e.g., locked files on macOS).
    """
    manifest = _promote_manifest(scratch_dir, destination_dir)
    manifest["backup_path"] = _backup_destination(destination_dir, label) if backup else None
    if destination_dir.exists():
        try:
            shutil.rmtree(destination_dir)
        except OSError:
            pass
    shutil.copytree(scratch_dir, destination_dir, dirs_exist_ok=True, ignore=_COPY_IGNORE)
    return manifest


_ROUTE_RE = re.compile(
    r'@\w+\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\']'
    r'(?:.*?response_model\s*=\s*(\w+))?'
    r'(?:.*?status_code\s*=\s*(\d+))?',
    re.DOTALL,
)
_MODEL_CLASS_RE = re.compile(r'^class\s+(\w+)\(.*BaseModel.*\):', re.MULTILINE)
_FIELD_RE = re.compile(r'^\s+(\w+)\s*:\s*(.+?)(?:\s*=.*)?$', re.MULTILINE)
# str-Enum classes and their string VALUES — the accepted vocabulary the frontend
# must conform to (statuses, categories, priorities, sort keys, etc.). These are
# what drift between backend and frontend and cause 400s, so we surface them.
_ENUM_CLASS_RE = re.compile(r'^class\s+(\w+)\s*\([^)]*\bEnum\b[^)]*\):', re.MULTILINE)
_ENUM_MEMBER_RE = re.compile(r'^\s+(\w+)\s*=\s*["\'](.+?)["\']', re.MULTILINE)


def extract_api_contract(backend_root: Path) -> dict:
    """Scan backend Python files to extract a lightweight API route + model summary."""
    contract: dict = {"routes": [], "models": [], "enums": {}}
    if not backend_root.exists():
        return contract

    for py_file in sorted(backend_root.rglob("*.py")):
        if any(part in IGNORED_PARTS for part in py_file.parts):
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for m in _ROUTE_RE.finditer(content):
            method = m.group(1).upper()
            path = m.group(2)
            resp_model = m.group(3) or ""
            status_code = int(m.group(4)) if m.group(4) else (200 if method == "GET" else 201)
            func_match = re.search(r'\ndef\s+(\w+)\s*\(', content[m.end():m.end() + 200])
            func_name = func_match.group(1) if func_match else ""
            contract["routes"].append({
                "method": method,
                "path": path,
                "function": func_name,
                "response_model": resp_model,
                "status_code": status_code,
                "file": str(py_file.relative_to(backend_root)),
            })

        for cm in _MODEL_CLASS_RE.finditer(content):
            class_name = cm.group(1)
            class_end = content.find("\nclass ", cm.end())
            if class_end == -1:
                class_end = len(content)
            class_body = content[cm.end():class_end]
            fields = {}
            for fm in _FIELD_RE.finditer(class_body):
                field_name = fm.group(1)
                if field_name.startswith("_"):
                    continue
                field_type = fm.group(2).strip().rstrip(",")
                fields[field_name] = field_type
            if fields:
                contract["models"].append({"name": class_name, "fields": fields})

        for em in _ENUM_CLASS_RE.finditer(content):
            enum_name = em.group(1)
            enum_end = content.find("\nclass ", em.end())
            if enum_end == -1:
                enum_end = len(content)
            values = [mm.group(2) for mm in _ENUM_MEMBER_RE.finditer(content[em.end():enum_end])]
            if values:
                contract["enums"][enum_name] = values

    # Flat union of every accepted enum value — the vocabulary the frontend must
    # send. Used by the contract-lint (check_contract) to flag drifted values.
    contract["enum_values"] = sorted({v for vals in contract["enums"].values() for v in vals})
    return contract


_SKIP_SECTIONS = {
    "when to apply",
    "how to use",
    "how to use this skill",
    "prerequisites",
    "search reference",
    "example workflow",
    "output formats",
    "tips for better results",
}

# Per-skill char cap on guidelines injected into the (re-sent-every-turn) system
# prompt. This was once raised to 60000 ("inject the full skill — modern models
# have ample context"), but that assumed CHEAP context. This gateway does NOT
# cache prompts, so the full skill is re-paid on every ReAct iteration: the 45K UI
# skill alone is ~11K tokens × ~16 turns ≈ 175K tokens for one frontend story.
# 8000 chars (~2K tokens) caps that. TRADE-OFF: blunt head-truncation can drop
# later rules (the UI skill's Pre-Delivery Checklist lives near the end), so
# frontend polish may regress — raise AGENTIC_MAX_SKILL_CHARS if so, or (better)
# condense the skill into a rules-only form rather than truncating.
_MAX_SKILL_CHARS = int(os.getenv("AGENTIC_MAX_SKILL_CHARS", "8000"))


def load_skill_guidelines(skill_path: Path, max_chars: int | None = None) -> str:
    """Load a SKILL.md file and extract actionable guideline sections.

    Strips YAML frontmatter and meta/instructional sections (how to use,
    prerequisites, etc.), keeping all substantive engineering and design
    guidelines. Truncates to `max_chars` (default `_MAX_SKILL_CHARS`). Callers that
    serve the skill ON DEMAND (fetched once via read_guidelines, not re-sent every
    turn) can pass a larger cap to keep the full guidance.
    """
    cap = max_chars if max_chars is not None else _MAX_SKILL_CHARS
    if not skill_path.exists():
        return ""
    try:
        raw = skill_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    lines = raw.split("\n")
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break

    body = lines[start:]

    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []

    for line in body:
        match = re.match(r"^##\s+(.+)", line)
        if match:
            if current_heading:
                sections.append((current_heading, current_lines))
            current_heading = match.group(1).strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_heading:
        sections.append((current_heading, current_lines))

    kept: list[str] = []
    for heading, content in sections:
        if heading.lower() not in _SKIP_SECTIONS:
            kept.extend(content)

    result = "\n".join(kept).strip()
    if len(result) > cap:
        result = result[:cap] + "\n... (truncated)"
    return result
