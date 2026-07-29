#!/usr/bin/env python3
"""Maintainer tool (NOT run at runtime): render the curated SuperTonic voice previews to OGG.

Generates ui/gnome-extension/ai-linux-assistant@local/samples/<id>.ogg for each curated voice.
Re-run only when the curated set (M1/M4/F1/F3) changes. Requires the AI_Linux conda env + ffmpeg.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from glados.TTS.tts_supertonic import SpeechSynthesizer as SuperTonicTTS

CURATED = ["M1", "M4", "F1", "F3"]
PHRASE = "Hi, I'm your Linux assistant. How can I help?"
OUT_DIR = Path(__file__).resolve().parents[1] / "ui/gnome-extension/ai-linux-assistant@local/samples"


def _write_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for vid in CURATED:
        tts = SuperTonicTTS(voice=vid)
        audio = tts.generate_speech_audio(PHRASE)
        rate = int(tts.sample_rate)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav_path = Path(tf.name)
        _write_wav(wav_path, np.asarray(audio, dtype="float32"), rate)
        ogg_path = OUT_DIR / f"{vid}.ogg"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
             "-c:a", "libvorbis", "-q:a", "3", str(ogg_path)],
            check=True,
        )
        wav_path.unlink(missing_ok=True)
        print(f"  wrote {ogg_path}  ({ogg_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
