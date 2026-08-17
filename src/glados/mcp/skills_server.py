"""Skills retriever exposed over MCP (AI_Linux). Thin wrappers over core.skills_index.

The engine usually injects the relevant skill's exact command into the prompt automatically (so the
model acts in one tool call), making find_skill a fallback for when no capability hint was injected.
Retrieval lives in core/skills_index.py, single source of truth, also used in-process by the engine.
"""

from __future__ import annotations

import json
import logging

from loguru import logger
from mcp.server.fastmcp import FastMCP

from ..core.skills_index import best_skill, load_skills

logger.remove()
logging.getLogger().setLevel(logging.CRITICAL)

mcp = FastMCP("skills")


@mcp.tool()
def list_skills() -> str:
    """List available skill procedures (name + trigger)."""
    return json.dumps([{"name": s["name"], "trigger": s["trigger"]} for s in load_skills()])


@mcp.tool()
def find_skill(query: str) -> str:
    """Return the best-matching skill procedure for a query (fallback keyword/hybrid match)."""
    skills = load_skills()
    if not skills:
        return json.dumps({"error": "no skills available"})
    best = best_skill(query)
    if best is None:
        return json.dumps({"match": None, "available": [s["name"] for s in skills]})
    return json.dumps({"match": best["name"], "trigger": best["trigger"], "procedure": best["text"]})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
