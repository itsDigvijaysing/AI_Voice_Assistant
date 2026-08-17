"""Action safety gate for tool calls.

AI_Linux addition. Some tools (desktop control ``mcp.computer_use.*`` and the
local shell executor ``mcp.shell.*``) take irreversible real-world actions
(clicks, keystrokes, running commands). This gate decides whether such a call is
allowed to run.

Why this is NOT an interactive y/N prompt: the assistant runs as a voice
loop and as a Textual TUI, where a background thread cannot safely read stdin (it
hangs under the TUI, which owns the terminal, and contends with the text listener
in input_mode "both"). So the gate is NON-BLOCKING and fail-safe instead of
prompting:

  * Gated tools are DENIED by default.
  * They run only when the session is explicitly armed: env GLADOS_ALLOW_ACTIONS
    in {1, true, yes, on}. A deliberate session-level "let the assistant act"
    consent that behaves identically in voice / TUI / headless.
  * In autonomy mode the gated families are ALWAYS denied (hard floor), regardless
    of any env override: an autonomous loop must never act unsupervised.
  * A ``prompt_fn`` hook is kept for a future per-action confirmation UI (a Textual
    modal or a spoken yes/no); when supplied it is used instead of the env policy.

Configuration:
    GLADOS_ALLOW_ACTIONS  Arm gated actions for the session ("1"/"true"/"yes"/"on").
                          Default off -> gated tools are denied.
    GLADOS_CONFIRM_TOOLS  Comma-separated fnmatch globs of gated tool names.
                          Defaults to the desktop + shell families. Empty string
                          ungates everything for interactive use, but NEVER weakens
                          the autonomy hard floor below.
"""

import fnmatch
import os
from collections.abc import Callable
from typing import Any

from loguru import logger

_DEFAULT_CONFIRM_PATTERNS: tuple[str, ...] = ("mcp.computer_use.*", "mcp.shell.*", "mcp.skills_actions.*")
# Families an autonomous loop may NEVER run, independent of GLADOS_CONFIRM_TOOLS.
_AUTONOMY_HARD_DENY: tuple[str, ...] = ("mcp.computer_use.*", "mcp.shell.*", "mcp.skills_actions.*")
_TRUTHY = {"1", "true", "yes", "on"}


def _confirm_patterns() -> list[str]:
    """Tool-name globs that are gated (env-overridable)."""
    raw = os.environ.get("GLADOS_CONFIRM_TOOLS")
    if raw is None:
        return list(_DEFAULT_CONFIRM_PATTERNS)
    return [p.strip() for p in raw.split(",") if p.strip()]


def requires_confirmation(tool_name: str) -> bool:
    """True if this tool is gated (needs authorization before execution)."""
    return any(fnmatch.fnmatch(tool_name, pat) for pat in _confirm_patterns())


def _actions_armed() -> bool:
    return os.environ.get("GLADOS_ALLOW_ACTIONS", "").strip().lower() in _TRUTHY


def confirm_tool_call(
    tool_name: str,
    args: dict[str, Any],
    *,
    autonomy_mode: bool,
    prompt_fn: Callable[[str], str] | None = None,
) -> bool:
    """Return True if the tool call is authorized to run (non-blocking, fail-safe)."""
    # Autonomy hard floor: never run desktop/shell actions unsupervised, independent
    # of GLADOS_CONFIRM_TOOLS and of arming.
    if autonomy_mode and any(fnmatch.fnmatch(tool_name, p) for p in _AUTONOMY_HARD_DENY):
        logger.warning("tool_safety: auto-denied '{}' (autonomy hard floor)", tool_name)
        return False
    if not requires_confirmation(tool_name):
        return True
    # Optional explicit confirmation hook (future TUI modal / spoken yes-no, or tests).
    if prompt_fn is not None:
        try:
            answer = prompt_fn(f"Allow {tool_name} with args {args}? [y/N] ")
        except Exception as exc:  # noqa: BLE001 - any prompt failure must fail safe
            logger.warning("tool_safety: prompt_fn failed for '{}' ({}); denying", tool_name, exc)
            return False
        approved = str(answer).strip().lower() in ("y", "yes")
        logger.info("tool_safety: '{}' {} via prompt", tool_name, "approved" if approved else "denied")
        return approved
    # No safe interactive channel from a background thread in this voice/TUI app:
    # require explicit session arming instead of reading stdin (which hangs/contends).
    if _actions_armed():
        logger.warning("tool_safety: allowing gated '{}' (GLADOS_ALLOW_ACTIONS set)", tool_name)
        return True
    logger.warning(
        "tool_safety: denied gated '{}'; set GLADOS_ALLOW_ACTIONS=1 to enable actions", tool_name
    )
    return False
