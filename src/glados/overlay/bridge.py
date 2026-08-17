"""AI Linux overlay bridge — a decoupled state/control channel between the engine and a
GNOME Shell extension (or any UI).

The engine writes its live state + transcript to ``state.json``; the UI writes
listening-mode / mic commands to ``control.json``. Both live in
``$XDG_RUNTIME_DIR/ai-linux/`` (tmpfs, per-user) so neither side ever blocks the other.

Enabled with env ``GLADOS_OVERLAY=1`` (the ``ai-linux`` launcher sets it). Modes:

* ``always`` — continuous listening (holds the mic).
* ``wake``   — listen for a wake word; acts only on it (still holds the mic).
* ``click``  — mic released; an ``activate`` command listens for one turn, then releases.

A ``mute`` action releases the mic; ``unmute`` reacquires. Releasing the mic
(``audio_io.stop_listening()``) frees the sound card for other applications — the whole
point of the click / mute controls.

state.json   : {"state": idle|listening|thinking|speaking|muted|off, "mode": always|wake|click,
                "you": "<last user text>", "assistant": "<last reply>", "you_ts": <ms>, "reply_ts": <ms>,
                "session": <bool>, "ts": <ms>}  (you_ts/reply_ts let the UI show a REPEATED identical
                utterance/reply as a new bubble — text equality alone can't)
control.json : {"mode": always|wake|click, "action": activate|mute|unmute|toggle_mute, "wake_word": "<word>"}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

from loguru import logger

_VALID_MODES = ("always", "wake", "click")


def runtime_dir() -> Path:
    """Per-user directory for the state/control files (tmpfs when possible)."""
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(os.path.expanduser("~"), ".cache")
    directory = Path(base) / "ai-linux"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)  # user-only (control/state/voice channel)
    return directory


def write_state_file(payload: dict) -> None:
    """Atomically write ``state.json`` (engine -> overlay).

    Shared by :class:`OverlayBridge` and the engine's one-shot ``loading`` announcement, which runs
    before the bridge thread exists so the overlay shows a live loading pulse during model init.
    """
    directory = runtime_dir()
    payload = {**payload, "ts": int(time.time() * 1000)}
    tmp = directory / "state.json.tmp"
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, directory / "state.json")


class OverlayBridge:
    """Polls engine state, writes ``state.json``, and applies ``control.json`` commands."""

    POLL = 0.1  # seconds between ticks (10 Hz)
    CLICK_GRACE = 2.0  # idle seconds after a turn before releasing the mic in click mode
    CLICK_TIMEOUT = 12.0  # if the user activates but never speaks, release after this
    STOP_JOIN_TIMEOUT = 1.5  # seconds to wait for the loop thread on stop()
    THINKING_TIMEOUT = 30.0  # cap on the 'thinking' state so a hung turn can't stick
    HEARTBEAT = 2.0  # rewrite state.json at least this often so the UI can see the engine is alive
    EVENT_SCAN_LIMIT = 80  # observability events examined per tick
    VOICE_RETRIES = 3  # attempts before giving up on a failing voice id
    VOICE_BACKOFF = 1.0  # seconds between those attempts

    def __init__(self, engine: object, mode: str | None = None, wake_word: str | None = None) -> None:
        self.engine = engine
        self.dir = runtime_dir()
        self.state_path = self.dir / "state.json"
        self.control_path = self.dir / "control.json"
        self.voice_path = self.dir / "voice.json"  # mcp.voice writes {"voice": "F3"}; we apply it live

        self.mode = (mode or os.environ.get("GLADOS_OVERLAY_MODE", "always")).strip().lower()
        if self.mode not in _VALID_MODES:
            self.mode = "always"
        self.wake_word = wake_word or os.environ.get("GLADOS_OVERLAY_WAKE", "computer")

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_control_mtime_ns = 0
        self._last_control_raw: str | None = None
        self._last_voice_applied = ""
        self._voice_attempt: tuple[str, int, float] | None = None  # (value, attempts, next_retry_ts) while failing
        self._last_event_ts = time.time()
        self._you = ""
        self._assistant = ""
        self._you_ts = 0.0
        self._assistant_ts = 0.0  # liveness (also bumped by tts play events)
        self._reply_ts = 0.0  # bumped ONLY by 'reply' events -> the UI's new-bubble signal
        self._muted = False
        self._last_written: tuple[object, ...] | None = None
        self._last_write_at = 0.0  # wall-clock of the last state.json write (for the heartbeat)

        # click-mode activation tracking
        self._activated = False
        self._activate_ts = 0.0
        self._saw_turn = False
        self._idle_since: float | None = None

    def start(self) -> None:
        # Seed the watermark from any control.json left by a previous run, so only commands written
        # after startup apply — otherwise the first tick replays a stale one over the startup mode.
        try:
            self._last_control_mtime_ns = self.control_path.stat().st_mtime_ns
            self._last_control_raw = self.control_path.read_text()  # seed content too, or the first
            # tick would re-apply the stale command via the content-differs path in _read_control
        except (FileNotFoundError, OSError):
            self._last_control_mtime_ns = 0
            self._last_control_raw = None
        self._apply_mode(self.mode)
        self._thread = threading.Thread(target=self._loop, name="OverlayBridge", daemon=True)
        self._thread.start()
        logger.info("OverlayBridge started (mode={}, dir={})", self.mode, self.dir)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.STOP_JOIN_TIMEOUT)
        try:
            self._write({"state": "off", "mode": self.mode, "you": self._you, "assistant": self._assistant})
        except Exception:
            pass

    def _listening(self) -> bool:
        return getattr(getattr(self.engine, "audio_io", None), "input_stream", None) is not None

    def _set_listening(self, on: bool) -> None:
        audio = getattr(self.engine, "audio_io", None)
        if audio is None:
            return
        try:
            if on and not self._listening():
                audio.start_listening()
            elif not on and self._listening():
                audio.stop_listening()  # releases the sound card for other apps
        except Exception as exc:  # noqa: BLE001
            logger.warning("OverlayBridge: listening toggle failed: {}", exc)

    def _set_wake(self, word: str | None) -> None:
        listener = getattr(self.engine, "speech_listener", None)
        if listener is None:
            return
        try:
            listener.wake_word = word
            listener.reset()
        except Exception:  # noqa: BLE001
            pass

    def _set_muted(self, muted: bool) -> None:
        setter = getattr(self.engine, "set_asr_muted", None)
        if callable(setter):
            try:
                setter(muted)
            except Exception:  # noqa: BLE001
                pass

    def _apply_mode(self, mode: str) -> None:
        mode = (mode or "").strip().lower()
        if mode not in _VALID_MODES:
            return
        self.mode = mode
        self._muted = False
        self._activated = False
        self._saw_turn = False
        self._idle_since = None

        if getattr(self.engine, "input_mode", "audio") not in ("audio", "both"):
            return  # text-only: nothing to manage

        if mode == "always":
            self._set_wake(None)
            self._set_muted(False)
            self._set_listening(True)
        elif mode == "wake":
            self._set_wake(self.wake_word)
            self._set_muted(False)
            self._set_listening(True)
        elif mode == "click":
            self._set_wake(None)
            self._set_listening(False)  # start with the device released

    def _apply_action(self, action: str) -> None:
        action = (action or "").strip().lower()
        if action == "mute":
            self._muted = True
            self._set_muted(True)  # keep the engine's asr_muted_event consistent with the overlay
            self._set_listening(False)
        elif action == "unmute":
            self._muted = False
            self._apply_mode(self.mode)
        elif action == "toggle_mute":
            self._apply_action("unmute" if self._muted else "mute")
        elif action == "quit":
            ev = getattr(self.engine, "shutdown_event", None)
            if ev is not None:
                logger.info("OverlayBridge: quit requested from the overlay menu")
                ev.set()  # engine.run() loop exits -> graceful shutdown
        elif action == "activate" and self.mode == "click":
            self._activated = True
            self._saw_turn = False
            self._idle_since = None
            self._activate_ts = time.time()
            self._muted = False
            self._set_muted(False)
            self._set_listening(True)

    def _read_control(self) -> None:
        try:
            mtime_ns = self.control_path.stat().st_mtime_ns
        except FileNotFoundError:
            return
        if mtime_ns < self._last_control_mtime_ns:
            return
        try:
            raw = self.control_path.read_text()
        except Exception:  # noqa: BLE001
            return
        # ns mtime + content watermark: a second command landing within the same timestamp tick
        # still applies (different content), while an unchanged file is processed exactly once.
        if mtime_ns == self._last_control_mtime_ns and raw == self._last_control_raw:
            return
        self._last_control_mtime_ns = mtime_ns
        self._last_control_raw = raw
        try:
            data = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            return
        if not isinstance(data, dict):
            return
        if "wake_word" in data:  # live wake-word switch from the overlay chooser
            word = str(data["wake_word"]).strip()
            if word and word.lower() != "always":
                self.wake_word = word
                if self.mode == "wake":
                    self._set_wake(word)
        if "mode" in data:
            self._apply_mode(str(data["mode"]))
        if "action" in data:
            self._apply_action(str(data["action"]))

    def _read_voice(self) -> None:
        """Apply a live TTS-voice switch requested via ``voice.json`` (written by mcp.voice).

        Content-based (apply when the requested voice *value* changes), not mtime-based, so two
        switches that land within one filesystem timestamp tick can't be missed. The file is a
        few bytes on tmpfs, so reading it each 10 Hz tick is negligible.
        """
        try:
            data = json.loads(self.voice_path.read_text() or "{}")
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - e.g. a mid-write read; the next 10 Hz tick retries
            logger.debug("OverlayBridge: voice.json unreadable this tick ({}); retrying", exc)
            return
        voice = data.get("voice") if isinstance(data, dict) else None
        if not voice:
            return
        voice = str(voice)
        if voice == self._last_voice_applied:
            return
        # Latch only after set_voice succeeds; a failing value must not retry 10x/sec forever, so back
        # off and give up after a few tries. The counter resets when a different voice is requested.
        now = time.time()
        attempts = 0
        if self._voice_attempt and self._voice_attempt[0] == voice:
            _, attempts, next_retry_ts = self._voice_attempt
            if attempts >= self.VOICE_RETRIES or now < next_retry_ts:
                return
        setter = getattr(self.engine, "set_voice", None)
        ok = False
        if callable(setter):
            try:
                ok = bool(setter(voice))
            except Exception as exc:  # noqa: BLE001 - voice switching must never break the loop
                logger.warning("OverlayBridge: set_voice raised: {}", exc)
        if ok:
            self._last_voice_applied = voice  # success: stop retrying this value
            self._voice_attempt = None
        else:
            attempts += 1
            self._voice_attempt = (voice, attempts, now + self.VOICE_BACKOFF)
            if attempts >= self.VOICE_RETRIES:
                logger.warning("OverlayBridge: giving up on voice '{}' after {} failed attempts", voice, attempts)

    def _scan_transcript(self) -> None:
        bus = getattr(self.engine, "observability_bus", None)
        if bus is None:
            return
        try:
            events = bus.snapshot(limit=self.EVENT_SCAN_LIMIT)
        except Exception:  # noqa: BLE001
            return
        newest = self._last_event_ts
        for ev in events:
            ts = getattr(ev, "timestamp", 0.0)
            if ts <= self._last_event_ts:
                continue
            newest = max(newest, ts)
            src = getattr(ev, "source", "")
            kind = getattr(ev, "kind", "")
            msg = getattr(ev, "message", "")
            if kind == "user_input" and src in ("asr", "text"):
                self._you = msg
                self._you_ts = ts
            elif kind == "reply":  # the full assistant reply for the turn -> one transcript bubble
                self._assistant = msg
                self._assistant_ts = ts
                self._reply_ts = ts
            elif src == "tts" and kind == "play":
                # keep the "speaking" timestamp fresh for liveness, but DON'T set the transcript text from a
                # single sentence (the whole reply arrives once via the 'reply' event above -> one bubble).
                self._assistant_ts = ts
        self._last_event_ts = newest

    def _busy(self) -> tuple[bool, bool]:
        speaking = False
        audio = getattr(self.engine, "audio_io", None)
        if audio is not None:
            try:
                speaking = bool(audio.check_if_speaking())
            except Exception:  # noqa: BLE001
                speaking = False
        # 'thinking' = user spoke, no reply yet (transcript timeline, not the sticky engine latch);
        # 30s cap so a hung turn can't stick.
        thinking = self._you_ts > self._assistant_ts and (time.time() - self._you_ts) < self.THINKING_TIMEOUT
        return speaking, thinking

    def _derive_state(self) -> str:
        speaking, thinking = self._busy()
        if speaking:
            return "speaking"
        if thinking:
            return "thinking"
        if not self._listening():
            # Only call it 'muted' when the user actually muted; mic-not-open in any mode is 'idle'
            # (don't light the Mute control or mislabel the orb just because the stream is closed).
            return "muted" if self._muted else "idle"
        return "listening"

    def _tick_click(self) -> None:
        if self.mode != "click" or not self._activated:
            return
        speaking, thinking = self._busy()
        busy = speaking or thinking
        now = time.time()
        if busy:
            self._saw_turn = True
            self._idle_since = None
        elif self._saw_turn:
            if self._idle_since is None:
                self._idle_since = now
            elif now - self._idle_since > self.CLICK_GRACE:
                self._activated = False
                self._saw_turn = False
                self._set_listening(False)  # turn finished -> release the device
        elif now - self._activate_ts > self.CLICK_TIMEOUT:
            self._activated = False
            self._set_listening(False)  # clicked but never spoke -> release

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._read_control()
                self._read_voice()
                self._scan_transcript()
                self._tick_click()
                self._write(
                    {
                        "state": self._derive_state(),
                        "mode": self.mode,
                        "you": self._you,
                        "assistant": self._assistant,
                        "you_ts": int(self._you_ts * 1000),
                        "reply_ts": int(self._reply_ts * 1000),
                        "session": self._wake_session(),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("OverlayBridge loop error: {}", exc)
            self._stop.wait(self.POLL)

    def _wake_session(self) -> bool:
        """True while a wake-word conversation window is open (wake mode only) — drives overlay show/hide."""
        if self.mode != "wake":
            return False
        fn = getattr(getattr(self.engine, "speech_listener", None), "in_wake_session", None)
        try:
            return bool(fn()) if callable(fn) else False
        except Exception:  # noqa: BLE001
            return False

    def _write(self, payload: dict) -> None:
        key = (
            payload["state"],
            payload["mode"],
            payload["you"],
            payload["assistant"],
            payload.get("you_ts"),
            payload.get("reply_ts"),
            payload.get("session"),
        )
        now = time.time()
        # dedup identical states but heartbeat every 2s so the overlay can tell the engine is alive
        # (ts freshness); stale ts -> overlay shows 'off'.
        if key == self._last_written and (now - self._last_write_at) < self.HEARTBEAT:
            return
        self._last_written = key
        self._last_write_at = now
        write_state_file(payload)
