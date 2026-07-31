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


def normalize(src: Path, dst: Path) -> None:
    """Loudness-normalize to TARGET_LUFS, resample to the station format (48kHz stereo AAC)."""
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
            f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11",
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
