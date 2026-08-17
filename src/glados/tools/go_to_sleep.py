"""Built-in tool: put the ASSISTANT to sleep (dormant), never the OS.

The engine is already dormant between wake sessions (the mic stays on but it ignores everything except the
wake word). So "go to sleep" just ends the current wake conversation window: the assistant stops responding
until the user says the wake word ("computer") again. Voice re-wake still works; nothing on the computer is
suspended, locked, or logged out. In always-listening mode (no wake word) there is no session to end, so we
release the mic instead.
"""

import queue
from typing import Any

from loguru import logger

tool_definition = {
    "type": "function",
    "function": {
        "name": "go_to_sleep",
        "description": (
            "Put the ASSISTANT to sleep: stop responding until the user says the wake word 'computer' again. "
            "Use this for 'goodbye', 'go to sleep', 'that's all', or 'stop listening'. It affects ONLY the "
            "assistant, it never suspends, sleeps, locks, logs out, or turns off the computer."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


class GoToSleep:
    def __init__(
        self,
        llm_queue: queue.Queue[dict[str, Any]],
        tool_config: dict[str, Any] | None = None,
    ) -> None:
        self.llm_queue = llm_queue
        tool_config = tool_config or {}
        self._speech_listener = tool_config.get("speech_listener")
        self._audio_io = tool_config.get("audio_io")

    def run(self, tool_call_id: str, call_args: dict[str, Any]) -> None:
        result = "The assistant is now asleep; say 'computer' to wake it."
        sl = self._speech_listener
        ended_session = False
        if sl is not None and getattr(sl, "wake_word", None):
            try:
                sl._wake_session_until = 0.0  # end the wake window -> dormant until the wake word is heard
                ended_session = True
            except Exception as exc:  # noqa: BLE001 - sleeping must never break the turn
                logger.warning("go_to_sleep: could not end wake session: {}", exc)
        if not ended_session and self._audio_io is not None:
            # No wake word (always-listening): release the mic so it stops responding.
            try:
                self._audio_io.stop_listening()
                # No wake word to re-arm, so tell the user the one recovery path (the overlay orb).
                result = "I've stopped listening, click the assistant orb when you want me again."
            except Exception as exc:  # noqa: BLE001
                logger.warning("go_to_sleep: could not stop listening: {}", exc)
        self.llm_queue.put(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
                "type": "function_call_output",
            }
        )
