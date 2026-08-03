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
import json
from datetime import timedelta

from pyparsing import empty
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..audio import renderer
from ..clock import StationClock, now
from ..config import settings
from ..db import agent_session
from ..library.media import probe_duration, process_voice
from ..llm import prompts
from ..llm.client import LLMError, ask_json
from ..llm.schemas import DJScript, NewsDialog
from ..models import (
    Channel,
    EntryKind,
    EntryStatus,
    ListenerRequest,
    NewsSegment,
    NewsSegmentKind,
    PlaylistEntry,
    PlaylistUpdate,
    ProgrammeSlot,
    SlotKind,
    SlotStatus,
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
    def __init__(
        self, channel_id: int, slug: str, tts_voice: str, news_tts_voice: str
    ) -> None:
        super().__init__(
            f"voice[{slug}]",
            settings.voice_interval_sec,
            wake=voice_event(channel_id),
        )
        self.channel_id = channel_id
        # Each station's DJ gets their own Kokoro voice, and the anchor another:
        # they talk to each other, so one voice would not be a conversation.
        self.tts = KokoroTTS(voice=tts_voice)
        self.dj_voice = tts_voice
        self.anchor_voice = news_tts_voice

    async def tick(self) -> None:
        # The newsroom's items are due on a mark, so they are recorded well
        # before the selector reaches them rather than at the last moment like a
        # break. Ahead of the break work: a bulletin waiting on Kokoro is a
        # bulletin that misses eleven o'clock.
        await self._record_upcoming_news()

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
            if channel is None or first_song is None:
                # A batch of newsroom items has no record for the DJ to open over
                # and nothing to introduce. It goes out as it stands.
                await self._activate(session, update, drafts, None)
                return

            if await self._talking_already(session, first_song.seq):
                # Whatever is in front of this batch is somebody speaking — a
                # bulletin, or a trail. Rolling a break against it would put two
                # voices back to back with no record between them.
                await self._activate(session, update, drafts, None)
                return

            prev_track = await self._last_committed_track(session, first_song.seq)
            next_track = first_song.track

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

    async def _talking_already(self, session: AsyncSession, before_seq: int) -> bool:
        """Is the item just before this song somebody speaking?

        Drafts count. A trail and the record it hands off to are promoted in the
        same batch, so at this point the trail is a draft sitting one seq behind
        the song — and it is precisely the case that must not have a rolled break
        pushed between them, or the DJ names a record that does not follow.
        """
        entry = await session.scalar(
            select(PlaylistEntry)
            .where(
                PlaylistEntry.channel_id == self.channel_id,
                PlaylistEntry.seq < before_seq,
                PlaylistEntry.status != EntryStatus.CANCELLED,
            )
            .order_by(PlaylistEntry.seq.desc())
            .limit(1)
        )
        return entry is not None and entry.kind == EntryKind.VOICE

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

    async def _recent_breaks(self, session: AsyncSession) -> list[VoiceSegment]:
        """The DJ's last few breaks, most recent first.

        Fed back into the prompt so the show has continuity: without it every break
        is a cold one-shot, which is why they all open the same way and no bit ever
        pays off. Ordered by id rather than created_at — SQLite timestamps are
        second-resolution and two breaks can tie.
        """
        rows = await session.scalars(
            select(VoiceSegment)
            .where(
                VoiceSegment.channel_id == self.channel_id,
                VoiceSegment.status == VoiceStatus.READY,
                VoiceSegment.script != "",
            )
            .order_by(VoiceSegment.id.desc())
            .limit(settings.voice_history_breaks)
        )
        return list(rows)

    async def _in_time(
        self, session: AsyncSession, first_song: PlaylistEntry | None
    ) -> bool:
        """True while the renderer has not yet committed audio up to the transition point.

        The renderer only ever renders `queued` entries, so an un-activated update's
        transition is at the end of the last already-committed entry.
        """
        state = await session.get(StationState, self.channel_id)
        assert state is not None, "StationState should exist for a running station"
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

        history = await self._recent_breaks(session)
        last_kind = history[0].break_kind if history else None
        break_kind = prompts.pick_break_kind(exclude=last_kind)

        segment = VoiceSegment(
            channel_id=channel.id,
            kind=VoiceKind.REPLY if request else VoiceKind.TRANSITION,
            break_kind=break_kind,
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
                    break_kind,
                    prev_song=(prev_track.artist, prev_track.title)
                    if prev_track
                    else None,
                    next_song=(next_track.artist, next_track.title)
                    if next_track
                    else None,
                    request_message=request.message if request else None,
                    requester=request.requester if request else None,
                    recent_scripts=[s.script for s in history],
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
            # Voice chain, not plain normalization: the DJ has to sit on top of a
            # record that is still playing underneath them.
            await asyncio.to_thread(process_voice, raw, dst)
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
            "voice segment %d ready [%s] (%.1fs): %s",
            segment.id,
            "request" if request else break_kind,
            duration,
            result.script[:80],
        )
        return segment

    # ---- the newsroom ----------------------------------------------------

    async def _record_upcoming_news(self) -> None:
        """Speak any news item whose airtime is close enough to prepare for."""
        async with agent_session() as session:
            due = (
                await session.scalars(
                    select(ProgrammeSlot)
                    .where(
                        ProgrammeSlot.channel_id == self.channel_id,
                        ProgrammeSlot.status == SlotStatus.PLANNED,
                        ProgrammeSlot.kind != SlotKind.SONG,
                        ProgrammeSlot.voice_segment_id.is_(None),
                        ProgrammeSlot.planned_start
                        <= now() + timedelta(seconds=settings.news_render_lead_sec),
                    )
                    .order_by(ProgrammeSlot.planned_start)
                    .limit(1)
                )
            ).all()
            for slot in due:
                channel = await session.get(Channel, self.channel_id)
                if channel is not None:
                    await self._record_news(session, channel, slot)

    async def _record_news(
        self, session: AsyncSession, channel: Channel, slot: ProgrammeSlot
    ) -> None:
        """Put one exchange on tape: the anchor's copy, the DJ either side of it.

        Both halves of a handover are written in one call, so the line the DJ
        closes on follows from what the anchor actually said. The anchor's own
        words are never regenerated here — they are the copy the newsroom wrote
        and checked, and this is a studio, not a second newsroom.
        """
        copy = await self._news_copy(session, slot)
        if copy is None:
            return  # nothing on the wire for this one yet; try again next tick

        teaser, bulletin, gossip = copy
        next_song = (
            await self._song_after(session, slot)
            if slot.kind == SlotKind.LINK
            else None
        )
        await session.commit()  # release the reader before the LLM call and Kokoro

        if not channel.news_anchor:
            # Nobody to hand over to. A station that has not cast a newsreader
            # gets the DJ reading the wire himself, which is one voice and no
            # exchange — asking somebody a question and then answering it in
            # your own voice is not a conversation.
            alone = teaser if slot.kind == SlotKind.LINK else bulletin
            await self._lay_down(
                session, slot, [(channel.dj_name, self.dj_voice, alone)]
            )
            return

        try:
            lines = await ask_json(
                prompts.news_handover_system(channel),
                prompts.news_handover(teaser, bulletin, next_song, gossip),
                NewsDialog,
            )
        except LLMError as e:
            self.log.warning("news handover failed: %s", e)
            return

        # Whatever the anchor is called — the station file opens with their name,
        # and the DJ says it on air.
        anchor = channel.news_anchor.split(",")[0].split(".")[0].strip() or "the news"
        if slot.kind == SlotKind.LINK:
            turns = [
                (channel.dj_name, self.dj_voice, lines.ask),
                (anchor, self.anchor_voice, teaser),
                (channel.dj_name, self.dj_voice, lines.close),
            ]
        else:
            turns = [
                (anchor, self.anchor_voice, bulletin),
                (channel.dj_name, self.dj_voice, lines.thanks),
            ]
        await self._lay_down(session, slot, turns)

    async def _lay_down(
        self,
        session: AsyncSession,
        slot: ProgrammeSlot,
        turns: list[tuple[str, str, str]],
    ) -> None:
        """Speak an exchange into one file and hand it to the slot.

        `turns` is `(speaker, voice, text)`. However many people are in it, what
        comes out is a single voice item — the renderer has no idea there was
        more than one person in the room.
        """
        segment = VoiceSegment(
            channel_id=self.channel_id,
            kind=VoiceKind.NEWS,
            break_kind=slot.kind.value,
            script="\n".join(f"{who}: {text}" for who, _, text in turns),
            turns=json.dumps(
                [
                    {"speaker": who, "voice": voice, "text": text}
                    for who, voice, text in turns
                ]
            ),
            status=VoiceStatus.GENERATING,
        )
        session.add(segment)
        await session.commit()  # release the writer before Kokoro and ffmpeg

        raw = settings.voice_dir / f"voice-{segment.id}-raw.wav"
        dst = settings.voice_dir / f"voice-{segment.id}.m4a"
        try:
            await asyncio.to_thread(
                self.tts.synthesize_turns,
                [(voice, text) for _, voice, text in turns],
                raw,
            )
            await asyncio.to_thread(process_voice, raw, dst)
            raw.unlink(missing_ok=True)
            duration = await asyncio.to_thread(probe_duration, dst)
        except Exception as e:
            self.log.warning("news TTS failed: %s", e)
            segment.status = VoiceStatus.FAILED
            await session.commit()
            return

        segment.audio_path = str(dst)
        segment.duration_sec = duration
        segment.status = VoiceStatus.READY
        segment.ready_at = now()
        slot.voice_segment_id = segment.id
        # The reservation is over: the block is fitted to the real length from
        # here on, and the programmer's next re-cut takes up the difference.
        slot.duration_sec = duration
        await session.commit()
        self.log.info(
            "%s recorded for %s (%.1fs, %d turns): %s",
            slot.kind.value,
            slot.planned_start.strftime("%H:%M:%S") if slot.planned_start else "?",
            duration,
            len(turns),
            turns[0][2][:60],
        )

    async def _news_copy(
        self, session: AsyncSession, slot: ProgrammeSlot
    ) -> tuple[str, str, bool] | None:
        """The newsroom's teaser and bulletin for this slot, and which kind it is.

        Both halves come from the same batch, so what the DJ trails at :55 is
        what the anchor reads at :00.
        """
        if slot.news_kind is None:
            return None
        gossip = slot.news_kind in (
            NewsSegmentKind.GOSSIP,
            NewsSegmentKind.GOSSIP_TEASER,
        )
        teaser_kind = (
            NewsSegmentKind.GOSSIP_TEASER if gossip else NewsSegmentKind.NEWS_TEASER
        )
        bulletin_kind = NewsSegmentKind.GOSSIP if gossip else NewsSegmentKind.NEWS

        # Newest first, so both halves come off the latest batch the newsroom
        # wrote — what is trailed at :55 is what is read at :00.
        rows = (
            await session.scalars(
                select(NewsSegment)
                .where(
                    NewsSegment.channel_id == self.channel_id,
                    NewsSegment.kind.in_([teaser_kind, bulletin_kind]),
                )
                .order_by(NewsSegment.id.desc())
                .limit(8)
            )
        ).all()
        teaser = next((r.script for r in rows if r.kind == teaser_kind), None)
        bulletin = next((r.script for r in rows if r.kind == bulletin_kind), None)
        if not teaser or not bulletin:
            return None
        return teaser, bulletin, gossip

    async def _song_after(
        self, session: AsyncSession, slot: ProgrammeSlot
    ) -> tuple[str | None, str] | None:
        """The record the DJ hands off to at the end of a trail."""
        nxt = await session.scalar(
            select(ProgrammeSlot)
            .where(
                ProgrammeSlot.channel_id == self.channel_id,
                ProgrammeSlot.block_start == slot.block_start,
                ProgrammeSlot.position > slot.position,
                ProgrammeSlot.kind == SlotKind.SONG,
                ProgrammeSlot.status == SlotStatus.PLANNED,
            )
            .order_by(ProgrammeSlot.position)
            .limit(1)
        )
        if nxt is None or nxt.track_id is None:
            return None
        track = await session.get(Track, nxt.track_id)
        return (track.artist, track.title) if track else None

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
            duration = entry.duration_sec or 0.0
            # Mirrors TimelineProducer._hold_out_sec — these are the estimates the
            # listener console shows, so they should not drift from the real mix.
            if entry.kind == EntryKind.VOICE:
                overlap = max(
                    0.0,
                    min(
                        settings.voice_ramp_out_max_sec,
                        duration - settings.voice_ramp_in_sec - renderer.DRY_SEC,
                    ),
                )
            else:
                overlap = settings.crossfade_sec
            cursor = cursor + timedelta(seconds=duration - overlap)

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
