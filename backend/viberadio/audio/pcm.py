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


def _duck_envelope(n: int, start: float, hold: float, end: float, edge: int) -> np.ndarray:
    """`start` -> `hold` over `edge` samples, hold there, then `hold` -> `end`.

    A real ducker moves quickly and then sits still. Sliding the gain linearly across
    the whole overlap instead — which is what a crossfade does — makes the music
    audibly swell through the DJ's sentence, and that is the tell.
    """
    edge = max(1, min(edge, n // 2))
    env = np.full(n, hold, dtype=np.float32)
    env[:edge] = np.linspace(start, hold, edge, dtype=np.float32)
    env[n - edge :] = np.linspace(hold, end, edge, dtype=np.float32)
    return env


def duck_mix(
    music: np.ndarray, voice: np.ndarray, duck_db: float, music_out: bool, edge: int
) -> np.ndarray:
    """Overlap music and voice with the music held down underneath.

    Unlike a crossfade the voice stays at full level throughout — it is the thing the
    listener must hear. `music_out` picks the direction: True when the outgoing song
    is ducking away under the top of the break, False when the next song has come up
    underneath and opens to full as the DJ hits the post.
    """
    n = min(len(music), len(voice))
    gain = float(10.0 ** (duck_db / 20.0))
    env = (
        _duck_envelope(n, 1.0, gain, 0.0, edge)
        if music_out
        else _duck_envelope(n, 0.0, gain, 1.0, edge)
    )
    mixed = music[:n] * env[:, None] + voice[:n]
    return np.clip(mixed, -1.0, 1.0)


def fade_in(pcm: np.ndarray, n: int) -> np.ndarray:
    """In-place linear fade-in over the first n samples (used after restart resume)."""
    n = min(n, len(pcm))
    if n > 0:
        pcm[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
    return pcm
