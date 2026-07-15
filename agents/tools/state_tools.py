"""State store tools — let agents read and write structured data."""

from __future__ import annotations

import json
from typing import Any

from .registry import Tool, ToolContext, ToolRegistry, ToolResult


def _read_storypack_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    import state_store

    requested = args.get("pack_id")
    pack_id = requested or ctx.storypack_id
    if not pack_id:
        return ToolResult(ok=False, content="No pack_id available")

    pack = state_store.get_storypack(pack_id)
    if not pack:
        # Agents frequently pass the REQUIREMENT id (req_*) or a stale id here.
        # Self-correct instead of erroring: resolve requirement_id -> pack, then
        # fall back to this run's actual storypack (the source of truth).
        resolved = state_store.get_storypack_by_requirement(pack_id)
        if resolved:
            pack = resolved
        elif ctx.storypack_id and pack_id != ctx.storypack_id:
            pack = state_store.get_storypack(ctx.storypack_id)

    if not pack:
        return ToolResult(
            ok=False,
            content=(
                f"Storypack {requested or pack_id} not found. This run's storypack id is "
                f"{ctx.storypack_id or 'unknown'} — call read_storypack with no pack_id to read it."
            ),
        )
    return ToolResult(ok=True, content=json.dumps(pack, indent=2, default=str))


def _read_guidelines_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Return the agent's engineering/design skill guidelines on demand.

    Tier B: agents stuff their (large) skill text into ctx.metadata["guidelines"]
    instead of baking it into the re-sent system prompt, then pull it ONCE via this
    tool. The result enters the transcript once and is later compacted away, rather
    than being re-paid on every ReAct turn against a gateway with no prompt caching.
    """
    text = ctx.metadata.get("guidelines") or ""
    if not text.strip():
        return ToolResult(ok=True, content="(no detailed guidelines configured for this agent)")
    return ToolResult(ok=True, content=text)


def _workspace_overview_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Return the workspace file tree + public export map in one call.

    Tier B: replaces the file-tree + export-map blocks that used to ride in the
    re-sent user prompt. The agent calls this once to orient itself.
    """
    from pathlib import Path
    from agents.reasoning import workspace_file_tree, workspace_export_map

    target = (
        ctx.metadata.get("overview_dir")
        or ctx.metadata.get("backend_dir")
        or ctx.metadata.get("workspace_dir")
    )
    if not target:
        return ToolResult(ok=False, content="No workspace directory available")
    suffixes = set(ctx.metadata.get("overview_suffixes") or [".py"])
    tree = workspace_file_tree(Path(target), suffixes)
    exports = workspace_export_map(Path(target), suffixes)
    body = (
        f"Files (suffixes {sorted(suffixes)}):\n{tree or '(empty workspace)'}\n\n"
        f"Public export map (symbols you MUST preserve when rewriting files):\n"
        f"{exports or '(no exports detected)'}"
    )
    return ToolResult(ok=True, content=body)


def _read_past_patterns_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    import state_store

    limit = int(args.get("limit", 10))
    patterns = state_store.get_failure_patterns(limit=limit)
    if not patterns:
        return ToolResult(ok=True, content="(no past failure patterns)")
    return ToolResult(ok=True, content=json.dumps(patterns, indent=2, default=str))


def _record_failure_pattern_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    import state_store

    storypack_id = ctx.storypack_id or "none"
    story_title = args.get("story_title", "unknown")
    agent_type = args.get("agent_type", ctx.agent_id)
    error_category = args.get("error_category", "")
    root_cause = args.get("root_cause", "")
    resolution = args.get("resolution", "")

    if not all([error_category, root_cause, resolution]):
        return ToolResult(
            ok=False,
            content="error_category, root_cause and resolution are required",
        )

    state_store.save_failure_pattern(
        storypack_id, story_title, agent_type, error_category, root_cause, resolution,
    )
    return ToolResult(ok=True, content=f"Recorded failure pattern: {error_category}")


def _read_logs_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    import state_store

    story_id = args.get("story_id") or ctx.story_id
    agent_type = args.get("agent_type")
    limit = int(args.get("limit", 30))
    logs = state_store.get_agent_logs(story_id=story_id, agent_type=agent_type, limit=limit)
    if not logs:
        return ToolResult(ok=True, content="(no logs)")
    summary = [
        f"[{log.get('created_at')}] {log.get('agent_type')}: {log.get('message')}"
        for log in logs
    ]
    return ToolResult(ok=True, content="\n".join(summary))


def _read_api_contract_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Frontend or test agent: fetch the latest published backend API contract."""
    from pathlib import Path

    workspace_dir = ctx.metadata.get("workspace_dir") or ""
    if not workspace_dir:
        return ToolResult(ok=False, content="No workspace_dir on context")
    contract_path = Path(workspace_dir) / "contracts" / "api_contract.json"
    if not contract_path.exists():
        return ToolResult(ok=True, content="(no API contract published yet)")
    try:
        text = contract_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult(ok=False, content=f"Failed reading contract: {exc}")
    return ToolResult(ok=True, content=text)


def _check_contract_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Static contract-lint: compare the enum VALUES the frontend uses against the
    backend's accepted vocabulary (from api_contract.json's `enums`) and flag drift.

    Catches the class of bug where the frontend sends values the backend rejects
    with a 400 (e.g. category 'general' vs 'General', status 'waiting' vs
    'waiting_on_customer', sort 'updated_at_desc' vs 'created_at'). No browser or
    backend needed — pure static comparison.
    """
    import json
    import re
    from pathlib import Path

    workspace_dir = ctx.metadata.get("workspace_dir") or ""
    contract_path = Path(workspace_dir) / "contracts" / "api_contract.json" if workspace_dir else None
    if not contract_path or not contract_path.exists():
        return ToolResult(ok=True, content="(no API contract published yet — run publish_contract on the backend first)")
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ToolResult(ok=False, content=f"Failed reading/parsing contract: {exc}")

    enums = contract.get("enums") or {}
    enum_values = contract.get("enum_values") or sorted({v for vs in enums.values() for v in vs})
    if not enum_values:
        return ToolResult(ok=True, content="(contract has no enums — nothing to lint. Re-publish the contract to capture them.)")
    B = set(enum_values)
    lower_b = {v.lower(): v for v in enum_values}

    # Locate the frontend source to scan (the agent's scratch copy first).
    base = None
    if ctx.scratch_dir and Path(ctx.scratch_dir).exists():
        base = Path(ctx.scratch_dir)
    elif workspace_dir:
        base = Path(workspace_dir) / "frontend"
    if base is None or not base.exists():
        return ToolResult(ok=False, content="No frontend source directory found to lint.")

    # Only inspect VALUE positions — the strings actually SENT to the API — so we
    # don't flag display labels (JSX text, `label:`) that legitimately differ in case.
    #   value="X" / value={'X'} / value: "X"  → option & control values
    #   status:/priority:/category:/sort:/... : "X"  → params sent in request objects
    #   const UPPER_CASE = ["X", ...]  → flat enum option arrays (CATEGORIES etc.)
    val_re = re.compile(r'value\s*[:=]\s*\{?\s*["\']([^"\']{1,40})["\']')
    key_re = re.compile(
        r'\b(?:status|priority|category|plan|team|sort|assignee|assigned_agent_id|direction|id)'
        r'\s*[:=]\s*\{?\s*["\']([^"\']{1,40})["\']'
    )
    arr_re = re.compile(r'=\s*\[([^\]{}]*)\]')          # flat string arrays (no objects)
    str_re = re.compile(r'["\']([^"\']{1,40})["\']')
    seen: dict[str, list[str]] = {}
    for f in base.rglob("*"):
        if f.suffix not in (".js", ".jsx", ".ts", ".tsx"):
            continue
        if any(p in ("node_modules", "dist", "build") for p in f.parts):
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            toks = val_re.findall(line) + key_re.findall(line)
            for arr in arr_re.findall(line):
                toks += str_re.findall(arr)
            for tok in toks:
                seen.setdefault(tok, []).append(f"{f.relative_to(base)}:{i}")

    case_drift = []     # value whose lowercase matches a backend value but casing differs
    variant_drift = []  # backend value not used exactly, but a near-variant appears as a value
    for tok in seen:
        if tok in B:
            continue
        exact_lower = lower_b.get(tok.lower())
        if exact_lower and exact_lower != tok:
            case_drift.append((tok, exact_lower))
    for b in enum_values:
        if b in seen or (len(b) < 6 and "_" not in b):  # skip short enums (open/bug/asc) — too noisy
            continue
        for tok in seen:
            if tok in B or tok.lower() == b.lower():
                continue
            tl, bl = tok.lower(), b.lower()
            if bl.startswith(tl) or tl.startswith(bl):
                variant_drift.append((tok, b))
                break

    lines = ["=== Backend-accepted enum vocabulary (source of truth) ==="]
    for name, vals in enums.items():
        lines.append(f"  {name}: {', '.join(vals)}")
    findings = []
    for tok, want in sorted(set(case_drift)):
        findings.append(f"  ✗ frontend uses '{tok}' — backend accepts '{want}' (case mismatch). e.g. {seen[tok][0]}")
    for tok, want in sorted(set(variant_drift)):
        findings.append(f"  ✗ frontend uses '{tok}' — backend value is '{want}' (not accepted as-is). e.g. {seen[tok][0]}")

    if findings:
        lines.append("")
        lines.append(f"=== {len(findings)} likely contract mismatch(es) — fix these to the backend values ===")
        lines.extend(findings[:30])
        return ToolResult(ok=False, content="\n".join(lines),
                          metadata={"mismatches": len(findings)})
    lines.append("")
    lines.append("✓ No enum-value drift detected between frontend and the backend contract.")
    return ToolResult(ok=True, content="\n".join(lines), metadata={"mismatches": 0})


def _contract_sample_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Generate a valid, contract-derived sample instance of a request model.

    Reads the published api_contract.json and builds a payload whose enum fields use
    ONLY the backend's accepted vocabulary (so the sample never 400s). With no
    `model`, lists the available request-shaped models. Deterministic (fixed seed)."""
    from pathlib import Path
    from agents.test_seed import sample_model, _models, _input_models

    workspace_dir = ctx.metadata.get("workspace_dir") or ""
    contract_path = Path(workspace_dir) / "contracts" / "api_contract.json" if workspace_dir else None
    if not contract_path or not contract_path.exists():
        return ToolResult(ok=True, content="(no API contract published yet — run publish_contract on the backend first)")
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ToolResult(ok=False, content=f"Failed reading/parsing contract: {exc}")

    models = _models(contract)
    if not models:
        return ToolResult(ok=True, content="(contract has no models to sample)")

    model = args.get("model")
    if not model:
        suggested = _input_models(models)
        return ToolResult(
            ok=True,
            content=("Pass model= one of these to get a sample payload.\n"
                     f"Request-shaped models: {', '.join(suggested)}\n"
                     f"All models: {', '.join(models)}"),
        )
    if model not in models:
        return ToolResult(
            ok=False,
            content=f"Model '{model}' not in contract. Available: {', '.join(models)}",
        )
    count = int(args.get("count", 1) or 1)
    samples = sample_model(model, contract, count=count)
    return ToolResult(
        ok=True,
        content=json.dumps(samples if count > 1 else (samples[0] if samples else {}), indent=2),
        metadata={"model": model, "count": len(samples)},
    )


def _read_test_directives_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Return the user-authored test directives (business/process/data flows, edge
    cases) with their current status — the flows the user most wants covered. These
    are HIGHEST priority: cover them before auto-detected gaps."""
    from pathlib import Path
    from agents.test_directives import load_directives

    workspace_dir = ctx.metadata.get("workspace_dir") or ""
    if not workspace_dir:
        return ToolResult(ok=False, content="No workspace_dir on context")
    directives = load_directives(Path(workspace_dir) / "tests")
    if not directives:
        return ToolResult(ok=True, content="(no user test directives authored for this POC)")
    lines = ["User test directives (cover these FIRST; annotate tests with "
             "`# directive: <id>`):"]
    for d in directives:
        steps = "; ".join(d.get("steps", []))
        lines.append(
            f"- [{d['priority']}] {d['id']} ({d['kind']}) — {d['title']} "
            f"[status: {d['status']}]\n    steps: {steps}\n    expect: {d['expected_outcome']}"
        )
    return ToolResult(ok=True, content="\n".join(lines),
                      metadata={"count": len(directives)})


def _read_test_ledger_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Return the prior test coverage + gaps for this POC so the agent extends
    cumulatively: cover the GAPS first (things with no test yet) rather than
    re-deriving coverage from scratch. Reads tests/test_manifest.json."""
    from pathlib import Path
    from agents.reasoning import load_json

    workspace_dir = ctx.metadata.get("workspace_dir") or ""
    if not workspace_dir:
        return ToolResult(ok=False, content="No workspace_dir on context")
    manifest = load_json(Path(workspace_dir) / "tests" / "test_manifest.json", {})
    if not manifest:
        return ToolResult(ok=True, content="(no prior test runs — this is the first run for this POC)")

    totals = manifest.get("totals", {})
    gaps = manifest.get("gaps", [])
    lines = ["Prior test coverage (extend it — prioritize the GAPS below):"]
    for layer, t in totals.items():
        lines.append(f"  {layer}: {t.get('covered',0)}/{t.get('total',0)} covered, "
                     f"{t.get('passed',0)} passed, {t.get('failed',0)} failed")
    if gaps:
        lines.append(f"\nGAPS to close ({len(gaps)}) — write tests for these first:")
        for g in gaps[:40]:
            pri = f" [{g['priority']}]" if g.get("priority") else ""
            lines.append(f"  - ({g.get('kind','')}){pri} {g.get('target','')} — {g.get('reason','')}")
    else:
        lines.append("\nNo gaps recorded — verify the suite still passes and fill any new criteria.")
    last = manifest.get("last_run", {})
    if last:
        lines.append(f"\nLast run: {last.get('status','')} ({last.get('cases',0)} cases, "
                     f"trigger {last.get('trigger','')}).")
    return ToolResult(ok=True, content="\n".join(lines),
                      metadata={"gaps": len(gaps)})


def _read_test_runs_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Return recent test runs and the tests that FAILED in the latest run, so the
    agent re-runs/fixes regressions before adding new tests."""
    import state_store
    from agents.test_ledger import project_id_from_workspace
    from pathlib import Path

    workspace_dir = ctx.metadata.get("workspace_dir") or ""
    if not workspace_dir:
        return ToolResult(ok=False, content="No workspace_dir on context")
    project_id = project_id_from_workspace(Path(workspace_dir))
    limit = int(args.get("limit", 5))
    runs = state_store.list_test_runs(project_id, limit=limit)
    if not runs:
        return ToolResult(ok=True, content="(no prior test runs recorded)")

    lines = [f"Recent test runs for '{project_id}':"]
    for r in runs:
        lines.append(f"  #{r['id']} {r.get('status','')} ({r.get('trigger','')}) — {r.get('summary','')[:80]}")
    # Failing cases from the most recent run → regressions to fix first.
    latest = state_store.get_test_run(runs[0]["id"]) or {}
    failing = [c for c in (latest.get("cases") or []) if c.get("status") == "failed"]
    if failing:
        lines.append(f"\nFAILING in the latest run ({len(failing)}) — fix these regressions first:")
        for c in failing[:25]:
            lines.append(f"  ✗ [{c.get('layer','')}] {c.get('test_name','')} "
                         f"({c.get('test_file','')}) — {(c.get('message','') or '')[:120]}")
    else:
        lines.append("\nNo failing tests in the latest run.")
    return ToolResult(ok=True, content="\n".join(lines),
                      metadata={"failing": len(failing)})


def register_state_tools(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="read_guidelines",
        description=(
            "Read the detailed engineering/design guidelines for your role (FastAPI, "
            "React/UI, testing, etc.). Call this ONCE near the start before writing code."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_read_guidelines_handler,
    ))
    registry.register(Tool(
        name="workspace_overview",
        description=(
            "Get the current workspace file tree and the public export map (functions/"
            "classes you must preserve) in one call. Use this once to orient yourself."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_workspace_overview_handler,
    ))
    registry.register(Tool(
        name="read_storypack",
        description="Read the full storypack (all stories) for the current run.",
        parameters_schema={
            "type": "object",
            "properties": {"pack_id": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=_read_storypack_handler,
    ))
    registry.register(Tool(
        name="read_past_patterns",
        description=(
            "Read failure patterns recorded from previous runs so you can avoid known mistakes."
        ),
        parameters_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "additionalProperties": False,
        },
        handler=_read_past_patterns_handler,
    ))
    registry.register(Tool(
        name="record_failure_pattern",
        description=(
            "Record a learned failure pattern (cross-run memory). PM agent should call this "
            "after a heal succeeds so future runs benefit."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "story_title": {"type": "string"},
                "agent_type": {"type": "string"},
                "error_category": {"type": "string"},
                "root_cause": {"type": "string"},
                "resolution": {"type": "string"},
            },
            "required": ["error_category", "root_cause", "resolution"],
            "additionalProperties": False,
        },
        handler=_record_failure_pattern_handler,
    ))
    registry.register(Tool(
        name="read_logs",
        description="Read recent agent log entries (any agent or filtered by agent_type/story_id).",
        parameters_schema={
            "type": "object",
            "properties": {
                "story_id": {"type": "string"},
                "agent_type": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
        handler=_read_logs_handler,
    ))
    registry.register(Tool(
        name="read_api_contract",
        description=(
            "Read the latest backend API contract (routes + models + accepted enum "
            "vocabularies) published in workspace/contracts/api_contract.json. Read this "
            "before building forms/filters so you SEND the exact enum values the backend "
            "accepts (statuses, categories, priorities, sort keys)."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_read_api_contract_handler,
    ))
    registry.register(Tool(
        name="check_contract",
        description=(
            "Static contract-lint: compares the enum VALUES your frontend uses against the "
            "backend's accepted vocabulary (from the published api_contract.json) and flags "
            "drift — e.g. category 'general' vs 'General', status 'waiting' vs "
            "'waiting_on_customer', sort 'updated_at_desc' vs 'created_at'. This is the class "
            "of bug that causes HTTP 400s on submit/filter. ok=False if mismatches are found. "
            "Run it after editing forms/filters/boards and fix every flagged value to the "
            "backend's. No browser or backend needed."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_check_contract_handler,
    ))
    registry.register(Tool(
        name="contract_sample",
        description=(
            "Generate a VALID, contract-derived sample payload for a backend request "
            "model — enum fields use only the backend's accepted vocabulary, so the "
            "sample never 400s. Use it to build request bodies for API/flow/integration "
            "tests and seed data instead of inventing values. Call with no args to list "
            "the request-shaped models; pass model=<Name> (and optional count) for samples."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Contract model name to sample."},
                "count": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "How many instances (default 1)."},
            },
            "additionalProperties": False,
        },
        handler=_contract_sample_handler,
    ))
    registry.register(Tool(
        name="read_test_directives",
        description=(
            "Read the user-authored test directives (business/process/data flows, edge "
            "cases the user explicitly wants covered) with their current status. These are "
            "the HIGHEST-priority things to test — cover them before auto-detected gaps. "
            "Annotate the test(s) you write for a directive with `# directive: <id>` so it "
            "is credited and its status updates."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_read_test_directives_handler,
    ))
    registry.register(Tool(
        name="read_test_ledger",
        description=(
            "Read the prior test coverage + GAPS for this POC (from the last run). Call "
            "this near the start so you extend coverage cumulatively — write tests for the "
            "listed gaps first instead of re-deriving what's already covered."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_read_test_ledger_handler,
    ))
    registry.register(Tool(
        name="read_test_runs",
        description=(
            "Read recent test runs and the tests that FAILED in the latest run. Use it to "
            "fix regressions (previously-passing tests now failing) before adding new tests."
        ),
        parameters_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            "additionalProperties": False,
        },
        handler=_read_test_runs_handler,
    ))
