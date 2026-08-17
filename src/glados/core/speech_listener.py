"""
Speech listener module for the Glados voice assistant.

This module provides the SpeechListener class that handles audio input streaming,
voice activity detection, speech recognition, and wake word detection.
"""

from collections import deque
import os
import queue
import threading
import time
from typing import Any

from Levenshtein import distance
from loguru import logger
import numpy as np
from numpy.typing import NDArray

from typing import Callable

from ..ASR import TranscriberProtocol
from ..audio_io import AudioProtocol
from .audio_state import AudioState
from ..observability import ObservabilityBus, trim_message

# Callback signature: (event_type: str) -> None
InterruptCallback = Callable[[str], None]


class SpeechListener:
    """
    Manages audio input and speech processing for a voice assistant.

    This class handles capturing audio, performing Voice Activity Detection (VAD),
    buffering pre-activation audio, triggering Automatic Speech Recognition (ASR),
    and coordinating with Language Model (LLM) and Text-to-Speech (TTS) components
    via shared events and queues. It supports optional wake word detection.
    """

    VAD_SIZE: int = 32  # Milliseconds of sample for Voice Activity Detection (VAD)
    BUFFER_SIZE: int = 800  # Milliseconds of buffer BEFORE VAD detection
    PAUSE_LIMIT: int = 640  # Milliseconds of pause allowed before processing
    SIMILARITY_THRESHOLD: int = 3  # wake-word match tolerance: allow up to 2 edits (ASR slips on "computer")

    def __init__(
        self,
        audio_io: AudioProtocol,  # Replace with actual type if known
        llm_queue: queue.Queue[dict[str, Any]],
        shutdown_event: threading.Event,
        currently_speaking_event: threading.Event,
        processing_active_event: threading.Event,
        asr_model: TranscriberProtocol,
        wake_word: str | None,
        pause_time: float,
        interruptible: bool = True,
        echo_hangover_s: float = 0.3,
        interaction_state: "InteractionState | None" = None,
        observability_bus: ObservabilityBus | None = None,
        asr_muted_event: threading.Event | None = None,
        audio_state: AudioState | None = None,
        on_interrupt: InterruptCallback | None = None,
    ) -> None:
        """
        Initializes the SpeechListener with audio I/O, inter-thread communication, and ASR model.

        Args:
            audio_io: An instance conforming to `AudioProtocol` for audio input/output.
            llm_queue: A queue for sending transcribed text to the language model.
            shutdown_event: A threading.Event to signal the application to shut down.
            currently_speaking_event: A threading.Event indicating if the assistant is currently speaking.
            processing_active_event: A threading.Event indicating if processing is active (e.g., for LLM/TTS).
            asr_model: An instance conforming to `TranscriberProtocol` for speech recognition.
            wake_word: Optional wake word string to activate the assistant. Defaults to None.
            interruptible: If True, allows new speech input to interrupt ongoing assistant speech.
        """
        self.audio_io = audio_io
        self.llm_queue = llm_queue
        self.asr_model = asr_model
        self.wake_word = wake_word.lower() if wake_word else None
        self.pause_time = pause_time
        self.interruptible = interruptible
        # Half-duplex echo suppression (non-interruptible only): ignore the mic this long after
        # the assistant stops, covering the room's acoustic tail and frames buffered mid-playback.
        self._echo_hangover_s = echo_hangover_s
        self._last_spoke_at: float | None = None
        self._was_speaking = False  # edge-detects end-of-reply, to re-arm the wake window below

        # Circular buffer to hold pre-activation samples
        self._buffer: deque[NDArray[np.float32]] = deque(maxlen=self.BUFFER_SIZE // self.VAD_SIZE)
        self._sample_queue = self.audio_io.get_sample_queue()

        # Internal state variables
        self._recording_started = False
        self._samples: list[NDArray[np.float32]] = []
        self._gap_counter = 0
        # Set when this segment's VAD trigger aborted an in-flight turn, so _process_detected_audio
        # folds the text into that unanswered turn. Consumed exactly once per segment.
        self._interrupted_pending_turn = False

        self.shutdown_event = shutdown_event
        self.currently_speaking_event = currently_speaking_event
        self.processing_active_event = processing_active_event
        self._interaction_state = interaction_state
        self._observability_bus = observability_bus
        self._asr_muted_event = asr_muted_event
        self._audio_state = audio_state
        self._on_interrupt = on_interrupt

        # Wake-word conversation window: seconds the assistant stays active without the wake word,
        # refreshed per utterance. GLADOS_WAKE_SESSION_S overrides; <=0 disables.
        try:
            self._wake_session_s = float(os.environ.get("GLADOS_WAKE_SESSION_S", "30") or "30")
        except ValueError:
            self._wake_session_s = 30.0
        self._wake_session_until = 0.0

    def run(self) -> None:
        """
        Starts the main listening event loop, continuously processing audio input.

        This method initializes the audio input stream and enters a loop that
        listens for incoming audio samples and their Voice Activity Detection (VAD) confidence.
        It retrieves samples from an internal queue and processes them via `_handle_audio_sample`.
        The loop runs until the `shutdown_event` is set. It also handles brief pauses
        in audio input using a timeout.

        Raises:
            Exception: Catches and logs general exceptions encountered during the listening loop,
                       without stopping the loop unless `shutdown_event` is set.
        """
        logger.success("SpeechListener ready")

        # Loop forever, but is 'paused' when new samples are not available
        try:
            while not self.shutdown_event.is_set():  # Check event BEFORE blocking get
                try:
                    # Use a timeout for the queue get
                    sample, vad_confidence = self._sample_queue.get(timeout=self.pause_time)
                    if self._asr_muted_event and self._asr_muted_event.is_set():
                        if self._recording_started or self._samples or self._buffer:
                            self.reset()
                        continue
                    self._handle_audio_sample(sample, vad_confidence)
                except queue.Empty:
                    # Timeout occurred, loop again to check shutdown_event
                    continue
                except (OSError, RuntimeError) as e:  # More specific exceptions
                    if not self.shutdown_event.is_set():  # Only log if not shutting down
                        logger.error(f"Error in listen loop ({type(e).__name__}): {e}")
                    continue

            logger.info("Shutdown event detected in listen loop, exiting loop.")

        finally:
            self.audio_io.stop_listening()
            logger.info("Listen event loop is stopping/exiting.")

        logger.info("Speech Listener thread finished.")

    def _handle_audio_sample(self, sample: NDArray[np.float32], vad_confidence: bool) -> None:
        """
        Routes the processing of an individual audio sample based on the current recording state.

        If recording has not started, the sample contributes to the pre-activation buffer.
        Once recording is active, the sample is added to the main speech segment
        and contributes to the voice activity gap detection.

        Args:
            sample: The audio sample (numpy array) to process.
            vad_confidence: True if voice activity is detected in the sample, False otherwise.
        """
        # Track when the assistant last produced audio, for the half-duplex echo window.
        speaking_now = self.currently_speaking_event.is_set()
        if speaking_now:
            self._last_spoke_at = time.monotonic()
        elif self._was_speaking and self.wake_word:
            # Falling edge: the window only refreshed on user utterances, so a long turn could eat
            # it entirely. Re-arm here so it covers the user's turn to respond.
            self._wake_session_until = time.monotonic() + self._wake_session_s
        self._was_speaking = speaking_now
        if self._audio_state is not None:
            if sample.size:
                rms = float(np.sqrt(np.mean(sample * sample)))
            else:
                rms = 0.0
            self._audio_state.update(rms, vad_confidence)
        if not self._recording_started:
            self._manage_pre_activation_buffer(sample, vad_confidence)
        else:
            self._process_activated_audio(sample, vad_confidence)

    def _manage_pre_activation_buffer(self, sample: NDArray[np.float32], vad_confidence: bool) -> None:
        """
        Manages the pre-activation circular buffer and starts recording on voice activity.

        Samples accumulate in a circular buffer until VAD fires. When not interruptible, the mic is
        dropped entirely during the assistant's own speech plus a short hangover (half-duplex echo
        suppression). On VAD, an in-flight turn is aborted only on a real barge-in (see the abort
        condition below), the buffered samples become `_samples`, and `_recording_started` is set.

        Args:
            sample: The current audio sample (numpy array) to be added to the buffer.
            vad_confidence: True if voice activity is detected in the sample, False otherwise.
        """
        # Half-duplex echo suppression: with no AEC on the raw-ALSA path the mic transcribes our own
        # TTS, so drop it while speaking plus a hangover. Skipped when interruptible (headphones/AEC).
        if not self.interruptible and self._in_echo_window():
            self._buffer.clear()
            return

        self._buffer.append(sample)  # Automatically handles overflow

        if vad_confidence:
            # Check if this is an interrupt (user speaking while the assistant is busy)
            was_speaking = self.currently_speaking_event.is_set()

            # Abort on real barge-in: while speaking in any mode, or while thinking only when
            # interruptible. Gating on was_speaking stops our own TTS false-aborting the reply.
            if was_speaking or (self.interruptible and self.processing_active_event.is_set()):
                self.audio_io.stop_speaking()
                self.processing_active_event.clear()
                self._interrupted_pending_turn = True
            self._samples = list(self._buffer)  # Clean conversion
            self._recording_started = True

            if was_speaking and self._on_interrupt:
                self._on_interrupt("user_interrupt")

    def _in_echo_window(self) -> bool:
        """True while the assistant is speaking, or within the post-speech hangover.

        Drives half-duplex echo suppression in `_manage_pre_activation_buffer` so the
        mic never captures the assistant's own TTS (see that method).
        """
        if self.currently_speaking_event.is_set():
            return True
        if self._echo_hangover_s <= 0 or self._last_spoke_at is None:
            return False
        return (time.monotonic() - self._last_spoke_at) < self._echo_hangover_s

    def _process_activated_audio(self, sample: NDArray[np.float32], vad_confidence: bool) -> None:
        """
        Accumulates audio samples and tracks pauses after voice activation.

        This method appends incoming audio samples to `self._samples`. It increments
        `_gap_counter` when no voice activity is detected. If the `_gap_counter`
        exceeds `PAUSE_LIMIT`, it signifies the end of a speech segment, triggering
        `_process_detected_audio`. Otherwise, if voice is detected, the gap counter is reset.

        Args:
            sample: A single audio sample (numpy array) from the input stream.
            vad_confidence: True if voice activity is currently detected, False otherwise.
        """
        self._samples.append(sample)

        if not vad_confidence:
            self._gap_counter += 1
            if self._gap_counter >= self.PAUSE_LIMIT // self.VAD_SIZE:
                self._process_detected_audio()
        else:
            self._gap_counter = 0

    def _wakeword_detected(self, text: str) -> bool:
        """
        Checks if the transcribed text contains a sufficiently similar match to the configured wake word.

        This method iterates through words in the `text` and calculates the Levenshtein distance
        (edit distance) between each word (converted to lowercase) and the `wake_word`.
        A match is considered found if the `closest_distance` is less than `SIMILARITY_THRESHOLD`.
        This helps account for minor misrecognitions of the wake word.

        Args:
            text: The transcribed text string to check for wake word similarity.

        Returns:
            True if a word in the text matches the wake word within the similarity threshold, False otherwise.

        Raises:
            AssertionError: If `self.wake_word` is None.
        """
        if self.wake_word is None:
            raise ValueError("Wake word should not be None")

        words = text.split()
        closest_distance = min(distance(word.lower(), self.wake_word) for word in words)
        return closest_distance < self.SIMILARITY_THRESHOLD

    def in_wake_session(self) -> bool:
        """True while a wake-word conversation window is open (follow-ups need no wake word)."""
        return self._wake_session_s > 0 and time.monotonic() < self._wake_session_until

    def reset(self) -> None:
        """
        Resets the internal state of the speech listener, clearing all audio buffers and counters.

        This prepares the listener for a new speech segment by:
        - Setting `_recording_started` to False.
        - Clearing the accumulated `_samples`.
        - Resetting the `_gap_counter`.
        - Emptying the pre-activation circular buffer (`_buffer.queue`), safely using its internal mutex.
        """
        logger.debug("Resetting recorder...")
        self._recording_started = False
        self._samples.clear()
        self._gap_counter = 0
        self._buffer.clear()
        if self._audio_state is not None:
            self._audio_state.reset()

    def _process_detected_audio(self) -> None:
        """
        Processes the accumulated audio samples once a speech pause is detected.

        This method performs the following steps:
        1. Transcribes the collected audio samples using the ASR model.
        2. If transcription is successful:
            a. Checks for the `wake_word` (if configured).
            b. If the wake word is detected (or not required), the transcribed text is
               placed into the `llm_queue`, and `processing_active_event` is set.
        3. Resets the listener's internal state using `self.reset()`, preparing for the next input.
        """
        logger.debug("Detected pause after speech. Processing...")

        # Consumed exactly once per segment, regardless of the outcome below (junk/no-wake-word
        # segments must not leak a stale flag onto some later, unrelated segment).
        continuation = self._interrupted_pending_turn
        self._interrupted_pending_turn = False

        detected_text = self.asr(self._samples)

        if detected_text:
            logger.success(f"ASR text: '{detected_text}'")

            if sum(ch.isalnum() for ch in detected_text) < 2:
                # drop content-free noise ("." / emoji); keeps real short words
                logger.info(f"Ignoring junk transcript: '{detected_text}'")
            elif self.wake_word and not self.in_wake_session() and not self._wakeword_detected(detected_text):
                logger.info(f"Required wake word {self.wake_word=} not detected (no active session).")
            else:
                # (re)arm the window so follow-ups skip the wake word. On reopening, history still
                # holds the old "I'm asleep" result, so _wake_note carries a one-turn cue past it.
                woke_from_sleep = bool(self.wake_word) and not self.in_wake_session()
                if self.wake_word:
                    self._wake_session_until = time.monotonic() + self._wake_session_s
                if self._observability_bus:
                    self._observability_bus.emit(
                        source="asr",
                        kind="user_input",
                        message=trim_message(detected_text),
                    )
                queued_item: dict[str, Any] = {
                    "role": "user",
                    "content": detected_text,
                    "_enqueued_at": time.time(),
                    "_lane": "priority",
                }
                if continuation:
                    # This segment only exists because it barged in on a turn that hadn't
                    # finished answering yet — it's the rest of the same thought, not a new one.
                    queued_item["_continuation"] = True
                if woke_from_sleep:
                    queued_item["_wake_note"] = (
                        "The user just said the wake word again — you are awake and listening "
                        "now. Disregard any earlier note that you were asleep; respond normally "
                        "to what they say."
                    )
                self.llm_queue.put(queued_item)
                if self._interaction_state:
                    self._interaction_state.mark_user()
                self.processing_active_event.set()

        self.reset()

    def asr(self, samples: list[NDArray[np.float32]]) -> str:
        """
        Performs Automatic Speech Recognition (ASR) on a list of audio samples.

        The samples are first concatenated into a single audio array. This combined
        audio is then normalized to a range of [-1.0, 1.0] to ensure consistent
        volume levels before being passed to the ASR model for transcription.

        Args:
            samples: A list of numpy arrays (float32) containing audio sample chunks.

        Returns:
            The transcribed text as a string.
        """
        if not samples:
            logger.warning("ASR received empty sample list")
            return ""

        audio = np.concatenate(samples)

        # Check for silent audio
        max_abs_val = np.max(np.abs(audio))
        if max_abs_val < 1e-10:  # Threshold for effectively silent audio
            logger.warning("ASR received effectively silent audio")
            return ""

        # Normalize to full range [-1.0, 1.0]
        audio = audio / max_abs_val

        detected_text = self.asr_model.transcribe(audio)
        return detected_text
