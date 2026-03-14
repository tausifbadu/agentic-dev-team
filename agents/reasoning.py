"""Shared reasoning helpers for code agents."""

from __future__ import annotations

import json
import shutil
import tempfile
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


def render_context(root: Path, paths: list[Path]) -> str:
    """Render source file snippets for prompting."""
    sections = []
    for path in paths:
        rel = path.relative_to(root)
        sections.append(f"=== {rel} ===\n{path.read_text(encoding='utf-8')}\n")
    return "\n".join(sections) if sections else "(empty workspace)"


def create_scratch_copy(source_dir: Path, prefix: str) -> Path:
    """Create an isolated scratch copy for an agent attempt."""
    scratch_root = Path(tempfile.mkdtemp(prefix=f"{prefix}_"))
    target = scratch_root / source_dir.name
    if source_dir.exists():
        shutil.copytree(source_dir, target, dirs_exist_ok=True)
    else:
        target.mkdir(parents=True, exist_ok=True)
    return target


def promote_scratch_copy(scratch_dir: Path, destination_dir: Path) -> None:
    """Replace destination with validated scratch output."""
    if destination_dir.exists():
        shutil.rmtree(destination_dir)
    shutil.copytree(scratch_dir, destination_dir)
