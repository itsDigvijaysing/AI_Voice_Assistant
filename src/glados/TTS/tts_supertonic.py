"""SuperTonic-3 TTS adapter (AI_Linux addition).

SuperTonic-3 (https://github.com/supertone-inc/supertonic) is a lightning-fast,
on-device, multilingual TTS that runs natively via ONNX Runtime — no PyTorch — which
fits this assistant's CPU/ONNX budget (the GPU is reserved for the LLM). It outputs
44.1 kHz float32 audio and ships a fixed set of speakers:

    male:   M1, M3, M4, M5
    female: F1, F2, F3, F4, F5

This adapts it to the engine's ``SpeechSynthesizerProtocol``: a ``sample_rate``
attribute plus ``generate_speech_audio(text) -> float32 mono array``. Selected via
the config ``voice`` of ``"supertonic"`` (default male M1) or ``"supertonic:<ID>"``.

Code is MIT; model weights are OpenRAIL-M. Weights are fetched once via
huggingface_hub (auto_download) and cached; nothing is committed to the repo.
"""

from __future__ import annotations

import os
import threading

import numpy as np
from numpy.typing import NDArray
from loguru import logger

DEFAULT_VOICE = "M1"  # male
DEFAULT_LANG = "en"
_KNOWN_VOICES = ("M1", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5")


class SpeechSynthesizer:
    """SuperTonic-3 synthesizer exposing the engine's TTS protocol."""

    def __init__(self, voice: str = DEFAULT_VOICE, lang: str = DEFAULT_LANG) -> None:
        from supertonic import TTS  # lazy: only imported when SuperTonic is selected

        # Keep TTS modest on CPU threads so it shares the box with ASR and the brain.
        # GLADOS_TTS_THREADS overrides; None lets onnxruntime pick a sensible default.
        threads = os.environ.get("GLADOS_TTS_THREADS", "")
        n_threads = int(threads) if threads.isdigit() else None
        # Load from the local cache with no network call; reach out only once, if the weights have
        # never been fetched on this machine. Inference is always on-device.
        try:
            self._tts = TTS(model="supertonic-3", auto_download=False, intra_op_num_threads=n_threads)
        except Exception as exc:  # noqa: BLE001 - usually "not cached yet"; retry once with download
            logger.info("SuperTonic local load failed ({}); attempting one-time weight download…", exc)
            try:
                self._tts = TTS(model="supertonic-3", auto_download=True, intra_op_num_threads=n_threads)
            except Exception:  # download didn't fix it -> surface the ORIGINAL error, not a misleading one
                logger.error("SuperTonic init failed after download attempt; original load error: {}", exc)
                raise
        self.sample_rate: int = int(self._tts.sample_rate)  # 44100
        self._lang = lang
        self._voice = voice
        # Guards the style swap: set_voice() runs on the bridge thread, synthesis on the TTS thread.
        # Held only for the reference read/swap, so a live change lands at the next utterance.
        self._lock = threading.Lock()
        self._style = self._make_style(voice)
        logger.info("SuperTonic TTS ready (voice={}, {} Hz)", voice, self.sample_rate)

    def _make_style(self, voice: str) -> object:
        try:
            return self._tts.get_voice_style(voice_name=voice)
        except Exception as exc:  # noqa: BLE001 - surface a clear, actionable message
            raise ValueError(
                f"SuperTonic voice '{voice}' is not available "
                f"(known voices: {', '.join(_KNOWN_VOICES)}). Original error: {exc}"
            ) from exc

    def set_voice(self, voice: str) -> None:
        style = self._make_style(voice)  # build outside the lock (validation can raise)
        with self._lock:
            self._style = style
            self._voice = voice

    def generate_speech_audio(self, text: str) -> NDArray[np.float32]:
        text = (text or "").strip()
        if not text:
            return np.zeros(0, dtype=np.float32)
        with self._lock:  # snapshot the current voice style for this whole utterance
            style = self._style
        wav, _duration = self._tts.synthesize(
            text=text,
            voice_style=style,
            lang=self._lang,
        )
        # SuperTonic returns float32 shape (1, N) in [-1, 1]; the engine wants mono 1-D.
        return np.asarray(wav, dtype=np.float32).reshape(-1)
