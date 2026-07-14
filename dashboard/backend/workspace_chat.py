"""Workspace-scoped dashboard chat: bounded tool loop, NDJSON event stream."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    OpenAI,
)

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

# Retry/backoff for the shared gateway (429 / 5xx / transport / empty-choices).
# Mirrors the agent pipeline's resilience locally — but deliberately does NOT touch
# the shared token accumulator (agents.llm_client._USAGE), so build telemetry stays
# clean even if a chat turn runs during a build.
_MAX_RETRIES = int(os.getenv("WORKSPACE_CHAT_MAX_RETRIES", "5"))
_BACKOFF_BASE = float(os.getenv("WORKSPACE_CHAT_BACKOFF_BASE", "3"))
_BACKOFF_MAX = float(os.getenv("WORKSPACE_CHAT_BACKOFF_MAX", "30"))
# How many pre-overwrite backups to keep per file under <root>/.backups/wschat/.
_MAX_FILE_BACKUPS = 10


def _backoff(attempt: int) -> float:
    return min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_MAX)


def _robust_chat(client: OpenAI, **kwargs: Any):
    """chat.completions.create with retry on 429/5xx/transport errors and the
    gateway's empty-choices response, plus the temperature-unsupported fallback.
    Raises after the retry budget so the turn surfaces a clear error instead of
    dying on the first throttle."""
    omit_temp = False
    attempts = 0
    while True:
        call_kw = dict(kwargs)
        if omit_temp:
            call_kw.pop("temperature", None)
        try:
            resp = client.chat.completions.create(**call_kw)
        except BadRequestError as exc:
            err = str(exc).lower()
            if not omit_temp and "temperature" in err and (
                "unsupported" in err or "only the default" in err or "default (1)" in err
            ):
                omit_temp = True
                continue
            raise
        except APIStatusError as exc:
            status = getattr(exc, "status_code", 0) or 0
            if (status == 429 or status >= 500) and attempts < _MAX_RETRIES:
                attempts += 1
                time.sleep(_backoff(attempts))
                continue
            raise
        except (APIConnectionError, APITimeoutError):
            if attempts < _MAX_RETRIES:
                attempts += 1
                time.sleep(_backoff(attempts))
                continue
            raise
        # Gateway sometimes returns HTTP 200 with an empty choices list when the
        # provider is exhausted — indexing choices[0] would crash the turn.
        if not getattr(resp, "choices", None):
            if attempts < _MAX_RETRIES:
                attempts += 1
                time.sleep(_backoff(attempts))
                continue
            raise RuntimeError("LLM gateway returned no choices after retries")
        return resp


def _extract_usage(resp: Any) -> dict[str, int]:
    """Pull token counts from a response's usage object (best-effort)."""
    u = getattr(resp, "usage", None)
    if u is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0}
    pt = int(getattr(u, "prompt_tokens", 0) or 0)
    ct = int(getattr(u, "completion_tokens", 0) or 0)
    cached = 0
    det = getattr(u, "prompt_tokens_details", None)
    if det is not None:
        cached = int(getattr(det, "cached_tokens", 0) or 0)
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct, "cached_tokens": cached}


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


def _syntax_check(rel_path: str, content: str) -> str | None:
    """Reject writes that would leave a file un-parseable. Returns an error string
    or None if OK. Guards .py (compile) and .json (loads); other types pass."""
    low = rel_path.lower()
    if low.endswith(".py"):
        try:
            compile(content, rel_path, "exec")
        except SyntaxError as e:
            return f"ERROR: refusing to write — Python syntax error at line {e.lineno}: {e.msg}"
    elif low.endswith(".json"):
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            return f"ERROR: refusing to write — invalid JSON: {e}"
    return None


def _backup_existing(root: Path, fp: Path, rel_path: str) -> str | None:
    """Copy an existing file into <root>/.backups/wschat/<ts>/ before overwriting it,
    so any edit is reversible. Best-effort; returns the backup path or None."""
    if not fp.exists():
        return None
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = root / ".backups" / "wschat" / ts / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fp, dest)
        # prune old backups of this file (keep most recent _MAX_FILE_BACKUPS)
        wschat_root = root / ".backups" / "wschat"
        stamps = sorted(d for d in wschat_root.iterdir() if d.is_dir()) if wschat_root.exists() else []
        for old in stamps[:-_MAX_FILE_BACKUPS]:
            shutil.rmtree(old, ignore_errors=True)
        return str(dest.relative_to(root))
    except OSError:
        return None


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
    syntax_err = _syntax_check(rel_path, content)
    if syntax_err:
        return syntax_err
    try:
        backup = _backup_existing(root, fp, rel_path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        excerpt = "\n".join(content.splitlines()[:40])
        if len(content) > 8000:
            excerpt += "\n...(file truncated for log)"
        state_store.save_workspace_chat_edit(session_id, project_id, rel_path, excerpt[:2000])
        suffix = f" (backup: {backup})" if backup else " (new file)"
        return f"OK: wrote {rel_path} ({len(content)} bytes){suffix}"
    except OSError as e:
        return f"ERROR: write failed: {e}"


def _tool_workspace_edit(
    *,
    root: Path,
    session_id: str,
    project_id: str,
    rel_path: str,
    old: str,
    new: str,
) -> str:
    """Surgical edit: replace an exact substring `old` with `new` in a file.
    Preferred over full-file rewrite — safer for large files (no truncation /
    accidental deletion of unrelated code). `old` must occur exactly once."""
    try:
        fp = _resolve_jailed_path(root, rel_path)
    except ValueError as e:
        return f"ERROR: {e}"
    if not fp.exists() or fp.is_dir():
        return "ERROR: file not found"
    if not old:
        return "ERROR: 'old' text is required (use workspace_write_file to create a file)"
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"ERROR: read failed: {e}"
    count = text.count(old)
    if count == 0:
        return "ERROR: 'old' text not found — read the file and copy the exact text (incl. whitespace)"
    if count > 1:
        return f"ERROR: 'old' text appears {count} times — add surrounding context so it is unique"
    updated = text.replace(old, new, 1)
    syntax_err = _syntax_check(rel_path, updated)
    if syntax_err:
        return syntax_err
    try:
        backup = _backup_existing(root, fp, rel_path)
        fp.write_text(updated, encoding="utf-8")
        state_store.save_workspace_chat_edit(
            session_id, project_id, rel_path,
            f"edit: -{len(old)}/+{len(new)} chars\n{new[:1000]}"[:2000],
        )
        return f"OK: edited {rel_path} (backup: {backup})" if backup else f"OK: edited {rel_path}"
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
                "name": "workspace_edit_file",
                "description": (
                    "PREFERRED for changing an existing file: replace an exact substring "
                    "`old` with `new`. `old` must appear exactly once (add surrounding "
                    "context to make it unique). Safer than a full rewrite — no truncation "
                    "or accidental deletion of unrelated code. The prior file is backed up "
                    "and .py/.json are syntax-checked before the write is accepted."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string", "description": "Exact text to replace (must be unique in the file)"},
                        "new": {"type": "string", "description": "Replacement text"},
                    },
                    "required": ["path", "old", "new"],
                    "additionalProperties": False,
                },
            },
        })
        base.append({
            "type": "function",
            "function": {
                "name": "workspace_write_file",
                "description": (
                    "Replace ENTIRE file content (UTF-8); creates parent dirs. Use for NEW "
                    "files or a full rewrite of a small file — for edits to an existing file "
                    "prefer workspace_edit_file. The prior file is backed up and .py/.json "
                    "are syntax-checked before the write is accepted."
                ),
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
    if name == "workspace_edit_file":
        if not allow_writes:
            return "ERROR: file writes disabled for this request"
        return _tool_workspace_edit(
            root=root,
            session_id=session_id,
            project_id=project_id,
            rel_path=args.get("path") or "",
            old=args.get("old") or "",
            new=args.get("new") or "",
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
    # Per-turn token burn, accumulated across the loop's LLM calls. Captured from
    # each response's usage object directly (NOT via the shared record_usage, so the
    # supervisor's build telemetry is never contaminated).
    turn = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0, "calls": 0}

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
                resp = _robust_chat(client, **kwargs)
            except Exception as exc:
                yield out({"type": "error", "message": str(exc)})
                # Persist whatever tokens were already burned before the failure.
                _persist_usage(session_id, project_id, turn)
                yield out({"type": "usage", "scope": "turn", **turn})
                return

            u = _extract_usage(resp)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
                turn[k] += u[k]
            turn["calls"] += 1
            yield out({"type": "usage", "scope": "call", **u})

            choice = resp.choices[0]
            msg = choice.message
            text = (msg.content or "").strip()
            tool_calls = getattr(msg, "tool_calls", None) or []

            if text:
                assistant_blob += text + "\n"
                yield out({"type": "assistant", "content": text})

            if not tool_calls:
                state_store.add_workspace_chat_message(session_id, "assistant", assistant_blob.strip() or text)
                _persist_usage(session_id, project_id, turn)
                yield out({"type": "usage", "scope": "turn", **turn})
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
        _persist_usage(session_id, project_id, turn)
        yield out({"type": "usage", "scope": "turn", **turn})
        yield out({"type": "done", "ok": False, "reason": "max_iterations"})

    finally:
        if not lock_acquired:
            pass


def _persist_usage(session_id: str, project_id: str, turn: dict[str, int]) -> None:
    """Best-effort persist of a turn's token burn (survives reload / feeds the UI)."""
    if turn.get("total_tokens", 0) <= 0:
        return
    try:
        state_store.save_workspace_chat_usage(
            session_id=session_id, project_id=project_id,
            prompt_tokens=turn["prompt_tokens"], completion_tokens=turn["completion_tokens"],
            total_tokens=turn["total_tokens"], cached_tokens=turn["cached_tokens"],
        )
    except Exception:  # noqa: BLE001 — telemetry must never break a turn
        pass


def try_acquire_workspace_chat_lock() -> bool:
    return _workspace_chat_turn_lock.acquire(blocking=False)


def release_workspace_chat_lock() -> None:
    try:
        _workspace_chat_turn_lock.release()
    except RuntimeError:
        pass
