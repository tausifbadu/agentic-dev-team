---
name: agentic-dev-team
description: >-
  Architecture guide and coding conventions for the agentic dev team project.
  Use when extending, modifying, or adding agents, schemas, prompts, or
  orchestration logic in this codebase. Use when the user mentions agents,
  stories, PM agent, frontend agent, backend agent, workspace, or story packs.
---

# Agentic Dev Team

## Project Overview

A three-agent development team orchestrated by a sequential Python pipeline:

1. **PM Agent** -- transforms a natural-language requirement into a `StoryPack` (list of `Story` objects).
2. **Human Review** -- stories printed to terminal; user types `approve` or `reject`.
3. **Engineering Agents** -- on approval, frontend and backend agents implement stories one at a time using a plan-then-code loop with scratch validation.

Entry point: `main.py` (CLI). State: JSON files in `./state/`. Generated code: `./workspace/frontend/` and `./workspace/backend/`.

```
Requirement --> PM Agent --> Human Review --> Frontend Agent --> Backend Agent
                                                  |                  |
                                            npm run build          pytest
```

## Directory Layout

```
main.py                  # CLI orchestrator
schemas.py               # Pydantic models: Requirement, Story, StoryPack
state_store.py           # JSON file persistence (./state/)
agents/
  __init__.py
  pm_agent.py            # PM agent: requirement -> StoryPack
  frontend_agent.py      # React/Vite agent with npm build validation
  backend_agent.py       # FastAPI agent with pytest validation
  reasoning.py           # Shared utilities (file selection, scratch copies, scope)
prompt/                  # Example requirement texts (not loaded by agents)
workspace/               # Generated code output (gitignored)
  frontend/              # React app
  backend/               # FastAPI app
  scope.json             # Tracks implemented story IDs
  debug/                 # Raw LLM responses on parse failure
state/                   # Persisted requirements and story packs (gitignored)
```

## Core Schemas (schemas.py)

```python
class Requirement(BaseModel):
    id: str               # "req_{uuid8}"
    text: str
    submitted_at: datetime

class Story(BaseModel):
    id: str               # "req_xxx_story_N"
    title: str
    description: str
    acceptance_criteria: list[str]
    implementation_notes: list[str]
    test_focus: list[str]
    ownership: Literal["frontend", "backend"]
    status: Literal["pending_review", "approved", "rejected", "in_progress", "done"]

class StoryPack(BaseModel):
    id: str               # "pack_{uuid8}"
    requirement_id: str
    requirement_text: str
    stories: list[Story]
    status: Literal["pending_review", "approved", "rejected"]
    created_at: datetime
```

## Agent Implementation Pattern

Every engineering agent follows this exact loop (see `frontend_agent.py` and `backend_agent.py`):

```
for attempt in range(MAX_RETRIES + 1):
    1. Plan   -- _plan_*_change()   -> LLM call with PLANNER_PROMPT
    2. Code   -- _generate_*_patch() -> LLM call with CODER_PROMPT
    3. Scratch -- create_scratch_copy() + _apply_changes()
    4. Validate -- _run_frontend_checks() or _run_pytest()
    5a. Pass  -- promote_scratch_copy() + update_scope()  -> return success
    5b. Fail  -- feed error into next attempt's prompt     -> retry
```

### Key internal functions per agent

| Function | Purpose |
|---|---|
| `_call_llm_json(client, prompt, system_prompt)` | LLM call with JSON retry loop (max 2 parse retries) |
| `_story_bundle(story, requirement_text, siblings)` | Build context: requirement + story + siblings + scope |
| `_plan_*_change(client, story, req_text, siblings)` | Planning LLM call using PLANNER_PROMPT |
| `_generate_*_patch(client, story, ..., plan, error)` | Coding LLM call using CODER_PROMPT; includes error on retry |
| `_apply_changes(data, root)` | Write files + merge dependencies into scratch dir |
| `_store_raw_response(raw)` | Save failed LLM output to `workspace/debug/` |

### LLM output schemas

**Planner output:**
```json
{
  "summary": "short plan",
  "files_to_touch": ["src/App.jsx"],
  "acceptance_map": [{"criterion": "AC1", "implementation": "how"}],
  "validation_steps": ["build"],
  "risk_checks": ["regressions"]
}
```

**Frontend coder output:**
```json
{
  "files": [{"path": "relative/from/frontend", "content": "full file"}],
  "dependencies_add": ["package"],
  "dev_dependencies_add": ["package"]
}
```

**Backend coder output:**
```json
{
  "files": [{"path": "relative/from/backend", "content": "full file"}],
  "requirements_add": ["package"]
}
```

## Shared Utilities (agents/reasoning.py)

| Function | Purpose |
|---|---|
| `select_relevant_files(root, text, suffixes, max_files)` | Keyword-score files for prompt context (avoids sending entire workspace) |
| `render_context(root, paths)` | Format selected files as `=== path ===\ncontent` blocks |
| `create_scratch_copy(source, prefix)` | Copy workspace dir to temp for isolated validation |
| `promote_scratch_copy(scratch, destination)` | Replace real workspace with validated scratch output |
| `update_scope(scope_path, entry)` | Add/update an entry in `scope.json` |
| `load_json(path, default)` / `save_json(path, payload)` | JSON file I/O |
| `important_keywords(text)` | Tokenize text into keywords for file scoring |

## Coding Conventions

- **Data models**: Pydantic `BaseModel` for all structured data.
- **LLM client**: `openai.OpenAI()` with `response_format={"type": "json_object"}`.
- **Model selection**: Env vars `PM_AGENT_MODEL`, `FRONTEND_AGENT_MODEL`, `BACKEND_AGENT_MODEL`. Default: `gpt-4o-mini`.
- **Temperature**: 0.2 for coding agents, 0.3 for PM agent.
- **Retries**: Max 2 for JSON parse failures; max 2 for validation failures (build/pytest).
- **Debug output**: Raw LLM responses saved to `workspace/debug/` on parse failure.
- **Prompts**: Hardcoded as module-level string constants in each agent file (SYSTEM_PROMPT, PLANNER_PROMPT, CODER_PROMPT, JSON_RETRY_PROMPT).
- **State**: JSON files in `./state/` via `state_store.py`. No database.
- **Scope tracking**: `workspace/scope.json` records implemented story IDs so agents know what exists.
- **Scratch validation**: Never modify the real workspace directly. Always copy, validate, then promote.
- **No async**: Everything runs synchronously and sequentially.
- **Dependencies**: Only `openai` and `pydantic` as project deps. Agent-generated code has its own deps.

## Adding a New Agent

To add a new agent (e.g., a test agent or infra agent):

1. **Update `schemas.py`**: Extend the `ownership` literal to include the new type:
   ```python
   ownership: Literal["frontend", "backend", "testing"]
   ```

2. **Create `agents/new_agent.py`** following the established pattern:
   - Define `MODEL_NAME` from env var
   - Define `PLANNER_PROMPT` and `CODER_PROMPT` as module constants
   - Implement `_call_llm_json()`, `_story_bundle()`, `_plan_*_change()`, `_generate_*_patch()`
   - Implement `_apply_changes()` for the agent's workspace area
   - Implement a validation function (e.g., run linter, run tests)
   - Implement `implement_*()` as the public entry point using the plan-scratch-validate loop

3. **Update `agents/__init__.py`**: Export the new agent's public function.

4. **Update `main.py`**: Filter stories by the new ownership value and call the agent in sequence.

5. **Workspace directory**: The agent should write to `workspace/<agent_name>/` and use `create_scratch_copy` / `promote_scratch_copy` for isolated validation.

## Running the Project

### Setup
```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
```

### Run
```bash
python main.py "Build a simple frontend page and backend endpoint"
```

### Flow
1. PM agent creates stories (1 LLM call)
2. Stories printed to terminal for review
3. Type `approve` to proceed or `reject` to exit
4. Frontend agent processes frontend stories sequentially (2 LLM calls per story: plan + code)
5. Backend agent processes backend stories sequentially (2 LLM calls per story: plan + code)

### Inspecting State
- Requirements: `state/requirement_*.json`
- Story packs: `state/storypack_*.json`
- Scope: `workspace/scope.json`
- Debug (failed LLM output): `workspace/debug/`
- Generated frontend: `workspace/frontend/`
- Generated backend: `workspace/backend/`

### Debugging Failures
- If JSON parsing fails repeatedly, check `workspace/debug/` for raw LLM responses.
- If build/pytest fails, the error output is included in the next retry prompt.
- After max retries, the full error is printed and the process exits with code 1.
- To change the LLM model, set env vars: `PM_AGENT_MODEL`, `FRONTEND_AGENT_MODEL`, `BACKEND_AGENT_MODEL`.

## Known Limitations (v0)

- No inter-agent communication (frontend doesn't see backend's generated API).
- No story dependency ordering.
- No test agent (planned but not implemented).
- No git integration (no commits or PRs).
- No rejection feedback loop (reject exits immediately).
- No parallel execution.
- No cost/token tracking.
- Scratch temp directories from failed attempts are not cleaned up.
- `datetime.utcnow()` is deprecated in Python 3.12+.
