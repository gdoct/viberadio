"""HLS output: encode PCM slices to MPEG-TS segments and maintain the live m3u8."""

import os
import subprocess
from pathlib import Path

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import now
from ..config import settings
from ..models import HlsSegment


def encode_segment(pcm: np.ndarray, dst: Path) -> None:
    """Encode a PCM slice (float32 stereo) to an AAC/MPEG-TS segment file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "f32le",
            "-ar",
            str(settings.sample_rate),
            "-ac",
            str(settings.channels),
            "-i",
            "pipe:",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-f",
            "mpegts",
            str(dst),
        ],
        input=pcm.astype(np.float32).tobytes(),
        check=True,
        capture_output=True,
    )


async def write_playlist(session: AsyncSession, channel_id: int, slug: str) -> None:
    """Rewrite a station's playlist.m3u8 from segments that have gone to air (atomic).

    The renderer deliberately runs up to `lookahead_sec` ahead of the wall clock, but
    that lead is a safety buffer — not programme material. Publishing it would put
    listeners ahead of the station's own clock, so that `GET /api/station` (which
    reports the entry airing *now*) describes a moment the listener passed a minute
    ago. Only segments whose airtime has arrived belong in the playlist.
    """
    window = (
        await session.scalars(
            select(HlsSegment)
            .where(
                HlsSegment.channel_id == channel_id,
                HlsSegment.start_time <= now(),
            )
            .order_by(HlsSegment.seq.desc())
            .limit(settings.hls_window_segments)
        )
    ).all()
    if not window:
        return
    window = list(reversed(window))
    first_seq = window[0].seq

    disc_before = (
        await session.scalar(
            select(func.count())
            .select_from(HlsSegment)
            .where(
                HlsSegment.channel_id == channel_id,
                HlsSegment.discontinuity.is_(True),
                HlsSegment.seq < first_seq,
            )
        )
        or 0
    )

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{settings.segment_duration_sec}",
        f"#EXT-X-MEDIA-SEQUENCE:{first_seq}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{disc_before}",
    ]
    for seg in window:
        if seg.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        stamp = seg.start_time.isoformat().replace("+00:00", "Z")
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp}")
        lines.append(f"#EXTINF:{seg.duration_sec:.3f},")
        lines.append(f"seg{seg.seq}.ts")

    out_dir = settings.hls_dir_for(slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "playlist.m3u8"
    tmp = path.with_suffix(".m3u8.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)
