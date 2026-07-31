"""The continuous PCM timeline.

The producer decodes playlist entries one after another into a single un-broken PCM
stream, applying crossfades sample-accurately in memory. The engineer then slices the
stream into exact segment-sized blocks — segment boundaries never interact with
musical boundaries.

Positions are absolute timeline samples (sample 0 == timeline epoch).
"""

import asyncio
import logging
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..clock import StationClock
from ..config import settings
from ..models import EntryKind, EntryStatus, PlaylistEntry
from . import pcm as pcmlib

log = logging.getLogger("renderer")

FALLBACK_CROSSFADE_SEC = 0.5


@dataclass
class Span:
    """Timeline span occupied by a rendered playlist entry."""

    start: int
    end: int
    entry_id: int


class TimelineProducer:
    def __init__(
        self,
        clock: StationClock,
        channel_id: int,
        produced_samples: int,
        fallback_path: str,
        last_consumed_seq: int,
        resume_entry: PlaylistEntry | None = None,
        resume_offset: int = 0,
    ):
        self.clock = clock
        self.channel_id = channel_id
        self.produced = (
            produced_samples  # absolute timeline sample of the pending buffer's end
        )
        self.pending = np.zeros((0, settings.channels), dtype=np.float32)
        self.tail: np.ndarray | None = None
        self.fallback_path = fallback_path
        self.last_consumed_seq = last_consumed_seq
        self.spans: list[Span] = []
        self._resume_entry = resume_entry
        self._resume_offset = resume_offset
        self.prev_kind: EntryKind | None = None

    async def _next_entry(self, session: AsyncSession) -> PlaylistEntry | None:
        return await session.scalar(
            select(PlaylistEntry)
            .options(
                selectinload(PlaylistEntry.track),
                selectinload(PlaylistEntry.voice_segment),
            )
            .where(
                PlaylistEntry.channel_id == self.channel_id,
                PlaylistEntry.status.in_([EntryStatus.QUEUED, EntryStatus.PLAYING]),
                PlaylistEntry.seq > self.last_consumed_seq,
            )
            .order_by(PlaylistEntry.seq)
            .limit(1)
        )

    @staticmethod
    def _audio_path(entry: PlaylistEntry) -> str | None:
        if entry.kind == EntryKind.SONG and entry.track is not None:
            return entry.track.normalized_path or entry.track.file_path
        if entry.kind == EntryKind.VOICE and entry.voice_segment is not None:
            return entry.voice_segment.audio_path
        return None

    def _append(
        self,
        audio: np.ndarray,
        crossfade: int,
        entry_id: int | None,
        kind: EntryKind | None = None,
    ) -> None:
        """Mix `audio`'s head with the held tail, append body, hold new tail.

        Song→song is an equal-power crossfade. Any transition involving the DJ ducks the
        music under the voice instead, so the words stay intelligible.
        """
        if self.tail is not None:
            n = min(len(self.tail), len(audio), crossfade)
            if n > 0:
                if kind == EntryKind.VOICE and self.prev_kind != EntryKind.VOICE:
                    mixed = pcmlib.duck_mix(
                        self.tail[:n], audio[:n], settings.voice_duck_db, music_out=True
                    )
                elif self.prev_kind == EntryKind.VOICE and kind != EntryKind.VOICE:
                    mixed = pcmlib.duck_mix(
                        audio[:n],
                        self.tail[:n],
                        settings.voice_duck_db,
                        music_out=False,
                    )
                else:
                    mixed = pcmlib.equal_power_mix(self.tail[:n], audio[:n])
                audio = np.concatenate([mixed, audio[n:]])
                # any tail beyond the mixable window is dropped (only when audio is very short)
        start = self.produced
        keep = max(len(audio) - crossfade, 0)
        new_tail = audio[keep:] if keep < len(audio) else None
        body = audio[:keep]
        self.pending = np.concatenate([self.pending, body])
        self.produced += len(body)
        self.tail = new_tail
        self.prev_kind = kind
        if entry_id is not None:
            self.spans.append(
                Span(start=start, end=start + len(audio), entry_id=entry_id)
            )

    async def ensure(self, min_pending: int, session: AsyncSession) -> None:
        """Decode entries (or the fallback jingle) until >= min_pending samples buffered.

        Commits after each entry so no write transaction is held across a decode —
        SQLite has a single writer and the other agents need it.
        """
        while len(self.pending) < min_pending:
            await session.commit()
            if self._resume_entry is not None:
                entry, offset = self._resume_entry, self._resume_offset
                self._resume_entry = None
                path = self._audio_path(entry)
                if path is not None:
                    audio = await asyncio.to_thread(pcmlib.decode, path)
                    audio = audio[offset:]
                    pcmlib.fade_in(audio, int(0.05 * settings.sample_rate))
                    self.tail = None
                    self._append(
                        audio,
                        int(settings.crossfade_sec * settings.sample_rate),
                        entry.id,
                        entry.kind,
                    )
                    self.last_consumed_seq = entry.seq
                    log.info("resumed entry %d at sample offset %d", entry.id, offset)
                    continue

            entry = await self._next_entry(session)
            if entry is not None:
                path = self._audio_path(entry)
                if path is None:
                    log.warning("entry %d has no audio; cancelling", entry.id)
                    entry.status = EntryStatus.CANCELLED
                    self.last_consumed_seq = entry.seq
                    continue
                audio = await asyncio.to_thread(pcmlib.decode, path)
                voice_edge = EntryKind.VOICE in (entry.kind, self.prev_kind)
                xf_sec = (
                    settings.voice_overlap_sec if voice_edge else settings.crossfade_sec
                )
                xf = int(xf_sec * settings.sample_rate)
                start = self.produced
                self._append(audio, xf, entry.id, entry.kind)
                entry.actual_start = self.clock.samples_to_time(start)
                entry.actual_end = self.clock.samples_to_time(start + len(audio))
                entry.duration_sec = len(audio) / settings.sample_rate
                self.last_consumed_seq = entry.seq
                log.info(
                    "rendered entry %d (%s) at %s (+%.1fs)",
                    entry.id,
                    entry.kind.value,
                    entry.actual_start.strftime("%H:%M:%S"),
                    entry.duration_sec,
                )
            else:
                audio = await asyncio.to_thread(pcmlib.decode, self.fallback_path)
                self._append(
                    audio, int(FALLBACK_CROSSFADE_SEC * settings.sample_rate), None
                )
                log.info(
                    "no queued entries — filled with fallback jingle (%.1fs)",
                    len(audio) / settings.sample_rate,
                )

    def take_segment(self) -> np.ndarray:
        n = settings.samples_per_segment
        assert len(self.pending) >= n
        out = self.pending[:n]
        self.pending = self.pending[n:]
        return out

    def span_at(self, sample: int) -> Span | None:
        """The entry span containing an absolute timeline sample (for checkpointing)."""
        for span in self.spans:
            if span.start <= sample < span.end:
                return span
        return None

    def prune_spans(self, before_sample: int) -> None:
        self.spans = [s for s in self.spans if s.end > before_sample]
