# agentic-dev-team

v1: Requirement -> PM Agent -> Human Review -> Frontend/Backend/Test Agents.

Includes a web dashboard and CLI interface.

## Setup

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
```

## Run (CLI)

```bash
# Inline requirement
python main.py "Build a simple frontend page and backend endpoint"

# From a prompt file (full path or just the name)
python main.py --file prompt/webhook.txt
python main.py --file webhook

# List available prompt files
python main.py --list
```

Add new requirements as `.txt` files in the `prompt/` directory.

### CLI Flow

1. PM Agent creates stories (with dependencies and test stories)
2. Stories printed to terminal for review
3. Type `approve` or `reject`
4. If approved:
   - Backend Agent implements backend stories and runs pytest
   - Frontend Agent implements frontend stories and runs npm build
   - Test Agent generates and runs API, UI, and integration tests

## Run (Dashboard)

```bash
# Start the dashboard API
uvicorn dashboard.backend.app:app --reload

# In another terminal, start the dashboard frontend
cd dashboard/frontend
npm install
npm run dev
```

- Dashboard UI: http://localhost:5173
- Dashboard API: http://127.0.0.1:8000/api/health
- API docs: http://127.0.0.1:8000/docs

## Architecture

```
Requirement -> PM Agent -> Story Board (Review)
                               |
                    Approve -> Backend Agent -> Frontend Agent -> Test Agent
                               |
                          Dashboard (real-time monitoring)
```

## Agents

| Agent | Purpose | Validation |
|-------|---------|------------|
| PM Agent | Converts requirements into stories | N/A |
| Backend Agent | Implements FastAPI backend | pytest |
| Frontend Agent | Implements React frontend | npm run build |
| Test Agent | Generates API, UI, integration tests | pytest + Playwright |

## Workspace & State

- `workspace/frontend`: generated React app
- `workspace/backend`: generated FastAPI app
- `workspace/tests`: generated test suites (api/, ui/, integration/)
- `workspace/scope.json`: tracks implemented story IDs
- `workspace/debug`: raw model outputs when JSON parsing fails
- `state/state.db`: SQLite database for requirements, storypacks, logs, test results

## MFA Run Guide

- See `RUN_MFA_APP.md` for step-by-step instructions to run and test the MFA app locally.
