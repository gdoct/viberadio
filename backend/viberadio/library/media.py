"""Media library: register audio files, probe durations, loudness-normalize at ingest.

Normalization happens once here (ffmpeg loudnorm to -16 LUFS, 48kHz stereo) so the
runtime renderer only does crossfade math on uniform PCM.
"""

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Track, TrackKind

log = logging.getLogger(__name__)

TARGET_LUFS = -16.0

# Downloads — and TTS reads — routinely carry a second or two of digital silence at
# each end. That silence lands exactly where the next song crossfades in and where the
# DJ opens over the outro, so a break can end up riding a dead file instead of a
# record. -50dB is well below any real fade, so this trims padding and not music.
_DESILENCE = (
    "silenceremove=start_periods=1:start_threshold=-50dB:start_duration=0:detection=rms"
)
TRIM_SILENCE = f"{_DESILENCE},areverse,{_DESILENCE},areverse"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "track"


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def _voice_filters() -> str:
    """The DJ's channel strip, in the order a real desk would run it.

    Kokoro delivers a clean, even, quiet read. Clean and even is exactly wrong here:
    the music underneath is only ducked, not removed, so an unprocessed voice sinks
    into it. Presence and compression are what let the words sit on top of a record,
    and the drive is what stops it sounding like a file being played back.
    """
    drive = max(0.0, min(1.0, settings.voice_drive))
    return ",".join(
        [
            # Trailing silence on a read is worse than on a song: it is counted as
            # break length, so the ramps end up scheduled around nothing.
            TRIM_SILENCE,
            "highpass=f=85",  # kill rumble the mic would never have picked up
            "equalizer=f=220:t=q:w=1.0:g=-3",  # clear the mud so it sits above the mix
            "equalizer=f=2800:t=q:w=1.4:g=5",  # presence: this is what cuts the duck
            # Dense and forward — a broadcast chain rides much harder than mastering.
            "acompressor=threshold=0.1:ratio=5:attack=4:release=90:knee=4:makeup=2",
            # Soft clip rather than distort: at drive=0 the threshold is 1.0 (a no-op).
            f"asoftclip=type=tanh:threshold={1.0 - 0.55 * drive:.3f}:oversample=4",
            "alimiter=limit=0.95:attack=1:release=40",
            f"loudnorm=I={settings.voice_target_lufs}:TP=-1.0:LRA=7",
        ]
    )


def process_voice(src: Path, dst: Path) -> None:
    """Run a TTS read through the voice chain into the station format.

    Used instead of `normalize` for DJ audio: same output format, different treatment.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(src),
            "-af",
            _voice_filters(),
            "-ar",
            str(settings.sample_rate),
            "-ac",
            str(settings.channels),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(dst),
        ],
        check=True,
        capture_output=True,
    )


def normalize(src: Path, dst: Path) -> None:
    """Trim dead air, loudness-normalize to TARGET_LUFS, resample to 48kHz stereo AAC."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(src),
            "-af",
            f"{TRIM_SILENCE},loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11",
            "-ar",
            str(settings.sample_rate),
            "-ac",
            str(settings.channels),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(dst),
        ],
        check=True,
        capture_output=True,
    )


def _ingest_sync(src: Path, title: str, artist: str | None) -> tuple[Path, float]:
    stem = _slug(f"{artist or ''} {title}")
    dst = settings.normalized_dir / f"{stem}.m4a"
    i = 1
    while dst.exists():
        dst = settings.normalized_dir / f"{stem}-{i}.m4a"
        i += 1
    normalize(src, dst)
    return dst, probe_duration(dst)


async def register_track(
    session: AsyncSession,
    src: Path,
    title: str,
    artist: str | None = None,
    kind: TrackKind = TrackKind.SONG,
    source_url: str | None = None,
) -> Track:
    """Normalize an audio file into the library and create the Track row."""
    normalized_path, duration = await asyncio.to_thread(
        _ingest_sync, src, title, artist
    )
    track = Track(
        kind=kind,
        title=title,
        artist=artist,
        file_path=str(src),
        normalized_path=str(normalized_path),
        duration_sec=duration,
        source_url=source_url,
    )
    session.add(track)
    await session.flush()
    log.info("Registered %s: %s — %s (%.1fs)", kind.value, artist, title, duration)
    return track


async def find_track(
    session: AsyncSession, artist: str | None, title: str
) -> Track | None:
    q = select(Track).where(
        Track.kind == TrackKind.SONG,
        func.lower(Track.title) == title.lower(),
    )
    if artist:
        q = q.where(func.lower(Track.artist) == artist.lower())
    return await session.scalar(q.limit(1))
