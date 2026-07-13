"""Frontend Agent — class-based ReAct runtime."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from agents.agentic.base import AgentBase, RunResult
from agents.frontend_agent import _ensure_project_scaffold
from agents.reasoning import (
    create_scratch_copy,
    load_json,
    load_skill_guidelines,
    promote_scratch_copy,
    save_json,
    update_scope,
    workspace_export_map,
    workspace_file_tree,
)
from schemas import Story


_SKILLS_DIR = Path(__file__).parent.parent.parent / ".cursor" / "skills" / "agentic-dev-team"
SKILL_UI_DESIGN_PATH = _SKILLS_DIR / "SKILL_UI_DESIGN.md"   # authored design-system + quality standard (primary)
SKILL_UI_PATH = _SKILLS_DIR / "SKILL_UI.md"                 # existing UI reference (supplementary)
SKILL_REACT_PATH = _SKILLS_DIR / "SKILL_REACT.md"

# Larger cap for the on-demand (lean) path: read_guidelines fetches these ONCE and
# they compact away, so we can afford the full design guidance — unlike the inline
# path which re-sends every turn and stays capped at the default.
_LEAN_SKILL_CHARS = int(os.getenv("AGENTIC_LEAN_SKILL_CHARS", "40000"))

# Inline (non-lean) path — capped (re-sent every turn).
DESIGN_GUIDELINES = load_skill_guidelines(SKILL_UI_DESIGN_PATH)
REACT_GUIDELINES = load_skill_guidelines(SKILL_REACT_PATH)
# On-demand (lean) path — full, un-truncated.
DESIGN_GUIDELINES_FULL = load_skill_guidelines(SKILL_UI_DESIGN_PATH, max_chars=_LEAN_SKILL_CHARS)
UI_GUIDELINES_FULL = load_skill_guidelines(SKILL_UI_PATH, max_chars=_LEAN_SKILL_CHARS)
REACT_GUIDELINES_FULL = load_skill_guidelines(SKILL_REACT_PATH, max_chars=_LEAN_SKILL_CHARS)


_SYSTEM_BASE = """You are the Frontend Agent — a Senior React + Vite + Tailwind CSS engineer
who ships clean, professional, information-dense business UIs (think admin console /
internal tool), following the Design System skill — NOT trendy consumer landing pages.

Your assignment: implement ONE frontend story end-to-end inside an isolated scratch
copy of the frontend workspace. You have full autonomy and ~60 iterations.

You self-heal. You do NOT phone the PM for code-level bugs.

Workflow you choose:
  1. Read the existing source (`list_dir`, `read_file`, `grep`).
  2. If you depend on backend data, call `read_api_contract` first — it lists the
     ACCEPTED ENUM VOCABULARY (statuses, categories, priorities, sort keys). Your
     dropdown option values, filter values, sort values, and board column keys MUST
     be exactly these strings (the backend 400s on anything else, e.g. sending
     'general' when it accepts 'General'). Never invent enum wording from the story
     text — the contract is the source of truth.
  3. Edit files via `write_file` or `apply_patch`.
  4. Validate with `run_npm_build` (auto-runs npm install + vite build).
     Use `run_lint` for fast feedback before the heavier build.
     If your story touches any form, filter, sort control, or status/board that
     sends enum values to the API, run `check_contract` — it statically flags any
     value that doesn't match the backend's accepted vocabulary (the #1 cause of
     400s). Fix every value it flags before finishing.
  5. After a green build, call `check_ui` to confirm the UI ACTUALLY RENDERS.
     A green build does NOT prove your UI works — only that it compiled. Pass
     `expect_text` and `expect_selectors` drawn straight from THIS story's
     acceptance criteria (e.g. expect_text=["Add task"],
     expect_selectors=["table", "button"]). check_ui also reports console/
     page errors — a blank page with errors means your UI is broken even though
     the build passed. Fix and re-run until check_ui passes.
     • If what you must verify is BEHIND AN INTERACTION (e.g. dropdown options
       inside a modal that only opens on click), pass `interactions` so check_ui
       reveals it first, e.g. interactions=[{"click": "text=New Ticket"}] then
       expect_text=["Feature Request"]. A static load can't see modal content.
     • If the elements depend on LIVE BACKEND DATA (tables, the board, populated
       lists), pass `boot_backend=true` so check_ui boots the backend and proxies
       /api — otherwise those screens render empty/error states and can't be verified.
  6. Only when build is green AND check_ui confirms the required elements render,
     call `finish_story(success=true)`.

Your definition of done is the story's acceptance criteria — NOT a green build.
Before finishing, make sure every UI element/state the criteria name (buttons,
inputs, icons, loading/empty/error states, specific classes) is actually present
in the rendered DOM, verified via check_ui.

Self-healing loop (this is your job, not PM's):
  • Build error or lint error → read the error carefully.
  • Use `read_file` / `grep` to look at the exact symbol/import.
  • Use `git_diff` to review your own changes before finishing.
  • Make the SMALLEST correct fix and re-run validation.
  • Repeat. You have ~60 iterations.

When (and only when) to ask others:
  - `ask_pm`: ONLY for product/UX ambiguity ("should this be a modal or a page?").
    NEVER for build/syntax bugs — fix those yourself.
  - `query_agent to_agent="backend"`: cross-stack questions about endpoint shapes
    the contract didn't capture. Peer-to-peer, not via PM.
  - `read_past_patterns`: cross-run lessons.

Definition of Done (enforced by the runtime):
  - You may NOT call finish_story(success=true) without a recent passing
    `run_npm_build` AND no file edits since that pass.
  - The runtime will reject premature success and tell you to re-validate.
  - If you genuinely cannot make the build pass, call finish_story(success=false)
    with a clear summary; the supervisor decides skip / simplify / halt.

Hard constraints:
  - The scaffold provides `index.html`, `vite.config.js`, `src/main.jsx`,
    `src/App.jsx`, `tailwind.config.js`, `postcss.config.js`. Only modify them if
    necessary. Do NOT remove `<div id="root"></div>` or the ReactDOM.createRoot call.
  - PRESERVE all existing exports when modifying a file.
  - COMPONENT OWNERSHIP (critical — multiple stories share this workspace):
    `src/App.jsx` is a SHARED shell owned by no single story. Put YOUR screen's
    implementation in its OWN new file (e.g. `src/screens/<Name>.jsx`), and touch
    App.jsx only to import your component and wire it to its route. When you edit
    App.jsx you MUST read it first and make the SMALLEST additive change — never
    rewrite it from scratch, never delete or replace other screens' routes, imports,
    components, or navigation, and never collapse features back into a placeholder.
    If a route currently shows a placeholder for YOUR story, replace only that one
    route's content with your component and leave every other route intact. Assume
    other screens already exist (or will) in App.jsx and must survive your edit.
  - Define your color tokens per the Design System skill (extend the scaffold's
    tailwind config / CSS variables); don't assume a fixed brand palette exists.
  - VISUAL REGISTER (unless the requirement explicitly asks for a consumer/branded/dark
    look): professional, business-grade, per the Design System skill — LIGHT theme by
    default, a neutral palette + ONE restrained accent, SOLID (non-glass) surfaces,
    information-dense and legibility-first, with loading/empty/error states and inline
    SVG icons (never emoji). Do NOT use glassmorphism, backdrop-blur, glow shadows,
    gradient text, or decorative background orbs/gradients — those read as trendy, not
    professional.

Stay inside your scratch dir; paths are relative to frontend/.

EFFICIENCY — batch your tool calls. Each turn is an expensive round-trip, so do
as much as is safe per turn: request ALL the files/inspections you need in ONE
turn (e.g. several `read_file` / `grep` / `list_dir` at once) rather than one per
turn. Only split across turns when a step DEPENDS on the previous result (e.g.
write_file then run_npm_build must be separate). Fewer, fuller turns = faster and
cheaper.

READ SURGICALLY — don't pull whole files when you need a part. Orient from
`workspace_overview` (file tree + export map), use `grep` to locate code, and use
`read_symbol(path, name)` to pull a single component/function. Reserve a full
`read_file` for files you are about to rewrite."""


class FrontendAgent(AgentBase):
    agent_id = "frontend"
    role_description = "Senior React/Tailwind Frontend Developer"
    workspace_subdir = "frontend"
    model_env_var = "FRONTEND_AGENT_MODEL"
    default_model = "codex/gpt-5.5"
    allowed_tools = [
        "read_file", "read_symbol", "write_file", "list_dir", "grep", "apply_patch", "delete_file",
        "run_npm_install", "run_npm_build", "check_ui",
        "run_lint", "git_diff",
        "ask_pm", "query_agent", "send_message",
        "read_past_patterns", "read_api_contract", "check_contract",
        # Tier B on-demand context (used when AGENTIC_LEAN_CONTEXT=1):
        "read_guidelines", "workspace_overview", "read_storypack",
        "finish_story",
    ]
    iteration_cap = 60

    def __init__(self, ctx, on_progress=None):
        super().__init__(ctx, on_progress)
        self.frontend_dir = ctx.workspace_dir / "frontend"

    def run(self, story: Story) -> RunResult:
        self._current_story = story
        self._emit("info", f"Frontend agent starting story: {story.title}")

        # Ensure the live workspace has the scaffold before we make a scratch copy.
        _ensure_project_scaffold(self.frontend_dir)
        scratch = create_scratch_copy(self.frontend_dir, "frontend_attempt")
        registry = self._build_registry(story, scratch)

        lean = os.getenv("AGENTIC_LEAN_CONTEXT", "0").strip().lower() not in ("0", "false", "no", "off")
        fe_suffixes = {".js", ".jsx", ".ts", ".tsx", ".css"}

        if lean:
            # Tier B: keep the re-sent head tiny. The (large) UI + React skills, file
            # tree, export map, sibling stories and API contract are pulled ON DEMAND
            # via tools (read_guidelines / workspace_overview / read_api_contract /
            # read_storypack), each fetched once and compacted away, instead of riding
            # every ReAct turn against a gateway with no prompt caching. The hard UI
            # constraints stay inline in _SYSTEM_BASE, so must-follow rules aren't lost.
            guidelines = ""
            if DESIGN_GUIDELINES_FULL:
                guidelines += (
                    "DESIGN SYSTEM & QUALITY STANDARD — this is your primary design "
                    f"authority, FOLLOW IT:\n{DESIGN_GUIDELINES_FULL}\n\n"
                )
            if UI_GUIDELINES_FULL:
                guidelines += f"Additional UI/UX reference:\n{UI_GUIDELINES_FULL}\n\n"
            if REACT_GUIDELINES_FULL:
                guidelines += f"React.js Engineering Guidelines:\n{REACT_GUIDELINES_FULL}"
            registry.context.metadata["guidelines"] = guidelines
            registry.context.metadata["overview_dir"] = str(scratch)
            registry.context.metadata["overview_suffixes"] = list(fe_suffixes)
            system_prompt = _SYSTEM_BASE + (
                "\n\nReference context is available via tools — call each ONCE near the start:\n"
                "  • read_guidelines() — detailed UI/UX + React engineering rules\n"
                "  • workspace_overview() — file tree + export map (preserve those symbols)\n"
                "  • read_api_contract() — backend API paths / methods / shapes\n"
                "  • read_storypack() — sibling stories for naming / dependencies"
            )
            user_prompt = f"""Story to implement:
{json.dumps(story.model_dump(), indent=2)}

Original requirement (truncated):
{self.ctx.requirement_text[:2000]}

Pull what you need with read_guidelines(), workspace_overview(), read_api_contract() and read_storypack().
Implement the story. When `run_npm_build` succeeds, call `finish_story`."""
        else:
            # Siblings are context only (this agent implements ONE story); id/title/
            # ownership suffices. Full acceptance criteria dropped — they re-rode every
            # ReAct turn at full (uncached) cost.
            sibling_summaries = [
                {"id": s.id, "title": s.title, "ownership": s.ownership}
                for s in self.ctx.all_stories if s.id != story.id
            ]

            file_tree = workspace_file_tree(self.frontend_dir, fe_suffixes)
            export_map = workspace_export_map(self.frontend_dir, fe_suffixes)

            contract_section = ""
            contract_path = self.ctx.workspace_dir / "contracts" / "api_contract.json"
            if contract_path.exists():
                contract = load_json(contract_path, {})
                contract_section = (
                    "\n\nLatest backend API contract (use these exact paths/methods):\n"
                    + json.dumps(contract, indent=2)[:4000]
                )

            system_prompt = _SYSTEM_BASE
            if DESIGN_GUIDELINES:
                system_prompt += f"\n\nDesign System & Quality Standard (FOLLOW THIS):\n{DESIGN_GUIDELINES}"
            if REACT_GUIDELINES:
                system_prompt += f"\n\nReact.js Engineering Guidelines:\n{REACT_GUIDELINES}"

            user_prompt = f"""Story to implement:
{json.dumps(story.model_dump(), indent=2)}

Original requirement (truncated):
{self.ctx.requirement_text[:2000]}

Sibling stories:
{json.dumps(sibling_summaries, indent=2)}

Frontend file listing (only import from these):
{file_tree}

Frontend export map (preserve ALL of these when rewriting files):
{export_map or "(none)"}{contract_section}

Implement the story. When `run_npm_build` succeeds, call `finish_story`."""

        outcome = self._execute_react(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            registry=registry,
            model=self._story_model(story),
        )

        ckpt = None
        if outcome.success:
            manifest = promote_scratch_copy(
                scratch, self.frontend_dir, label=(story.id if story else ""),
            )
            self._log_promotion(manifest)
            self._record_scope(story, outcome.summary)
            self._emit("info", "Frontend story complete; scratch promoted.", outcome.summary)
        else:
            self._emit("error", f"Frontend story failed: {outcome.summary}", outcome.error)
            ckpt = self._preserve_checkpoint(
                scratch=scratch, target_dir=self.frontend_dir, registry=registry, story=story,
            )

        meta = dict(outcome.metadata or {})
        if ckpt:
            meta["recoverable_checkpoint"] = ckpt
        return RunResult(
            success=outcome.success,
            summary=outcome.summary or outcome.final_text,
            iterations=outcome.iterations,
            tool_calls=registry.context.tool_call_count,
            metadata=meta,
        )

    # ---------- Helpers ----------

    def _record_scope(self, story: Story, summary: str) -> None:
        scope_file = self.ctx.workspace_dir / "scope.json"
        scope = update_scope(scope_file, {
            "id": story.id,
            "title": story.title,
            "owner": "frontend",
            "summary": summary[:300],
            "applied_at": datetime.utcnow().isoformat(),
        })
        save_json(scope_file, scope)
