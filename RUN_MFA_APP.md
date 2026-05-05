# Agentic Dev Team — Run Guide

## Prerequisites

- Python 3.12+ virtual environment at `.venv`
- Node.js and `npm` installed
- OpenAI API key in `.env`

## 1. Activate the Virtual Environment

```bash
cd /Users/tausifbadu/agentic-dev-team/agentic-dev-team
source .venv/bin/activate
```

## 2. Start the Dashboard Backend

```bash
uvicorn dashboard.backend.app:app --reload \
  --reload-dir agents --reload-dir dashboard \
  --reload-dir schemas.py --reload-dir state_store.py
```

Dashboard API: `http://localhost:8000/api/health`

## 3. Start the Dashboard Frontend

In a second terminal:

```bash
cd dashboard/frontend
npm install
npm run dev
```

Dashboard UI: `http://localhost:5173`

## 4. Wipe DB and Workspace (Fresh Start)

Stop the dashboard backend first, then:

```bash
rm -f state/state.db
rm -rf workspace/frontend workspace/backend workspace/debug workspace/tests workspace/scope.json
```

Then restart the dashboard backend (step 2) so `init_db()` recreates the tables.

## 5. Run the Generated App

After agents finish building, start the generated app:

**Backend** (terminal 3):

```bash
cd workspace/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8001
```

**Frontend** (terminal 4):

```bash
cd workspace/frontend
npm install
npm run dev
```

Generated app: `http://localhost:5173` (or next available port)
Generated API: `http://localhost:8001/docs`

## 6. Environment Variables

`.env` file in project root:

```
OPENAI_API_KEY=sk-...
PM_AGENT_MODEL=gpt-4o-mini
BACKEND_AGENT_MODEL=gpt-5.3-codex
FRONTEND_AGENT_MODEL=gpt-5.3-codex
TEST_AGENT_MODEL=gpt-4o-mini
```

## 7. Common Issues

| Problem | Fix |
|---|---|
| `Address already in use` | `lsof -ti :8000 \| xargs kill -9` |
| Agent stuck at "Planning..." | Restart uvicorn; check `--reload-dir` flags |
| `npm: command not found` | `brew install node` |
| `uvicorn: command not found` | Activate the venv first: `source .venv/bin/activate` |
| Frontend can't reach backend | Check CORS middleware and Vite proxy config |
| Workspace venv gone after fix | Recreate: `cd workspace/backend && python3 -m venv .venv` |

## 8. Stop Everything

Press `Ctrl+C` in each terminal.
