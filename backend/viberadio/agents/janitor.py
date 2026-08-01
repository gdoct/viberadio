"""Housekeeping: drops audio that nobody can reach any more.

Three things accumulate. HLS segments land at ~1 MB per station per minute and
are only useful for as long as a listener can rewind into them. Rendered DJ
audio is mixed into those segments as it goes to air, after which its source
file is never decoded again. And breaks that were spoken for a transition that
never happened sit in `data/voice` waiting for a pairing that will not come
back.

The janitor runs once for the whole process rather than once per station: a
station that has been reaped for being idle still has two hours of segments on
disk, and its agents are exactly the ones that are no longer there to collect
them.

Files are removed before their rows: a file with no row is picked up by the
orphan sweep, whereas a row with no file would make the playlist advertise a
segment that 404s.
"""

from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import now
from ..config import settings
from ..db import agent_session
from ..models import HlsSegment, PlaylistEntry, VoiceSegment
from .base import AgentLoop

# Backstop margin for the disk sweeps. A segment file is written up to
# `lookahead_sec` before the airtime its row is purged on, so sweeping strictly
# by file age has to lag the row purge or it would delete segments that are
# still inside the listen-back window.
ORPHAN_MARGIN_SEC = 600


def _unlink(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


class Janitor(AgentLoop):
    def __init__(self) -> None:
        super().__init__("janitor", settings.janitor_interval_sec)

    async def tick(self) -> None:
        async with agent_session() as session:
            segments = await self._purge_segments(session)
            voices = await self._purge_aired_voice_audio(session)
            unused = await self._purge_unused_voice_segments(session)
            await session.commit()
            orphans = await self._sweep_orphan_files(session)

        if segments or voices or unused or orphans:
            self.log.info(
                "purged %d segment(s), %d aired break(s), %d unused break(s), "
                "%d orphan file(s)",
                segments,
                voices,
                unused,
                orphans,
            )

    async def _purge_segments(self, session: AsyncSession) -> int:
        """Segment files (and rows) that have fallen out of the listen-back window."""
        cutoff = now() - timedelta(seconds=settings.listen_back_sec)
        old = (
            await session.scalars(
                select(HlsSegment).where(HlsSegment.start_time < cutoff)
            )
        ).all()
        for seg in old:
            _unlink(seg.file_path)
        if old:
            # Deleted by the same predicate rather than by id: two hours of
            # segments across three stations is well past SQLite's bound-parameter
            # limit, and a segment row is only ever written with an airtime in the
            # future, so nothing can slip in behind the cutoff between the two.
            await session.execute(
                delete(HlsSegment).where(HlsSegment.start_time < cutoff)
            )
        return len(old)

    async def _purge_aired_voice_audio(self, session: AsyncSession) -> int:
        """Drop the rendered audio of breaks that have finished broadcasting.

        The row stays: its script is what `/api/station` shows as the DJ's line and
        what the next break is written against, and it is a few hundred bytes.
        Keyed off the entry's airtime rather than its status, because the status is
        only flipped while the station's agents are up.
        """
        cutoff = now() - timedelta(seconds=settings.voice_audio_grace_sec)
        aired = select(PlaylistEntry.voice_segment_id).where(
            PlaylistEntry.voice_segment_id.isnot(None),
            PlaylistEntry.actual_end.isnot(None),
            PlaylistEntry.actual_end < cutoff,
        )
        rows = (
            await session.scalars(
                select(VoiceSegment).where(
                    VoiceSegment.audio_path.isnot(None),
                    VoiceSegment.id.in_(aired),
                )
            )
        ).all()
        for segment in rows:
            _unlink(segment.audio_path)
            segment.audio_path = None
        return len(rows)

    async def _purge_unused_voice_segments(self, session: AsyncSession) -> int:
        """Delete breaks that were spoken but never made it on to the playlist.

        A rejected update, or one whose TTS came in late, leaves a ready segment
        behind. `Voice._valid_segment` will reuse one if that exact transition comes
        round again, so these are only stale once they are older than the listen-back
        window — by then re-airing a break written about a pairing from two hours ago
        would be worse than writing a new one. Unreferenced by construction, so the
        row goes with the file.
        """
        cutoff = now() - timedelta(seconds=settings.listen_back_sec)
        used = select(PlaylistEntry.voice_segment_id).where(
            PlaylistEntry.voice_segment_id.isnot(None)
        )
        rows = (
            await session.scalars(
                select(VoiceSegment).where(
                    VoiceSegment.created_at < cutoff,
                    VoiceSegment.id.notin_(used),
                )
            )
        ).all()
        for segment in rows:
            if segment.audio_path:
                _unlink(segment.audio_path)
        if rows:
            await session.execute(
                delete(VoiceSegment).where(VoiceSegment.id.in_([r.id for r in rows]))
            )
        return len(rows)

    async def _sweep_orphan_files(self, session: AsyncSession) -> int:
        """Files on disk that no row accounts for.

        A kill between writing a file and committing its row, a TTS that died
        between the raw wav and the encode, a database reset — all leave audio that
        the row-driven purges above can never name. Age-gated well past the
        retention window so this only ever catches what those have already missed.
        """
        count = 0
        seg_cutoff = now().timestamp() - settings.listen_back_sec - ORPHAN_MARGIN_SEC
        for station_dir in self._subdirs(settings.hls_dir):
            for path in station_dir.glob("seg*.ts"):
                if self._mtime(path) < seg_cutoff:
                    _unlink(path)
                    count += 1

        voice_cutoff = now().timestamp() - settings.listen_back_sec - ORPHAN_MARGIN_SEC
        known = set(
            await session.scalars(
                select(VoiceSegment.audio_path).where(
                    VoiceSegment.audio_path.isnot(None)
                )
            )
        )
        if settings.voice_dir.is_dir():
            for path in settings.voice_dir.iterdir():
                if not path.is_file() or str(path) in known:
                    continue
                if self._mtime(path) < voice_cutoff:
                    _unlink(path)
                    count += 1
        return count

    @staticmethod
    def _subdirs(root: Path) -> list[Path]:
        if not root.is_dir():
            return []
        return [p for p in root.iterdir() if p.is_dir()]

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:  # vanished under us — nothing left to collect
            return float("inf")
