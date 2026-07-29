"""Todoist integration exposed over MCP (AI_Linux addition).

Lets the assistant check the user's Todoist tasks and add new ones
(https://developer.todoist.com/api/v1/). The personal API token is read from
TODOIST_API_TOKEN in the environment — never stored in config, never committed (same
convention as every other API key this project reads, e.g. GLADOS_API_KEY). Ungated: these calls only touch the user's own Todoist
account, an external and trivially reversible service — never the local machine — so
they carry none of the local-shell/GUI risk the action gate exists for.
"""

from __future__ import annotations

import json
import logging
import os

import requests
from loguru import logger
from mcp.server.fastmcp import FastMCP

logger.remove()
logging.getLogger().setLevel(logging.CRITICAL)

mcp = FastMCP("todoist")

_API_BASE = "https://api.todoist.com/api/v1"
_TIMEOUT = 10.0
_MAX_TASKS = 50  # keep the model's context small even if the account has hundreds of tasks


def _token() -> str | None:
    return os.environ.get("TODOIST_API_TOKEN", "").strip() or None


def _task_brief(t: dict) -> dict:
    """Trim a Todoist task object to what the model needs to speak about it."""
    due = t.get("due") or {}
    return {
        "id": t.get("id"),
        "content": t.get("content"),
        "due": due.get("string") or due.get("date"),
        "priority": t.get("priority"),
    }


@mcp.tool()
def list_tasks(filter: str = "today") -> str:
    """List the user's Todoist tasks.

    Args:
        filter: a Todoist filter query — "today" (default), "overdue", "today | overdue",
            or "" / "all" for every open task across all projects.

    Returns JSON {count, tasks: [{id, content, due, priority}, ...]} or {error}.
    """
    token = _token()
    if not token:
        return json.dumps({"error": "TODOIST_API_TOKEN is not set — export it before starting the assistant"})
    headers = {"Authorization": f"Bearer {token}"}
    f = (filter or "").strip()
    try:
        if f and f.lower() != "all":
            # NOTE: verified against the live API — despite what the hosted docs page says, the
            # query-string param this endpoint actually requires is "query", not "filter" (a
            # "filter" param 400s with error_tag ARGUMENT_MISSING pointing at "query").
            resp = requests.get(f"{_API_BASE}/tasks/filter", headers=headers, params={"query": f}, timeout=_TIMEOUT)
        else:
            resp = requests.get(f"{_API_BASE}/tasks", headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return json.dumps({"error": f"Todoist request failed: {exc}"})
    data = resp.json()
    if isinstance(data, dict):
        items = data.get("results") or []
    else:
        items = data if isinstance(data, list) else []
    tasks = [_task_brief(t) for t in items[:_MAX_TASKS]]
    return json.dumps({"count": len(tasks), "tasks": tasks})


@mcp.tool()
def create_task(content: str, due_string: str = "today") -> str:
    """Create a new Todoist task.

    Args:
        content: the task text, e.g. "go for a walk".
        due_string: a natural-language due date Todoist understands, e.g. "today",
            "tomorrow", "every Friday". Defaults to "today".

    Returns JSON {ok, task: {id, content, due}} or {error}.
    """
    token = _token()
    if not token:
        return json.dumps({"error": "TODOIST_API_TOKEN is not set — export it before starting the assistant"})
    text = (content or "").strip()
    if not text:
        return json.dumps({"error": "create_task needs task content"})
    body: dict[str, str] = {"content": text}
    if (due_string or "").strip():
        body["due_string"] = due_string.strip()
    try:
        resp = requests.post(
            f"{_API_BASE}/tasks", headers={"Authorization": f"Bearer {token}"}, json=body, timeout=_TIMEOUT
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        return json.dumps({"error": f"Todoist request failed: {exc}"})
    return json.dumps({"ok": True, "task": _task_brief(resp.json())})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
