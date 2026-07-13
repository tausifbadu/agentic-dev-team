"""Execution tools: pytest, npm build/install, uvicorn smoke checks.

Tools run inside the agent's scratch dir and return stdout/stderr trimmed to
fit comfortably inside an LLM context window.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .registry import Tool, ToolContext, ToolError, ToolRegistry, ToolResult

OUTPUT_TAIL_CHARS = 6000

# Patterns of noisy log lines we drop before showing pytest output to the agent.
# These come from SQLAlchemy's INFO-level engine logger and similar verbose
# integrations that can drown out the actual test failures.
_NOISE_PATTERNS = (
    "INFO sqlalchemy.engine.Engine ",
    "DEBUG sqlalchemy.engine.Engine ",
    "INFO     sqlalchemy.engine.Engine:",
    "DEBUG    sqlalchemy.engine.Engine:",
)

# Pytest summary lines we always want to surface even after trimming.
_SUMMARY_PATTERNS = (
    "= FAILURES =",
    "= ERRORS =",
    "= short test summary info =",
    "passed in ",
    "failed in ",
    "error in ",
    " no tests ran in ",
    "FAILED ",
    "ERROR ",
)


def _filter_noise(text: str) -> str:
    """Drop noisy logger lines from a captured-output blob before trimming.

    Keeps everything that is not a SQLAlchemy / similar verbose log line, so the
    actual pytest assertion errors and short summary info survive.
    """
    if not text:
        return text
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        # Lines starting with a timestamp + 'INFO sqlalchemy...' or similar.
        if any(p in stripped for p in _NOISE_PATTERNS):
            continue
        kept.append(line)
    return "\n".join(kept)


def _extract_summary(text: str) -> str:
    """Pull out the lines that summarise pytest's verdict so they always survive."""
    if not text:
        return ""
    summary_lines: list[str] = []
    for line in text.splitlines():
        if any(p in line for p in _SUMMARY_PATTERNS):
            summary_lines.append(line)
    return "\n".join(summary_lines[:60])


def _mark_validation(ctx: ToolContext, tool_name: str, ok: bool) -> None:
    """Record the latest validator outcome for the DoD gate in finish_story.

    Also keep a per-tool map so multi-validator gates (e.g. testing needs BOTH
    run_pytest and run_e2e green) can check each validator independently, not just
    the single most-recent one."""
    record = {
        "tool": tool_name,
        "ok": bool(ok),
        "at_call": ctx.tool_call_count,
        "at_time": time.time(),
    }
    ctx.metadata["last_validation"] = record
    ctx.metadata.setdefault("validations", {})[tool_name] = record
    if ok:
        ctx.metadata["dirty_since_validation"] = False


def _http_check_max_invocations_per_story() -> int:
    """0 means no cap. Set HTTP_CHECK_MAX_PER_STORY to a positive integer to limit real runs."""
    raw = os.getenv("HTTP_CHECK_MAX_PER_STORY", "").strip()
    if not raw:
        return 0
    try:
        n = int(raw, 10)
        return n if n > 0 else 0
    except ValueError:
        return 0


def _scratch(ctx: ToolContext) -> Path:
    if ctx.scratch_dir is None:
        raise ToolError("This tool requires a scratch_dir on the agent context")
    return Path(ctx.scratch_dir)


def _trim(text: str) -> str:
    if len(text) <= OUTPUT_TAIL_CHARS:
        return text
    return f"... (truncated, showing last {OUTPUT_TAIL_CHARS} chars)\n{text[-OUTPUT_TAIL_CHARS:]}"


# Caching: pip installs into the shared interpreter env, and `requirements.txt` is
# usually identical across stories/attempts — yet the old code reran `pip install`
# on every http_check / pytest / smoke (the biggest avoidable wall-clock cost).
# We now skip the install when this exact requirements set has already been
# installed in this interpreter, keyed by content hash + interpreter prefix.
_PIP_MARKER_DIR = Path(tempfile.gettempdir()) / "agentic_pip_markers"


def _run_pip_install(cwd: Path) -> None:
    req = cwd / "requirements.txt"
    if not req.exists():
        return
    try:
        digest = hashlib.sha256(req.read_bytes() + sys.prefix.encode()).hexdigest()
    except OSError:
        digest = None
    marker = (_PIP_MARKER_DIR / digest) if digest else None
    if marker is not None and marker.exists():
        return  # this exact requirements set is already installed in this env
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "--prefer-offline",
         "-r", "requirements.txt"],
        cwd=cwd,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if marker is not None and result.returncode == 0:
        try:
            _PIP_MARKER_DIR.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok", encoding="utf-8")
        except OSError:
            pass


def _run_pytest_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    target = args.get("target", ".")
    timeout = int(args.get("timeout", 180))
    allow_no_tests = bool(args.get("allow_no_tests", False))

    cwd = _scratch(ctx)
    _run_pip_install(cwd)
    # In the test agent's combined scratch the code under test lives in backend/,
    # and test-only deps (httpx, playwright) are declared in tests/. Install both
    # so the suite can import the app and its test tooling.
    if (cwd / "backend").is_dir():
        _run_pip_install(cwd / "backend")
    if (cwd / "tests").is_dir():
        _run_pip_install(cwd / "tests")

    # Let tests import the code under test. Backend modules use top-level imports
    # (`from main import app`), so both the scratch root and a backend/ subdir
    # (when the test agent mirrors it) must be on PYTHONPATH.
    env = os.environ.copy()
    py_paths = [str(cwd)]
    if (cwd / "backend").is_dir():
        py_paths.append(str(cwd / "backend"))
    if env.get("PYTHONPATH"):
        py_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(py_paths)

    # Emit a machine-readable JUnit XML report so the test ledger (test_ledger.py)
    # can record per-case results without parsing human pytest output. One file per
    # run_pytest call; paths are accumulated on the context for the recorder.
    junit_dir = cwd / ".junit"
    try:
        junit_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    junit_path = junit_dir / f"pytest_{len(ctx.metadata.get('junit_reports', []))}.xml"

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                "--tb=short",                  # shorter tracebacks
                "-q",                          # quiet per-test progress
                "--log-cli-level=WARNING",     # suppress INFO logs (e.g. SQLAlchemy)
                "--no-header",
                f"--junitxml={junit_path}",    # structured results for the test ledger
                target,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        _mark_validation(ctx, "run_pytest", False)
        raw = (exc.stdout or "") + (exc.stderr or "")
        return ToolResult(
            ok=False,
            content=f"pytest timed out after {timeout}s\n{_trim(_filter_noise(raw))}",
        )

    try:
        if junit_path.exists():
            ctx.metadata.setdefault("junit_reports", []).append(str(junit_path))
    except OSError:
        pass

    raw_combined = f"{result.stdout}\n{result.stderr}"
    summary = _extract_summary(raw_combined)
    cleaned = _filter_noise(raw_combined)
    body = _trim(cleaned)
    combined = (
        (f"--- pytest summary ---\n{summary}\n--- output ---\n" if summary else "")
        + body
    )
    # Exit 0 = real pass. Exit 5 = no tests collected; default FAIL because the
    # test agent's job is producing tests, and the backend/frontend should at
    # minimum have a smoke test. Pass `allow_no_tests=true` for explicit
    # implementation-only stories where you've written no test files.
    if result.returncode == 0:
        passed = True
        verdict = "PASS"
    elif result.returncode == 5:
        passed = bool(allow_no_tests)
        verdict = "PASS (no tests, allowed)" if allow_no_tests else "FAIL (no tests collected)"
    else:
        passed = False
        verdict = "FAIL"

    _mark_validation(ctx, "run_pytest", passed)
    return ToolResult(
        ok=passed,
        content=f"pytest exit={result.returncode} ({verdict})\n{combined}",
        metadata={"exit_code": result.returncode, "passed": passed},
    )


def _npm_command() -> list[str] | None:
    npm_exe = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm_exe:
        return None
    if npm_exe.lower().endswith(".cmd"):
        return ["cmd", "/c", npm_exe]
    return [npm_exe]


# Caching: each frontend story runs in a fresh scratch dir that excludes
# node_modules, so the old code did a full `npm install` per story. We keep a
# shared node_modules cache keyed by package.json (+ lockfile) hash: the first
# story populates it, later stories symlink it in so `npm install` is a near-noop.
_NPM_CACHE_DIR = Path(tempfile.gettempdir()) / "agentic_npm_cache"


def _npm_cache_node_modules(cwd: Path) -> Path | None:
    pkg = cwd / "package.json"
    if not pkg.exists():
        return None
    try:
        blob = pkg.read_bytes()
        lock = cwd / "package-lock.json"
        if lock.exists():
            blob += lock.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()[:16]
    except OSError:
        return None
    return _NPM_CACHE_DIR / digest / "node_modules"


def _link_node_modules(cwd: Path) -> None:
    """Symlink a cached node_modules into the scratch dir before `npm install`."""
    scratch_nm = cwd / "node_modules"
    if scratch_nm.exists() or scratch_nm.is_symlink():
        return
    cache_nm = _npm_cache_node_modules(cwd)
    if cache_nm is not None and cache_nm.exists():
        try:
            scratch_nm.symlink_to(cache_nm, target_is_directory=True)
        except OSError:
            pass


def _save_node_modules(cwd: Path) -> None:
    """Populate the shared cache from a freshly installed (real) node_modules."""
    cache_nm = _npm_cache_node_modules(cwd)
    if cache_nm is None or cache_nm.exists():
        return
    scratch_nm = cwd / "node_modules"
    if scratch_nm.is_symlink() or not scratch_nm.exists():
        return  # nothing real to cache
    try:
        cache_nm.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(scratch_nm, cache_nm, symlinks=False)
    except OSError:
        pass


def _run_npm_install_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    cwd = _scratch(ctx)
    cmd = _npm_command()
    if not cmd:
        return ToolResult(ok=False, content="npm not found on PATH")

    _link_node_modules(cwd)
    try:
        result = subprocess.run(
            [*cmd, "install", "--prefer-offline", "--no-audit", "--no-fund"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        return ToolResult(ok=False, content=f"npm install timed out\n{_trim((exc.stdout or '') + (exc.stderr or ''))}")
    if result.returncode == 0:
        _save_node_modules(cwd)

    output = _trim(f"{result.stdout}\n{result.stderr}")
    return ToolResult(
        ok=result.returncode == 0,
        content=f"npm install exit={result.returncode}\n{output}",
        metadata={"exit_code": result.returncode},
    )


def _run_npm_build_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    cwd = _scratch(ctx)
    cmd = _npm_command()
    if not cmd:
        return ToolResult(ok=False, content="npm not found on PATH")

    npx_exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx_exe:
        return ToolResult(ok=False, content="npx not found on PATH")

    _link_node_modules(cwd)
    install = subprocess.run(
        [*cmd, "install", "--prefer-offline", "--no-audit", "--no-fund"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if install.returncode != 0:
        return ToolResult(
            ok=False,
            content=f"npm install failed (exit={install.returncode})\n{_trim(install.stdout + install.stderr)}",
        )
    _save_node_modules(cwd)

    try:
        build = subprocess.run(
            [npx_exe, "vite", "build"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        return ToolResult(ok=False, content=f"vite build timed out\n{_trim((exc.stdout or '') + (exc.stderr or ''))}")

    output = _trim(f"{build.stdout}\n{build.stderr}")
    if build.returncode != 0:
        _mark_validation(ctx, "run_npm_build", False)
        return ToolResult(ok=False, content=f"vite build exit={build.returncode}\n{output}")

    dist_index = cwd / "dist" / "index.html"
    if not dist_index.exists():
        _mark_validation(ctx, "run_npm_build", False)
        return ToolResult(ok=False, content="vite build succeeded but dist/index.html missing")

    _mark_validation(ctx, "run_npm_build", True)
    return ToolResult(ok=True, content=f"vite build PASS\n{output}", metadata={"exit_code": 0})


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _terminate_and_drain(proc, timeout: float = 5.0) -> tuple[str, str]:
    """Stop a still-running server Popen and drain its stdout/stderr without blocking.

    Calling ``proc.stderr.read()`` on a *live* process blocks until that process
    exits on its own — a server that bound nothing but hung during startup never
    does, so the read hangs forever (the exact unbounded stall the per-call
    timeouts exist to prevent). Terminating first guarantees the pipes reach EOF;
    ``communicate()`` bounds the drain and escalates to ``kill()`` if terminate is
    ignored. Returns ``(stdout, stderr)`` as strings, never raising.
    """
    if proc is None:
        return "", ""
    try:
        if proc.poll() is None:
            proc.terminate()
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate(timeout=timeout)
        except Exception:  # noqa: BLE001
            out, err = "", ""
    except Exception:  # noqa: BLE001
        out, err = "", ""
    return (out or ""), (err or "")


def _smoke_uvicorn_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Boot uvicorn against the working copy's main:app and check for startup errors.

    Also probes ``GET /openapi.json`` to confirm FastAPI fully bootstrapped (router
    wiring, middleware, lifespan startup all succeeded). A startup that binds the
    port but fails to serve OpenAPI is still treated as a failure.
    """
    cwd = _scratch(ctx)
    main_py = cwd / "main.py"
    if not main_py.exists():
        # No app to smoke — record as a passing validation so the agent isn't
        # blocked from finishing impl-only tasks that don't include main.py.
        _mark_validation(ctx, "smoke_uvicorn", True)
        return ToolResult(ok=True, content="No main.py found — skipping uvicorn smoke test.")

    _run_pip_install(cwd)

    port = _find_free_port()
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        if not _wait_for_port(port, timeout=30):
            # The server never bound. It may have exited (clean failure) or be hung
            # alive — terminate before draining so the pipe read can't block forever.
            _, stderr = _terminate_and_drain(proc)
            _mark_validation(ctx, "smoke_uvicorn", False)
            return ToolResult(
                ok=False,
                content=f"uvicorn failed to bind port {port} within 30s\nStderr:\n{_trim(stderr)}",
            )

        # Probe /openapi.json to verify the router actually wired up.
        time.sleep(0.5)
        probe_status: int | None = None
        probe_body = ""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/openapi.json", timeout=5
            ) as resp:
                probe_status = resp.status
                probe_body = resp.read(2000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            probe_status = exc.code
            try:
                probe_body = exc.read(2000).decode("utf-8", errors="replace")
            except Exception:
                probe_body = ""
        except Exception as exc:  # noqa: BLE001
            probe_body = f"(probe failed: {exc})"

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        stdout = proc.stdout.read() if proc.stdout else ""
        stderr = proc.stderr.read() if proc.stderr else ""
        combined = _trim(f"{stdout}\n{stderr}")

        bad_markers = (
            "Traceback", "ModuleNotFoundError", "ImportError", "SyntaxError",
            "Error loading ASGI app", "Application startup failed",
        )
        for marker in bad_markers:
            if marker in combined:
                _mark_validation(ctx, "smoke_uvicorn", False)
                return ToolResult(
                    ok=False,
                    content=f"uvicorn startup error detected ({marker}):\n{combined}",
                )

        if probe_status != 200:
            _mark_validation(ctx, "smoke_uvicorn", False)
            return ToolResult(
                ok=False,
                content=(
                    f"uvicorn bound port {port} but /openapi.json returned status={probe_status}. "
                    f"FastAPI did not finish bootstrapping.\nProbe body: {probe_body[:500]}\n"
                    f"Server logs:\n{combined}"
                ),
            )

        _mark_validation(ctx, "smoke_uvicorn", True)
        return ToolResult(
            ok=True,
            content=(
                f"uvicorn smoke PASS on port {port}; /openapi.json returned 200 "
                f"({len(probe_body)} chars).\n{combined}"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        _mark_validation(ctx, "smoke_uvicorn", False)
        return ToolResult(ok=False, content=f"uvicorn smoke crashed: {exc}")
    finally:
        if proc and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def _run_python_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Execute a short Python snippet inside the agent's scratch dir.

    The snippet runs with the scratch dir prepended to ``sys.path`` so the
    agent can quickly verify imports, instantiate classes, or inspect
    behaviour without writing a full test file. Output is captured and
    trimmed; the snippet is killed after 10 seconds.
    """
    code = args.get("code", "")
    timeout = int(args.get("timeout", 10))
    if not code or not isinstance(code, str):
        return ToolResult(ok=False, content="'code' is required and must be a string")
    if len(code) > 8000:
        return ToolResult(ok=False, content="Snippet too large (>8000 chars). Write a file instead.")

    cwd = _scratch(ctx)
    # Bootstrap sys.path so `from main import ...` works without packaging.
    bootstrap = textwrap.dedent(f"""
        import sys, os
        sys.path.insert(0, {str(cwd)!r})
        os.chdir({str(cwd)!r})
    """).strip()
    full = bootstrap + "\n" + code

    try:
        result = subprocess.run(
            [sys.executable, "-c", full],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return ToolResult(
            ok=False,
            content=(
                f"run_python timed out after {timeout}s\n"
                f"{_trim((exc.stdout or '') + (exc.stderr or ''))}"
            ),
        )

    combined = _trim(f"{result.stdout}\n{result.stderr}")
    ok = result.returncode == 0
    return ToolResult(
        ok=ok,
        content=f"run_python exit={result.returncode}\n{combined}",
        metadata={"exit_code": result.returncode},
    )


def _http_check_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Boot uvicorn briefly, send one HTTP request, and return status + body.

    Lets backend agents actually exercise an endpoint instead of guessing from
    static analysis. Boots ``main:app`` like ``smoke_uvicorn``, then issues a
    single request via ``urllib`` and shuts the server down.
    """
    method = (args.get("method") or "GET").upper()
    path = args.get("path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    json_body = args.get("json_body")
    headers = args.get("headers") or {}
    timeout = int(args.get("timeout", 10))

    last_val = ctx.metadata.get("last_validation")
    dirty = bool(ctx.metadata.get("dirty_since_validation"))
    if (
        last_val
        and last_val.get("tool") == "http_check"
        and last_val.get("ok")
        and not dirty
    ):
        return ToolResult(
            ok=True,
            content=(
                "Skipped redundant http_check: a passing check is already recorded and "
                "there were no file edits since. Use publish_contract / finish_story, "
                "or edit files then call http_check again."
            ),
            metadata={"skipped_redundant": True},
        )

    cap = _http_check_max_invocations_per_story()
    used = int(ctx.metadata.get("http_check_invocations", 0))
    if cap > 0 and used >= cap:
        return ToolResult(
            ok=False,
            content=(
                f"http_check: reached max invocations for this story ({cap}, "
                f"set via HTTP_CHECK_MAX_PER_STORY). Use run_python to debug, or reduce noise."
            ),
        )
    ctx.metadata["http_check_invocations"] = used + 1

    cwd = _scratch(ctx)
    main_py = cwd / "main.py"
    if not main_py.exists():
        _mark_validation(ctx, "http_check", False)
        return ToolResult(ok=False, content="No main.py found — cannot http_check.")

    _run_pip_install(cwd)

    port = _find_free_port()
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if not _wait_for_port(port, timeout=20):
            # See _terminate_and_drain: stop the process before reading its pipes so
            # a hung-but-alive server can't block the read indefinitely.
            _, stderr = _terminate_and_drain(proc)
            _mark_validation(ctx, "http_check", False)
            return ToolResult(
                ok=False,
                content=f"uvicorn failed to bind port {port} within 20s\nStderr:\n{_trim(stderr)}",
            )

        time.sleep(0.5)
        url = f"http://127.0.0.1:{port}{path}"
        body_bytes: bytes | None = None
        req_headers = {str(k): str(v) for k, v in headers.items()}
        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
        status: int | None = None
        body_text = ""
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                body_text = resp.read(8000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body_text = exc.read(8000).decode("utf-8", errors="replace")
            except Exception:
                body_text = ""
        except Exception as exc:  # noqa: BLE001
            _mark_validation(ctx, "http_check", False)
            return ToolResult(ok=False, content=f"HTTP request failed: {exc}")

        # 2xx only — redirects (3xx) must not count as success or agents get false
        # greens from slash redirects (e.g. POST /api/foo → 307) without a real body.
        ok_status = status is not None and 200 <= status < 300
        _mark_validation(ctx, "http_check", ok_status)
        hint = ""
        if status is not None and 300 <= status < 400:
            hint = (
                "\n(note: 3xx redirects do not count as ok=True — use the canonical path "
                "and json_body for POST, or expect 2xx.)"
            )
        return ToolResult(
            ok=ok_status,
            content=(
                f"{method} {path} → {status}\n"
                f"--- body ({len(body_text)} chars) ---\n{body_text}{hint}"
            ),
            metadata={"status": status, "method": method, "path": path},
        )
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def _run_lint_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Run static analysis appropriate to the scratch dir.

    Auto-detects the language(s) by scanning for a few signal files:
      - ``*.py``: ``ruff check`` (preferred) or ``python -m pyflakes`` (fallback)
      - ``package.json``: ``npx eslint`` if available
    If neither is available, returns a soft-pass note explaining why.
    """
    cwd = _scratch(ctx)
    target = args.get("target", ".")

    has_python = any(cwd.rglob("*.py"))
    has_node = (cwd / "package.json").exists()
    if not has_python and not has_node:
        return ToolResult(
            ok=True,
            content="run_lint: no Python or Node sources found — nothing to lint.",
        )

    outputs: list[str] = []
    overall_ok = True

    if has_python:
        ruff = shutil.which("ruff")
        if ruff:
            try:
                result = subprocess.run(
                    [ruff, "check", target],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                outputs.append(
                    f"[ruff] exit={result.returncode}\n{_trim(result.stdout + result.stderr)}"
                )
                if result.returncode != 0:
                    overall_ok = False
            except subprocess.TimeoutExpired:
                outputs.append("[ruff] timed out after 60s")
                overall_ok = False
        else:
            # Try pyflakes via the active interpreter; if it is not importable
            # treat that as "no linter available" (soft pass) — a missing tool
            # should never block the agent's progress.
            pyflakes_ok = False
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pyflakes", target],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                stderr_lower = (result.stderr or "").lower()
                if "no module named pyflakes" in stderr_lower:
                    outputs.append(
                        "[lint] pyflakes not installed in this venv; skipping Python lint "
                        "(soft pass). Install with `pip install pyflakes` or `pip install ruff`."
                    )
                else:
                    pyflakes_ok = True
                    outputs.append(
                        f"[pyflakes] exit={result.returncode}\n"
                        f"{_trim(result.stdout + result.stderr)}"
                    )
                    if result.returncode != 0:
                        overall_ok = False
            except FileNotFoundError:
                outputs.append("[lint] No ruff or pyflakes installed; skipping Python lint (soft pass).")
            except subprocess.TimeoutExpired:
                if pyflakes_ok:
                    outputs.append("[pyflakes] timed out after 60s")
                    overall_ok = False
                else:
                    outputs.append("[lint] pyflakes timed out before producing output (soft pass).")

    if has_node:
        npx = shutil.which("npx") or shutil.which("npx.cmd")
        if npx:
            try:
                result = subprocess.run(
                    [npx, "--yes", "eslint", "--no-error-on-unmatched-pattern",
                     "src", "--ext", ".js,.jsx,.ts,.tsx"],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                )
                outputs.append(
                    f"[eslint] exit={result.returncode}\n"
                    f"{_trim(result.stdout + result.stderr)}"
                )
                if result.returncode != 0:
                    # ESLint exits 1 on lint warnings — only fail hard on exit 2 (config).
                    if result.returncode > 1:
                        overall_ok = False
            except subprocess.TimeoutExpired:
                outputs.append("[eslint] timed out after 120s")
                overall_ok = False
        else:
            outputs.append("[lint] npx not found; skipping JS lint.")

    return ToolResult(
        ok=overall_ok,
        content="\n\n".join(outputs) or "(no lint output)",
    )


def _git_diff_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Show what's changed in the agent's scratch dir vs the live workspace.

    Uses ``git diff --no-index`` if git is available; otherwise falls back to a
    file-by-file comparison. Output is trimmed to the last 6000 chars.
    """
    if ctx.scratch_dir is None or ctx.workspace_dir is None:
        return ToolResult(
            ok=False,
            content="git_diff requires both scratch_dir and workspace_dir on the context.",
        )

    scratch = Path(ctx.scratch_dir).resolve()
    # Compare scratch against the matching live subdir (backend/frontend/tests).
    workspace = Path(ctx.workspace_dir).resolve()
    sub_candidates = ["backend", "frontend", "tests"]
    workspace_target: Path | None = None
    for sub in sub_candidates:
        if scratch.name.startswith(f"{sub}_attempt") or sub in scratch.parts:
            candidate = workspace / sub
            if candidate.exists():
                workspace_target = candidate
                break
    if workspace_target is None:
        # Best effort: use the workspace_dir directly.
        workspace_target = workspace

    git = shutil.which("git")
    if git:
        try:
            result = subprocess.run(
                [git, "diff", "--no-index", "--stat", str(workspace_target), str(scratch)],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            stat = result.stdout.strip() or "(no differences)"

            full = subprocess.run(
                [git, "diff", "--no-index", str(workspace_target), str(scratch)],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            body = _trim(full.stdout + full.stderr) if full.stdout else "(no differences)"
            return ToolResult(
                ok=True,
                content=f"--- summary ---\n{stat}\n\n--- diff ---\n{body}",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, content="git diff timed out")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, content=f"git diff crashed: {exc}")

    # Fallback: list new/modified/deleted files only.
    new_files: list[str] = []
    modified: list[str] = []
    for path in scratch.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(scratch)
        if any(part in {"node_modules", ".venv", "__pycache__", "dist"} for part in rel.parts):
            continue
        peer = workspace_target / rel
        if not peer.exists():
            new_files.append(str(rel))
        else:
            try:
                if path.read_bytes() != peer.read_bytes():
                    modified.append(str(rel))
            except OSError:
                pass

    if not new_files and not modified:
        return ToolResult(ok=True, content="(no differences)")
    body = ""
    if new_files:
        body += "NEW FILES:\n" + "\n".join(f"  + {f}" for f in new_files) + "\n"
    if modified:
        body += "MODIFIED:\n" + "\n".join(f"  ~ {f}" for f in modified) + "\n"
    return ToolResult(ok=True, content=body.rstrip())


# ---------------------------------------------------------------------------
# check_ui — serve the built frontend and assert rendered DOM in a headless browser
# ---------------------------------------------------------------------------

# Runs in a subprocess (isolates Playwright + avoids any asyncio-loop conflict
# with the dashboard's uvicorn host). Prints a single JSON line to stdout.
_PLAYWRIGHT_DRIVER = r'''
import sys, json
params = json.loads(sys.argv[1])
url = params["url"]
expect_text = params.get("expect_text", [])
expect_selectors = params.get("expect_selectors", [])
timeout = int(params.get("timeout", 25))
try:
    from playwright.sync_api import sync_playwright
except Exception as exc:
    print(json.dumps({"_error": "playwright_missing", "detail": str(exc)})); sys.exit(0)
console_errors = []
try:
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            print(json.dumps({"_error": "browser_missing", "detail": str(exc)})); sys.exit(0)
        page = browser.new_page()
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append("pageerror: " + str(e)))
        resp = page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        page.wait_for_timeout(600)
        interaction_errors = []
        for step in params.get("interactions", []):
            try:
                if "click" in step:
                    page.click(step["click"], timeout=5000)
                elif "fill" in step:
                    page.fill(step["fill"]["selector"], str(step["fill"].get("value", "")), timeout=5000)
                elif "wait_ms" in step:
                    page.wait_for_timeout(int(step["wait_ms"]))
                page.wait_for_timeout(250)
            except Exception as exc:
                interaction_errors.append(str(step)[:120] + " -> " + str(exc)[:200])
        body_text = page.inner_text("body")[:6000]
        text_results = [{"text": t, "found": t.lower() in body_text.lower()} for t in expect_text]
        sel_results = []
        for s in expect_selectors:
            try:
                cnt = page.locator(s).count()
            except Exception:
                cnt = -1
            sel_results.append({"selector": s, "count": cnt, "found": cnt > 0})
        browser.close()
    print(json.dumps({
        "status": resp.status if resp else None, "body_text": body_text,
        "text_results": text_results, "selector_results": sel_results,
        "console_errors": console_errors[:20],
        "interaction_errors": interaction_errors[:10],
    }))
except Exception as exc:
    print(json.dumps({"_error": "driver_crash", "detail": str(exc)}))
'''


def _serve_dir(directory: Path, port: int, api_port: int | None = None):
    """Serve a built SPA on 127.0.0.1:port in a daemon thread, falling back to
    index.html for client-side routes. Returns the server (call .shutdown()).

    When ``api_port`` is set, requests under ``/api`` are reverse-proxied to a
    backend booted on that port, so the SPA's relative ``/api/*`` fetches resolve
    to real data (tables, boards) during a headless UI check instead of 404ing.
    """
    from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

    class _SPAHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):  # silence access logs
            pass

        def _is_api(self):
            return api_port is not None and self.path.split("?")[0].startswith("/api")

        def _proxy(self):
            body_len = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(body_len) if body_len else None
            fwd_headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in ("host", "content-length", "connection")}
            req = urllib.request.Request(
                f"http://127.0.0.1:{api_port}{self.path}",
                data=body, headers=fwd_headers, method=self.command,
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                    status, headers_out = resp.status, resp.getheaders()
            except urllib.error.HTTPError as exc:
                data = exc.read()
                status = exc.code
                headers_out = list(exc.headers.items())
            except Exception as exc:  # noqa: BLE001
                data = json.dumps({"proxy_error": str(exc)}).encode()
                status, headers_out = 502, [("Content-Type", "application/json")]
            self.send_response(status)
            for hk, hv in headers_out:
                if hk.lower() in ("content-length", "transfer-encoding", "connection"):
                    continue
                self.send_header(hk, hv)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self._is_api():
                return self._proxy()
            fs_path = self.translate_path(self.path)
            base = os.path.basename(self.path.split("?")[0])
            if not os.path.exists(fs_path) and "." not in base:
                self.path = "/index.html"  # SPA fallback
            return super().do_GET()

        def do_POST(self):
            if self._is_api():
                return self._proxy()
            self.send_error(405)

        do_PATCH = do_POST
        do_PUT = do_POST
        do_DELETE = do_POST

    handler = functools.partial(_SPAHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _boot_backend_for_ui(ctx: ToolContext):
    """Boot the sibling backend (uvicorn main:app) for a data-driven UI check.

    Returns (proc, port, error). The backend dir comes from ctx.metadata
    (set by the agent base) or falls back to <workspace>/backend.
    """
    backend_dir = ctx.metadata.get("backend_dir")
    bd = Path(backend_dir) if backend_dir else (
        Path(ctx.workspace_dir) / "backend" if ctx.workspace_dir else None
    )
    if bd is None or not (bd / "main.py").exists():
        return None, None, f"no backend main.py (looked in {bd})"
    _run_pip_install(bd)
    port = _find_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=bd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if not _wait_for_port(port, timeout=30):
        _terminate_and_drain(proc)
        return None, None, "backend failed to bind within 30s"
    return proc, port, None


def _check_ui_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Serve the built frontend and verify rendered DOM in a headless browser.

    Use AFTER ``run_npm_build`` to confirm the story's UI actually renders the
    elements/text it requires (build success alone proves nothing about the DOM).
    Pass ``expect_text`` and/or ``expect_selectors`` from the story's acceptance
    criteria. Soft-passes if Playwright or its browser is not installed.
    """
    path = args.get("path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    expect_text = [str(t) for t in (args.get("expect_text") or [])]
    expect_selectors = [str(s) for s in (args.get("expect_selectors") or [])]
    timeout = int(args.get("timeout", 25))
    interactions = args.get("interactions") or []
    boot_backend = bool(args.get("boot_backend", False))

    cwd = _scratch(ctx)
    index = cwd / "dist" / "index.html"
    if not index.exists():
        return ToolResult(
            ok=False,
            content="No dist/index.html found. Run run_npm_build first, then call check_ui.",
        )

    backend_proc = None
    api_port = None
    backend_note = ""
    if boot_backend:
        backend_proc, api_port, boot_err = _boot_backend_for_ui(ctx)
        if boot_err:
            backend_note = f"\n(boot_backend requested but failed: {boot_err} — /api calls will 404)"

    port = _find_free_port()
    server = _serve_dir(cwd / "dist", port, api_port)
    try:
        url = f"http://127.0.0.1:{port}{path}"
        params = json.dumps({
            "url": url, "expect_text": expect_text,
            "expect_selectors": expect_selectors, "timeout": timeout,
            "interactions": interactions,
        })
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _PLAYWRIGHT_DRIVER, params],
                capture_output=True, text=True, timeout=timeout + 45,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, content=f"check_ui timed out after {timeout + 45}s")

        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out.splitlines()[-1]) if out else {}
        except (json.JSONDecodeError, IndexError):
            return ToolResult(
                ok=True,
                content=(
                    "check_ui: could not parse browser output (soft pass — not a validation).\n"
                    f"stdout: {out[:400]}\nstderr: {(proc.stderr or '')[-400:]}"
                ),
            )

        err = data.get("_error")
        if err in ("playwright_missing", "browser_missing"):
            return ToolResult(
                ok=True,
                content=(
                    f"check_ui skipped: headless browser unavailable ({err}). "
                    "Enable runtime UI checks with: "
                    "`pip install playwright && python -m playwright install chromium`. "
                    "(soft pass — not a validation)"
                ),
                metadata={"skipped": True, "reason": err},
            )
        if err:
            return ToolResult(ok=False, content=f"check_ui driver error: {str(data.get('detail', ''))[:600]}")

        text_results = data.get("text_results", [])
        sel_results = data.get("selector_results", [])
        console_errors = data.get("console_errors", [])
        interaction_errors = data.get("interaction_errors", [])
        missing_text = [t["text"] for t in text_results if not t["found"]]
        missing_sel = [s["selector"] for s in sel_results if not s["found"]]
        has_expectations = bool(expect_text or expect_selectors)
        ok = (not missing_text and not missing_sel) if has_expectations else True

        lines = [f"check_ui {path} → HTTP {data.get('status')} ({'PASS' if ok else 'FAIL'})"]
        if text_results:
            lines.append("Text: " + ", ".join(
                f"{'✓' if t['found'] else '✗'} {t['text'][:40]}" for t in text_results))
        if sel_results:
            lines.append("Selectors: " + ", ".join(
                f"{'✓' if s['found'] else '✗'} {s['selector']} (n={s['count']})" for s in sel_results))
        if console_errors:
            lines.append(f"⚠ console/page errors ({len(console_errors)}): "
                         + " | ".join(str(e)[:120] for e in console_errors[:5]))
        if interaction_errors:
            lines.append(f"⚠ interaction errors ({len(interaction_errors)}): "
                         + " | ".join(str(e)[:140] for e in interaction_errors[:5]))
        lines.append("--- rendered body text (excerpt) ---")
        lines.append((data.get("body_text") or "").strip()[:2500] or "(empty body — page rendered nothing)")

        return ToolResult(
            ok=ok,
            content="\n".join(lines) + backend_note,
            metadata={"missing_text": missing_text, "missing_selectors": missing_sel,
                      "console_errors": console_errors, "interaction_errors": interaction_errors,
                      "status": data.get("status")},
        )
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        if backend_proc and backend_proc.poll() is None:
            backend_proc.terminate()
            try:
                backend_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                backend_proc.kill()


# ---------------------------------------------------------------------------
# run_e2e — boot the full app stack and run the UI / flow / integration suites
# ---------------------------------------------------------------------------

_PLAYWRIGHT_READY: bool | None = None


def _playwright_ready() -> bool:
    """One-time probe: is Playwright installed AND a chromium browser launchable?
    Cached for the process. Lets run_e2e gracefully skip UI/flow layers on hosts
    without a browser (consistent with check_ui's soft-skip) instead of hard-failing."""
    global _PLAYWRIGHT_READY
    if _PLAYWRIGHT_READY is not None:
        return _PLAYWRIGHT_READY
    probe = (
        "from playwright.sync_api import sync_playwright\n"
        "p=sync_playwright().start()\n"
        "b=p.chromium.launch(); b.close(); p.stop()\n"
        "print('ok')"
    )
    try:
        r = subprocess.run([sys.executable, "-c", probe],
                           capture_output=True, text=True, timeout=90)
        _PLAYWRIGHT_READY = r.returncode == 0 and "ok" in (r.stdout or "")
    except Exception:  # noqa: BLE001
        _PLAYWRIGHT_READY = False
    return _PLAYWRIGHT_READY


def _build_frontend_dist(frontend_dir: Path) -> tuple[bool, str]:
    """npm install + vite build inside frontend_dir when dist/index.html is missing.
    Returns (ok, note); ok=True means a servable dist/index.html exists."""
    index = frontend_dir / "dist" / "index.html"
    if index.exists():
        return True, "dist already present"
    if not (frontend_dir / "package.json").exists():
        return False, "no package.json in frontend"
    cmd = _npm_command()
    npx_exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not cmd or not npx_exe:
        return False, "npm/npx not found on PATH"
    _link_node_modules(frontend_dir)
    inst = subprocess.run(
        [*cmd, "install", "--prefer-offline", "--no-audit", "--no-fund"],
        cwd=frontend_dir, capture_output=True, text=True, check=False, timeout=300,
    )
    if inst.returncode != 0:
        return False, f"npm install failed: {_trim(inst.stdout + inst.stderr)[-600:]}"
    _save_node_modules(frontend_dir)
    try:
        build = subprocess.run(
            [npx_exe, "vite", "build"], cwd=frontend_dir,
            capture_output=True, text=True, check=False, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "vite build timed out"
    if build.returncode != 0 or not index.exists():
        return False, f"vite build failed: {_trim(build.stdout + build.stderr)[-600:]}"
    return True, "built dist"


def _run_e2e_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Boot the full app stack (backend + built frontend, /api reverse-proxied) and
    run the UI / flow / integration pytest suites against it, with BASE_URL exported.

    Emits JUnit XML for the test ledger. ok=True only if the selected suites pass
    (exit 0). UI/flow layers are skipped (soft) when a headless browser is
    unavailable; if that leaves nothing runnable, returns a soft skip."""
    timeout = int(args.get("timeout", 300))
    requested = args.get("layers") or ["ui", "flows", "integration"]
    cwd = _scratch(ctx)
    tests_dir = cwd / "tests"

    # Install python test deps (pytest/httpx/playwright) + the code under test.
    _run_pip_install(cwd)
    if (cwd / "backend").is_dir():
        _run_pip_install(cwd / "backend")
    if tests_dir.is_dir():
        _run_pip_install(tests_dir)

    # Which requested layers actually have test files?
    browser_ok = _playwright_ready()
    layers, skipped = [], []
    for layer in requested:
        d = tests_dir / layer
        if not (d.exists() and any(d.rglob("test_*.py"))):
            continue
        if layer in ("ui", "flows") and not browser_ok:
            skipped.append(layer)
            continue
        layers.append(layer)

    if not layers:
        note = (f" (skipped {', '.join(skipped)}: no headless browser — "
                "install with `python -m playwright install chromium`)" if skipped else "")
        # Soft pass so a browser-less host isn't permanently blocked (like check_ui);
        # still records the validator so the testing DoD gate can proceed.
        _mark_validation(ctx, "run_e2e", True)
        return ToolResult(
            ok=True,
            content=f"run_e2e: no runnable UI/flow/integration tests{note} (soft pass — not a validation).",
            metadata={"skipped": True, "skipped_layers": skipped},
        )

    targets = [f"tests/{layer}" for layer in layers]

    # Boot the backend.
    backend_proc, api_port, boot_err = _boot_backend_for_ui(ctx)
    server = None
    notes = []
    if skipped:
        notes.append(f"UI/flow skipped (no browser): {', '.join(skipped)}")
    try:
        if boot_err:
            _mark_validation(ctx, "run_e2e", False)
            return ToolResult(ok=False, content=f"run_e2e: backend failed to boot: {boot_err}")

        # Serve the built frontend (build it if needed) so UI/flow tests hit the real
        # app with real data; BASE_URL points at it (/api proxied to the backend).
        frontend_dir = cwd / "frontend"
        if frontend_dir.exists() and (frontend_dir / "package.json").exists():
            built, fnote = _build_frontend_dist(frontend_dir)
            notes.append(f"frontend: {fnote}")
            if built:
                fport = _find_free_port()
                server = _serve_dir(frontend_dir / "dist", fport, api_port)
                base_url = f"http://127.0.0.1:{fport}"
            else:
                base_url = f"http://127.0.0.1:{api_port}"
                notes.append("UI/flow tests need a built frontend; using backend URL")
        else:
            base_url = f"http://127.0.0.1:{api_port}"

        env = os.environ.copy()
        py_paths = [str(cwd)]
        if (cwd / "backend").is_dir():
            py_paths.append(str(cwd / "backend"))
        if env.get("PYTHONPATH"):
            py_paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(py_paths)
        env["BASE_URL"] = base_url

        junit_dir = cwd / ".junit"
        try:
            junit_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        junit_path = junit_dir / f"e2e_{len(ctx.metadata.get('junit_reports', []))}.xml"

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "--tb=short", "-q", "--no-header",
                 "--log-cli-level=WARNING", f"--junitxml={junit_path}", *targets],
                cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            _mark_validation(ctx, "run_e2e", False)
            raw = (exc.stdout or "") + (exc.stderr or "")
            return ToolResult(ok=False, content=f"run_e2e timed out after {timeout}s\n{_trim(_filter_noise(raw))}")

        try:
            if junit_path.exists():
                ctx.metadata.setdefault("junit_reports", []).append(str(junit_path))
        except OSError:
            pass

        raw_combined = f"{result.stdout}\n{result.stderr}"
        summary = _extract_summary(raw_combined)
        body = _trim(_filter_noise(raw_combined))
        combined = (f"--- pytest summary ---\n{summary}\n--- output ---\n" if summary else "") + body

        if result.returncode == 0:
            passed, verdict = True, "PASS"
        elif result.returncode == 5:
            passed, verdict = False, "FAIL (no tests collected)"
        else:
            passed, verdict = False, "FAIL"

        _mark_validation(ctx, "run_e2e", passed)
        note_str = ("\n(" + "; ".join(notes) + ")") if notes else ""
        return ToolResult(
            ok=passed,
            content=(f"run_e2e exit={result.returncode} ({verdict}) "
                     f"layers={','.join(layers)} base_url={base_url}{note_str}\n{combined}"),
            metadata={"exit_code": result.returncode, "passed": passed,
                      "base_url": base_url, "layers": layers, "skipped_layers": skipped},
        )
    finally:
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
        if backend_proc and backend_proc.poll() is None:
            backend_proc.terminate()
            try:
                backend_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                backend_proc.kill()


def register_exec_tools(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="run_pytest",
        description=(
            "Run pytest in the agent's scratch directory and return the output. "
            "Use to validate Python code changes. ok=True only if exit==0. "
            "Exit==5 (no tests collected) is treated as FAIL by default; pass "
            "allow_no_tests=true ONLY for explicit implementation-only stories "
            "that intentionally have no tests."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "pytest target path (defaults to '.')"},
                "timeout": {"type": "integer", "minimum": 10, "maximum": 600},
                "allow_no_tests": {
                    "type": "boolean",
                    "description": "Treat 'no tests collected' as a pass. Default false.",
                },
            },
            "additionalProperties": False,
        },
        handler=_run_pytest_handler,
    ))
    registry.register(Tool(
        name="run_npm_install",
        description="Run `npm install` in the scratch directory.",
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_run_npm_install_handler,
    ))
    registry.register(Tool(
        name="run_npm_build",
        description=(
            "Run `npm install` then `vite build` in the scratch directory. "
            "Returns ok=True only if dist/index.html is produced. Use to validate React changes."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_run_npm_build_handler,
    ))
    registry.register(Tool(
        name="smoke_uvicorn",
        description=(
            "Boot the working copy's FastAPI app via uvicorn, wait up to 30s for it to bind, "
            "then probe GET /openapi.json to confirm the router fully bootstrapped. "
            "Use as a final check that the backend imports and serves cleanly."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_smoke_uvicorn_handler,
    ))
    registry.register(Tool(
        name="run_python",
        description=(
            "Execute a short Python snippet inside the agent's scratch dir (with the dir "
            "on sys.path). Use for quick 'does this import / instantiate / return what I "
            "expect' checks instead of writing a full test file. 10s timeout."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 60},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
        handler=_run_python_handler,
    ))
    registry.register(Tool(
        name="http_check",
        description=(
            "Boot uvicorn briefly and send one HTTP request against the backend in scratch. "
            "Returns the response status and body. **ok=True** only for HTTP 2xx (not 3xx "
            "redirects). Use to verify endpoints; for POST include json_body. "
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "HTTP method (default GET)"},
                "path": {"type": "string", "description": "Request path, e.g. '/api/customers'"},
                "json_body": {"type": "object", "description": "JSON body for POST/PUT"},
                "headers": {"type": "object"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 60},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_http_check_handler,
    ))
    registry.register(Tool(
        name="run_lint",
        description=(
            "Run static analysis on the scratch dir (ruff/pyflakes for Python, eslint for JS/TS). "
            "Catches undefined names, missing imports, unused variables. Cheap and fast — "
            "run after edits before invoking the heavier validators."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Subpath to lint (default '.')"},
            },
            "additionalProperties": False,
        },
        handler=_run_lint_handler,
    ))
    registry.register(Tool(
        name="git_diff",
        description=(
            "Show what files have changed in the agent's scratch dir vs the live workspace. "
            "Use before finish_story to review your own work and catch accidental deletions "
            "of public functions or scaffold files."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_git_diff_handler,
    ))
    registry.register(Tool(
        name="check_ui",
        description=(
            "Serve the built frontend (dist/) and verify the RENDERED DOM in a headless "
            "browser. Use AFTER run_npm_build to prove the story's UI actually renders — "
            "build success alone says nothing about the DOM. Pass expect_text (visible "
            "strings) and/or expect_selectors (CSS selectors) drawn from the story's "
            "acceptance criteria; ok=True only if all are found. Also reports console/page "
            "errors (blank-page symptoms). "
            "To verify UI behind an interaction (e.g. options inside a modal that opens on "
            "click), pass `interactions` (e.g. [{\"click\": \"text=New Ticket\"}]) — the steps "
            "run before assertions so the revealed content is checked. To verify data-driven "
            "screens (tables, boards) against REAL data, pass `boot_backend=true` to boot the "
            "sibling backend and proxy /api to it. Soft-passes if a headless browser isn't installed."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Route to load (default '/')"},
                "expect_text": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Visible text strings that must appear in the rendered page",
                },
                "expect_selectors": {
                    "type": "array", "items": {"type": "string"},
                    "description": "CSS selectors that must match at least one element",
                },
                "interactions": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Optional steps run after load, before assertions, to reveal UI behind "
                        "an interaction. Each item is one of: {\"click\": \"<css or text=...>\"}, "
                        "{\"fill\": {\"selector\": \"#id\", \"value\": \"x\"}}, or {\"wait_ms\": 500}. "
                        "Example (open a modal): [{\"click\": \"text=New Ticket\"}]"
                    ),
                },
                "boot_backend": {
                    "type": "boolean",
                    "description": (
                        "Boot the sibling backend and reverse-proxy /api to it so data-driven "
                        "screens render real data. Default false (static dist only)."
                    ),
                },
                "timeout": {"type": "integer", "minimum": 5, "maximum": 120},
            },
            "additionalProperties": False,
        },
        handler=_check_ui_handler,
    ))
    registry.register(Tool(
        name="run_e2e",
        description=(
            "Boot the full app stack (backend + built frontend, with /api reverse-proxied) "
            "and run the UI / flow / integration pytest suites against it. Sets BASE_URL for "
            "the tests (available via the `base_url`/`api_client` fixtures). Builds the "
            "frontend automatically if needed. Emits results to the test ledger. ok=True only "
            "if the selected suites pass. UI/flow layers are skipped (soft) when no headless "
            "browser is installed; integration still runs. Use this to prove user flows and "
            "backend integration flows end-to-end — a passing run_pytest (API) proves only the "
            "in-process API layer."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "layers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["ui", "flows", "integration"]},
                    "description": "Which E2E layers to run (default: all three that have test files).",
                },
                "timeout": {"type": "integer", "minimum": 30, "maximum": 600},
            },
            "additionalProperties": False,
        },
        handler=_run_e2e_handler,
    ))
