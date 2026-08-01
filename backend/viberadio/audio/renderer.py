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

# A beat of the DJ in the clear, after the outgoing song has gone and before the next
# one comes up underneath. Without it the two ramps meet and the break has no seam.
DRY_SEC = 0.6


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

    async def _next_entries(self, session: AsyncSession) -> list[PlaylistEntry]:
        """The next queued entry, plus the one behind it.

        The second is only read to size the overlap: a song that knows a DJ break
        follows holds back more of its outro for the DJ to open over.
        """
        rows = await session.scalars(
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
            .limit(2)
        )
        return list(rows)

    @staticmethod
    def _hold_out_sec(
        kind: EntryKind | None, audio_sec: float, nxt: PlaylistEntry | None
    ) -> float:
        """How much of this entry to hold back for the next one to mix into.

        The held tail *is* the overlap, so this is the single place the shape of a
        DJ break is decided.
        """
        if kind == EntryKind.VOICE:
            # Everything past the DJ's opening rides the next song's intro, so the
            # break sits on music from end to end instead of on dead air.
            room = audio_sec - settings.voice_ramp_in_sec - DRY_SEC
            return max(0.0, min(settings.voice_ramp_out_max_sec, room))
        if nxt is not None and nxt.kind == EntryKind.VOICE:
            # Never more than half the break, or the outgoing song would still be
            # ducking away after the DJ has finished talking. When the break has not
            # been queued yet this peek misses and the opening falls back to a plain
            # crossfade — shorter, but never wrong.
            half = (nxt.duration_sec or 0.0) / 2.0
            if half > 0:
                return min(settings.voice_ramp_in_sec, half)
        return settings.crossfade_sec

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
        hold_out: int,
        entry_id: int | None,
        kind: EntryKind | None = None,
    ) -> None:
        """Mix `audio`'s head with the held tail, append body, hold a new tail.

        Whatever the previous entry held back is the overlap — all of it is used, so
        no audio is ever silently truncated. Song→song is an equal-power crossfade;
        any edge involving the DJ ducks the music underneath instead, so the words
        stay on top of a record that is still playing rather than replacing it.
        """
        if self.tail is not None:
            n = min(len(self.tail), len(audio))
            if n > 0:
                edge = int(settings.voice_duck_edge_sec * settings.sample_rate)
                if kind == EntryKind.VOICE and self.prev_kind != EntryKind.VOICE:
                    mixed = pcmlib.duck_mix(
                        self.tail[:n],
                        audio[:n],
                        settings.voice_duck_db,
                        music_out=True,
                        edge=edge,
                    )
                elif self.prev_kind == EntryKind.VOICE and kind != EntryKind.VOICE:
                    mixed = pcmlib.duck_mix(
                        audio[:n],
                        self.tail[:n],
                        settings.voice_duck_db,
                        music_out=False,
                        edge=edge,
                    )
                else:
                    mixed = pcmlib.equal_power_mix(self.tail[:n], audio[:n])
                audio = np.concatenate([mixed, audio[n:]])
                # Tail beyond the incoming audio is dropped, which only happens when an
                # entry is shorter than the overlap — _hold_out_sec caps every overlap
                # at half of what follows it, so that region is already faded to silence.
        start = self.produced
        # Never hold back the entire entry: the body is what makes `pending` grow.
        hold_out = min(hold_out, max(len(audio) - 1, 0))
        keep = max(len(audio) - hold_out, 0)
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
                    # Resuming into the middle of a break: what is left of it should
                    # still hand off to the next song the way a whole one would.
                    hold_sec = self._hold_out_sec(
                        entry.kind, len(audio) / settings.sample_rate, None
                    )
                    self._append(
                        audio,
                        int(hold_sec * settings.sample_rate),
                        entry.id,
                        entry.kind,
                    )
                    self.last_consumed_seq = entry.seq
                    log.info("resumed entry %d at sample offset %d", entry.id, offset)
                    continue

            queued = await self._next_entries(session)
            entry = queued[0] if queued else None
            if entry is not None:
                path = self._audio_path(entry)
                if path is None:
                    log.warning("entry %d has no audio; cancelling", entry.id)
                    entry.status = EntryStatus.CANCELLED
                    self.last_consumed_seq = entry.seq
                    continue
                audio = await asyncio.to_thread(pcmlib.decode, path)
                audio_sec = len(audio) / settings.sample_rate
                hold_sec = self._hold_out_sec(
                    entry.kind, audio_sec, queued[1] if len(queued) > 1 else None
                )
                start = self.produced
                self._append(
                    audio,
                    int(hold_sec * settings.sample_rate),
                    entry.id,
                    entry.kind,
                )
                entry.actual_start = self.clock.samples_to_time(start)
                entry.actual_end = self.clock.samples_to_time(start + len(audio))
                entry.duration_sec = audio_sec
                self.last_consumed_seq = entry.seq
                log.info(
                    "rendered entry %d (%s) at %s (+%.1fs, %.1fs held for the next)",
                    entry.id,
                    entry.kind.value,
                    entry.actual_start.strftime("%H:%M:%S"),
                    entry.duration_sec,
                    hold_sec,
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
        """The entry span containing an absolute timeline sample (for checkpointing).

        Spans overlap now that the DJ talks over both neighbouring records, so the
        answer is the *latest* entry to have started — during a break riding the next
        song's intro, what is playing is the song. Taking the first match instead
        would checkpoint the break, and a restart there would replay it.
        """
        for span in reversed(self.spans):
            if span.start <= sample < span.end:
                return span
        return None

    def prune_spans(self, before_sample: int) -> None:
        self.spans = [s for s in self.spans if s.end > before_sample]
