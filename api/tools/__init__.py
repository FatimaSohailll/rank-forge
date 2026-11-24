"""Real tools the agents call. Each tool logs itself on the current run via log_call."""
import run_context


class ToolUnavailable(Exception):
    """A tool cannot run (missing key, network failure). Agents degrade instead of crashing."""


def log_call(name: str, args: dict) -> None:
    """Record the call on the active RunContext; a no-op outside a run (e.g. scripts, tests)."""
    try:
        run_context.current().add_tool_call(name, args)
    except RuntimeError:
        pass
