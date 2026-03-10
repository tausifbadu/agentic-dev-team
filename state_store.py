"""JSON file state. Write, read, inspect. No database."""

import json
from pathlib import Path

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, "r") as f:
        return json.load(f)


def requirement_path(req_id: str) -> Path:
    return STATE_DIR / f"requirement_{req_id}.json"


def storypack_path(pack_id: str) -> Path:
    return STATE_DIR / f"storypack_{pack_id}.json"
