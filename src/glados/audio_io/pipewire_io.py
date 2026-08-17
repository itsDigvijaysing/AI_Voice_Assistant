"""PipeWire audio backend with WebRTC echo cancellation (AI_Linux — real full-duplex barge-in).

Why this exists: conda's PortAudio is ALSA-`hw:`-only and cannot see PipeWire virtual nodes, so it
can't use PipeWire's WebRTC `module-echo-cancel`. Without echo cancellation, an open mic transcribes
the assistant's own TTS, which is why the default (sounddevice) backend is half-duplex. This backend
routes audio through PipeWire instead — capturing from an echo-cancelled source and playing to the
echo-cancel sink (the AEC reference) — so the mic the engine hears has the assistant's voice removed.
That is the local equivalent of how LightSpeak/RealtimeVoiceChat get full-duplex (browser WebRTC AEC).

Proven on this box: `libspa-aec-webrtc.so` present; `module-echo-cancel` creates `ai_aec_source`/
`ai_aec_sink`; `pw-record --target ai_aec_source` yields echo-cancelled audio. The module is loaded via
a held-open `pw-cli` whose lifetime == this process, so it is fully reversible (no config files, no
service restart, no apt). If the user has already set up persistent AEC nodes, those are reused.

Enabled by ``audio_io: "pipewire"`` (the ai-linux ``--barge-in`` flag sets it via GLADOS_AUDIO_BACKEND),
which also turns on ``interruptible`` so the user can talk over the assistant.
"""

from __future__ import annotations

import atexit
import os
import queue
import secrets
import subprocess
import threading
import time

from loguru import logger
import numpy as np
from numpy.typing import NDArray

from . import VAD


class PipeWireAudioIO:
    """AudioProtocol backend over PipeWire (pw-record / pw-cat) with WebRTC echo cancellation."""

    SAMPLE_RATE: int = 16000
    VAD_SIZE: int = 32          # ms per VAD frame -> 512 samples @ 16k
    VAD_THRESHOLD: float = 0.8  # balance: catch the user's barge-in vs. ignore AEC residual (GLADOS_VAD_THRESHOLD tunes it)

    def __init__(self, vad_threshold: float | None = None) -> None:
        self._vad_model = VAD()
        env_vad = os.environ.get("GLADOS_VAD_THRESHOLD", "").strip()
        if env_vad:
            try:
                vad_threshold = float(env_vad)
            except ValueError:
                pass
        self.vad_threshold = self.VAD_THRESHOLD if vad_threshold is None else vad_threshold
        self._sample_queue: queue.Queue[tuple[NDArray[np.float32], bool]] = queue.Queue()

        self._src = os.environ.get("GLADOS_AEC_SOURCE", "ai_aec_source")
        self._sink = os.environ.get("GLADOS_AEC_SINK", "ai_aec_sink")

        # capture state — `input_stream` mirrors the sounddevice attr the overlay bridge checks for "listening"
        self.input_stream: object | None = None
        self._rec_proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stop_rec = threading.Event()

        # playback state (mirrors sounddevice_io contract)
        self._pending_audio: NDArray[np.float32] | None = None
        self._pending_sample_rate: int = self.SAMPLE_RATE
        self._is_playing = False
        self._stop_event = threading.Event()
        self._play_proc: subprocess.Popen | None = None

        self._aec_proc: subprocess.Popen | None = None  # held-open pw-cli keeping the module loaded
        self._aec_owned = False
        self._aec_ok = False  # True once ai_aec_source/sink exist; else fall back to default devices
        self._tmpdir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        atexit.register(self._teardown_aec)
        self._ensure_aec()  # eager: AEC source/sink ready before the first capture or announcement

    def _node_exists(self, name: str) -> bool:
        try:
            out = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True, text=True, timeout=4).stdout
        except Exception:  # noqa: BLE001
            return False
        return f'"{name}"' in out or f"node.name = {name}" in out

    def _ensure_aec(self) -> None:
        """Load WebRTC module-echo-cancel (named ai_aec_*) unless those nodes already exist. Reversible."""
        if self._node_exists(self._src):
            self._aec_ok = True
            logger.info("AEC source '{}' already present — reusing it", self._src)
            return
        cmd = (
            "load-module libpipewire-module-echo-cancel "
            "{ library.name = aec/libspa-aec-webrtc "
            f'source.props = {{ node.name = {self._src} node.description = "AI AEC Mic" }} '
            f'sink.props = {{ node.name = {self._sink} node.description = "AI AEC Speaker" }} }}\n'
        )
        try:
            # interactive pw-cli stays connected while stdin is open -> module stays loaded for our lifetime
            self._aec_proc = subprocess.Popen(
                ["pw-cli"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._aec_proc.stdin.write(cmd.encode())  # type: ignore[union-attr]
            self._aec_proc.stdin.flush()  # type: ignore[union-attr]
            self._aec_owned = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("AEC load failed ({}); falling back to non-cancelled capture", exc)
            return
        for _ in range(40):  # wait up to ~4s for the node to register
            if self._node_exists(self._src):
                self._aec_ok = True
                logger.success("WebRTC echo-cancel loaded ({} / {})", self._src, self._sink)
                return
            time.sleep(0.1)
        logger.warning("AEC node '{}' did not appear in time; using default devices (no echo cancel)", self._src)

    def _teardown_aec(self) -> None:
        if self._aec_proc is not None:
            try:
                self._aec_proc.stdin.close()  # type: ignore[union-attr]
                self._aec_proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._aec_proc = None

    def get_sample_queue(self) -> queue.Queue[tuple[NDArray[np.float32], bool]]:
        return self._sample_queue

    def start_listening(self) -> None:
        if self._rec_proc is not None or (self._reader is not None and self._reader.is_alive()):
            self.stop_listening()
        self._ensure_aec()
        self._stop_rec.clear()
        while True:  # drop stale frames captured before the last stop so they can't leak into this session
            try:
                self._sample_queue.get_nowait()
            except queue.Empty:
                break
        # --container raw -> headerless PCM on stdout; target the AEC'd source when available
        cmd = ["pw-record", "--container", "raw"]
        if self._aec_ok:
            cmd += ["--target", self._src]
        cmd += ["--rate", str(self.SAMPLE_RATE), "--channels", "1", "--format", "s16", "-"]
        try:
            self._rec_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except FileNotFoundError as exc:
            raise RuntimeError(f"pw-record not found ({exc}); install pipewire utils") from exc
        self.input_stream = self._rec_proc
        self._reader = threading.Thread(target=self._read_loop, name="PWCapture", daemon=True)
        self._reader.start()
        logger.success("PipeWire capture started (source={})", self._src if self._aec_ok else "default(no-AEC)")

    def _read_loop(self) -> None:
        frame = int(self.SAMPLE_RATE * self.VAD_SIZE / 1000)  # 512 samples
        nbytes = frame * 2  # s16 = 2 bytes/sample
        buf = b""
        bad = 0  # consecutive frame failures: isolated glitches drop the frame, persistent failure stops
        stream = self._rec_proc.stdout if self._rec_proc else None
        if stream is None:
            return
        while not self._stop_rec.is_set():
            try:
                chunk = stream.read(nbytes)
            except Exception:  # noqa: BLE001
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= nbytes:
                raw, buf = buf[:nbytes], buf[nbytes:]
                try:
                    data = (np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0)
                    vad = self._vad_model(np.expand_dims(data, 0))
                    self._sample_queue.put((data.copy(), bool(vad > self.vad_threshold)))
                    bad = 0
                except Exception as exc:  # noqa: BLE001 - a bad frame must not silently kill capture
                    bad += 1
                    logger.warning("PWCapture: VAD/emit failed ({}); dropping frame ({}/10)", exc, bad)
                    if bad >= 10:
                        logger.error("PWCapture: 10 consecutive frame failures; stopping reader")
                        return

    def stop_listening(self) -> None:
        self._stop_rec.set()
        if self._rec_proc is not None:
            try:
                self._rec_proc.terminate()
                self._rec_proc.wait(timeout=1.0)
            except Exception:  # noqa: BLE001
                try:
                    self._rec_proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._rec_proc = None
        # Join the reader before a restart clears _stop_rec, so a quick stop->start never runs two
        # readers on one queue. Keep the reference if the join times out, so the next start re-joins it.
        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.5)
        if reader is None or not reader.is_alive():
            self._reader = None
        self.input_stream = None

    def start_speaking(self, audio_data: NDArray[np.float32], sample_rate: int | None = None, text: str = "") -> None:
        if not isinstance(audio_data, np.ndarray) or audio_data.size == 0:
            raise ValueError("Invalid audio data")
        self.stop_speaking()
        self._stop_event = threading.Event()
        self._is_playing = True
        self._pending_audio = audio_data
        self._pending_sample_rate = sample_rate or self.SAMPLE_RATE

    def measure_percentage_spoken(self, total_samples: int, sample_rate: int | None = None) -> tuple[bool, int]:
        import soundfile as sf  # core dep; lazy import keeps backend load cheap

        audio = self._pending_audio
        if audio is None:
            return False, -1  # sentinel: nothing queued, nothing played
        sr = sample_rate or self._pending_sample_rate
        stop = self._stop_event
        # pw-play rejects raw PCM on stdin, so write a temp WAV on tmpfs and play the file. It blocks
        # for the clip's duration, which is the playback monitoring the engine needs for barge-in.
        path = os.path.join(self._tmpdir, f"ai_tts_{secrets.token_hex(8)}.wav")  # unpredictable temp name
        try:
            sf.write(path, np.clip(audio, -1.0, 1.0).astype(np.float32), sr)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TTS temp write failed ({}); audio dropped", exc)
            self._is_playing = False
            return False, -1  # sentinel: nothing played (see measure_percentage_spoken contract)
        cmd = ["pw-play"]
        if self._aec_ok:
            cmd += ["--target", self._sink]  # play to the AEC sink so it becomes the cancellation reference
        cmd += [path]
        dur = len(audio) / sr if sr else 0.0
        interrupted = False
        dropped = False
        timed_out = False
        play_rc: int | None = None
        t0 = time.monotonic()
        try:
            self._play_proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
            while self._play_proc.poll() is None:
                if stop.is_set():
                    interrupted = True
                    self._play_proc.terminate()
                    break
                if time.monotonic() - t0 > dur + 5.0:  # absolute deadline: a hung pw-play must not stall the speak loop
                    timed_out = True
                    logger.warning("pw-play still running 5s past clip end; killing it")
                    self._play_proc.kill()
                    break
                time.sleep(0.02)  # ~20ms barge-in abort granularity
            if stop.is_set():  # stop_speaking() may have killed the proc before the check above ran
                interrupted = True
        except FileNotFoundError as exc:
            logger.warning("pw-play not found ({}); audio dropped", exc)
            dropped = True
        finally:
            p = self._play_proc
            if p is not None:
                try:
                    p.wait(timeout=0.5)
                except Exception:  # noqa: BLE001
                    try:
                        p.kill()
                    except Exception:  # noqa: BLE001
                        pass
                play_rc = p.returncode
            self._play_proc = None
            self._is_playing = False
            try:
                os.unlink(path)
            except Exception:  # noqa: BLE001
                pass
        if dropped:
            return False, -1  # sentinel: player binary missing, nothing was heard
        if timed_out:
            return False, 100  # the clip's duration fully elapsed before the kill; count it as heard
        if interrupted:
            return True, (min(int((time.monotonic() - t0) / dur * 100), 100) if dur > 0 else 0)
        if play_rc not in (0, None):  # pw-play launched but exited non-zero (sink gone, ALSA busy) -> not heard
            logger.warning("pw-play exited {}; audio dropped", play_rc)
            return False, -1
        return False, 100

    def check_if_speaking(self) -> bool:
        return self._is_playing

    def stop_speaking(self) -> None:
        if self._is_playing:
            self._stop_event.set()
            self._is_playing = False
        p = self._play_proc
        if p is not None:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
