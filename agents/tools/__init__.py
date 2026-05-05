"""Agent tool registry, ReAct loop, and built-in tool collections."""

from .registry import Tool, ToolRegistry, ToolError, ToolResult, ToolContext
from .file_tools import register_file_tools
from .exec_tools import register_exec_tools
from .agent_tools import register_agent_tools
from .state_tools import register_state_tools

__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolError",
    "ToolResult",
    "ToolContext",
    "register_file_tools",
    "register_exec_tools",
    "register_agent_tools",
    "register_state_tools",
    "build_default_registry",
]


def build_default_registry(context: "ToolContext") -> "ToolRegistry":
    """Construct a registry pre-populated with the standard tool set."""
    registry = ToolRegistry(context=context)
    register_file_tools(registry)
    register_exec_tools(registry)
    register_state_tools(registry)
    register_agent_tools(registry)
    return registry
