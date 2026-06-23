"""Workspace-scoped dashboard chat: bounded tool loop, NDJSON event stream."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from openai import BadRequestError, OpenAI

import state_store

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# Single-flight lock — independent from agent Supervisor / enhancements.
_workspace_chat_turn_lock = threading.Lock()

IGNORED_DIRS = {"node_modules", "__pycache__", ".pytest_cache", "dist", "build", ".git", ".venv", ".backups"}

MAX_READ_BYTES = 160_000
MAX_GREP_MATCHES = 50
MAX_GREP_FILES = 400
MAX_ITERATIONS = 18
MODEL_NAME = os.getenv("WORKSPACE_CHAT_MODEL", "codex/gpt-5.5")


def _resolve_jailed_path(root: Path, rel_path: str) -> Path:
    if not rel_path or rel_path.startswith("..") or "/../" in rel_path:
        raise ValueError("Invalid path")
    rel = Path(rel_path).as_posix().lstrip("/")
    parts = Path(rel).parts
    if any(p in IGNORED_DIRS or p.startswith(".") for p in parts):
        raise ValueError("Path in ignored segment")
    if rel.endswith(".env") or ".env/" in rel or "/.env" in f"/{rel}/":
        raise ValueError(".env paths are blocked")
    full = (root / rel).resolve()
    root_res = root.resolve()
    if not str(full).startswith(str(root_res) + os.sep) and full != root_res:
        raise ValueError("Path escapes workspace root")
    return full


def _iter_files(root: Path) -> Iterator[Path]:
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORED_DIRS and not d.startswith(".")
        ]
        rel_dir = Path(dirpath).relative_to(root)
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if n >= MAX_GREP_FILES:
                return
            n += 1
            yield p


def _tool_workspace_list(root: Path, rel_dir: str) -> str:
    try:
        d = _resolve_jailed_path(root, rel_dir or ".")
    except ValueError as e:
        return f"ERROR: {e}"
    if not d.is_dir():
        return f"ERROR: not a directory: {rel_dir}"
    entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines: list[str] = []
    for p in entries[:200]:
        if p.name in IGNORED_DIRS or p.name.startswith("."):
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        kind = "dir" if p.is_dir() else "file"
        lines.append(f"[{kind}] {rel}")
    return "\n".join(lines) if lines else "(empty)"


def _tool_workspace_read(root: Path, rel_path: str) -> str:
    try:
        fp = _resolve_jailed_path(root, rel_path)
    except ValueError as e:
        return f"ERROR: {e}"
    if fp.is_dir():
        return "ERROR: path is a directory"
    if not fp.exists():
        return "ERROR: file not found"
    try:
        data = fp.read_bytes()
        if len(data) > MAX_READ_BYTES:
            return (
                data[: MAX_READ_BYTES - 120].decode("utf-8", errors="replace")
                + f"\n\n... truncated ({len(data)} bytes total)"
            )
        return data.decode("utf-8", errors="replace")
    except OSError as e:
        return f"ERROR: read failed: {e}"


def _tool_workspace_grep(root: Path, pattern: str, file_glob: str = "*") -> str:
    if not pattern or len(pattern) > 256:
        return "ERROR: pattern required (max 256 chars)"
    use_regex = True
    try:
        rx = re.compile(pattern)
    except re.error:
        use_regex = False

    matches: list[str] = []
    scanned = 0
    for fp in _iter_files(root):
        rel = fp.relative_to(root).as_posix()
        if not fnmatch.fnmatch(rel, file_glob) and not fnmatch.fnmatch(Path(rel).name, file_glob):
            continue
        if fp.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2"}:
            continue
        scanned += 1
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if use_regex:
                ok = rx.search(line) is not None
            else:
                ok = pattern in line
            if ok:
                matches.append(f"{rel}:{i}:{line.strip()[:500]}")
                if len(matches) >= MAX_GREP_MATCHES:
                    return (
                        f"(scanned ~{scanned} files)\n" + "\n".join(matches)
                        + "\n...(max matches reached)"
                    )
    return (
        f"(scanned {scanned} files, {len(matches)} matches)\n" + ("\n".join(matches) if matches else "no matches")
    )


def _tool_workspace_write(
    *,
    root: Path,
    session_id: str,
    project_id: str,
    rel_path: str,
    content: str,
) -> str:
    try:
        fp = _resolve_jailed_path(root, rel_path)
    except ValueError as e:
        return f"ERROR: {e}"
    if fp.exists() and fp.is_dir():
        return "ERROR: path is directory"
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        excerpt = "\n".join(content.splitlines()[:40])
        if len(content) > 8000:
            excerpt += "\n...(file truncated for log)"
        state_store.save_workspace_chat_edit(session_id, project_id, rel_path, excerpt[:2000])
        return f"OK: wrote {rel_path} ({len(content)} bytes)"
    except OSError as e:
        return f"ERROR: write failed: {e}"


def _openai_tools(allow_writes: bool) -> list[dict[str, Any]]:
    base: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "workspace_list",
                "description": "List files and directories relative to workspace root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": 'Relative directory; "" or "." for root'},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "workspace_read",
                "description": "Read a UTF-8 text file under workspace (size-capped).",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "workspace_grep",
                "description": "Search files for a regex pattern; returns matching lines with file:line.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "file_glob": {"type": "string", "description": 'e.g. "*.jsx", default "*"' },
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    if allow_writes:
        base.append({
            "type": "function",
            "function": {
                "name": "workspace_write_file",
                "description": "Replace entire file content (UTF-8). Creates parent directories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        })
    return base


def _invoke_tool(
    name: str,
    args: dict[str, Any],
    *,
    root: Path,
    session_id: str,
    project_id: str,
    allow_writes: bool,
) -> str:
    if name == "workspace_list":
        return _tool_workspace_list(root, args.get("path") or ".")
    if name == "workspace_read":
        return _tool_workspace_read(root, args.get("path") or "")
    if name == "workspace_grep":
        return _tool_workspace_grep(
            root,
            args.get("pattern") or "",
            args.get("file_glob") or "*",
        )
    if name == "workspace_write_file":
        if not allow_writes:
            return "ERROR: file writes disabled for this request"
        return _tool_workspace_write(
            root=root,
            session_id=session_id,
            project_id=project_id,
            rel_path=args.get("path") or "",
            content=args.get("content") or "",
        )
    return f"ERROR: unknown tool {name}"


def iter_workspace_chat_turn(
    *,
    project_id: str,
    session_id: str,
    user_message: str,
    allow_writes: bool,
    lock_acquired: bool = False,
) -> Iterator[str]:
    """
    Yield newline-delimited JSON events (strings already serialized with trailing \\n).

    Caller should hold `_workspace_chat_turn_lock` unless lock_acquired is True inside worker.
    """
    root = REPO_ROOT / "workspace"
    try:
        from workspace_paths import resolve_workspace_dir

        root = resolve_workspace_dir(project_id).resolve()
    except Exception:
        root = (REPO_ROOT / "workspace").resolve()

    state_store.touch_workspace_chat_session(session_id)
    state_store.add_workspace_chat_message(session_id, "user", user_message)

    hist = state_store.list_workspace_chat_messages(session_id, limit=36)
    messages: list[dict[str, Any]] = [{
        "role": "system",
        "content": (
            "You are a concise coding assistant for a local workspace. "
            f"Workspace root corresponds to project_id={project_id!r}. "
            f"File writes via tools are {'ENABLED' if allow_writes else 'DISABLED — use read/list/grep only'}. "
            "Inspect relevant files before suggesting changes. "
            "If writes are disabled, explain what edits to make; do not call workspace_write_file. "
            "When writes are enabled, prefer small scoped edits via full-file replace only when the "
            "file is small; for large files read first then write the complete updated file."
        ),
    }]
    for row in hist[:-1]:
        role = row["role"]
        if role not in ("user", "assistant"):
            continue
        messages.append({"role": role, "content": row["content"][-12000:]})
    messages.append({"role": "user", "content": user_message})

    client = OpenAI(timeout=240.0)
    tools = _openai_tools(allow_writes)

    assistant_blob = ""

    def out(obj: dict[str, Any]) -> str:
        return json.dumps(obj, default=str) + "\n"

    try:
        yield out({"type": "meta", "project_id": project_id, "session_id": session_id, "allow_writes": allow_writes})

        for it in range(MAX_ITERATIONS):
            yield out({"type": "iteration", "n": it + 1})

            kwargs: dict[str, Any] = {
                "model": MODEL_NAME,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.2,
            }
            try:
                resp = client.chat.completions.create(**kwargs)
            except BadRequestError as exc:
                err = str(exc).lower()
                if "temperature" in err and ("unsupported" in err or "only the default" in err):
                    kwargs.pop("temperature", None)
                    resp = client.chat.completions.create(**kwargs)
                else:
                    yield out({"type": "error", "message": str(exc)})
                    return
            except Exception as exc:
                yield out({"type": "error", "message": str(exc)})
                return

            choice = resp.choices[0]
            msg = choice.message
            text = (msg.content or "").strip()
            tool_calls = getattr(msg, "tool_calls", None) or []

            if text:
                assistant_blob += text + "\n"
                yield out({"type": "assistant", "content": text})

            if not tool_calls:
                state_store.add_workspace_chat_message(session_id, "assistant", assistant_blob.strip() or text)
                yield out({"type": "done", "ok": True, "reason": "final"})
                return

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content if msg.content else None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
            messages.append(assistant_msg)

            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield out({"type": "tool_start", "name": name, "arguments": args})

                body = _invoke_tool(
                    name, args,
                    root=root,
                    session_id=session_id,
                    project_id=project_id,
                    allow_writes=allow_writes,
                )
                clipped = body if len(body) <= 32000 else body[:32000] + "\n...(truncated)"
                yield out({"type": "tool_result", "name": name, "content": clipped[:8000], "truncated": len(body) > 8000})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": clipped,
                })

        state_store.add_workspace_chat_message(
            session_id, "assistant", assistant_blob.strip() or "(stopped: max iterations)",
        )
        yield out({"type": "done", "ok": False, "reason": "max_iterations"})

    finally:
        if not lock_acquired:
            pass


def try_acquire_workspace_chat_lock() -> bool:
    return _workspace_chat_turn_lock.acquire(blocking=False)


def release_workspace_chat_lock() -> None:
    try:
        _workspace_chat_turn_lock.release()
    except RuntimeError:
        pass
