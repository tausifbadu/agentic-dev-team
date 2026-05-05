"""Backend Agent — class-based ReAct runtime."""

from __future__ import annotations

import json
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

Workflow you choose:
  1. Read the existing code (`list_dir`, `read_file`, `grep`).
  2. Edit with `write_file` or `apply_patch`.
  3. Validate aggressively after every meaningful change:
       - `run_pytest` for behaviour
       - `run_python` for quick "does this import / instantiate" sanity checks
       - `run_lint` to catch undefined names, unused imports, broken refs
       - `http_check` to actually exercise an endpoint
       - `smoke_uvicorn` as the final import/startup probe
  4. When validation passes, call `publish_contract` so the frontend gets the API shape.
  5. Then call `finish_story(success=true)` with a short summary.

Self-healing loop (this is your job, not PM's):
  • If a validator returns ok=false, READ the error carefully.
  • Use `read_file` / `grep` to look at the exact line that failed.
  • Use `git_diff` to see what you've changed in this run vs the workspace.
  • Make the SMALLEST correct fix and re-run the validator.
  • Repeat. You have ~60 iterations.

When (and only when) to ask others:
  - `ask_pm`: ONLY for product/scope ambiguity ("is this AC really required?",
    "should the endpoint return 200 or 201?"). NEVER for code bugs — debug yourself.
  - `query_agent to_agent="frontend"`: cross-stack questions ("what payload do
    you POST to /api/customers?"). Peer-to-peer, not via PM.
  - `read_past_patterns`: cross-run lessons from previous failures.

Definition of Done (enforced by the runtime):
  - You may NOT call finish_story(success=true) without a recent passing
    `run_pytest` (or `smoke_uvicorn`) AND no file edits since that pass.
  - The runtime will reject premature success and tell you to re-validate.
  - If you genuinely cannot make validation pass, call finish_story(success=false)
    with a clear summary; the supervisor will decide skip / simplify / halt.

Hard constraints:
  - PRESERVE all existing public functions/classes when modifying a file.
  - Use Python 3.9 syntax: NEVER `X | Y` unions; use `Optional[X]` / `Union[X, Y]`.
  - Add new runtime packages to `requirements.txt`.
  - Use FastAPI lifespan, not deprecated event hooks.
  - Mock external boundaries in tests; no real network calls.
  - Stay inside your scratch dir — paths are relative to backend/.

Output style: keep reasoning concise; rely on tools to act. Never paste large
code into chat — write it directly with `write_file` or `apply_patch`."""


class BackendAgent(AgentBase):
    agent_id = "backend"
    role_description = "Senior FastAPI Backend Developer"
    workspace_subdir = "backend"
    model_env_var = "BACKEND_AGENT_MODEL"
    default_model = "gpt-4o-mini"
    allowed_tools = [
        "read_file", "write_file", "list_dir", "grep", "apply_patch", "delete_file",
        "run_pytest", "smoke_uvicorn",
        "run_python", "http_check", "run_lint", "git_diff",
        "ask_pm", "query_agent", "request_review", "send_message", "publish_contract",
        "read_storypack", "read_past_patterns", "read_logs", "read_api_contract",
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

        sibling_summaries = [
            {"id": s.id, "title": s.title, "ownership": s.ownership,
             "acceptance_criteria": s.acceptance_criteria}
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
