"""Supervisor — light-touch coordinator for the agentic runtime.

The Supervisor replaces the fixed `_run_pipeline()` script. Its job is:

  1. Bootstrap the bus, register all agents, and seed the run.
  2. Hand off control to the agents — each one runs its own ReAct loop and
     self-heals via tools (read_file, run_pytest, smoke_uvicorn, etc.).
  3. Enforce safety: wall-clock timeout, total tool-call budget, halt requests.
  4. Coordinate phases that genuinely depend on order (backend before contract
     publish, contract before frontend, code before tests). Inside each phase
     agents act with full autonomy via tools and the bus.
  5. When an agent self-reports failure (via `finish_story(success=false)` or a
     runtime error), ask PM ONCE for a product-level rescope decision —
     simplify, skip, or halt. PM is **not** invoked to debug code; that role
     belongs to the dev agent itself.

This file is the thinnest possible orchestration shim — the heavy lifting
(implementation, debugging, validation) lives in agent ReAct loops + tools.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from agents.agentic import (
    AgentBase,
    BackendAgent,
    Budget,
    FrontendAgent,
    PMAgent,
    RunContext,
    RunResult,
    TestAgent,
)
from agents.bus import MessageBus
from agents.llm_client import reset_usage, usage_delta, usage_snapshot
from agents.reasoning import extract_api_contract, save_json
from schemas import Story
import state_store


# Markers of a transient/infrastructure failure (gateway throttling, network,
# empty responses) — as opposed to a genuine "the agent couldn't do the story".
# On these we must NOT ask PM to rescope (which permanently dilutes/halts a good
# story) and must NOT record a failure-pattern (which poisons future PM planning).
_TRANSIENT_FAILURE_MARKERS = (
    "429", "rate limit", "rate_limit", "too many requests", "quota",
    "timed out", "timeout", "apitimeout", "connection error", "apiconnection",
    "no choices", "temporarily unavailable", "502", "503", "bad gateway",
    "service unavailable",
)


def _is_transient_failure(*texts: str) -> bool:
    blob = " ".join(t for t in texts if t).lower()
    return any(m in blob for m in _TRANSIENT_FAILURE_MARKERS)


@dataclass
class SupervisorConfig:
    # Kept for backward compat with callers — no longer used by the runtime.
    # Dev agents now self-heal inside their own ReAct loop; the supervisor only
    # asks PM once per failure for a rescope decision (skip/simplify/halt).
    max_pm_heal_attempts: int = 0
    # Default budgets read from the environment so they can actually be raised for
    # a run. (The Supervisor takes min(config, env_budget); if these defaults stayed
    # hardcoded at 1800/600, the min() would clamp any larger env value back down.)
    max_wall_seconds: float = field(
        default_factory=lambda: float(os.getenv("AGENTIC_MAX_WALL_SECONDS", "1800"))
    )
    max_total_tool_calls: int = field(
        default_factory=lambda: int(os.getenv("AGENTIC_MAX_TOOL_CALLS", "600"))
    )
    run_tests: bool = field(
        default_factory=lambda: os.getenv("AGENTIC_RUN_TESTS", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )
    run_smoke: bool = field(
        default_factory=lambda: os.getenv("AGENTIC_RUN_SMOKE", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )
    # Acceptance-criteria verification: after an agent passes its DoD gate, an
    # independent reviewer checks the artifact against the story's acceptance
    # criteria. Unmet criteria are fed back for up to `max_verify_retries` retries.
    verify_acceptance: bool = True
    max_verify_retries: int = field(
        default_factory=lambda: int(os.getenv("AGENTIC_MAX_VERIFY_RETRIES", "1"))
    )


@dataclass
class StoryOutcome:
    story_id: str
    title: str
    ownership: str
    status: str          # "passed" | "failed" | "skipped"
    iterations: int
    tool_calls: int
    summary: str
    heal_attempts: int = 0
    model: str = ""


@dataclass
class SupervisorResult:
    success: bool
    summary: str
    outcomes: list[StoryOutcome] = field(default_factory=list)
    total_tool_calls: int = 0
    elapsed_s: float = 0.0
    # "completed" (all passed) | "completed_with_failures" (some passed, some not)
    # | "failed" (nothing passed). Distinguishes a clean run from a partial one so
    # callers never read a budget-starved run as a success.
    conclusion: str = ""


ProgressCallback = Optional[Callable[[str, str, str, Optional[str]], None]]
"""(agent_id, level, message, detail) — surfaced to the dashboard as agent_logs rows."""


class Supervisor:
    """Drives one full requirement-to-validated-app run."""

    def __init__(
        self,
        *,
        storypack_id: str,
        requirement_text: str,
        stories: list[Story],
        workspace_dir: Path,
        config: SupervisorConfig | None = None,
        on_progress: ProgressCallback = None,
        all_stories: list[Story] | None = None,
    ):
        self.storypack_id = storypack_id
        self.run_id = f"run_{uuid.uuid4().hex[:8]}"
        self.requirement_text = requirement_text
        self.stories = stories
        self.workspace_dir = workspace_dir
        self.config = config or SupervisorConfig()
        self.on_progress = on_progress or (lambda agent, level, msg, detail=None: None)

        self.bus = MessageBus(storypack_id=storypack_id, run_id=self.run_id)
        # Allow env-based overrides for budgets (config args win when explicitly set).
        env_budget = Budget.from_env()
        self.budget = Budget(
            max_tool_calls=min(self.config.max_total_tool_calls, env_budget.max_tool_calls),
            max_wall_seconds=min(self.config.max_wall_seconds, env_budget.max_wall_seconds),
        )
        ctx_stories = all_stories if all_stories is not None else stories
        self.run_ctx = RunContext(
            storypack_id=storypack_id,
            run_id=self.run_id,
            workspace_dir=workspace_dir,
            bus=self.bus,
            budget=self.budget,
            requirement_text=requirement_text,
            all_stories=ctx_stories,
        )

        # Instantiate agents and register them on the bus.
        self.pm = PMAgent(self.run_ctx, on_progress=self._mk_progress("pm"))
        self.backend = BackendAgent(self.run_ctx, on_progress=self._mk_progress("backend"))
        self.frontend = FrontendAgent(self.run_ctx, on_progress=self._mk_progress("frontend"))
        self.test = TestAgent(self.run_ctx, on_progress=self._mk_progress("test"))

        for agent in (self.pm, self.backend, self.frontend, self.test):
            agent.register_with_bus()

        # Pipeline state for the dashboard.
        self.outcomes: dict[str, StoryOutcome] = {}

        # Set when a story fails on a transient/infra error (rate limit, timeout,
        # gateway) so the run halts cleanly instead of rescoping good stories and
        # burning the rest against a throttled gateway. Resume retries them as-is.
        self._transient_halt = False
        self._halt_reason = ""

    # ---------- Public entry point ----------

    def run(self) -> SupervisorResult:
        started = time.monotonic()
        reset_usage()  # zero the token telemetry so this run's totals are clean
        self._log("supervisor", "info",
                  f"Run {self.run_id} starting with {len(self.stories)} stories.")
        self.bus.publish(
            topic="run.started",
            from_agent="supervisor",
            payload={"storypack_id": self.storypack_id, "run_id": self.run_id,
                     "story_count": len(self.stories)},
            summary=f"Run started: {len(self.stories)} stories",
        )

        try:
            backend_stories = self._sort([s for s in self.stories if s.ownership == "backend"])
            frontend_stories = self._sort([s for s in self.stories if s.ownership == "frontend"])
            test_stories = self._sort([s for s in self.stories if s.ownership == "testing"])

            # Phase 1: Backend stories.
            self._log("supervisor", "info",
                      f"Phase: backend ({len(backend_stories)} stories)")
            for idx, story in enumerate(backend_stories):
                if self._budget_exceeded():
                    self._abort_remaining(backend_stories[idx:], "run budget exceeded")
                    break
                before = usage_snapshot()
                self._run_story(self.backend, story)
                self._log_story_tokens(story, before)

            # After backend phase, publish the contract for frontend agents to consume.
            if any(o.status == "passed" for sid, o in self.outcomes.items()
                   if any(s.id == sid and s.ownership == "backend" for s in self.stories)):
                self._publish_contract()

            # Phase 2: Frontend stories.
            self._log("supervisor", "info",
                      f"Phase: frontend ({len(frontend_stories)} stories)")
            for idx, story in enumerate(frontend_stories):
                if self._budget_exceeded():
                    self._abort_remaining(frontend_stories[idx:], "run budget exceeded")
                    break
                # Frontend story is skipped if its declared backend dependency failed.
                if not self._dependencies_satisfied(story):
                    self._mark_skipped(story, "Skipped: backend dependency failed")
                    continue
                before = usage_snapshot()
                self._run_story(self.frontend, story)
                self._log_story_tokens(story, before)

            # Phase 3: Test suite (one run validates the whole pack).
            test_outcome: Optional[StoryOutcome] = None
            if self.config.run_tests and (test_stories or self._has_passed("backend") or self._has_passed("frontend")):
                self._log("supervisor", "info", "Phase: testing")
                if self._budget_exceeded():
                    self._abort_remaining(test_stories, "run budget exceeded")
                else:
                    test_story = test_stories[0] if test_stories else _make_synthetic_test_story()
                    before = usage_snapshot()
                    test_outcome = self._run_story(self.test, test_story)
                    self._log_story_tokens(test_story, before)
                    # Mirror the single test-suite outcome onto every testing story
                    # so they are all accounted for instead of silently missing.
                    self._propagate_test_outcome(test_story, test_stories)

            # Phase 4: Smoke (optional).
            if self.config.run_smoke and not self._budget_exceeded():
                self._log("supervisor", "info", "Phase: smoke")
                self._run_smoke_phase()

            # Reconcile: every story must have an outcome. Anything still missing
            # means the run ended (budget/halt) before reaching it — record it as a
            # failure rather than letting it vanish from the tally and inflate the
            # apparent success rate.
            self._finalize_outcomes()

            success = self._compute_success(test_outcome)
            conclusion = (
                "completed" if success
                else "completed_with_failures"
                if any(o.status == "passed" for o in self.outcomes.values())
                else "failed"
            )
            summary_msg = self._final_summary(success)
            self._log("supervisor", "info" if success else "error", summary_msg)
            self.bus.publish(
                topic="run.completed" if success else "run.failed",
                from_agent="supervisor",
                payload={"summary": summary_msg, "conclusion": conclusion},
                summary=summary_msg[:200],
            )

            return SupervisorResult(
                success=success,
                summary=summary_msg,
                outcomes=list(self.outcomes.values()),
                total_tool_calls=self.budget.tool_calls_used,
                elapsed_s=time.monotonic() - started,
                conclusion=conclusion,
            )
        finally:
            for agent in (self.pm, self.backend, self.frontend, self.test):
                agent.unregister()
            self.bus.close()

    # ---------- Core story execution: agent self-heals; PM only handles rescope ----------

    def _set_story_status(self, story_id: str, status: str) -> None:
        """Persist a per-story status transition so the Story Board reflects live
        progress (in_progress / done). Best-effort — never breaks the run."""
        try:
            state_store.update_story_status(self.storypack_id, story_id, status)
        except Exception:  # noqa: BLE001
            pass

    def _run_story(
        self, agent: AgentBase, story: Story, *, allow_simplify: bool = True
    ) -> StoryOutcome:
        """Run one story to completion.

        Architectural note (Apr 2026): the previous implementation wrapped the
        agent's ReAct loop in an outer "PM heal" cycle that retried up to 4 times
        with PM-supplied advice between attempts. That coupled product decisions
        (PM) to engineering debugging (dev agent) and produced lower-quality
        instructions than the agent's own reasoning. The current model:

          1. The dev agent runs ONE long ReAct loop and self-heals using its own
             tools (read_file, grep, run_pytest, smoke_uvicorn, etc.). It now has
             ~60 iterations and a Definition-of-Done gate on `finish_story`.
          2. If the agent itself signals failure (`finish_story(success=false)`
             or a runtime error), the supervisor asks PM ONCE for a product-level
             rescope decision (simplify / skip / halt). PM never debugs code.
          3. A simplified story re-runs through the agent ONCE more — but with
             ``allow_simplify=False`` so we don't recurse forever.
        """
        if story is None:
            return None  # type: ignore[return-value]

        self._log(agent.agent_id, "info", f"Starting story: {story.title}")
        self._set_story_status(story.id, "in_progress")
        self.bus.publish(
            topic="story.assigned",
            from_agent="supervisor",
            payload={"agent": agent.agent_id, "story": story.model_dump()},
            story_id=story.id,
            summary=f"Assigned to {agent.agent_id}: {story.title}",
        )

        outcome = StoryOutcome(
            story_id=story.id,
            title=story.title,
            ownership=story.ownership,
            status="failed",
            iterations=0,
            tool_calls=0,
            summary="",
            model=getattr(story, "model", None) or getattr(agent, "model", "") or "",
        )

        if self._budget_exceeded():
            outcome.summary = "Aborted: budget exceeded before story start"
            self.outcomes[story.id] = outcome
            return outcome

        # ---- Single autonomous attempt ----
        result: RunResult = agent.run(story)
        outcome.iterations = result.iterations
        outcome.tool_calls = result.tool_calls

        # ---- Acceptance-criteria verification gate ----
        # The agent's DoD gate proved the artifact RUNS. This independent pass
        # proves it SATISFIES the story. Unmet criteria are fed back for a bounded
        # retry; if still unmet, `result` becomes a failure and falls through to
        # the normal story.failed / PM-rescope path below.
        if result.success:
            result = self._verify_and_heal(agent, story, result, outcome)

        if result.success:
            outcome.status = "passed"
            outcome.summary = result.summary
            self._set_story_status(story.id, "done")
            self.bus.publish(
                topic="story.completed",
                from_agent=agent.agent_id,
                payload={"summary": result.summary},
                story_id=story.id,
                summary=result.summary[:160],
            )
            self.outcomes[story.id] = outcome
            return outcome

        # Reset to pending_review (not a stuck "in_progress") so the board shows it
        # as runnable again and it stays selectable for a resume run.
        self._set_story_status(story.id, "pending_review")

        # ---- Transient/infra failure → HALT, do NOT rescope ----
        # A rate-limit/timeout/gateway crash is not the story's fault. Rescoping it
        # would permanently dilute a good story for a transient reason, and a
        # failure-pattern would teach PM the wrong lesson. Halt cleanly so a resume
        # retries the SAME story (criteria intact) once the gateway recovers.
        if _is_transient_failure(result.summary, getattr(result, "error", "") or ""):
            outcome.status = "blocked"
            outcome.summary = f"Blocked by transient/infra error (resume to retry): {result.summary[:300]}"
            self._transient_halt = True
            self._halt_reason = "transient gateway/infra error (e.g. rate limit) — resume to retry"
            self._log(agent.agent_id, "error",
                      f"Story '{story.title}' hit a transient error — halting run without rescope.",
                      result.summary[:400])
            self.bus.publish(
                topic="story.blocked",
                from_agent=agent.agent_id,
                payload={"summary": result.summary},
                story_id=story.id,
                summary=f"Story blocked (transient): {result.summary[:110]}",
            )
            self.outcomes[story.id] = outcome
            return outcome

        # ---- Genuine agent-reported failure → publish + ask PM for rescope only ----
        self.bus.publish(
            topic="story.failed",
            from_agent=agent.agent_id,
            payload={"summary": result.summary},
            story_id=story.id,
            summary=f"Story failed: {result.summary[:120]}",
        )

        # Persist a cross-run learning entry so future PM story-creation is smarter.
        try:
            state_store.save_failure_pattern(
                self.storypack_id, story.title, agent.agent_id,
                "agent_self_failed",
                result.summary[:200],
                "Agent could not converge within its iteration cap.",
            )
        except Exception:
            pass

        # PM rescope is a *product* decision (skip/simplify/halt), not debugging.
        rescope_reply = self.bus.request(
            from_agent="supervisor",
            to_agent="pm",
            event_type="rescope_request",
            payload={"story_id": story.id, "errors": result.summary[:4000]},
            timeout=120,
        )
        decision = ((rescope_reply or {}).get("payload") or {}).get("decision", "halt")
        reason = ((rescope_reply or {}).get("payload") or {}).get("reason", "")
        self._log("pm", "info", f"PM rescope for {story.title}: {decision}", reason)

        if decision == "skip":
            outcome.status = "skipped"
            outcome.summary = f"Skipped: {reason}"
        elif decision == "simplify" and allow_simplify:
            simplified_data = ((rescope_reply or {}).get("payload") or {}).get("simplified_story")
            if isinstance(simplified_data, dict):
                try:
                    simpler = Story(**{**story.model_dump(), **simplified_data})
                    self._log("pm", "info", f"Retrying with simplified story: {simpler.title}")
                    sub_outcome = self._run_story(agent, simpler, allow_simplify=False)
                    # Aggregate cumulative work back onto the parent outcome.
                    sub_outcome.iterations += outcome.iterations
                    sub_outcome.tool_calls += outcome.tool_calls
                    return sub_outcome
                except Exception as exc:  # noqa: BLE001
                    outcome.status = "failed"
                    outcome.summary = f"Simplify produced invalid story: {exc}"
            else:
                outcome.status = "failed"
                outcome.summary = "PM simplify did not return a valid story object"
        else:
            outcome.status = "failed"
            outcome.summary = f"Halted: {reason or 'PM determined unrecoverable'}"

        self.outcomes[story.id] = outcome
        return outcome

    # ---------- Smoke phase: boot uvicorn + npm dev and check for errors ----------

    def _run_smoke_phase(self) -> None:
        try:
            from dashboard.backend.execution import _smoke_test_backend, _smoke_test_frontend  # type: ignore
        except Exception:
            self._log("supervisor", "info", "Smoke helpers unavailable; skipping smoke phase.")
            return

        be_ok, be_msg = _smoke_test_backend(self.workspace_dir)
        self._log("supervisor", "info" if be_ok else "error",
                  f"Backend smoke: {'PASS' if be_ok else 'FAIL'}", be_msg[:1200])
        fe_ok, fe_msg = _smoke_test_frontend(self.workspace_dir)
        self._log("supervisor", "info" if fe_ok else "error",
                  f"Frontend smoke: {'PASS' if fe_ok else 'FAIL'}", fe_msg[:1200])

        self.bus.publish(
            topic="smoke.completed",
            from_agent="supervisor",
            payload={"backend_ok": be_ok, "frontend_ok": fe_ok},
            summary=f"Smoke: backend {'PASS' if be_ok else 'FAIL'}, frontend {'PASS' if fe_ok else 'FAIL'}",
        )

    # ---------- Helpers ----------

    def _publish_contract(self) -> None:
        try:
            contract = extract_api_contract(self.workspace_dir / "backend")
            (self.workspace_dir / "contracts").mkdir(parents=True, exist_ok=True)
            save_json(self.workspace_dir / "contracts" / "api_contract.json", contract)
            self.bus.publish(
                topic="contract.published",
                from_agent="backend",
                payload=contract,
                summary=f"{len(contract.get('routes', []))} routes, {len(contract.get('models', []))} models",
            )
            self._log("supervisor", "info",
                      f"Contract published ({len(contract.get('routes', []))} routes)")
        except Exception as exc:  # noqa: BLE001
            self._log("supervisor", "error", f"Contract publish failed: {exc}")

    def _sort(self, stories: list[Story]) -> list[Story]:
        """Topological sort within a phase (matches old behaviour)."""
        id_set = {s.id for s in stories}
        by_id = {s.id: s for s in stories}
        in_degree: dict[str, int] = {s.id: 0 for s in stories}
        for s in stories:
            for dep in s.dependencies:
                if dep in id_set:
                    in_degree[s.id] = in_degree.get(s.id, 0) + 1
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        ordered: list[Story] = []
        while queue:
            queue.sort()
            sid = queue.pop(0)
            ordered.append(by_id[sid])
            for s in stories:
                if sid in s.dependencies and s.id in id_set:
                    in_degree[s.id] -= 1
                    if in_degree[s.id] == 0:
                        queue.append(s.id)
        remaining = [s for s in stories if s not in ordered]
        ordered.extend(remaining)
        return ordered

    def _dependencies_satisfied(self, story: Story) -> bool:
        for dep in story.dependencies:
            outcome = self.outcomes.get(dep)
            if outcome and outcome.status != "passed":
                return False
        return True

    def _has_passed(self, ownership: str) -> bool:
        for s in self.stories:
            if s.ownership == ownership and self.outcomes.get(s.id, StoryOutcome(
                story_id="", title="", ownership="", status="failed",
                iterations=0, tool_calls=0, summary=""
            )).status == "passed":
                return True
        return False

    def _mark_skipped(self, story: Story, reason: str) -> None:
        outcome = StoryOutcome(
            story_id=story.id, title=story.title, ownership=story.ownership,
            status="skipped", iterations=0, tool_calls=0, summary=reason,
        )
        self.outcomes[story.id] = outcome
        self._log("supervisor", "info", f"Skipped {story.title}: {reason}")

    # ---------- Acceptance-criteria verification ----------

    def _verification_enabled(self) -> bool:
        if not self.config.verify_acceptance:
            return False
        return os.getenv("AGENTIC_VERIFY_ACCEPTANCE", "1").strip().lower() not in (
            "0", "false", "no", "off",
        )

    def _verify_and_heal(
        self, agent: AgentBase, story: Story, result: RunResult, outcome: StoryOutcome
    ) -> RunResult:
        """Verify the artifact against acceptance criteria; feed unmet criteria back
        to the agent for up to `max_verify_retries` retries. Returns a successful
        RunResult if criteria are met, or a failure RunResult (which the caller
        routes through the normal failure/rescope path)."""
        # Test stories are validated by the test agent's own run_pytest (its DoD
        # gate), not by static AC review. The verifier can't confirm "tests pass"
        # from code, and a partial run (e.g. backend-only) won't have the frontend
        # that a generic "UI tests cover key flows" criterion expects — so verifying
        # a testing story only yields false failures.
        if story.ownership == "testing":
            return result
        if not self._verification_enabled() or not (story.acceptance_criteria or []):
            return result

        attempts = 0
        while True:
            verdict = self._verify_story(agent, story)
            if verdict is None:
                # Verifier unavailable/crashed — do not block the agent's own pass.
                return result
            if verdict.get("inconclusive"):
                # The reviewer malfunctioned (returned no usable assessment). That is
                # NOT a story failure — pass the agent's own validated result through.
                self._log("verifier", "info",
                          f"Acceptance check inconclusive for {story.title}; passing through.",
                          verdict.get("note"))
                return result
            if verdict.get("all_met"):
                self._log("verifier", "info",
                          f"Acceptance check PASSED for {story.title} "
                          f"({len(verdict.get('criteria', []))} criteria).")
                return result

            unmet = verdict.get("unmet", [])
            unmet_titles = "; ".join(c.get("criterion", "")[:80] for c in unmet)
            self._log("verifier", "error",
                      f"Acceptance check found {len(unmet)} unmet criteria for {story.title}",
                      unmet_titles)
            self.bus.publish(
                topic="acceptance.failed",
                from_agent="verifier",
                payload={"story_id": story.id, "unmet": unmet},
                story_id=story.id,
                summary=f"{len(unmet)} acceptance criteria unmet: {unmet_titles[:140]}",
            )

            if attempts >= self.config.max_verify_retries or self._budget_exceeded():
                return RunResult(
                    success=False,
                    summary=(
                        f"Acceptance criteria not met after verification: {unmet_titles}"
                    ),
                    iterations=result.iterations,
                    tool_calls=result.tool_calls,
                    metadata=result.metadata,
                )

            attempts += 1
            self._log("supervisor", "info",
                      f"Re-running {story.title} with verification feedback "
                      f"(verify attempt {attempts}/{self.config.max_verify_retries}).")
            retry = agent.run(self._with_ac_feedback(story, unmet))
            outcome.iterations += retry.iterations
            outcome.tool_calls += retry.tool_calls
            if not retry.success:
                return retry  # agent failed on retry — route to failure path
            result = retry  # loop re-verifies the new artifact

    def _verify_story(self, agent: AgentBase, story: Story) -> Optional[dict]:
        from agents.verifier import verify_story_acceptance
        try:
            return verify_story_acceptance(
                story=story,
                workspace_dir=self.workspace_dir,
                client=agent.client,
                model=os.getenv("VERIFIER_MODEL", agent.model),
            )
        except Exception as exc:  # noqa: BLE001
            self._log("verifier", "error", f"Verifier crashed for {story.title}: {exc}")
            return None

    def _with_ac_feedback(self, story: Story, unmet: list[dict]) -> Story:
        """Return a copy of the story with the unmet criteria appended as explicit
        implementation notes so the agent's next attempt targets the gaps."""
        notes = list(story.implementation_notes) + [
            "VERIFICATION FEEDBACK — an independent reviewer judged these acceptance "
            "criteria UNMET. Implement them specifically and re-validate before finishing:",
        ] + [
            f"UNMET: {c.get('criterion', '')} (reviewer: {c.get('evidence', '')[:160]})"
            for c in unmet
        ]
        return Story(**{**story.model_dump(), "implementation_notes": notes})

    def _abort_remaining(self, stories: list[Story], reason: str) -> None:
        """Record stories that never ran (budget/halt) as failures, so they are
        counted instead of silently dropped from the tally."""
        # On a transient halt the honest reason is the gateway/infra error, and the
        # un-run stories should stay cleanly resumable rather than reading as real
        # failures — so mark them "blocked".
        if self._transient_halt and self._halt_reason:
            reason = self._halt_reason
        status = "blocked" if self._transient_halt else "failed"
        for s in stories:
            if s.id in self.outcomes:
                continue
            self.outcomes[s.id] = StoryOutcome(
                story_id=s.id, title=s.title, ownership=s.ownership,
                status=status, iterations=0, tool_calls=0,
                summary=f"Aborted: {reason}",
            )
            self._log("supervisor", "error", f"Aborted {s.title}: {reason}")

    def _propagate_test_outcome(self, ran_story: Story, test_stories: list[Story]) -> None:
        """The test agent runs once for the whole pack. Mirror that single outcome
        onto every testing story so each is accounted for in the final tally."""
        src = self.outcomes.get(ran_story.id)
        if not src:
            return
        for s in test_stories:
            if s.id == ran_story.id or s.id in self.outcomes:
                continue
            self.outcomes[s.id] = StoryOutcome(
                story_id=s.id, title=s.title, ownership=s.ownership,
                status=src.status, iterations=0, tool_calls=0,
                summary=f"Covered by the test-suite run ({ran_story.id}): {src.status}",
            )

    def _finalize_outcomes(self) -> None:
        """Guarantee every story has an outcome before the tally is computed."""
        for s in self.stories:
            if s.id in self.outcomes:
                continue
            if s.ownership == "testing" and not self.config.run_tests:
                self.outcomes[s.id] = StoryOutcome(
                    story_id=s.id, title=s.title, ownership=s.ownership,
                    status="skipped", iterations=0, tool_calls=0,
                    summary="Testing phase disabled for this run (fast track).",
                )
            else:
                self.outcomes[s.id] = StoryOutcome(
                    story_id=s.id, title=s.title, ownership=s.ownership,
                    status="failed", iterations=0, tool_calls=0,
                    summary="Not executed: run ended before this story (budget/halt).",
                )

    def _compute_success(self, test_outcome: Optional[StoryOutcome]) -> bool:
        """A run succeeds only when every backend & frontend story passed and —
        when testing is enabled — the test suite passed too. Aborted, skipped, or
        failed stories all count against it."""
        for s in self.stories:
            if s.ownership in ("backend", "frontend"):
                o = self.outcomes.get(s.id)
                if not o or o.status != "passed":
                    return False
        if self.config.run_tests:
            if test_outcome is None or test_outcome.status != "passed":
                return False
        return True

    def _budget_exceeded(self) -> bool:
        if self._transient_halt:
            return True
        if self.budget.wall_exceeded():
            self._log("supervisor", "error", "Wall-clock budget exceeded; halting run.")
            return True
        if self.budget.tool_calls_exceeded():
            self._log("supervisor", "error", "Tool-call budget exceeded; halting run.")
            return True
        return False

    def _final_summary(self, success: bool) -> str:
        passed = sum(1 for o in self.outcomes.values() if o.status == "passed")
        skipped = sum(1 for o in self.outcomes.values() if o.status == "skipped")
        failed = sum(1 for o in self.outcomes.values() if o.status == "failed")
        u = usage_snapshot()
        tot = u["prompt_tokens"] + u["completion_tokens"]
        hit = round(100 * u["cached_tokens"] / u["prompt_tokens"], 1) if u["prompt_tokens"] else 0.0
        return (
            f"Run {self.run_id} {'succeeded' if success else 'finished with failures'}: "
            f"{passed} passed, {failed} failed, {skipped} skipped, "
            f"{self.budget.tool_calls_used} tool calls, "
            f"{tot:,} tokens across {u['calls']} LLM calls (cache hit {hit}%)."
        )

    def _log_story_tokens(self, story: Story, before: dict) -> None:
        """Log the token cost attributed to a single story (delta since `before`).
        The cache-hit % shows whether the prompt cache is actually engaging — a low
        number on a long story means the prefix is being busted (e.g. by compaction)."""
        if story is None:
            return
        d = usage_delta(before)
        if d.get("total_tokens", 0) <= 0:
            return
        self._log(
            "supervisor", "info",
            f"Tokens · {story.title}: {d['total_tokens']:,} "
            f"(prompt {d['prompt_tokens']:,} / completion {d['completion_tokens']:,}, "
            f"cache hit {d['cache_hit_pct']}%)",
        )
        # Persist an append-only execution record (survives re-runs / overwrites).
        o = self.outcomes.get(story.id)
        try:
            state_store.save_story_run(
                run_id=self.run_id,
                storypack_id=self.storypack_id,
                story_id=story.id,
                story_title=story.title,
                agent_type=(o.ownership if o else story.ownership),
                model=(o.model if o else (getattr(story, "model", None) or "")),
                status=(o.status if o else "unknown"),
                iterations=(o.iterations if o else 0),
                tool_calls=(o.tool_calls if o else 0),
                prompt_tokens=d.get("prompt_tokens", 0),
                completion_tokens=d.get("completion_tokens", 0),
                total_tokens=d.get("total_tokens", 0),
                cached_tokens=d.get("cached_tokens", 0),
                summary=(o.summary if o else "")[:500],
            )
        except Exception:  # noqa: BLE001 — telemetry must never break a run
            pass

    def _log(self, agent: str, level: str, msg: str, detail: Optional[str] = None) -> None:
        try:
            state_store.add_agent_log(None, agent, msg, level=level, detail=detail)
        except Exception:
            pass
        try:
            self.on_progress(agent, level, msg, detail)
        except Exception:
            pass

    def _mk_progress(self, agent_id: str):
        def cb(level: str, message: str, detail: Optional[str] = None):
            try:
                state_store.add_agent_log(None, agent_id, message, level=level, detail=detail)
            except Exception:
                pass
            try:
                self.on_progress(agent_id, level, message, detail)
            except Exception:
                pass
        return cb


def _make_synthetic_test_story() -> Story:
    """Used when the storypack has no explicit testing story but we still want the
    test agent to run a regression suite."""
    return Story(
        id="synthetic_test_suite",
        title="Test Suite",
        description="Generate regression tests for the implemented stories.",
        acceptance_criteria=["API tests pass", "UI tests cover key flows"],
        implementation_notes=["Use pytest + httpx for API tests"],
        test_focus=["End-to-end happy path"],
        ownership="testing",
        dependencies=[],
    )
