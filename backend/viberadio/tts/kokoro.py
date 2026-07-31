"""Kokoro local TTS. Model files live under data/models/ (downloaded once, see README)."""

import logging
import wave
from pathlib import Path

import numpy as np

from ..config import settings

log = logging.getLogger("tts")

_kokoro = None


def _load():
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro

        model = settings.models_dir / "kokoro-v1.0.onnx"
        voices = settings.models_dir / "voices-v1.0.bin"
        if not model.exists() or not voices.exists():
            raise RuntimeError(
                f"Kokoro model files missing in {settings.models_dir}. See README for the download step."
            )
        _kokoro = Kokoro(str(model), str(voices))
        log.info("Loaded Kokoro model")
    return _kokoro


class KokoroTTS:
    def __init__(self, voice: str | None = None, speed: float | None = None):
        self.voice = voice or settings.tts_voice
        self.speed = speed if speed is not None else settings.tts_speed

    def synthesize(self, text: str, dst: Path) -> Path:
        samples, sample_rate = _load().create(
            text, voice=self.voice, speed=self.speed, lang="en-us"
        )
        dst.parent.mkdir(parents=True, exist_ok=True)
        pcm16 = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
        pcm16 = (pcm16 * 32767).astype(np.int16)
        with wave.open(str(dst), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm16.tobytes())
        return dst
