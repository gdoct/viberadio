"""Voice segment agent.

Per the spec: a playlist update is only accepted when the voice segment for the next
break is ready in time. If a valid segment already exists, the update activates
immediately. Otherwise the update stays pending while the script + TTS are generated;
if that finishes before the renderer commits the transition, the segment is inserted and
the update activates. If it is late, the update is rejected, the drafts are cancelled,
and the song selector is asked to replan — the previously accepted playlist is untouched,
so the revert is implicit.
"""

import asyncio
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..clock import StationClock, now
from ..config import settings
from ..db import agent_session
from ..library.media import normalize, probe_duration
from ..llm import prompts
from ..llm.client import LLMError, ask_json
from ..llm.schemas import DJScript
from ..models import (
    Channel,
    EntryKind,
    EntryStatus,
    ListenerRequest,
    PlaylistEntry,
    PlaylistUpdate,
    StationState,
    Track,
    UpdateStatus,
    VoiceKind,
    VoiceSegment,
    VoiceStatus,
)
from ..tts.kokoro import KokoroTTS
from . import voice_event, wake_selector
from .base import AgentLoop


class Voice(AgentLoop):
    def __init__(self, channel_id: int, slug: str, tts_voice: str) -> None:
        super().__init__(
            f"voice[{slug}]",
            settings.voice_interval_sec,
            wake=voice_event(channel_id),
        )
        self.channel_id = channel_id
        # Each station's DJ gets their own Kokoro voice.
        self.tts = KokoroTTS(voice=tts_voice)

    async def tick(self) -> None:
        async with agent_session() as session:
            update = await session.scalar(
                select(PlaylistUpdate)
                .where(
                    PlaylistUpdate.channel_id == self.channel_id,
                    PlaylistUpdate.status == UpdateStatus.PENDING,
                )
                .order_by(PlaylistUpdate.created_at)
                .limit(1)
            )
            if update is None:
                return

            channel = await session.get(Channel, self.channel_id)
            drafts = (
                await session.scalars(
                    select(PlaylistEntry)
                    .options(selectinload(PlaylistEntry.track))
                    .where(
                        PlaylistEntry.update_id == update.id,
                        PlaylistEntry.status == EntryStatus.DRAFT,
                    )
                    .order_by(PlaylistEntry.seq)
                )
            ).all()
            if not drafts:
                await self._activate(session, update, [], None)
                return

            first_song = next((d for d in drafts if d.kind == EntryKind.SONG), None)
            prev_track = await self._last_committed_track(
                session, first_song.seq if first_song else None
            )
            next_track = first_song.track if first_song else None

            existing = await self._valid_segment(session, prev_track, next_track)
            if existing is not None:
                await self._activate(session, update, drafts, existing)
                return

            segment = await self._generate(session, channel, prev_track, next_track)
            if segment is None:
                await self._reject(session, update, drafts, "voice_generation_failed")
                return

            if not await self._in_time(session, first_song):
                await self._reject(session, update, drafts, "voice_deadline_missed")
                return

            await self._activate(session, update, drafts, segment)

    async def _last_committed_track(
        self, session: AsyncSession, before_seq: int | None
    ) -> Track | None:
        """The song that will be on air just before this update's first new song."""
        q = (
            select(PlaylistEntry)
            .options(selectinload(PlaylistEntry.track))
            .where(
                PlaylistEntry.channel_id == self.channel_id,
                PlaylistEntry.kind == EntryKind.SONG,
                PlaylistEntry.status.in_(
                    [EntryStatus.QUEUED, EntryStatus.PLAYING, EntryStatus.PLAYED]
                ),
            )
            .order_by(PlaylistEntry.seq.desc())
            .limit(1)
        )
        if before_seq is not None:
            q = q.where(PlaylistEntry.seq < before_seq)
        entry = await session.scalar(q)
        return entry.track if entry else None

    async def _valid_segment(
        self, session: AsyncSession, prev_track: Track | None, next_track: Track | None
    ) -> VoiceSegment | None:
        """A ready, unused segment recorded for exactly this transition."""
        used = select(PlaylistEntry.voice_segment_id).where(
            PlaylistEntry.voice_segment_id.isnot(None)
        )
        return await session.scalar(
            select(VoiceSegment)
            .where(
                VoiceSegment.channel_id == self.channel_id,
                VoiceSegment.status == VoiceStatus.READY,
                VoiceSegment.prev_track_id == (prev_track.id if prev_track else None),
                VoiceSegment.next_track_id == (next_track.id if next_track else None),
                VoiceSegment.id.notin_(used),
            )
            .limit(1)
        )

    async def _in_time(
        self, session: AsyncSession, first_song: PlaylistEntry | None
    ) -> bool:
        """True while the renderer has not yet committed audio up to the transition point.

        The renderer only ever renders `queued` entries, so an un-activated update's
        transition is at the end of the last already-committed entry.
        """
        state = await session.get(StationState, self.channel_id)
        clock = StationClock(state.timeline_epoch)
        rendered_edge = clock.rendered_edge(state.samples_rendered)

        last_end = await session.scalar(
            select(func.max(PlaylistEntry.actual_end)).where(
                PlaylistEntry.channel_id == self.channel_id,
                PlaylistEntry.status.in_([EntryStatus.QUEUED, EntryStatus.PLAYING]),
            )
        )
        if last_end is None:
            # Nothing committed ahead: the transition happens as soon as we activate.
            return True
        deadline = last_end - timedelta(seconds=settings.voice_safety_margin_sec)
        ok = rendered_edge < deadline
        if not ok:
            self.log.warning(
                "voice deadline missed: rendered edge %s >= deadline %s",
                rendered_edge.strftime("%H:%M:%S"),
                deadline.strftime("%H:%M:%S"),
            )
        return ok

    async def _generate(
        self,
        session: AsyncSession,
        channel: Channel,
        prev_track: Track | None,
        next_track: Track | None,
    ) -> VoiceSegment | None:
        request = None
        if next_track is not None:
            request = await session.scalar(
                select(ListenerRequest)
                .where(
                    ListenerRequest.channel_id == self.channel_id,
                    ListenerRequest.matched_track_id == next_track.id,
                )
                .order_by(ListenerRequest.created_at.desc())
                .limit(1)
            )

        segment = VoiceSegment(
            channel_id=channel.id,
            kind=VoiceKind.REPLY if request else VoiceKind.TRANSITION,
            script="",
            status=VoiceStatus.GENERATING,
            prev_track_id=prev_track.id if prev_track else None,
            next_track_id=next_track.id if next_track else None,
            request_id=request.id if request else None,
        )
        session.add(segment)
        await session.commit()  # release the writer before script generation + TTS

        try:
            result = await ask_json(
                prompts.channel_system(channel),
                prompts.dj_script(
                    channel,
                    (prev_track.artist, prev_track.title) if prev_track else None,
                    (next_track.artist, next_track.title) if next_track else None,
                    request.message if request else None,
                    request.requester if request else None,
                ),
                DJScript,
            )
        except LLMError as e:
            self.log.warning("script generation failed: %s", e)
            segment.status = VoiceStatus.FAILED
            await session.commit()
            return None

        raw = settings.voice_dir / f"voice-{segment.id}-raw.wav"
        dst = settings.voice_dir / f"voice-{segment.id}.m4a"
        try:
            await asyncio.to_thread(self.tts.synthesize, result.script, raw)
            # Match the DJ's loudness to the music so breaks don't jump in level.
            await asyncio.to_thread(normalize, raw, dst)
            raw.unlink(missing_ok=True)
            duration = await asyncio.to_thread(probe_duration, dst)
        except Exception as e:
            self.log.warning("TTS failed: %s", e)
            segment.status = VoiceStatus.FAILED
            await session.commit()
            return None

        segment.script = result.script
        segment.audio_path = str(dst)
        segment.duration_sec = duration
        segment.status = VoiceStatus.READY
        segment.ready_at = now()
        await session.commit()
        self.log.info(
            "voice segment %d ready (%.1fs): %s",
            segment.id,
            duration,
            result.script[:80],
        )
        return segment

    async def _activate(
        self,
        session: AsyncSession,
        update: PlaylistUpdate,
        drafts: list[PlaylistEntry],
        segment: VoiceSegment | None,
    ) -> None:
        """Accept the update: insert the voice entry, queue the drafts, assign planned starts."""
        entries = list(drafts)
        if segment is not None and drafts:
            first_song = next((d for d in drafts if d.kind == EntryKind.SONG), None)
            if first_song is not None:
                slot = first_song.seq
                # Shift the song (and everything after it in this batch) one slot later,
                # so the DJ break lands immediately before it.
                for d in drafts:
                    if d.seq >= slot:
                        d.seq += 1
                voice_entry = PlaylistEntry(
                    channel_id=update.channel_id,
                    update_id=update.id,
                    seq=slot,
                    kind=EntryKind.VOICE,
                    voice_segment_id=segment.id,
                    status=EntryStatus.QUEUED,
                    duration_sec=segment.duration_sec,
                )
                session.add(voice_entry)
                entries.append(voice_entry)

        cursor = await self._planned_cursor(session)
        for entry in sorted(entries, key=lambda e: e.seq):
            entry.status = EntryStatus.QUEUED
            entry.planned_start = cursor
            overlap = (
                settings.voice_overlap_sec
                if entry.kind == EntryKind.VOICE
                else settings.crossfade_sec
            )
            cursor = cursor + timedelta(seconds=(entry.duration_sec or 0) - overlap)

        update.status = UpdateStatus.ACTIVE
        update.decided_at = now()
        await session.commit()
        self.log.info(
            "activated playlist update %d (%d entries)", update.id, len(drafts)
        )

    async def _planned_cursor(self, session: AsyncSession):
        """Where the new entries start: after everything already committed."""
        last_end = await session.scalar(
            select(func.max(PlaylistEntry.actual_end)).where(
                PlaylistEntry.channel_id == self.channel_id,
                PlaylistEntry.status.in_([EntryStatus.QUEUED, EntryStatus.PLAYING]),
            )
        )
        return last_end or now()

    async def _reject(
        self,
        session: AsyncSession,
        update: PlaylistUpdate,
        drafts: list[PlaylistEntry],
        reason: str,
    ) -> None:
        for entry in drafts:
            entry.status = EntryStatus.CANCELLED
        update.status = UpdateStatus.REJECTED
        update.reason = reason
        update.decided_at = now()
        await session.commit()
        self.log.warning(
            "rejected playlist update %d: %s — asking selector to replan",
            update.id,
            reason,
        )
        wake_selector(self.channel_id)
