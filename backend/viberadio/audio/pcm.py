"""PCM decode + crossfade math. Everything is float32 stereo at the station sample rate."""

import subprocess

import numpy as np

from ..config import settings


def decode(path: str) -> np.ndarray:
    """Decode any audio file to float32 stereo PCM at the station rate, shape (N, 2)."""
    out = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-f",
            "f32le",
            "-ar",
            str(settings.sample_rate),
            "-ac",
            str(settings.channels),
            "pipe:",
        ],
        capture_output=True,
        check=True,
    )
    return (
        np.frombuffer(out.stdout, dtype=np.float32)
        .reshape(-1, settings.channels)
        .copy()
    )


def equal_power_mix(tail: np.ndarray, head: np.ndarray) -> np.ndarray:
    """Equal-power crossfade of the outgoing tail with the incoming head (same length)."""
    n = len(tail)
    t = np.linspace(0.0, np.pi / 2, n, dtype=np.float32)[:, None]
    mixed = tail * np.cos(t) + head * np.sin(t)
    return np.clip(mixed, -1.0, 1.0)


def duck_mix(
    music: np.ndarray, voice: np.ndarray, duck_db: float, music_out: bool
) -> np.ndarray:
    """Overlap music and voice with the music ducked underneath.

    Unlike a crossfade the voice stays at full level throughout — it is the thing the
    listener must hear. `music_out` picks the direction: True when the music is ending
    under the DJ, False when the next song rises as the DJ finishes.
    """
    n = min(len(music), len(voice))
    gain = float(10.0 ** (duck_db / 20.0))
    ramp = (
        np.linspace(1.0, gain, n, dtype=np.float32)
        if music_out
        else np.linspace(gain, 1.0, n, dtype=np.float32)
    )
    if music_out:
        ramp = ramp * np.linspace(1.0, 0.0, n, dtype=np.float32) ** 0.5
    mixed = music[:n] * ramp[:, None] + voice[:n]
    return np.clip(mixed, -1.0, 1.0)


def fade_in(pcm: np.ndarray, n: int) -> np.ndarray:
    """In-place linear fade-in over the first n samples (used after restart resume)."""
    n = min(n, len(pcm))
    if n > 0:
        pcm[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
    return pcm
