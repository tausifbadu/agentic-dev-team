"""Backend Agent — class-based ReAct runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from agents.agentic.base import AgentBase, RunResult
from agents.reasoning import (
    create_scratch_copy,
    load_skill_guidelines,
    promote_scratch_copy,
    save_json,
    update_scope,
    workspace_export_map,
    workspace_file_tree,
)
from datetime import datetime
from schemas import Story


SKILL_FASTAPI_PATH = Path(__file__).parent.parent.parent / ".cursor" / "skills" / "agentic-dev-team" / "SKILL_FASTAPI.md"
FASTAPI_GUIDELINES = load_skill_guidelines(SKILL_FASTAPI_PATH)


_SYSTEM_BASE = """You are the Backend Agent — a Senior Python / FastAPI developer.

Your assignment: implement ONE backend story end-to-end inside an isolated scratch
copy of the backend workspace. You have full autonomy and ~60 iterations to converge.

You self-heal. You do NOT phone the PM for code-level bugs.

You do NOT have `run_pytest`, `run_lint`, or `smoke_uvicorn`. Regression tests are owned
by the Test agent in a later phase — avoid burning iterations on full test suites here.

Workflow you choose:
  1. Read the existing code (`list_dir`, `read_file`, `grep`).
  2. Edit with `write_file` or `apply_patch`.
  3. Light validation while coding (cheap, repeat as needed):
       - `run_python` for quick import/instantiate sanity checks
       - `git_diff` to review your changes vs the live workspace
  4. Before finishing: call `http_check` when your routes are ready — hit the path and
     method that prove this story (e.g. GET /api/health, POST /api/customers). Run it
     once per milestone, NOT after every tiny edit. If you call `http_check` again with
     no edits after a passing check, the tool skips the duplicate run—use `finish_story`
     or edit first.
  5. Call `publish_contract` so the frontend gets the API shape.
  6. Call `finish_story(success=true)` only after a passing `http_check` with no further edits.
     (`http_check` ok=True only on HTTP 2xx, not redirects. Env `BACKEND_DOD_HTTP_CHECK=0` disables
     this requirement for rare cases — default is on.)

Self-healing loop (this is your job, not PM's):
  • If `http_check` returns ok=false, READ status and body; fix the smallest issue.
  • Use `read_file` / `grep` on the failing route or import.
  • Re-run `http_check` after fixing — not on every intermediate typo.

When (and only when) to ask others:
  - `ask_pm`: ONLY for product/scope ambiguity ("is this AC really required?",
    "should the endpoint return 200 or 201?"). NEVER for code bugs — debug yourself.
  - `query_agent to_agent="frontend"`: cross-stack questions ("what payload do
    you POST to /api/customers?"). Peer-to-peer, not via PM.
  - `read_past_patterns`: cross-run lessons from previous failures.

Definition of Done (enforced by the runtime):
  - You may NOT call finish_story(success=true) without a recent passing `http_check`
    AND no file edits since that check.
  - If you genuinely cannot make `http_check` pass, call finish_story(success=false)
    with a clear summary; the supervisor will decide skip / simplify / halt.

Hard constraints:
  - PRESERVE all existing public functions/classes when modifying a file.
  - Use Python 3.9 syntax: NEVER `X | Y` unions; use `Optional[X]` / `Union[X, Y]`.
  - Add new runtime packages to `requirements.txt`.
  - Use FastAPI lifespan, not deprecated event hooks.
  - Write code so tests can mock external services later (geocoding, HTTP clients).
  - Stay inside your scratch dir — paths are relative to backend/.

Output style: keep reasoning concise; rely on tools to act. Never paste large
code into chat — write it directly with `write_file` or `apply_patch`.

EFFICIENCY — batch your tool calls. Each turn is an expensive round-trip, so do
as much as is safe per turn: request ALL the files/inspections you need in ONE
turn (e.g. several `read_file` / `grep` / `list_dir` at once) rather than one per
turn. Only split across turns when a step DEPENDS on the previous result (e.g.
write_file then http_check must be separate). Fewer, fuller turns = faster and
cheaper.

READ SURGICALLY — don't pull whole files when you need a part. Orient from
`workspace_overview` (file tree + export map), use `grep` to locate code, and use
`read_symbol(path, name)` to pull a single function/class. Reserve a full
`read_file` for files you are about to rewrite."""


class BackendAgent(AgentBase):
    agent_id = "backend"
    role_description = "Senior FastAPI Backend Developer"
    workspace_subdir = "backend"
    model_env_var = "BACKEND_AGENT_MODEL"
    default_model = "codex/gpt-5.5"
    allowed_tools = [
        "read_file", "read_symbol", "write_file", "list_dir", "grep", "apply_patch", "delete_file",
        "run_python", "http_check", "git_diff",
        "ask_pm", "query_agent", "send_message", "publish_contract",
        "read_past_patterns", "read_api_contract",
        # Tier B on-demand context (used when AGENTIC_LEAN_CONTEXT=1):
        "read_guidelines", "workspace_overview", "read_storypack",
        "finish_story",
    ]
    iteration_cap = 60

    def __init__(self, ctx, on_progress=None):
        super().__init__(ctx, on_progress)
        self.backend_dir = ctx.workspace_dir / "backend"

    def run(self, story: Story) -> RunResult:
        self._current_story = story
        self._emit("info", f"Backend agent starting story: {story.title}")

        scratch = create_scratch_copy(self.backend_dir, "backend_attempt")
        registry = self._build_registry(story, scratch)
        registry.context.metadata["backend_dir"] = str(scratch)

        lean = os.getenv("AGENTIC_LEAN_CONTEXT", "0").strip().lower() not in ("0", "false", "no", "off")

        if lean:
            # Tier B: keep the re-sent prompt head tiny. The full FastAPI skill, file
            # tree, export map and sibling stories are pulled ON DEMAND via tools
            # (read_guidelines / workspace_overview / read_storypack) — each enters the
            # transcript once and is later compacted away, instead of riding every
            # ReAct turn against a gateway with no prompt caching. The critical hard
            # constraints already live inline in _SYSTEM_BASE, so removing the appended
            # skill does not strip the must-follow rules.
            registry.context.metadata["guidelines"] = (
                f"FastAPI Engineering Guidelines:\n{FASTAPI_GUIDELINES}" if FASTAPI_GUIDELINES else ""
            )
            system_prompt = _SYSTEM_BASE + (
                "\n\nReference context is available via tools — call each ONCE near the start:\n"
                "  • read_guidelines() — detailed FastAPI engineering rules\n"
                "  • workspace_overview() — file tree + export map (preserve those symbols)\n"
                "  • read_storypack() — sibling stories for naming / dependencies / contracts"
            )
            user_prompt = f"""Story to implement:
{json.dumps(story.model_dump(), indent=2)}

Original requirement (truncated):
{self.ctx.requirement_text[:2000]}

Pull what you need with read_guidelines(), workspace_overview() and read_storypack().
Implement the story. When validated, call `publish_contract` and then `finish_story`."""
        else:
            # Siblings are context only (this agent implements ONE story), so id/title/
            # ownership is enough for naming + dependency awareness. Full acceptance
            # criteria are dropped — they re-rode every ReAct turn at full (uncached) cost.
            sibling_summaries = [
                {"id": s.id, "title": s.title, "ownership": s.ownership}
                for s in self.ctx.all_stories if s.id != story.id
            ]

            file_tree = workspace_file_tree(self.backend_dir, {".py"})
            export_map = workspace_export_map(self.backend_dir, {".py"})

            system_prompt = _SYSTEM_BASE
            if FASTAPI_GUIDELINES:
                system_prompt += f"\n\nFastAPI Engineering Guidelines:\n{FASTAPI_GUIDELINES}"

            user_prompt = f"""Story to implement:
{json.dumps(story.model_dump(), indent=2)}

Original requirement (truncated):
{self.ctx.requirement_text[:2000]}

Sibling stories (context for naming, dependencies, contracts):
{json.dumps(sibling_summaries, indent=2)}

Backend file listing (every existing file you may import from):
{file_tree}

Backend export map (public symbols you MUST preserve when rewriting files):
{export_map or "(no exports detected — workspace is empty)"}

Implement the story. When validated, call `publish_contract` and then `finish_story`."""

        outcome = self._execute_react(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            registry=registry,
            model=self._story_model(story),
        )

        if outcome.success:
            promote_scratch_copy(scratch, self.backend_dir)
            self._record_scope(story, outcome.summary)
            self._emit("info", "Backend story complete; scratch promoted to workspace.",
                       outcome.summary)
        else:
            self._emit("error", f"Backend story failed: {outcome.summary}", outcome.error)

        return RunResult(
            success=outcome.success,
            summary=outcome.summary or outcome.final_text,
            iterations=outcome.iterations,
            tool_calls=registry.context.tool_call_count,
            metadata=outcome.metadata,
        )

    def handle_message(self, msg) -> Optional[dict]:
        # Custom answer for API-shape queries from the frontend agent.
        if msg.event_type in ("query", "question") and msg.from_agent == "frontend":
            from agents.reasoning import extract_api_contract
            contract = extract_api_contract(self.backend_dir)
            question = (msg.payload or {}).get("question", "")
            return {
                "answer": (
                    f"Current backend contract has {len(contract.get('routes', []))} routes "
                    f"and {len(contract.get('models', []))} models. "
                    f"Question: {question}\n\n"
                    f"Contract excerpt:\n{json.dumps(contract, indent=2)[:1500]}"
                ),
            }
        return super().handle_message(msg)

    # ---------- Helpers ----------

    def _record_scope(self, story: Story, summary: str) -> None:
        scope_file = self.ctx.workspace_dir / "scope.json"
        scope = update_scope(scope_file, {
            "id": story.id,
            "title": story.title,
            "owner": "backend",
            "summary": summary[:300],
            "applied_at": datetime.utcnow().isoformat(),
        })
        save_json(scope_file, scope)
