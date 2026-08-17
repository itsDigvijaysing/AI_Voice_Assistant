"""Skill writer exposed over MCP (AI_Linux, self-improving skills).

Lets the assistant LEARN: save_skill writes a SKILL-*.md draft under skills/learned/ so the retriever
picks it up next turn. It writes a MARKDOWN FILE ONLY and never runs anything, the commands it stores
stay inert until the model later calls the gated mcp.shell.run_command. Ungated is correct (same as
voice_server writing voice.json); the safety gate still governs every execution. Writing logic lives in
core/skills_index.write_skill (shared with the engine's /learn command).
"""

from __future__ import annotations

import json
import logging

from loguru import logger
from mcp.server.fastmcp import FastMCP

from ..core.skills_index import write_skill

logger.remove()
logging.getLogger().setLevel(logging.CRITICAL)

mcp = FastMCP("skills_writer")


@mcp.tool()
def save_skill(name: str, trigger: str, commands: list[str], notes: str = "") -> str:
    """Save a new skill (a how-to for a desktop task) so you can reuse it later.

    Writes a markdown draft only, it NEVER runs anything. The stored commands execute later only when
    you call mcp.shell.run_command (which is safety-gated). Use this when the user teaches you how to do
    something, or after you find a command that works.

    Args:
        name: short skill name, e.g. "take a screenshot".
        trigger: when to use it, e.g. "user asks to take or grab a screenshot".
        commands: the exact shell command(s), e.g. ["grim ~/Pictures/shot.png"].
        notes: optional extra context.

    Returns JSON {ok, file} or {error}.
    """
    return json.dumps(write_skill(name, trigger, commands, notes))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
