"""Audio engineering agent: keeps the rendered edge ahead of the wall clock.

Owns the timeline. On startup it decides between fresh start / resume / fast-forward,
then each tick renders segments until `lookahead_sec` ahead of real time and flips
entry statuses based on the wall clock. Expiring what it has written is the
janitor's job — a station is reaped when nobody is listening, but its segments
outlive it.
"""

import asyncio

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..clock import StationClock, now
from ..config import settings
from ..db import agent_session
from ..models import (
    EntryStatus,
    HlsSegment,
    PlaylistEntry,
    StationState,
    Track,
    TrackKind,
)
from ..audio.hls import encode_segment, write_playlist
from ..audio.renderer import TimelineProducer
from . import wake_selector
from .base import AgentLoop


class Engineer(AgentLoop):
    def __init__(self, channel_id: int, slug: str) -> None:
        super().__init__(f"engineer[{slug}]", settings.engineer_interval_sec)
        self.clock: StationClock | None = None
        self.producer: TimelineProducer | None = None
        self.channel_id = channel_id
        self.slug = slug
        self.pending_discontinuity = False

    async def tick(self) -> None:
        async with agent_session() as session:
            if self.producer is None:
                await self._init(session)
                await session.commit()
            state = await session.get(StationState, self.channel_id)

            rendered = 0
            while (
                self.clock.rendered_edge(state.samples_rendered) - now()
            ).total_seconds() < settings.lookahead_sec:
                # Decode/mix the next segment's worth of audio, then release the DB
                # before the slow encode so other agents can write.
                await self.producer.ensure(settings.samples_per_segment, session)
                pcm = self.producer.take_segment()
                seq = state.next_seq
                await session.commit()

                seg_path = settings.hls_dir_for(self.slug) / f"seg{seq}.ts"
                await asyncio.to_thread(encode_segment, pcm, seg_path)

                session.add(
                    HlsSegment(
                        channel_id=self.channel_id,
                        seq=seq,
                        file_path=str(seg_path),
                        start_time=self.clock.seq_start(seq),
                        duration_sec=float(settings.segment_duration_sec),
                        discontinuity=self.pending_discontinuity,
                    )
                )
                self.pending_discontinuity = False
                state.next_seq = seq + 1
                state.samples_rendered += settings.samples_per_segment

                span = self.producer.span_at(state.samples_rendered)
                state.current_entry_id = span.entry_id if span else None
                state.entry_sample_offset = (
                    (state.samples_rendered - span.start) if span else 0
                )
                self.producer.prune_spans(
                    state.samples_rendered - settings.samples_per_segment
                )
                await session.commit()
                rendered += 1

            if rendered:
                self.log.debug(
                    "rendered %d segment(s), edge=%s",
                    rendered,
                    self.clock.rendered_edge(state.samples_rendered),
                )

            # Every tick, not just after rendering: the playlist publishes segments
            # as their airtime arrives, and the renderer works in bursts — it can
            # sit idle for a minute once it is `lookahead_sec` ahead. Only writing
            # here when `rendered` would leave the live edge frozen in between.
            await write_playlist(session, self.channel_id, self.slug)

            await self._flip_statuses(session)
            await session.commit()

    async def _init(self, session: AsyncSession) -> None:
        state = await session.get(StationState, self.channel_id)

        jingle = await session.scalar(
            select(Track).where(Track.kind == TrackKind.JINGLE).limit(1)
        )
        if jingle is None:
            raise RuntimeError(
                "No fallback jingle registered — bootstrap should have created one"
            )
        fallback_path = jingle.normalized_path or jingle.file_path

        t = now()
        if state.samples_rendered == 0:
            # Fresh timeline: (re-)anchor the epoch to right now.
            state.timeline_epoch = t
            state.next_seq = 0
            self.clock = StationClock(t)
            self.producer = TimelineProducer(
                self.clock, self.channel_id, 0, fallback_path, last_consumed_seq=-1
            )
            self.log.info("fresh timeline, epoch=%s", t)
            return

        self.clock = StationClock(state.timeline_epoch)
        edge = self.clock.rendered_edge(state.samples_rendered)

        if t < edge:
            # Fast restart while still ahead of real time: resume mid-entry.
            current = await session.scalar(
                select(PlaylistEntry)
                .options(
                    selectinload(PlaylistEntry.track),
                    selectinload(PlaylistEntry.voice_segment),
                )
                .where(
                    PlaylistEntry.channel_id == self.channel_id,
                    PlaylistEntry.actual_start <= edge,
                    PlaylistEntry.actual_end > edge,
                    PlaylistEntry.status.in_([EntryStatus.QUEUED, EntryStatus.PLAYING]),
                )
                .order_by(PlaylistEntry.seq.desc())
                .limit(1)
            )
            last_done = await session.scalar(
                select(PlaylistEntry.seq)
                .where(
                    PlaylistEntry.channel_id == self.channel_id,
                    PlaylistEntry.actual_end <= edge,
                    PlaylistEntry.actual_start.isnot(None),
                )
                .order_by(PlaylistEntry.seq.desc())
                .limit(1)
            )
            resume_offset = 0
            if current is not None:
                resume_offset = self.clock.time_to_samples(
                    edge
                ) - self.clock.time_to_samples(current.actual_start)
            self.producer = TimelineProducer(
                self.clock,
                self.channel_id,
                state.samples_rendered,
                fallback_path,
                last_consumed_seq=last_done if last_done is not None else -1,
                resume_entry=current,
                resume_offset=max(resume_offset, 0),
            )
            self.log.info(
                "resumed ahead of clock: edge=%s, entry=%s",
                edge,
                current.id if current else None,
            )
        else:
            # We fell behind real time: fast-forward the timeline with a discontinuity.
            new_seq = self.clock.seq_for_time(t) + 1
            state.next_seq = new_seq
            state.samples_rendered = new_seq * settings.samples_per_segment
            await session.execute(
                update(PlaylistEntry)
                .where(
                    PlaylistEntry.channel_id == self.channel_id,
                    PlaylistEntry.status == EntryStatus.PLAYING,
                )
                .values(status=EntryStatus.PLAYED)
            )
            last_played = await session.scalar(
                select(PlaylistEntry.seq)
                .where(
                    PlaylistEntry.channel_id == self.channel_id,
                    PlaylistEntry.status == EntryStatus.PLAYED,
                )
                .order_by(PlaylistEntry.seq.desc())
                .limit(1)
            )
            self.pending_discontinuity = True
            self.producer = TimelineProducer(
                self.clock,
                self.channel_id,
                state.samples_rendered,
                fallback_path,
                last_consumed_seq=last_played if last_played is not None else -1,
            )
            self.log.info("fast-forwarded past downtime: resuming at seq %d", new_seq)

    async def _flip_statuses(self, session: AsyncSession) -> None:
        t = now()
        await session.execute(
            update(PlaylistEntry)
            .where(
                PlaylistEntry.channel_id == self.channel_id,
                PlaylistEntry.status == EntryStatus.QUEUED,
                PlaylistEntry.actual_start <= t,
                PlaylistEntry.actual_end > t,
            )
            .values(status=EntryStatus.PLAYING)
        )
        finished = await session.execute(
            update(PlaylistEntry)
            .where(
                PlaylistEntry.channel_id == self.channel_id,
                PlaylistEntry.status.in_([EntryStatus.QUEUED, EntryStatus.PLAYING]),
                PlaylistEntry.actual_end <= t,
            )
            .values(status=EntryStatus.PLAYED)
        )
        if finished.rowcount:
            wake_selector(self.channel_id)
