# agentic-dev-team

v0: Requirement -> PM Agent -> Human Review -> Frontend/Backend Agents (patching mode).

## Setup

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
```

## Run

```bash
python main.py "Build a simple frontend page and backend endpoint"
```

1. PM Agent creates stories
2. Stories printed to terminal
3. Type `approve` or `reject`
4. If approved:
   - Frontend Agent patches `workspace/frontend` (React/HTML/CSS) and runs `npm run build`
   - Backend Agent patches `workspace/backend` and runs pytest

## Workspace & Patching

- `workspace/frontend`: frontend app generated and patched over time
- `workspace/backend`: backend app generated and patched over time
- `workspace/scope.json`: tracks implemented story IDs
- `workspace/debug`: raw model outputs when JSON parsing fails

## MFA Run Guide

- See `RUN_MFA_APP.md` for step-by-step instructions to run and test the MFA app locally.
