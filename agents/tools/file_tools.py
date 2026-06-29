"""Filesystem tools exposed to LLM agents.

All paths are resolved relative to `ToolContext.scratch_dir` (the agent's working
copy) when one is set, otherwise to the live workspace. Every write is sandboxed
inside the resolved root so the agent cannot escape its working area.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import Any

from .registry import Tool, ToolContext, ToolError, ToolRegistry, ToolResult

MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 400_000
MAX_LISTING_ENTRIES = 500


def _resolve_root(ctx: ToolContext) -> Path:
    if ctx.scratch_dir is not None:
        return Path(ctx.scratch_dir).resolve()
    if ctx.workspace_dir is not None:
        return Path(ctx.workspace_dir).resolve()
    raise ToolError("No scratch_dir or workspace_dir configured for this agent")


def _resolve_path(ctx: ToolContext, rel_path: str) -> Path:
    root = _resolve_root(ctx)
    if not rel_path or rel_path.startswith("/"):
        raise ToolError(f"Path must be relative inside the workspace: {rel_path!r}")
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ToolError(f"Path {rel_path!r} escapes workspace root {root}") from exc
    return candidate


def _read_file_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rel = args.get("path", "")
    if not rel:
        return ToolResult(ok=False, content="Missing 'path' argument")

    p = _resolve_path(ctx, rel)
    if not p.exists():
        return ToolResult(ok=False, content=f"File not found: {rel}")
    if not p.is_file():
        return ToolResult(ok=False, content=f"Not a regular file: {rel}")

    try:
        data = p.read_bytes()
    except OSError as exc:
        return ToolResult(ok=False, content=f"Could not read {rel}: {exc}")

    byte_truncated = False
    if len(data) > MAX_READ_BYTES:
        data = data[:MAX_READ_BYTES]
        byte_truncated = True

    text = data.decode("utf-8", errors="replace")

    # Optional line-range pagination so large files stay navigable instead of being
    # silently clipped to the head. `offset` is 1-based; `limit` is a line count.
    # When the agent only gets the head of a big file it can't see the code it must
    # modify — this lets it page through (read more with offset=<next line>).
    offset = args.get("offset")
    limit = args.get("limit")
    notes: list[str] = []
    if byte_truncated:
        notes.append(f"file exceeds {MAX_READ_BYTES} bytes; showing first {MAX_READ_BYTES}")
    if offset is not None or limit is not None:
        lines = text.split("\n")
        total = len(lines)
        try:
            start = max(1, int(offset)) if offset is not None else 1
        except (TypeError, ValueError):
            start = 1
        try:
            count = max(0, int(limit)) if limit is not None else total
        except (TypeError, ValueError):
            count = total
        end = min(total, start - 1 + count) if count else total
        window = lines[start - 1:end]
        text = "\n".join(window)
        notes.append(f"lines {start}-{start - 1 + len(window)} of {total}")
        if end < total:
            notes.append(f"read more with offset={end + 1}")

    if notes:
        text += "\n\n... (" + "; ".join(notes) + ")"
    return ToolResult(
        ok=True,
        content=text,
        metadata={"path": rel, "bytes": len(data), "truncated": byte_truncated},
    )


def _write_file_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rel = args.get("path", "")
    content = args.get("content", "")
    if not rel:
        return ToolResult(ok=False, content="Missing 'path' argument")
    if not isinstance(content, str):
        return ToolResult(ok=False, content="'content' must be a string")
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return ToolResult(
            ok=False,
            content=f"File too large (max {MAX_WRITE_BYTES} bytes)",
        )

    if rel.endswith(".py"):
        try:
            compile(content, rel, "exec")
        except SyntaxError as exc:
            return ToolResult(
                ok=False,
                content=(
                    f"Refusing to write {rel}: SyntaxError on line {exc.lineno}: {exc.msg}. "
                    "Fix the syntax and re-write."
                ),
            )

    p = _resolve_path(ctx, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    ctx.metadata["dirty_since_validation"] = True
    return ToolResult(
        ok=True,
        content=f"Wrote {rel} ({len(content)} chars)",
        metadata={"path": rel, "chars": len(content)},
    )


def _list_dir_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rel = args.get("path", ".")
    p = _resolve_path(ctx, rel)
    if not p.exists():
        return ToolResult(ok=False, content=f"Directory not found: {rel}")
    if not p.is_dir():
        return ToolResult(ok=False, content=f"Not a directory: {rel}")

    skip_parts = {"node_modules", ".venv", "__pycache__", "dist", "build", ".git"}
    entries: list[str] = []
    root = _resolve_root(ctx)
    for child in sorted(p.rglob("*")):
        if any(part in skip_parts for part in child.parts):
            continue
        if not child.is_file():
            continue
        try:
            rel_str = str(child.relative_to(root))
        except ValueError:
            continue
        entries.append(rel_str)
        if len(entries) >= MAX_LISTING_ENTRIES:
            entries.append(f"... (more than {MAX_LISTING_ENTRIES} entries — truncated)")
            break

    body = "\n".join(entries) if entries else "(empty)"
    return ToolResult(ok=True, content=body, metadata={"count": len(entries)})


def _grep_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = args.get("pattern", "")
    rel = args.get("path", ".")
    case_insensitive = bool(args.get("case_insensitive", False))
    max_matches = int(args.get("max_matches", 50))

    if not pattern:
        return ToolResult(ok=False, content="Missing 'pattern'")

    try:
        regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as exc:
        return ToolResult(ok=False, content=f"Invalid regex: {exc}")

    p = _resolve_path(ctx, rel)
    if not p.exists():
        return ToolResult(ok=False, content=f"Path not found: {rel}")

    skip_parts = {"node_modules", ".venv", "__pycache__", "dist", "build", ".git"}
    matches: list[str] = []
    files = [p] if p.is_file() else [
        f for f in p.rglob("*")
        if f.is_file() and not any(part in skip_parts for part in f.parts)
    ]

    for file_path in files:
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel_path = str(file_path.relative_to(_resolve_root(ctx)))
                matches.append(f"{rel_path}:{line_no}: {line.rstrip()[:300]}")
                if len(matches) >= max_matches:
                    break
        if len(matches) >= max_matches:
            matches.append(f"... (stopped at {max_matches} matches)")
            break

    body = "\n".join(matches) if matches else "(no matches)"
    return ToolResult(ok=True, content=body, metadata={"matches": len(matches)})


def _delete_file_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rel = args.get("path", "")
    if not rel:
        return ToolResult(ok=False, content="Missing 'path'")
    p = _resolve_path(ctx, rel)
    if not p.exists():
        return ToolResult(ok=False, content=f"File not found: {rel}")
    try:
        p.unlink()
    except OSError as exc:
        return ToolResult(ok=False, content=f"Could not delete {rel}: {exc}")
    ctx.metadata["dirty_since_validation"] = True
    return ToolResult(ok=True, content=f"Deleted {rel}")


def _apply_patch_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Bulk apply a `{files: [{path, content}], requirements_add, dependencies_add}` payload.

    This is a convenience tool so an agent can write many files atomically rather
    than calling write_file N times.
    """
    payload = args.get("patch")
    if not isinstance(payload, dict):
        return ToolResult(ok=False, content="'patch' must be an object")

    files = payload.get("files", [])
    if not isinstance(files, list):
        return ToolResult(ok=False, content="'patch.files' must be an array")

    written: list[str] = []
    syntax_errors: list[str] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        content = entry.get("content", "")
        if not path or not isinstance(content, str):
            continue
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            return ToolResult(ok=False, content=f"File {path} exceeds {MAX_WRITE_BYTES} bytes")

        # Pre-write Python syntax check — reject the whole patch on any SyntaxError.
        if path.endswith(".py"):
            try:
                compile(content, path, "exec")
            except SyntaxError as exc:
                syntax_errors.append(f"{path}: {exc.msg} (line {exc.lineno})")
                continue

        target = _resolve_path(ctx, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(path)

    if syntax_errors:
        return ToolResult(
            ok=False,
            content=(
                "Patch rejected: Python syntax errors in:\n"
                + "\n".join(f"  - {err}" for err in syntax_errors)
                + (f"\n(Other files in this patch were skipped: {[p for p in written]})"
                   if written else "")
            ),
        )

    requirements_add = payload.get("requirements_add", []) or []
    if requirements_add:
        try:
            req = _resolve_path(ctx, "requirements.txt")
            existing = req.read_text(encoding="utf-8") if req.exists() else ""
            for pkg in requirements_add:
                if pkg and pkg not in existing:
                    existing = existing.rstrip() + f"\n{pkg}\n"
            req.parent.mkdir(parents=True, exist_ok=True)
            req.write_text(existing, encoding="utf-8")
        except ToolError:
            pass

    if written:
        ctx.metadata["dirty_since_validation"] = True

    return ToolResult(
        ok=True,
        content=f"Applied {len(written)} files: {', '.join(written) or '(none)'}",
        metadata={"files": written, "added_requirements": list(requirements_add)},
    )


# --- read_symbol: pull ONE function/class/component instead of a whole file ---

_JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}


def _extract_python_symbol(text: str, name: str):
    """Exact extraction via the AST: returns (start_line, end_line, source) or None."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines = text.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            start = node.lineno
            if node.decorator_list:
                start = min(start, min(d.lineno for d in node.decorator_list))
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            return start, end, "\n".join(lines[start - 1:end])
    return None


def _python_symbol_names(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return sorted({
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    })


def _extract_braced_symbol(text: str, name: str):
    """Heuristic for JS-family (and a Python fallback): find the declaration of
    `name`, then capture the balanced-brace block. Returns (start, end, src) or None."""
    lines = text.split("\n")
    decl = re.compile(
        rf"\b(?:export\s+)?(?:default\s+)?(?:async\s+)?"
        rf"(?:function\s+{re.escape(name)}\b"
        rf"|class\s+{re.escape(name)}\b"
        rf"|(?:const|let|var)\s+{re.escape(name)}\b"
        rf"|{re.escape(name)}\s*[:=]\s*(?:async\s*)?(?:function|\([^)]*\)\s*=>|\w))"
    )
    for i, line in enumerate(lines):
        if not decl.search(line):
            continue
        depth = 0
        started = False
        for j in range(i, len(lines)):
            for ch in lines[j]:
                if ch == "{":
                    depth += 1
                    started = True
                elif ch == "}":
                    depth -= 1
            if started and depth <= 0:
                return i + 1, j + 1, "\n".join(lines[i:j + 1])
        if not started:
            # no brace block (e.g. arrow returning a value) — capture to the statement end
            for j in range(i, min(i + 25, len(lines))):
                if lines[j].rstrip().endswith(";") or (j > i and lines[j].strip() == ""):
                    return i + 1, j + 1, "\n".join(lines[i:j + 1])
            return i + 1, i + 1, lines[i]
    return None


def _js_symbol_names(text: str) -> list[str]:
    return sorted(set(re.findall(
        r"\b(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+(\w+)",
        text,
    )))


def _read_symbol_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rel = args.get("path", "")
    name = args.get("name", "")
    if not rel or not name:
        return ToolResult(ok=False, content="read_symbol needs both 'path' and 'name'")
    try:
        p = _resolve_path(ctx, rel)
    except ToolError as exc:
        return ToolResult(ok=False, content=str(exc))
    if not p.exists() or not p.is_file():
        return ToolResult(ok=False, content=f"File not found: {rel}")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult(ok=False, content=f"Could not read {rel}: {exc}")

    ext = p.suffix.lower()
    result = None
    names: list[str] = []
    if ext == ".py":
        result = _extract_python_symbol(text, name)
        if result is None:
            names = _python_symbol_names(text)
    if result is None:
        result = _extract_braced_symbol(text, name)
    if result is None and not names:
        names = _js_symbol_names(text) if ext in _JS_SUFFIXES else _python_symbol_names(text)

    if result is None:
        hint = f" Available symbols: {', '.join(names[:40])}." if names else ""
        return ToolResult(
            ok=False,
            content=f"Symbol '{name}' not found in {rel}.{hint} Use read_file for the whole file.",
        )
    start, end, body = result
    if len(body) > MAX_READ_BYTES:
        body = body[:MAX_READ_BYTES] + "\n... (symbol truncated)"
    return ToolResult(
        ok=True,
        content=f"{rel}:{start}-{end}\n{body}",
        metadata={"path": rel, "symbol": name, "lines": f"{start}-{end}"},
    )


def register_file_tools(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="read_file",
        description=(
            "Read a UTF-8 text file from the agent's working copy. Returns the file "
            "contents. For large files, page through with `offset` (1-based start "
            "line) and `limit` (number of lines) instead of getting only the head."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the working copy root"},
                "offset": {"type": "integer", "minimum": 1, "description": "1-based line to start reading from (optional)"},
                "limit": {"type": "integer", "minimum": 1, "description": "Max number of lines to return from offset (optional)"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_read_file_handler,
    ))
    registry.register(Tool(
        name="read_symbol",
        description=(
            "Read ONE function/class/component by name from a file — not the whole "
            "file. Prefer this over read_file when you only need a specific symbol "
            "(cheaper, focused). Exact for Python (AST); heuristic for JS/TS. If not "
            "found, it lists the file's available symbol names."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the working copy root"},
                "name": {"type": "string", "description": "Function / class / component name to extract"},
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=_read_symbol_handler,
    ))
    registry.register(Tool(
        name="write_file",
        description="Create or overwrite a text file in the agent's working copy.",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the working copy"},
                "content": {"type": "string", "description": "Full file contents to write"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=_write_file_handler,
    ))
    registry.register(Tool(
        name="list_dir",
        description="Recursively list source files under a directory in the working copy.",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative directory (defaults to '.')"},
            },
            "additionalProperties": False,
        },
        handler=_list_dir_handler,
    ))
    registry.register(Tool(
        name="grep",
        description="Search files for a regex pattern. Returns matching lines as 'path:line: content'.",
        parameters_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression"},
                "path": {"type": "string", "description": "File or directory to search (defaults to '.')"},
                "case_insensitive": {"type": "boolean"},
                "max_matches": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        handler=_grep_handler,
    ))
    registry.register(Tool(
        name="delete_file",
        description="Delete a file in the working copy.",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_delete_file_handler,
    ))
    registry.register(Tool(
        name="apply_patch",
        description=(
            "Apply a multi-file patch atomically. Use when changing multiple files at once. "
            "The 'patch' object must contain a 'files' array of {path, content} entries. "
            "Optionally include 'requirements_add' to append to requirements.txt."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "patch": {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "content": {"type": "string"},
                                },
                                "required": ["path", "content"],
                            },
                        },
                        "requirements_add": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["files"],
                },
            },
            "required": ["patch"],
            "additionalProperties": False,
        },
        handler=_apply_patch_handler,
    ))
