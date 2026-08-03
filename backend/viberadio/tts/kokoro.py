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


def _write_wav(samples: np.ndarray, sample_rate: int, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    pcm16 = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm16 = (pcm16 * 32767).astype(np.int16)
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())
    return dst


class KokoroTTS:
    def __init__(self, voice: str | None = None, speed: float | None = None):
        self.voice = voice or settings.tts_voice
        self.speed = speed if speed is not None else settings.tts_speed

    def synthesize(self, text: str, dst: Path) -> Path:
        samples, sample_rate = _load().create(
            text, voice=self.voice, speed=self.speed, lang="en-us"
        )
        return _write_wav(samples, sample_rate, dst)

    def synthesize_turns(self, turns: list[tuple[str, str]], dst: Path) -> Path:
        """Speak an exchange — `(voice, text)` per turn — into one file.

        Two people talking is still one item on the playlist: the turns are put
        end to end here, before anything else touches the audio, so what the
        renderer is handed is the same single voice file an ordinary break
        produces. Every Kokoro voice shares a sample rate, so the join is a
        concatenation with a beat of silence at each seam.
        """
        kokoro = _load()
        rate: int | None = None
        pieces: list[np.ndarray] = []
        for voice, text in turns:
            if not text.strip():
                continue
            samples, sample_rate = kokoro.create(
                text, voice=voice, speed=self.speed, lang="en-us"
            )
            if pieces:
                gap = int(settings.news_turn_gap_sec * sample_rate)
                pieces.append(np.zeros(gap, dtype=np.float32))
            rate = sample_rate
            pieces.append(np.asarray(samples, dtype=np.float32))
        if not pieces or rate is None:
            raise ValueError("nothing to say")
        return _write_wav(np.concatenate(pieces), rate, dst)
