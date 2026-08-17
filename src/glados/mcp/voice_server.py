"""Live TTS voice switcher exposed over MCP (AI_Linux addition).

Lets the assistant change its own speaking voice on request ("use a female voice").
It does NOT touch the audio device — it drops the requested voice into the overlay
bridge's ``voice.json`` control file in ``$XDG_RUNTIME_DIR/ai-linux/``; the running engine
reads it (~0.1 s) and applies it to the live synthesizer, so the next reply uses the new
voice with no restart. Ungated (changing one's own voice is harmless), and a no-op if the
engine's overlay bridge isn't running (it is, by default, under ``ai-linux``).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re

from loguru import logger
from mcp.server.fastmcp import FastMCP

logger.remove()
logging.getLogger().setLevel(logging.CRITICAL)

mcp = FastMCP("voice")

# SuperTonic-3 speakers (the default TTS). The Kokoro fallback instead uses names that
# contain an underscore (e.g. am_michael / bm_george / af_bella), which we pass through.
_VOICES: dict[str, str] = {
    "M1": "male",
    "M3": "male",
    "M4": "male",
    "M5": "male",
    "F1": "female",
    "F2": "female",
    "F3": "female",
    "F4": "female",
    "F5": "female",
}

# Kokoro (fallback engine) ids look like ``am_michael`` — match that shape so real ids pass and typos
# are rejected up front. The engine bridge is still the final validator.
_KOKORO_RE = re.compile(r"[a-z]{2}_[a-z]+")


def _runtime_dir() -> Path:
    """Same per-user tmpfs dir the overlay bridge polls (kept in sync with overlay/bridge.py)."""
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(os.path.expanduser("~"), ".cache")
    directory = Path(base) / "ai-linux"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)  # user-only control channel
    return directory


@mcp.tool()
def list_voices() -> str:
    """List the assistant's selectable TTS voices.

    Returns JSON mapping voice id -> gender. Use ``set_voice`` with an id such as ``M1``
    (male) or ``F3`` (female).
    """
    return json.dumps({"voices": _VOICES})


@mcp.tool()
def set_voice(voice: str) -> str:
    """Change the assistant's own speaking voice for the rest of the conversation.

    Args:
        voice: a SuperTonic voice id — male ``M1``/``M3``/``M4``/``M5`` or female
            ``F1``/``F2``/``F3``/``F4``/``F5`` (e.g. pass ``"F3"`` for a female voice).

    Takes effect on the next spoken reply; no restart needed. Returns JSON {ok, voice}.
    """
    voice = (voice or "").strip()
    if not voice:
        return json.dumps({"error": "no voice id given", "available": _VOICES})
    canon = voice.upper()
    if canon in _VOICES:
        voice = canon
    elif _KOKORO_RE.fullmatch(voice.lower()):  # a well-formed Kokoro id (e.g. am_michael) -> pass through
        voice = voice.lower()
    else:  # neither a known SuperTonic id nor a valid Kokoro name
        return json.dumps(
            {"error": f"unknown voice '{voice}' (use M1/F3, or a Kokoro id like am_michael)", "available": _VOICES}
        )
    try:
        directory = _runtime_dir()
        tmp = directory / "voice.json.tmp"
        tmp.write_text(json.dumps({"voice": voice}))
        os.replace(tmp, directory / "voice.json")
    except Exception as exc:  # noqa: BLE001 - report a write failure to the model
        return json.dumps({"error": f"could not request voice change: {exc}"})
    return json.dumps({"ok": True, "voice": voice, "gender": _VOICES.get(voice, "custom")})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
