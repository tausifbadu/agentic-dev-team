"""Class-based agentic runtime.

Each agent is a class with:
  - `agent_id` (used as bus address and tool-call attribution)
  - `tools` (a curated list of tool names from the registry)
  - `system_prompt()` (role description + project guidelines)
  - `run(story)` (invokes ReAct loop on its scratch dir)
  - `handle_message(msg)` (called by the bus when peers send questions)
"""

from .base import AgentBase, RunContext, RunResult, Budget
from .backend import BackendAgent
from .frontend import FrontendAgent
from .test import TestAgent
from .pm import PMAgent

__all__ = [
    "AgentBase",
    "RunContext",
    "RunResult",
    "Budget",
    "BackendAgent",
    "FrontendAgent",
    "TestAgent",
    "PMAgent",
]
