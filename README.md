# agentic-dev-team

v0: Requirement -> PM Agent -> Human Review -> Backend Agent.

## Setup

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
```

## Run

```bash
python main.py "Build a health check API endpoint that returns 200"
```

1. PM Agent creates stories
2. Stories printed to terminal
3. Type `approve` or `reject`
4. If approved, Backend Agent implements backend stories and runs pytest