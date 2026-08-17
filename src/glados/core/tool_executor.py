# --- tool_executor.py ---
import json
import queue
import threading
import time
from typing import Any, Callable

from loguru import logger
from ..mcp import MCPManager
from ..observability import ObservabilityBus, trim_message
from ..tools import all_tools, tool_classes
from . import skills_feedback
from .tool_safety import confirm_tool_call

_ACTION_PREFIXES = ("mcp.shell.", "mcp.computer_use.", "mcp.skills_actions.")  # outcomes worth logging for self-improvement
_SHELL_RESULT_PREFIXES = ("mcp.shell.", "mcp.skills_actions.")  # tools whose result is run_shell JSON

# Callback signature: (event_type: str, tool_name: str) -> None
ToolEventCallback = Callable[[str, str], None]


class _SingleAnswerQueue:
    """Wrap the LLM queue for ONE built-in tool dispatch so only the FIRST ``tool``-role message
    for ``tool_call_id`` is forwarded.

    A built-in that TIMES OUT is abandoned (we can't kill the thread), but it may still finish later
    and enqueue its own result, a SECOND ``tool`` message for the same id, which strict
    OpenAI-compatible endpoints reject and Ollama finds confusing. Routing both the tool's own put
    and the executor's error/timeout put through this guard drops that late duplicate. Everything
    that is not a duplicate ``tool`` answer passes straight through to the base queue unchanged.
    """

    def __init__(self, base: "queue.Queue[dict[str, Any]]", tool_call_id: str) -> None:
        self._base = base
        self._id = tool_call_id
        self._answered = False
        self._lock = threading.Lock()

    def _claim(self, item: Any) -> bool:
        """True if this item may be forwarded; False if it is a duplicate answer to drop."""
        if isinstance(item, dict) and item.get("role") == "tool" and item.get("tool_call_id") == self._id:
            with self._lock:
                if self._answered:
                    return False
                self._answered = True
        return True

    def put(self, item: dict[str, Any]) -> None:
        if self._claim(item):
            self._base.put(item)

    def put_nowait(self, item: dict[str, Any]) -> None:
        if self._claim(item):
            self._base.put_nowait(item)

    def __getattr__(self, name: str) -> Any:  # stay transparent for any other queue method a tool uses
        return getattr(self._base, name)


class ToolExecutor:
    """
    A thread that executes tool calls from the LLM.
    This class is designed to run in a separate thread, continuously checking
    for new tool calls until a shutdown event is set.
    """

    def __init__(
        self,
        llm_queue_priority: queue.Queue[dict[str, Any]],
        llm_queue_autonomy: queue.Queue[dict[str, Any]],
        tool_calls_queue: queue.Queue[dict[str, Any]],
        processing_active_event: threading.Event,  # To check if we should stop streaming
        shutdown_event: threading.Event,
        tool_config: dict[str, Any] | None = None,
        tool_timeout: float = 30.0,
        pause_time: float = 0.05,
        mcp_manager: MCPManager | None = None,
        observability_bus: ObservabilityBus | None = None,
        on_tool_event: ToolEventCallback | None = None,
    ) -> None:
        self.llm_queue_priority = llm_queue_priority
        self.llm_queue_autonomy = llm_queue_autonomy
        self.tool_calls_queue = tool_calls_queue
        self.processing_active_event = processing_active_event
        self.shutdown_event = shutdown_event
        self.tool_config = tool_config or {}
        self.tool_timeout = tool_timeout
        self.pause_time = pause_time
        self.mcp_manager = mcp_manager
        self._observability_bus = observability_bus
        self._on_tool_event = on_tool_event

    def _emit_tool_event(self, event_type: str, tool_name: str) -> None:
        """Emit a tool event to the callback if registered."""
        if self._on_tool_event:
            self._on_tool_event(event_type, tool_name)

    def _fail(
        self,
        target_queue: "queue.Queue[dict[str, Any]]",
        tool: str,
        tool_call_id: str,
        message: str,
        lane: str,
        autonomy_flag: dict[str, Any],
        *,
        event: str | None = "tool_failure",
        kind: str = "error",
        level: str = "error",
        detail: str | None = None,
        args: dict[str, Any] | None = None,
        meta_extra: dict[str, Any] | None = None,
    ) -> None:
        """Report a refused/failed/timed-out tool call in ONE place: log, tool event, feedback record,
        observability, and always a ``tool`` answer so the assistant's tool_call never dangles.

        ``detail=None`` skips the self-improvement record (a timeout is not a command outcome);
        ``event=None`` skips the UI tool event (paths that never had one).
        """
        (logger.warning if level == "warning" else logger.error)(f"ToolExecutor: {message}")
        if event:
            self._emit_tool_event(event, tool)
        if detail is not None and tool.startswith(_ACTION_PREFIXES):
            skills_feedback.record(tool, args or {}, ok=False, detail=detail)
        if self._observability_bus:
            meta: dict[str, Any] = {"tool": tool, "tool_call_id": tool_call_id}
            if meta_extra:
                meta.update(meta_extra)
            self._observability_bus.emit(
                source="tool",
                kind=kind,
                message=trim_message(message),
                level=level,
                meta=meta,
            )
        self._enqueue(
            target_queue,
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": message,
                "type": "function_call_output",
                **autonomy_flag,
            },
            lane=lane,
        )

    def run(self) -> None:
        """
        Starts the main loop for the ToolExecutor thread.

        This method continuously checks the tool calls queue for tool calls to
        run. It processes the tool arguments, sends them to the tool and
        streams the response. The thread will run until the shutdown event is
        set, at which point it will exit gracefully.
        """
        logger.info("ToolExecutor thread started.")
        while not self.shutdown_event.is_set():
            try:
                tool_call = self.tool_calls_queue.get(timeout=self.pause_time)
                if not self.processing_active_event.is_set():  # Check if we were interrupted before starting
                    logger.info("ToolExecutor: Interruption signal active, discarding tool call.")
                    continue

                logger.info(f"ToolExecutor: Received tool call: '{tool_call}'")
                tool = tool_call["function"]["name"]
                logger.success("ToolExecutor: executing {}", tool)
                tool_call_id = tool_call["id"]
                started_at = time.perf_counter()
                autonomy_mode = bool(tool_call.get("autonomy", False))
                autonomy_flag = {"autonomy": True} if autonomy_mode else {}
                base_queue = self.llm_queue_autonomy if autonomy_mode else self.llm_queue_priority
                lane = "autonomy" if autonomy_mode else "priority"
                llm_queue = self._wrap_llm_queue(base_queue) if autonomy_mode else base_queue
                if self._observability_bus:
                    self._observability_bus.emit(
                        source="tool",
                        kind="start",
                        message=tool,
                        meta={"tool_call_id": tool_call_id, "autonomy": autonomy_mode},
                    )

                try:
                    raw_args = tool_call["function"]["arguments"]
                    if isinstance(raw_args, str):
                        args = json.loads(raw_args)
                    else:
                        args = raw_args
                except json.JSONDecodeError:
                    logger.trace(
                        "ToolExecutor: Failed to parse non-JSON tool call args: "
                        f"{tool_call['function']['arguments']}"
                    )
                    args = {}
                # qwen3 sometimes nests the real args one level down: {"arguments": {...}, "function": name}.
                # Unwrap only that exact shape (observed live 2026-07-05) so the tool isn't called with bogus params.
                if (
                    isinstance(args, dict)
                    and isinstance(args.get("arguments"), dict)
                    and set(args) <= {"arguments", "function", "name"}
                ):
                    args = args["arguments"]

                # ALWAYS call confirm_tool_call: it self-checks requires_confirmation, and the autonomy
                # hard-floor must never sit behind it (GLADOS_CONFIRM_TOOLS can empty/narrow that check).
                if not confirm_tool_call(tool, args, autonomy_mode=autonomy_mode):
                    rejection = (
                        f"error: tool '{tool}' is blocked by the safety gate "
                        "(set GLADOS_ALLOW_ACTIONS=1 to enable gated actions; autonomy is always blocked)"
                    )
                    self._fail(
                        llm_queue, tool, tool_call_id, rejection, lane, autonomy_flag,
                        event="tool_rejected", level="warning",
                        detail="gate-denied", args=args, meta_extra={"rejected": True},
                    )
                    continue

                if tool.startswith("mcp."):
                    if not self.mcp_manager:
                        self._fail(
                            llm_queue, tool, tool_call_id,
                            "error: MCP tools are unavailable", lane, autonomy_flag, event=None,
                        )
                        continue
                    try:
                        result = self.mcp_manager.call_tool(tool, args, timeout=self.tool_timeout)
                        if self._observability_bus:
                            elapsed = time.perf_counter() - started_at
                            self._observability_bus.emit(
                                source="tool",
                                kind="finish",
                                message=tool,
                                meta={"tool_call_id": tool_call_id, "elapsed_s": round(elapsed, 3)},
                            )
                        logger.success("ToolExecutor: finished {}", tool)
                        self._emit_tool_event("tool_success", tool)
                        if tool.startswith(_ACTION_PREFIXES):  # MCP "success" can still be a failed shell command
                            ok, rc = (
                                skills_feedback.shell_outcome(str(result))
                                if tool.startswith(_SHELL_RESULT_PREFIXES)
                                else (True, None)
                            )
                            skills_feedback.record(tool, args, ok=ok, returncode=rc)
                        self._enqueue(
                            llm_queue,
                            {
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": str(result),
                                "type": "function_call_output",
                                **autonomy_flag,
                            },
                            lane=lane,
                        )
                    except Exception as e:
                        self._fail(
                            llm_queue, tool, tool_call_id,
                            f"error: MCP tool '{tool}' failed - {e}", lane, autonomy_flag,
                            detail=str(e), args=args,
                        )
                    continue

                if tool in all_tools:
                    # Both our timeout message and the tool's own result funnel through here, so a
                    # timed-out-then-finished built-in can't leave a duplicate answer for this id.
                    guarded_queue = _SingleAnswerQueue(llm_queue, tool_call_id)
                    try:
                        tool_instance = tool_classes.get(tool)(
                            llm_queue=guarded_queue,
                            tool_config=self.tool_config,
                        )
                    except Exception as e:  # a constructor failure must NOT leave the tool_call dangling
                        self._fail(
                            guarded_queue, tool, tool_call_id,
                            f"error: tool '{tool}' failed - {e}", lane, autonomy_flag,
                            detail=str(e), args=args,
                        )
                        continue
                    # Daemon thread, not a pool: concurrent.futures' atexit hook joins workers with
                    # NO timeout, so one hung tool would block interpreter exit forever.
                    tool_error_box: list[Exception | None] = [None]

                    # Bind by default-arg: an ABANDONED worker must not see these rebound by the
                    # next loop iteration.
                    def _invoke_tool(_inst=tool_instance, _id=tool_call_id, _a=args, _box=tool_error_box) -> None:
                        try:
                            _inst.run(_id, _a)
                        except Exception as exc:  # noqa: BLE001 - surfaced to the model by the caller
                            _box[0] = exc

                    worker = threading.Thread(target=_invoke_tool, name=f"tool-{tool}", daemon=True)
                    worker.start()
                    # Wait bounded by tool_timeout but ALSO break on shutdown, so quitting mid-tool
                    # never stalls this thread for the full timeout.
                    deadline = time.monotonic() + self.tool_timeout
                    while worker.is_alive() and not self.shutdown_event.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        worker.join(timeout=min(0.1, remaining))
                    if worker.is_alive():
                        # Abandoned either way; _SingleAnswerQueue drops its late answer.
                        if self.shutdown_event.is_set():
                            logger.info("ToolExecutor: abandoning '{}' (shutting down).", tool)
                        else:
                            self._fail(
                                guarded_queue, tool, tool_call_id,
                                f"error: tool '{tool}' timed out after {self.tool_timeout}s",
                                lane, autonomy_flag,
                                event="tool_timeout", kind="timeout", level="warning",
                            )
                    elif tool_error_box[0] is not None:
                        # Any tool failure must NOT dangle the tool_call (mirrors the mcp.* branch):
                        # e.g. a soundfile decode error in "slow clap" left it with no result.
                        exc = tool_error_box[0]
                        self._fail(
                            guarded_queue, tool, tool_call_id,
                            f"error: tool '{tool}' failed - {exc}", lane, autonomy_flag,
                            detail=str(exc), args=args,
                        )
                    else:
                        if self._observability_bus:
                            elapsed = time.perf_counter() - started_at
                            self._observability_bus.emit(
                                source="tool",
                                kind="finish",
                                message=tool,
                                meta={"tool_call_id": tool_call_id, "elapsed_s": round(elapsed, 3)},
                            )
                        logger.success("ToolExecutor: finished {}", tool)
                        self._emit_tool_event("tool_success", tool)
                else:
                    self._fail(
                        llm_queue, tool, tool_call_id,
                        f"error: no tool named {tool} is available", lane, autonomy_flag, event=None,
                    )
            except queue.Empty:
                pass  # Normal
            except Exception as e:
                logger.exception(f"ToolExecutor: Unexpected error in main run loop: {e}")
                time.sleep(0.1)
        logger.info("ToolExecutor thread finished.")

    @staticmethod
    def _wrap_llm_queue(llm_queue: queue.Queue[dict[str, Any]]) -> "queue.Queue[dict[str, Any]]":
        class AutonomyQueue:
            def __init__(self, base_queue: queue.Queue[dict[str, Any]]) -> None:
                self._base_queue = base_queue

            def put(self, item: dict[str, Any]) -> None:
                if "autonomy" not in item:
                    item = {**item, "autonomy": True}
                if "_enqueued_at" not in item:
                    item = {**item, "_enqueued_at": time.time(), "_lane": "autonomy"}
                if item.get("role") == "tool" and "_allow_tools" not in item:
                    item = {**item, "_allow_tools": False}
                try:
                    self._base_queue.put_nowait(item)
                except queue.Full:
                    logger.warning("ToolExecutor: dropped autonomy tool output because LLM queue is full.")

            def put_nowait(self, item: dict[str, Any]) -> None:
                self.put(item)

        return AutonomyQueue(llm_queue)

    @staticmethod
    def _enqueue(
        target_queue: queue.Queue[dict[str, Any]],
        item: dict[str, Any],
        lane: str = "priority",
    ) -> None:
        try:
            if "_enqueued_at" not in item:
                item = {**item, "_enqueued_at": time.time(), "_lane": lane}
            if item.get("role") == "tool" and "_allow_tools" not in item:
                item = {**item, "_allow_tools": False}
            target_queue.put_nowait(item)
        except queue.Full:
            logger.warning("ToolExecutor: dropped tool output because LLM queue is full.")
