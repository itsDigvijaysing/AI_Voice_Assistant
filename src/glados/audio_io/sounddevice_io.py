import os
import queue
import threading
import time
from typing import Any

from loguru import logger
import numpy as np
from numpy.typing import NDArray
import sounddevice as sd  # type: ignore

try:  # high-quality sample-rate conversion (AI_Linux: for hardware that won't open 16k/24k)
    import soxr  # type: ignore

    _HAVE_SOXR = True
except Exception:  # pragma: no cover
    _HAVE_SOXR = False

from . import VAD


class SoundDeviceAudioIO:
    """Audio I/O implementation using sounddevice for both input and output.

    This class provides an implementation of the AudioIO interface using the
    sounddevice library to interact with system audio devices. It handles
    real-time audio capture with voice activity detection and audio playback.
    """

    SAMPLE_RATE: int = 16000  # Sample rate for input stream
    VAD_SIZE: int = 32  # Milliseconds of sample for Voice Activity Detection (VAD)
    VAD_THRESHOLD: float = 0.85  # raised from 0.8 to reject ambient/TV speech; tune if it misses you

    def __init__(self, vad_threshold: float | None = None) -> None:
        """Initialize the sounddevice audio I/O.

        Args:
            vad_threshold: Threshold for VAD detection (default: 0.8)

        Raises:
            ImportError: If the sounddevice module is not available
            ValueError: If invalid parameters are provided
        """
        if vad_threshold is None:
            self.vad_threshold = self.VAD_THRESHOLD
        else:
            self.vad_threshold = vad_threshold

        if not 0 <= self.vad_threshold <= 1:
            raise ValueError("VAD threshold must be between 0 and 1")

        self._vad_model = VAD()

        self._sample_queue: queue.Queue[tuple[NDArray[np.float32], bool]] = queue.Queue()
        self.input_stream: sd.InputStream | None = None
        self._is_playing = False
        self._playback_thread = None
        self._stop_event = threading.Event()
        self._pending_audio: NDArray[np.float32] | None = None
        self._pending_sample_rate: int = self.SAMPLE_RATE

        # conda's raw-ALSA PortAudio often can't open the pipeline's 16k/24k rates, and the default
        # output may be HDMI. Resolve a usable device + native rate once, resampling via soxr.
        self._in_device, self._out_device, self._capture_rate, self._playback_rate = self._resolve_audio_device()
        # Output rates already known-good (probed lazily in measure_percentage_spoken); seed the resolved one.
        self._out_rate_ok: dict[int, bool] = {self._playback_rate: True}
        self._in_resampler: Any = None
        self._resample_in: Any = None
        self._in_buf: NDArray[np.float32] = np.zeros(0, dtype=np.float32)

    @staticmethod
    def _default_input_index() -> int | None:
        """Resolve a concrete default input device index.

        PortAudio's *global* default (``sd.default.device[0]``) can be -1 even when a
        host API has a valid default input, so fall back to the host-API default and
        finally to the first device exposing input channels.
        """
        try:
            din = sd.default.device[0]
            if isinstance(din, int) and din >= 0:
                return din
        except Exception:
            pass
        try:
            for h in sd.query_hostapis():
                di = h.get("default_input_device", -1)
                if isinstance(di, int) and di >= 0:
                    return di
        except Exception:
            pass
        try:
            for i, dv in enumerate(sd.query_devices()):
                if dv["max_input_channels"] > 0:
                    return i
        except Exception:
            pass
        return None

    def _resolve_audio_device(self) -> tuple[int | str | None, int | str | None, int, int]:
        """Pick the audio device(s) + a hardware-supported native rate.

        Fast path: if the default devices already open the pipeline rates (16 kHz capture /
        24 kHz playback — the normal case on PulseAudio/PipeWire-visible setups), use them
        unchanged (device=None, no resampling).

        Fallback: honor ``GLADOS_AUDIO_DEVICE`` (index or name substring), else use the
        default *input* device (which tracks the system default source) for both directions
        when it has output. Capture/playback rates fall back to 48 kHz and we resample.

        Returns ``(in_device, out_device, capture_rate, playback_rate)``.
        """

        try:  # force PortAudio init / stable device enumeration before probing
            sd.query_devices()
        except Exception:
            pass

        def _ok(check: Any, device: int | str | None, rate: int, ch: int) -> bool:
            try:
                check(device=device, samplerate=rate, channels=ch)
                return True
            except Exception:
                return False

        tts_rate = 24000  # a low common TTS rate just to probe/seed the device; NOT the active TTS rate
        if _ok(sd.check_input_settings, None, self.SAMPLE_RATE, 1) and _ok(
            sd.check_output_settings, None, tts_rate, 1
        ):
            # Default device works. Whether playback resamples is decided per-utterance against the
            # real TTS rate (measure_percentage_spoken -> _output_supports), so 44.1k plays natively.
            return None, None, self.SAMPLE_RATE, tts_rate

        device: int | str | None = None
        env = os.environ.get("GLADOS_AUDIO_DEVICE", "").strip()
        if env:
            device = int(env) if env.lstrip("-").isdigit() else env
        else:
            device = self._default_input_index()

        # capture rate: prefer 16 kHz (no resample), else a rate the input actually supports
        capture_rate = self.SAMPLE_RATE
        if not _ok(sd.check_input_settings, device, self.SAMPLE_RATE, 1):
            capture_rate = next((r for r in (48000, 44100) if _ok(sd.check_input_settings, device, r, 1)), 48000)

        # Pick a CONCRETE audible output: the system default is HDMI here, the real "no voice" cause.
        # Prefer the same card, then any non-HDMI output, probing rates it actually supports.
        out_device, playback_rate = self._pick_output(device, tts_rate)

        needs_resample = capture_rate != self.SAMPLE_RATE or playback_rate not in (tts_rate,)
        if needs_resample and not _HAVE_SOXR:
            logger.warning("Audio device needs resampling but 'soxr' is not installed (pip install soxr).")
        logger.info(
            "Audio: in_device={} out_device={} capture={}Hz playback_native={}Hz resample={}",
            device, out_device, capture_rate, playback_rate, _HAVE_SOXR and needs_resample,
        )
        return device, out_device, capture_rate, playback_rate

    @staticmethod
    def _pick_output(in_device: int | str | None, tts_rate: int) -> tuple[int | str | None, int]:
        """Pick a concrete, audible output device + a native rate it supports.

        Order: the input device itself if it has output (a USB headset does both), then other
        output-capable devices with non-HDMI first, and only as a last resort the system default
        (which is often HDMI here -> silent). This is the fix for TTS playing to a dead default.
        """
        def ok(dev: int | str | None, rate: int) -> bool:
            try:
                sd.check_output_settings(device=dev, samplerate=rate, channels=1)
                return True
            except Exception:
                return False

        try:
            devs = sd.query_devices()
        except Exception:
            devs = []

        def first_rate(idx: int) -> int | None:  # native rate first (most reliable), then common rates
            nat = int(devs[idx]["default_samplerate"])
            for rate in (nat, 48000, 44100, tts_rate):
                if ok(idx, rate):
                    return rate
            return None

        # 1) same card if it has output (a USB headset does both). Trust max_output_channels even if the
        #    probe momentarily races PipeWire — the playback stream open already retries with backoff.
        if isinstance(in_device, int) and 0 <= in_device < len(devs) and devs[in_device]["max_output_channels"] > 0:
            return in_device, (first_rate(in_device) or int(devs[in_device]["default_samplerate"]))
        # 2) any other output-capable device, non-HDMI first (HDMI default is the usual silent trap)
        others = sorted(
            (i for i, d in enumerate(devs) if d["max_output_channels"] > 0),
            key=lambda i: ("hdmi" in devs[i]["name"].lower(), i),
        )
        for idx in others:
            rate = first_rate(idx)
            if rate:
                return idx, rate
        # 3) last resort: system default
        for rate in (tts_rate, 48000, 44100):
            if ok(None, rate):
                return None, rate
        return None, 48000

    def _output_supports(self, rate: int) -> bool:
        """True if the resolved output device can open this rate (probed once, then cached)."""
        ok = self._out_rate_ok.get(rate)
        if ok is None:
            try:
                sd.check_output_settings(device=self._out_device, samplerate=rate, channels=1)
                ok = True
            except Exception:  # noqa: BLE001
                ok = False
            self._out_rate_ok[rate] = ok
        return ok

    def start_listening(self) -> None:
        """Start capturing audio from the system microphone.

        Creates and starts a sounddevice InputStream that continuously captures
        audio from the resolved input device. Audio is resampled to SAMPLE_RATE
        (16 kHz) when the device's native rate differs, then chunked into fixed
        VAD frames, processed with the VAD model, and placed in the sample queue.

        Raises:
            RuntimeError: If the audio input stream cannot be started
            sd.PortAudioError: If there's an issue with the audio hardware
        """
        if self.input_stream is not None:
            self.stop_listening()

        if self._capture_rate != self.SAMPLE_RATE and not _HAVE_SOXR:
            # Without soxr we'd hand the VAD a wrong-size frame and the PortAudio callback would abort
            # silently. Fail loudly and actionably instead.
            raise RuntimeError(
                f"Capture device runs at {self._capture_rate} Hz but the pipeline needs "
                f"{self.SAMPLE_RATE} Hz and 'soxr' is not installed; install soxr or use a "
                "16 kHz-capable input device."
            )
        resample_in = self._capture_rate != self.SAMPLE_RATE and _HAVE_SOXR
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._in_resampler = None
        if resample_in:
            try:  # streaming resampler keeps filter state across blocks (no boundary clicks)
                self._in_resampler = soxr.ResampleStream(self._capture_rate, self.SAMPLE_RATE, 1, dtype="float32")
                self._resample_in = lambda x: self._in_resampler.resample_chunk(x)
            except Exception:  # fall back to stateless per-block resampling
                self._resample_in = lambda x: soxr.resample(x, self._capture_rate, self.SAMPLE_RATE)

        frame = int(self.SAMPLE_RATE * self.VAD_SIZE / 1000)  # 512 samples @16k handed to the VAD

        def _emit(chunk: NDArray[np.float32]) -> None:
            vad_value = self._vad_model(np.expand_dims(chunk, 0))
            self._sample_queue.put((chunk, bool(vad_value > self.vad_threshold)))

        def audio_callback(
            indata: NDArray[np.float32],
            frames: int,
            time: sd.CallbackStop,
            status: sd.CallbackFlags,
        ) -> None:
            """Resample (if needed), chunk into fixed VAD frames, and queue with VAD confidence."""
            if status:
                logger.debug(f"Audio callback status: {status}")

            data = np.array(indata, dtype=np.float32).copy().squeeze()  # single channel
            if resample_in:
                data = np.asarray(self._resample_in(data), dtype=np.float32).reshape(-1)
                if data.size == 0:
                    return
                self._in_buf = np.concatenate((self._in_buf, data))
                while self._in_buf.size >= frame:
                    _emit(self._in_buf[:frame].copy())
                    self._in_buf = self._in_buf[frame:]
            else:
                _emit(data)

        try:
            self.input_stream = sd.InputStream(
                samplerate=self._capture_rate,
                device=self._in_device,
                channels=1,
                callback=audio_callback,
                blocksize=int(self._capture_rate * self.VAD_SIZE / 1000),
            )
            self.input_stream.start()
        except sd.PortAudioError as e:
            raise RuntimeError(f"Failed to start audio input stream: {e}") from e

    def stop_listening(self) -> None:
        """Stop capturing audio and clean up resources.

        Stops the input stream if it's active and releases associated resources.
        This method should be called when audio input is no longer needed or
        before application shutdown.
        """
        if self.input_stream is not None:
            try:
                self.input_stream.stop()
                self.input_stream.close()
            except Exception as e:
                logger.error(f"Error stopping input stream: {e}")
            finally:
                self.input_stream = None

    def start_speaking(self, audio_data: NDArray[np.float32], sample_rate: int | None = None, text: str = "") -> None:
        """Queue audio for playback through the system speakers.

        Stores audio data for playback via measure_percentage_spoken(), which
        uses a single OutputStream to both play and monitor progress. This avoids
        the race condition that occurs when sd.play() and a monitoring OutputStream
        run concurrently.

        Parameters:
            audio_data: The audio data to play as a numpy float32 array
            sample_rate: The sample rate of the audio data in Hz
            text: Optional text associated with the audio (not used by this implementation)

        Raises:
            ValueError: If audio_data is empty or not a valid numpy array
        """
        if not isinstance(audio_data, np.ndarray) or audio_data.size == 0:
            raise ValueError("Invalid audio data")

        if sample_rate is None:
            sample_rate = self.SAMPLE_RATE

        # Stop any existing playback and create a fresh stop event for this session
        self.stop_speaking()
        self._stop_event = threading.Event()

        logger.debug(f"Playing audio with sample rate: {sample_rate} Hz, length: {len(audio_data)} samples")
        self._is_playing = True
        self._pending_audio = audio_data
        self._pending_sample_rate = sample_rate

    def measure_percentage_spoken(self, total_samples: int, sample_rate: int | None = None) -> tuple[bool, int]:
        """
        Play queued audio and monitor playback progress with interrupt detection.

        Uses a single OutputStream to both play the audio stored by start_speaking()
        and track progress, avoiding the race condition from running sd.play() and a
        separate monitoring stream concurrently.

        Args:
            total_samples (int): Total number of samples in the audio data being played.
            sample_rate (int | None): Sample rate override; uses the value from start_speaking() if None.
        Returns:
            tuple[bool, int]: A tuple containing:
                - bool: True if playback was interrupted, False if completed normally
                - int: Percentage of audio played (0-100)
        """
        audio_data = self._pending_audio
        if audio_data is None:
            return False, -1  # sentinel: nothing queued, nothing played

        if sample_rate is None:
            sample_rate = self._pending_sample_rate

        if sample_rate <= 0:
            logger.warning(f"Invalid sample rate {sample_rate}; skipping playback")
            if self._pending_audio is audio_data:
                self._pending_audio = None
                self._is_playing = False
            return False, -1  # sentinel: nothing played (caller must not record the reply as spoken)

        # Play at the TTS rate directly when the device supports it; resample only when it genuinely
        # can't. Keep the original audio_data reference for the identity-checked teardown below.
        play_data = audio_data
        stream_rate = sample_rate
        if not self._output_supports(sample_rate) and _HAVE_SOXR:
            target = self._playback_rate if self._output_supports(self._playback_rate) else sample_rate
            play_data = np.asarray(soxr.resample(audio_data, sample_rate, target), dtype=np.float32)
            stream_rate = target
        elif not self._output_supports(sample_rate):  # soxr missing -> can't resample; the open will likely fail
            logger.warning(
                f"Output device can't open {sample_rate} Hz and 'soxr' is not installed; TTS may be "
                "dropped. Install soxr or use a device that supports the TTS rate."
            )

        # Derive playback length from the actual buffer so a wrong caller-supplied
        # total_samples can't break the timeout or percentage math.
        effective_total = len(play_data)
        if effective_total <= 0:
            if self._pending_audio is audio_data:
                self._pending_audio = None
                self._is_playing = False
            return False, -1  # sentinel: nothing played (caller must not record the reply as spoken)

        position = 0
        interrupted = False
        completion_event = threading.Event()
        # Capture current stop_event so a new start_speaking() call doesn't affect this session
        stop_event = self._stop_event

        def stream_callback(
            outdata: NDArray[np.float32], frames: int, time_info: Any, status: sd.CallbackFlags
        ) -> None:
            nonlocal position, interrupted

            if stop_event.is_set():
                outdata.fill(0)
                interrupted = True
                completion_event.set()
                raise sd.CallbackStop

            remaining = effective_total - position
            chunk_size = min(frames, remaining)

            if chunk_size > 0:
                outdata[:chunk_size, 0] = play_data[position : position + chunk_size]
                if chunk_size < frames:
                    outdata[chunk_size:].fill(0)
                position += chunk_size
            else:
                outdata.fill(0)

            if position >= effective_total:
                completion_event.set()
                raise sd.CallbackStop

        # raw-ALSA exclusive open races PipeWire -> intermittent failure + silent drop; retry with backoff
        logger.debug(f"Using sample rate: {stream_rate} Hz, total samples: {effective_total}")
        max_timeout = effective_total / stream_rate + 1
        stream = None
        last_err: Exception | None = None
        deadline = time.monotonic() + 4.0
        attempt = 0
        while stream is None and time.monotonic() < deadline:
            candidate = None
            try:
                candidate = sd.OutputStream(
                    callback=stream_callback,
                    samplerate=stream_rate,
                    device=self._out_device,
                    channels=1,
                )
                candidate.start()
                stream = candidate
            except (sd.PortAudioError, RuntimeError) as exc:
                last_err = exc
                if candidate is not None:  # constructed but start() failed: close it (no __del__ -> would leak)
                    try:
                        candidate.close()
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(min(0.05 * (2**attempt), 0.4))  # 50,100,200,400,400…ms
                attempt += 1
        dropped = stream is None
        if stream is None:
            logger.warning(f"TTS playback: output stream failed to open after retries ({last_err}); audio dropped")
        else:
            try:
                completed = completion_event.wait(max_timeout)
                if not completed:
                    # Timeout: signal stop and mark as interrupted
                    stop_event.set()
                    interrupted = True
                    logger.debug("Audio playback timed out, forcing interruption")
            finally:
                try:
                    stream.stop()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass

        # Identity-checked teardown: only clear shared state if it still belongs to this
        # session, otherwise a new start_speaking() that ran concurrently could be wiped out.
        if self._pending_audio is audio_data:
            self._pending_audio = None
        if self._stop_event is stop_event:
            self._is_playing = False
        if dropped:
            return False, -1  # sentinel: stream never opened, nothing was heard
        percentage_played = min(int(position / effective_total * 100), 100)
        return interrupted, percentage_played

    def check_if_speaking(self) -> bool:
        """Check if audio is currently being played.

        Returns:
            bool: True if audio is currently playing, False otherwise
        """
        return self._is_playing

    def stop_speaking(self) -> None:
        """Stop audio playback and clean up resources.

        Signals the current playback session to stop by setting the stop event.
        The active OutputStream callback will detect this on its next invocation
        and raise CallbackStop to cleanly terminate the stream.
        """
        if self._is_playing:
            self._stop_event.set()
            self._is_playing = False

    def get_sample_queue(self) -> queue.Queue[tuple[NDArray[np.float32], bool]]:
        """Get the queue containing audio samples and VAD confidence.

        Returns:
            queue.Queue: A thread-safe queue containing tuples of
                        (audio_sample, vad_confidence)
        """
        return self._sample_queue
