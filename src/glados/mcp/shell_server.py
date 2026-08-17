"""Local shell/command executor exposed over MCP (AI_Linux, Phase 5).

This is the assistant's "executor" for arbitrary system tasks, running a command
on the local machine. It is intentionally lean (a thin subprocess wrapper) rather
than a heavyweight agent framework: the project goal is simple/regular tasks, not
complex autonomous agent work. Common desktop actions have dedicated typed tools in
``mcp.skills_actions.*``; this stays for genuine arbitrary commands.

SAFETY: every call to ``mcp.shell.*`` is gated (see ``glados.core.tool_safety``):
denied unless the session is armed (GLADOS_ALLOW_ACTIONS=1), and always denied in
autonomy mode. Execution + the destructive-command denylist live in
``glados.mcp.shell_exec`` (shared with the skills_actions tools).
"""

from __future__ import annotations

import json
import logging

from loguru import logger
from mcp.server.fastmcp import FastMCP

# _destructive_reason is re-exported for callers/tests that import it from this module.
from glados.mcp.shell_exec import _destructive_reason, run_shell  # noqa: F401

logger.remove()
logging.getLogger().setLevel(logging.CRITICAL)

mcp = FastMCP("shell")


@mcp.tool()
def run_command(command: str) -> str:
    """Run a shell command on the local machine and return its result.

    Use for regular system tasks (listing files, checking status, opening things,
    small edits). Gated by the action safety gate (runs only when actions are armed).

    Returns JSON: {returncode, stdout, stderr} or {error}. Output is truncated.
    """
    result = run_shell(command)
    if result.get("refused"):
        logger.warning("shell: refused destructive command ({}): {}", result["refused"], (command or "")[:120])
        result = {"error": result["error"]}
    return json.dumps(result)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
