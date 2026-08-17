from __future__ import annotations

# --- llm_processor.py ---
import json
import queue
import re
import threading
import time
from typing import Any, ClassVar
from urllib.parse import urlparse
import uuid

from loguru import logger
from pydantic import HttpUrl  # If HttpUrl is used by config
import requests
from ..autonomy import ConstitutionalState, TaskSlotStore
from .context import ContextBuilder
from .conversation_store import ConversationStore
from .store import Store
from .llm_tracking import InFlightCounter
from ..mcp import MCPManager
from ..observability import ObservabilityBus, trim_message
from ..tools import tool_definitions
from ..vision.vision_state import VisionState

class LanguageModelProcessor:
    """
    A thread that processes text input for a language model, streaming responses and sending them to TTS.

    Supports multiple lanes (priority/autonomy) - instantiate once per lane for parallel inference.
    Handles streaming with thinking tag extraction for reasoning models.
    """

    PUNCTUATION_SET: ClassVar[set[str]] = {".", "!", "?", ":", ";", "?!", "\n", "\n\n"}

    # Standard thinking tags (GLM-4.7, MiniMax M2.7/M3, DeepSeek, etc.)
    THINKING_OPEN_TAGS: ClassVar[tuple[str, ...]] = ("<think>", "<thinking>", "<reasoning>")
    THINKING_CLOSE_TAGS: ClassVar[tuple[str, ...]] = ("</think>", "</thinking>", "</reasoning>")

    # GPT-OSS harmony format channel markers
    HARMONY_CHANNEL_MARKER: ClassVar[str] = "<|channel|>"
    HARMONY_ANALYSIS_CHANNELS: ClassVar[tuple[str, ...]] = ("analysis", "commentary")
    HARMONY_FINAL_CHANNEL: ClassVar[str] = "final"
    HARMONY_MESSAGE_MARKER: ClassVar[str] = "<|message|>"
    HARMONY_END_MARKER: ClassVar[str] = "<|end|>"

    def __init__(
        self,
        llm_input_queue: queue.Queue[dict[str, Any]],
        tool_calls_queue: queue.Queue[dict[str, Any]],
        tts_input_queue: queue.Queue[str],
        conversation_store: ConversationStore,
        completion_url: HttpUrl,
        model_name: str,  # Renamed from 'model' to avoid conflict
        api_key: str | None,
        processing_active_event: threading.Event,  # To check if we should stop streaming
        shutdown_event: threading.Event,
        pause_time: float = 0.05,
        vision_state: VisionState | None = None,
        slot_store: TaskSlotStore | None = None,
        preferences_store: Store[Any] | None = None,
        constitutional_state: ConstitutionalState | None = None,
        context_builder: ContextBuilder | None = None,
        autonomy_system_prompt: str | None = None,
        mcp_manager: MCPManager | None = None,
        observability_bus: ObservabilityBus | None = None,
        extra_headers: dict[str, str] | None = None,
        think: bool | None = None,
        lane: str = "priority",
        inflight_counter: InFlightCounter | None = None,
    ) -> None:
        self.llm_input_queue = llm_input_queue
        self.tool_calls_queue = tool_calls_queue
        self.tts_input_queue = tts_input_queue
        self._conversation_store = conversation_store
        self.completion_url = completion_url
        self.model_name = model_name
        self.api_key = api_key
        self.processing_active_event = processing_active_event
        self.shutdown_event = shutdown_event
        self.pause_time = pause_time
        self.vision_state = vision_state
        self.slot_store = slot_store
        self.preferences_store = preferences_store
        self.constitutional_state = constitutional_state
        self.context_builder = context_builder
        self.autonomy_system_prompt = autonomy_system_prompt
        self.mcp_manager = mcp_manager
        self._observability_bus = observability_bus
        self._lane = lane
        self._inflight_counter = inflight_counter
        self._think = think  # None=leave model default; False=no_think (low latency); True=force reasoning
        self._ollama_mode = self._is_ollama_endpoint()
        self._reply_accumulator: list[str] = []  # spoken sentences of the current turn -> one transcript event at EOS

        self.prompt_headers = {"Content-Type": "application/json"}
        if api_key:
            self.prompt_headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            self.prompt_headers.update(extra_headers)

    def _is_ollama_endpoint(self) -> bool:
        try:
            parsed = urlparse(str(self.completion_url))
        except Exception:
            return False
        path = (parsed.path or "").rstrip("/")
        return path.endswith("/api/chat")

    @staticmethod
    def _ensure_tool_calls_answered(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Insert a synthetic tool result for any assistant tool_call left unanswered.

        A barge-in can abort a turn after the assistant's ``tool_calls`` message is stored but before the
        tool runs, leaving a dangling call with no matching ``tool`` reply — which strict OpenAI-compatible
        endpoints reject and which can confuse the next turn. This is a PURE message-list transform run right
        before the request: it never writes the conversation store and never triggers generation.
        """
        out: list[dict[str, Any]] = []
        for i, message in enumerate(messages):
            out.append(message)
            calls = message.get("tool_calls") if message.get("role") == "assistant" else None
            if not isinstance(calls, list) or not calls:
                continue
            answered: set[str] = set()  # ids replied to before the next assistant/user turn
            for later in messages[i + 1:]:
                role = later.get("role")
                if role == "tool":
                    tid = later.get("tool_call_id")
                    if tid:
                        answered.add(tid)
                elif role in ("assistant", "user"):
                    break
            for call in calls:
                cid = call.get("id") if isinstance(call, dict) else None
                if cid and cid not in answered:
                    out.append({"role": "tool", "tool_call_id": cid, "content": "(interrupted before completion)"})
        return out

    @staticmethod
    def _sanitize_messages_for_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        messages = LanguageModelProcessor._ensure_tool_calls_answered(messages)
        allowed_keys = {"role", "content", "name", "tool_calls", "tool_call_id", "images", "function_call"}
        sanitized: list[dict[str, Any]] = []
        for message in messages:
            cleaned = {key: value for key, value in message.items() if key in allowed_keys}
            tool_calls = cleaned.get("tool_calls")
            if isinstance(tool_calls, list):
                normalized_calls: list[dict[str, Any]] = []
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function", {}) if isinstance(tool_call.get("function"), dict) else {}
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            function["arguments"] = json.loads(arguments)
                        except json.JSONDecodeError:
                            function["arguments"] = {}
                    tool_call["function"] = function
                    normalized_calls.append(tool_call)
                cleaned["tool_calls"] = normalized_calls
            sanitized.append(cleaned)
        return sanitized

    @staticmethod
    def _sanitize_messages_for_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        messages = LanguageModelProcessor._ensure_tool_calls_answered(messages)
        allowed_keys = {"role", "content", "name", "tool_calls", "tool_call_id"}
        sanitized: list[dict[str, Any]] = []
        for message in messages:
            cleaned = {key: value for key, value in message.items() if key in allowed_keys}
            tool_calls = cleaned.get("tool_calls")
            if isinstance(tool_calls, list):
                normalized_calls: list[dict[str, Any]] = []
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function", {}) if isinstance(tool_call.get("function"), dict) else {}
                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        function["arguments"] = json.dumps(arguments or {})
                    tool_call_id = tool_call.get("id") or ""
                    normalized_calls.append(
                        {
                            "id": tool_call_id,
                            "type": tool_call.get("type", "function"),
                            "function": function,
                        }
                    )
                cleaned["tool_calls"] = normalized_calls
            sanitized.append(cleaned)
        return sanitized

    def _clean_raw_bytes(self, line: bytes) -> dict[str, str] | None:
        """
        Clean and parse a raw byte line from the LLM response.
        Handles both OpenAI and Ollama formats, returning a dictionary or None if parsing fails.

        Args:
            line (bytes): The raw byte line from the LLM response.
        Returns:
            dict[str, str] | None: Parsed JSON dictionary or None if parsing fails.
        """
        try:
            # Handle OpenAI format
            if line.startswith(b"data: "):
                json_str = line.decode("utf-8")[6:]
                if json_str.strip() == "[DONE]":  # Handle OpenAI [DONE] marker
                    return {"done_marker": "True"}
                parsed_json: dict[str, Any] = json.loads(json_str)
                return parsed_json
            # Handle Ollama format
            else:
                parsed_json = json.loads(line.decode("utf-8"))
                if isinstance(parsed_json, dict):
                    return parsed_json
                return None
        except json.JSONDecodeError:
            # If it's not JSON, it might be Ollama's final summary object which isn't part of the stream
            # Or just noise.
            logger.trace(
                "LLM Processor: Failed to parse non-JSON server response line: "
                f"{line[:100].decode('utf-8', errors='replace')}"
            )  # Log only a part
            return None
        except Exception as e:
            logger.warning(
                "LLM Processor: Failed to parse server response: "
                f"{e} for line: {line[:100].decode('utf-8', errors='replace')}"
            )
            return None

    def _process_chunk(self, line: dict[str, Any]) -> str | list[dict[str, Any]] | None:
        # Copy from Glados._process_chunk
        if not line or not isinstance(line, dict):
            return None
        try:
            # Handle OpenAI format
            if line.get("done_marker"):  # Handle [DONE] marker
                return None
            elif "choices" in line:  # OpenAI format
                delta = line.get("choices", [{}])[0].get("delta", {})
                tool_calls = delta.get("tool_calls")
                if tool_calls:
                    return tool_calls

                content = delta.get("content")
                return str(content) if content else None
            # Handle Ollama format
            else:
                message = line.get("message", {})
                tool_calls = message.get("tool_calls")
                if tool_calls:
                    return tool_calls

                content = message.get("content")
                return content if content else None
        except Exception as e:
            logger.error(f"LLM Processor: Error processing chunk: {e}, chunk: {line}")
            return None

    def _process_tool_chunks(
        self,
        tool_calls_buffer: list[dict[str, Any]],
        tool_chunks: list[dict[str, Any]],
    ) -> None:
        """
        Extract tool call data from chunks to populate final tool_calls_buffer.

        Args:
            tool_calls_buffer: List of tool calls to be run.
            tool_chunks: List of streaming tool call data split into chunks.
        """
        for tool_chunk in tool_chunks:
            tool_chunk_index = tool_chunk.get("index", 0)
            try:
                tool_chunk_index = int(tool_chunk_index)
            except (TypeError, ValueError):
                tool_chunk_index = 0
            if tool_chunk_index < 0:
                tool_chunk_index = 0
            while tool_chunk_index >= len(tool_calls_buffer):
                # we have a new tool call to initialize
                tool_calls_buffer.append(
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                )

            tool_call = tool_calls_buffer[tool_chunk_index]

            tool_id = tool_chunk.get("id")
            name = tool_chunk.get("function", {}).get("name")
            arguments = tool_chunk.get("function", {}).get("arguments")

            if tool_id:
                tool_call["id"] += tool_id
            if name:
                tool_call["function"]["name"] += name
            if arguments:
                if isinstance(arguments, str):
                    # OpenAI format
                    tool_call["function"]["arguments"] += arguments
                else:
                    # Ollama format
                    tool_call["function"]["arguments"] = arguments

    @staticmethod
    def _sanitize_tool_name(name: str) -> str:
        return "".join(ch for ch in name.casefold() if ch.isalnum())

    def _normalize_tool_name(self, name: str, known_names: set[str]) -> str:
        if not name or not known_names:
            return name
        if name in known_names:
            return name
        for candidate in known_names:
            if candidate.casefold() == name.casefold():
                return candidate
        if name.startswith("mcp."):
            candidates = [candidate for candidate in known_names if candidate.startswith("mcp.")]
        else:
            candidates = [candidate for candidate in known_names if not candidate.startswith("mcp.")]
        if not candidates:
            candidates = list(known_names)
        normalized = self._sanitize_tool_name(name)
        if normalized:
            normalized_candidates = [(candidate, self._sanitize_tool_name(candidate)) for candidate in candidates]
            exact = [candidate for candidate, norm in normalized_candidates if norm == normalized]
            if len(exact) == 1:
                return exact[0]
            substring = [candidate for candidate, norm in normalized_candidates if norm and norm in normalized]
            if substring:
                return max(substring, key=len)
            superstrings = [candidate for candidate, norm in normalized_candidates if normalized and normalized in norm]
            if len(superstrings) == 1:
                return superstrings[0]
        return name

    def _normalize_tool_calls(self, tool_calls: list[dict[str, Any]], tool_names: set[str]) -> None:
        for tool_call in tool_calls:
            tool_name = tool_call.get("function", {}).get("name")
            if not tool_name:
                continue
            tool_call["function"]["name"] = self._normalize_tool_name(tool_name, tool_names)

    def _process_tool_call(
        self,
        tool_calls: list[dict[str, Any]],
        autonomy_mode: bool,
        tool_names: set[str],
    ) -> None:
        """
        Add tool calls to conversation history and send each to the tool calls queue.

        Args:
            tool_calls: List of tool calls to be run.
        """
        self._normalize_tool_calls(tool_calls, tool_names)
        for tool_call in tool_calls:
            tool_call.setdefault("type", "function")
            if not tool_call.get("id"):
                tool_call["id"] = f"toolcall_{uuid.uuid4().hex}"
        self._conversation_store.append(
            {"role": "assistant", "index": 0, "tool_calls": tool_calls, "finish_reason": "tool_calls"}
        )
        tool_labels = [call.get("function", {}).get("name", "unknown") for call in tool_calls]
        tool_label_text = ", ".join(tool_labels)
        suffix = " (autonomy)" if autonomy_mode else ""
        logger.success("LLM tool calls queued: {}{}", tool_label_text, suffix)
        for tool_call in tool_calls:
            if autonomy_mode:
                tool_call["autonomy"] = True
            logger.debug("LLM Processor: Sending to tool calls queue: '{}'", tool_call)
            self.tool_calls_queue.put(tool_call)
        if self._observability_bus:
            tool_names = [call.get("function", {}).get("name", "unknown") for call in tool_calls]
            self._observability_bus.emit(
                source="llm",
                kind="tool_calls",
                message=",".join(tool_names),
                meta={"count": len(tool_names), "autonomy": autonomy_mode},
            )

    def _flush_streamed_sentences(self, sentence_buffer: list[str]) -> list[str]:
        """Send every COMPLETE sentence in the buffer to TTS; return the unfinished tail.

        The LLM stream is chunked unpredictably — a word, a clause, a whole sentence, or a lone
        punctuation mark can each arrive as one chunk. The old check only flushed when an entire
        chunk *was* punctuation, so for token-by-token backends the whole reply buffered until the
        stream ended (no streaming TTS). Instead, scan the accumulated text and emit each finished
        sentence as soon as its terminator is confirmed, so the first audio plays almost immediately.

        A terminator (``. ! ? : ;``) ends a sentence when it is followed by whitespace (the next
        word has begun), except a ``.`` between digits (a decimal like ``3.14``). At the very end of
        the buffer-so-far only the unambiguous ``! ?`` and newlines flush, so a trailing ``.`` waits
        one more chunk rather than mis-splitting a decimal/time/abbreviation in progress.
        """
        text = "".join(sentence_buffer)
        flushed_to = 0
        n = len(text)
        for i, ch in enumerate(text):
            boundary = False
            if ch == "\n":
                boundary = True
            elif ch in ".!?:;":
                nxt = text[i + 1] if i + 1 < n else ""
                prev = text[i - 1] if i > 0 else ""
                if nxt.isspace():
                    boundary = not (ch == "." and prev.isdigit())
                elif nxt == "":
                    boundary = ch in "!?"
            if boundary:
                segment = text[flushed_to : i + 1]
                if segment.strip():
                    self._process_sentence_for_tts([segment])
                flushed_to = i + 1
        remainder = text[flushed_to:]
        # Safety valve for a long run with no sentence terminator, which would buffer until
        # end-of-stream: flush to the last space so TTS can start, keeping the partial word.
        if len(remainder) > 1000:
            cut = remainder.rstrip().rfind(" ")
            if cut <= 0:
                cut = len(remainder)
            head, remainder = remainder[:cut], remainder[cut:]
            if head.strip():
                self._process_sentence_for_tts([head])
        return [remainder] if remainder.strip() else []

    def _process_sentence_for_tts(self, current_sentence_parts: list[str]) -> None:
        """
        Process the current sentence parts and send the complete sentence to the TTS queue.
        Cleans up the sentence by removing unwanted characters and formatting it for TTS.
        Args:
            current_sentence_parts (list[str]): List of sentence parts to be processed.
        """
        sentence = "".join(current_sentence_parts)
        sentence = re.sub(r"\*.*?\*|\(.*?\)", "", sentence)
        sentence = sentence.replace("\n\n", ". ").replace("\n", ". ").replace("  ", " ").replace(":", " ")

        # Robustness: a weak model can emit a tool call as TEXT ({"name":...,"arguments":{...}} or an mcp.* blob)
        # instead of a real tool_call. Never speak raw JSON — drop the fragment (the reliable path is a real call).
        low = sentence.lower()
        looks_toolish = (
            '"arguments"' in low
            or ('"name"' in low and ("{" in sentence or "mcp." in low))
            or ("mcp." in low and ('"' in sentence or "{" in sentence or "}" in sentence))
        )
        if not re.sub(r'[\s{}\[\]",:]+', "", sentence):  # nothing but JSON punctuation -> don't speak
            return
        if looks_toolish and ('"' in sentence or "{" in sentence or "}" in sentence):
            logger.warning("LLM Processor: not speaking tool-call/JSON fragment: {}", sentence[:80])
            return

        if sentence and sentence != ".":  # Avoid sending just a period
            logger.info(f"LLM Processor: Sending to TTS queue: '{sentence}'")
            self._reply_accumulator.append(sentence)  # for the single per-turn transcript event (emitted at EOS)
            self.tts_input_queue.put(sentence)

    def _extract_thinking(
        self,
        chunk: str,
        in_thinking: bool,
        thinking_buffer: list[str],
        harmony_mode: bool = False,
    ) -> tuple[str, bool, bool]:
        """
        Extract thinking tags from streaming chunk, returning only speakable content.

        Supports two formats:
        1. Standard: <think>...</think> (GLM-4.7, MiniMax M2.7/M3, DeepSeek)
        2. Harmony: <|channel|>analysis vs <|channel|>final (GPT-OSS-120B)

        Args:
            chunk: The current text chunk from the stream
            in_thinking: Whether we're currently inside a thinking block
            thinking_buffer: Buffer to accumulate thinking content (for logging)
            harmony_mode: Whether we've detected harmony format

        Returns:
            Tuple of (speakable_content, still_in_thinking, is_harmony_mode)
        """
        # Auto-detect harmony format
        if not harmony_mode and self.HARMONY_CHANNEL_MARKER in chunk:
            harmony_mode = True

        if harmony_mode:
            return self._extract_thinking_harmony(chunk, in_thinking, thinking_buffer)

        return (*self._extract_thinking_standard(chunk, in_thinking, thinking_buffer), False)

    def _extract_thinking_standard(
        self,
        chunk: str,
        in_thinking: bool,
        thinking_buffer: list[str],
    ) -> tuple[str, bool]:
        """Extract thinking using standard <think>...</think> tags."""
        result: list[str] = []
        i = 0
        text = chunk

        while i < len(text):
            if in_thinking:
                # Look for closing tag
                close_idx = -1
                close_tag = ""
                for tag in self.THINKING_CLOSE_TAGS:
                    idx = text.find(tag, i)
                    if idx != -1 and (close_idx == -1 or idx < close_idx):
                        close_idx = idx
                        close_tag = tag

                if close_idx != -1:
                    # Found closing tag - buffer thinking content, exit thinking mode
                    thinking_buffer.append(text[i:close_idx])
                    i = close_idx + len(close_tag)
                    in_thinking = False
                    # Log thinking content for debugging
                    if thinking_buffer:
                        thinking_content = "".join(thinking_buffer)
                        if thinking_content.strip():
                            logger.debug(f"LLM thinking: {thinking_content[:200]}...")
                        thinking_buffer.clear()
                else:
                    # Still in thinking block, buffer everything
                    thinking_buffer.append(text[i:])
                    break
            else:
                # Look for opening tag
                open_idx = -1
                open_tag = ""
                for tag in self.THINKING_OPEN_TAGS:
                    idx = text.find(tag, i)
                    if idx != -1 and (open_idx == -1 or idx < open_idx):
                        open_idx = idx
                        open_tag = tag

                if open_idx != -1:
                    # Found opening tag - emit content before it, enter thinking mode
                    result.append(text[i:open_idx])
                    i = open_idx + len(open_tag)
                    in_thinking = True
                else:
                    # No thinking tag, emit everything
                    result.append(text[i:])
                    break

        return "".join(result), in_thinking

    def _extract_thinking_harmony(
        self,
        chunk: str,
        in_thinking: bool,
        thinking_buffer: list[str],
    ) -> tuple[str, bool, bool]:
        """
        Extract thinking using GPT-OSS harmony format.

        Format: <|channel|>analysis<|message|>... for thinking
                <|channel|>final<|message|>... for output
        """
        result: list[str] = []
        text = chunk
        i = 0

        while i < len(text):
            # Look for channel marker
            channel_idx = text.find(self.HARMONY_CHANNEL_MARKER, i)

            if channel_idx == -1:
                # No more channel markers
                if in_thinking:
                    thinking_buffer.append(text[i:])
                else:
                    # Strip harmony end markers from output
                    content = text[i:].replace(self.HARMONY_END_MARKER, "")
                    result.append(content)
                break

            # Found a channel marker - check what type
            if not in_thinking:
                # Emit content before the marker
                result.append(text[i:channel_idx])

            # Find channel name (between <|channel|> and <|message|>)
            channel_start = channel_idx + len(self.HARMONY_CHANNEL_MARKER)
            message_idx = text.find(self.HARMONY_MESSAGE_MARKER, channel_start)

            if message_idx == -1:
                # Incomplete marker, wait for more data
                if in_thinking:
                    thinking_buffer.append(text[i:])
                break

            channel_name = text[channel_start:message_idx].strip().split()[0]  # e.g., "final" or "analysis"
            i = message_idx + len(self.HARMONY_MESSAGE_MARKER)

            if channel_name == self.HARMONY_FINAL_CHANNEL:
                # Switch to output mode
                if in_thinking and thinking_buffer:
                    thinking_content = "".join(thinking_buffer)
                    if thinking_content.strip():
                        logger.debug(f"LLM thinking (harmony): {thinking_content[:200]}...")
                    thinking_buffer.clear()
                in_thinking = False
            elif channel_name in self.HARMONY_ANALYSIS_CHANNELS:
                # Switch to thinking mode
                in_thinking = True

        return "".join(result), in_thinking, True

    def _build_messages(self, autonomy_mode: bool) -> list[dict[str, Any]]:
        """Build the message list for the LLM request, injecting context from registered sources."""
        messages = self._conversation_store.snapshot()
        extra_messages: list[dict[str, Any]] = []

        if autonomy_mode and self.autonomy_system_prompt:
            extra_messages.append({"role": "system", "content": self.autonomy_system_prompt})

        # Use ContextBuilder if available (new pattern)
        if self.context_builder:
            extra_messages.extend(self.context_builder.build_system_messages())
        else:
            # Fallback to old pattern for backward compatibility
            if self.slot_store:
                slot_message = self.slot_store.as_message()
                if slot_message:
                    extra_messages.append(slot_message)
            if self.preferences_store:
                prefs_prompt = self.preferences_store.as_prompt()
                if prefs_prompt:
                    extra_messages.append({"role": "system", "content": prefs_prompt})
            if self.constitutional_state:
                modifiers_prompt = self.constitutional_state.get_modifiers_prompt()
                if modifiers_prompt:
                    extra_messages.append({"role": "system", "content": modifiers_prompt})

        # MCP context is handled separately (returns list of messages)
        if self.mcp_manager:
            try:
                extra_messages.extend(self.mcp_manager.get_context_messages(block=False))
            except Exception as e:
                logger.warning(f"LLM Processor: Failed to load MCP context messages: {e}")

        # Vision context is handled separately (has special formatting)
        if self.vision_state:
            vision_message = self.vision_state.as_message()
            if vision_message:
                extra_messages.append(vision_message)

        if extra_messages:
            insert_index = 0
            while insert_index < len(messages) and messages[insert_index].get("role") == "system":
                insert_index += 1
            for offset, message in enumerate(extra_messages):
                messages.insert(insert_index + offset, message)

        return messages

    def _build_tools(self, autonomy_mode: bool) -> list[dict[str, Any]]:
        """Return the tool list for the LLM request."""
        tools = list(tool_definitions)
        if self.vision_state is None:
            tools = [tool for tool in tools if tool.get("function", {}).get("name") != "vision_look"]
        if not autonomy_mode:
            tools = [
                tool
                for tool in tools
                if tool.get("function", {}).get("name") not in {"speak", "do_nothing"}
            ]
            tools = [tool for tool in tools if tool.get("function", {}).get("name") != "vision_look"]
        if self.mcp_manager:
            try:
                tools.extend(self.mcp_manager.get_tool_definitions())
            except Exception as e:
                logger.warning(f"LLM Processor: Failed to load MCP tool definitions: {e}")
        return tools

    def run(self) -> None:
        """
        Starts the main loop for the LanguageModelProcessor thread.

        This method continuously checks the LLM input queue for text to process.
        It processes the text, sends it to the LLM API, and streams the response.
        It handles conversation history, manages streaming responses, and sends synthesized sentences
        to a TTS queue. The thread will run until the shutdown event is set, at which point it will exit gracefully.
        """
        logger.info("LanguageModelProcessor thread started.")
        while not self.shutdown_event.is_set():
            try:
                llm_input = self.llm_input_queue.get(timeout=self.pause_time)
                # always process a dequeued request (the old discard-on-clear-flag dropped replies);
                # mark the turn active — listener clears it only on a real barge-in.
                self.processing_active_event.set()

                inflight_guard = False
                enqueued_at = llm_input.get("_enqueued_at")
                wait_s = None
                if isinstance(enqueued_at, (int, float)):
                    wait_s = time.time() - float(enqueued_at)
                queue_depth = None
                try:
                    queue_depth = self.llm_input_queue.qsize()
                except NotImplementedError:
                    queue_depth = None
                autonomy_mode = bool(llm_input.get("autonomy", False))
                llm_message = {
                    key: value
                    for key, value in llm_input.items()
                    if key != "autonomy" and not key.startswith("_")
                }
                logger.info(f"LLM Processor: Received input for LLM: '{llm_message}'")
                if self._observability_bus:
                    message_text = llm_message.get("content", "")
                    self._observability_bus.emit(
                        source="llm",
                        kind="request",
                        message=trim_message(str(message_text)),
                        meta={"autonomy": autonomy_mode, "lane": self._lane},
                    )
                    if wait_s is not None:
                        self._observability_bus.emit(
                            source="llm",
                            kind="queue",
                            message=self._lane,
                            meta={
                                "lane": self._lane,
                                "wait_s": round(wait_s, 3),
                                "queue_depth": queue_depth,
                            },
                        )
                if self._inflight_counter and self._lane == "autonomy":
                    self._inflight_counter.increment()
                    inflight_guard = True
                else:
                    inflight_guard = False
                # A wake-from-sleep transition rides in on the same queue item, so it belongs to
                # THIS turn. Kept as its own system message so the user's words stay unmodified.
                wake_note = llm_input.get("_wake_note")
                if wake_note:
                    self._conversation_store.append({"role": "system", "content": str(wake_note)})

                # Barge-in resume: this fragment interrupted a turn that was never answered, so it
                # is the rest of that thought — fold it in rather than stacking a competing turn.
                merged_into_previous = False
                if llm_input.get("_continuation") and llm_message.get("role") == "user":
                    history = self._conversation_store.snapshot()
                    if history and history[-1].get("role") == "user":
                        addition = str(llm_message.get("content") or "").strip()

                        def _merge_turn(msg: dict[str, Any], addition: str = addition) -> dict[str, Any]:
                            merged = dict(msg)
                            prior = str(merged.get("content") or "").rstrip()
                            merged["content"] = f"{prior} {addition}".strip() if prior else addition
                            return merged

                        merged_into_previous = self._conversation_store.modify_message(
                            len(history) - 1, _merge_turn
                        )
                        if merged_into_previous:
                            logger.info(
                                f"LLM Processor: merged barge-in continuation into the unanswered "
                                f"prior turn: '{addition}'"
                            )
                if not merged_into_previous:
                    self._conversation_store.append(llm_message)

                allow_tools = bool(llm_input.get("_allow_tools", True))
                # Skills are native function-calling tools, so there is NO per-turn retrieval, command
                # injection or tool-narrowing — that misfired on ambient words. The tool set IS the menu.
                tools = self._build_tools(autonomy_mode) if allow_tools else []
                tool_names = {
                    tool.get("function", {}).get("name", "")
                    for tool in tools
                    if tool.get("function", {}).get("name")
                }
                base_messages = self._build_messages(autonomy_mode)
                data = {
                    "model": self.model_name,
                    "stream": True,
                    # Add other parameters like temperature, max_tokens if needed from config
                }
                if allow_tools and tools:
                    data["tools"] = tools

                tool_calls_buffer: list[dict[str, Any]] = []
                sentence_buffer: list[str] = []
                thinking_buffer: list[str] = []
                in_thinking = False
                harmony_mode = False
                try:
                    http_error_detail: tuple[str | int, str] | None = None
                    request_urls = [str(self.completion_url)]
                    if self._ollama_mode:
                        fallback_url = str(self.completion_url).replace("/api/chat", "/v1/chat/completions")
                        if fallback_url != request_urls[0]:
                            request_urls.append(fallback_url)

                    for attempt, request_url in enumerate(request_urls):
                        # Each retry endpoint is a fresh stream: reset accumulators so a partial
                        # first attempt can't bleed buffered text / thinking-state into the fallback.
                        tool_calls_buffer = []
                        sentence_buffer = []
                        thinking_buffer = []
                        in_thinking = False
                        harmony_mode = False
                        native_ollama = self._ollama_mode and not request_url.endswith("/v1/chat/completions")
                        # Only the native Ollama /api/chat accepts a top-level `think` boolean;
                        # the OpenAI-compatible fallback has no such field, so strip it there.
                        if native_ollama and self._think is not None:
                            data["think"] = self._think
                        else:
                            data.pop("think", None)
                        if request_url.endswith("/v1/chat/completions"):
                            data["messages"] = self._sanitize_messages_for_openai(base_messages)
                        elif self._ollama_mode:
                            data["messages"] = self._sanitize_messages_for_ollama(base_messages)
                        else:
                            data["messages"] = self._sanitize_messages_for_openai(base_messages)
                        try:
                            with requests.post(
                                request_url,
                                headers=self.prompt_headers,
                                json=data,
                                stream=True,
                                timeout=30,  # Add a timeout for the request itself
                            ) as response:
                                if response.status_code >= 400:
                                    response_text = response.text.strip()
                                    http_error_detail = (response.status_code, response_text)
                                    logger.error(
                                        "LLM Processor: HTTP error {} from LLM service: {}",
                                        response.status_code,
                                        response_text or response.reason,
                                    )
                                    logger.error(
                                        "LLM Processor: LLM payload (truncated): {}",
                                        json.dumps(data)[:1200],
                                    )
                                    response.raise_for_status()
                                logger.debug("LLM Processor: Request to LLM successful, processing stream...")
                                for line in response.iter_lines():
                                    if not self.processing_active_event.is_set() or self.shutdown_event.is_set():
                                        logger.info("LLM Processor: Interruption or shutdown detected during LLM stream.")
                                        break  # Stop processing stream

                                    if line:
                                        cleaned_line_data = self._clean_raw_bytes(line)
                                        if cleaned_line_data:
                                            chunk = self._process_chunk(cleaned_line_data)
                                            if chunk:
                                                if isinstance(chunk, list):
                                                    self._process_tool_chunks(tool_calls_buffer, chunk)
                                                elif not autonomy_mode:
                                                    # Interactive lane only: in autonomy mode agents
                                                    # speak via tool calls, so raw text skips TTS.
                                                    speakable, in_thinking, harmony_mode = self._extract_thinking(
                                                        chunk, in_thinking, thinking_buffer, harmony_mode
                                                    )
                                                    if speakable:
                                                        sentence_buffer.append(speakable)
                                                        # Emit each COMPLETE sentence as it forms so
                                                        # TTS starts a sentence in, not at the end.
                                                        sentence_buffer = self._flush_streamed_sentences(
                                                            sentence_buffer
                                                        )
                                            elif cleaned_line_data.get("done_marker"):
                                                break
                                            elif cleaned_line_data.get("done") and cleaned_line_data.get("response") == "":
                                                break

                                if self.processing_active_event.is_set() and tool_calls_buffer:
                                    self._process_tool_call(tool_calls_buffer, autonomy_mode, tool_names)
                                elif self.processing_active_event.is_set() and sentence_buffer:
                                    self._process_sentence_for_tts(sentence_buffer)
                            break
                        except requests.exceptions.HTTPError as e:
                            response = getattr(e, "response", None)
                            status_code = response.status_code if response is not None else "unknown"
                            response_text = ""
                            if response is not None:
                                response_text = response.text.strip()
                            http_error_detail = (status_code, response_text or str(e))
                            if attempt < len(request_urls) - 1:
                                logger.warning(
                                    "LLM Processor: Retrying with fallback endpoint {}",
                                    request_urls[attempt + 1],
                                )
                                continue
                            raise

                except requests.exceptions.ConnectionError as e:
                    logger.error(f"LLM Processor: Connection error to LLM service: {e}")
                    self.tts_input_queue.put(
                        "I'm unable to connect to my thinking module. Please check the LLM service connection."
                    )
                except requests.exceptions.Timeout as e:
                    logger.error(f"LLM Processor: Request to LLM timed out: {e}")
                    self.tts_input_queue.put("My brain seems to be taking too long to respond. It might be overloaded.")
                except requests.exceptions.HTTPError as e:
                    if http_error_detail:
                        status_code, detail = http_error_detail
                        logger.error(f"LLM Processor: HTTP error {status_code} from LLM service: {detail}")
                        self.tts_input_queue.put(
                            f"I received an error from my thinking module. HTTP status {status_code}."
                        )
                    else:
                        status_code = (
                            e.response.status_code
                            if hasattr(e, "response") and hasattr(e.response, "status_code")
                            else "unknown"
                        )
                        logger.error(f"LLM Processor: HTTP error {status_code} from LLM service: {e}")
                        self.tts_input_queue.put(
                            f"I received an error from my thinking module. HTTP status {status_code}."
                        )
                except requests.exceptions.RequestException as e:
                    logger.error(f"LLM Processor: Request to LLM failed: {e}")
                    self.tts_input_queue.put("Sorry, I encountered an error trying to reach my brain.")
                except Exception as e:
                    logger.exception(f"LLM Processor: Unexpected error during LLM request/streaming: {e}")
                    self.tts_input_queue.put("I'm having a little trouble thinking right now.")
                finally:
                    if self.processing_active_event.is_set():  # Only send EOS if not interrupted
                        logger.debug("LLM Processor: Sending EOS token to TTS queue.")
                        self.tts_input_queue.put("<EOS>")
                        # ONE transcript event per turn (the full spoken reply) so the overlay shows a single
                        # bubble instead of one per streamed sentence.
                        if not autonomy_mode and self._observability_bus and self._reply_accumulator:
                            full_reply = " ".join(self._reply_accumulator).strip()
                            if full_reply:
                                self._observability_bus.emit(source="llm", kind="reply", message=full_reply)
                        self._reply_accumulator = []
                    else:
                        logger.info("LLM Processor: Interrupted, not sending EOS from LLM processing.")
                        self._reply_accumulator = []
                        # The AudioPlayer will handle clearing its state.
                        # If an EOS was already sent by TTS from a *previous* partial sentence,
                        # this could lead to an early clear of currently_speaking.
                        # The `processing_active_event` is key to synchronize.
                    if inflight_guard:
                        self._inflight_counter.decrement()

            except queue.Empty:
                pass  # Normal
            except Exception as e:
                logger.exception(f"LLM Processor: Unexpected error in main run loop: {e}")
                time.sleep(0.1)
        logger.info("LanguageModelProcessor thread finished.")
