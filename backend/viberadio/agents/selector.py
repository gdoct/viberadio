"""Song selector agent.

What airs is decided hours ahead by the programmer, so this no longer chooses
records — it promotes them. Each tick it takes the next few slots off the
programme, drafts playlist entries for them and hands the batch to the voice
agent, which is where a break gets written and the update goes live.

Listener requests are still its own work, and still come first: an eligible one
is verified against the media library, downloaded if missing, and inserted into
the programme at the earliest slot that has not been committed to the timeline
yet. The programmer's next re-fit pays for it out of the same block, so the
half-hour still ends where it should.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import now
from ..config import settings
from ..db import agent_session
from ..library.downloader import DownloadError, download_song
from ..library.media import find_track, register_track
from ..llm import prompts
from ..llm.client import LLMError, ask_json
from ..llm.schemas import RequestVerdict
from ..models import (
    Channel,
    EntryKind,
    EntryStatus,
    ListenerRequest,
    PlaylistEntry,
    PlaylistUpdate,
    ProgrammeSlot,
    RequestStatus,
    SlotOrigin,
    SlotStatus,
    Track,
    TrackKind,
    UpdateStatus,
)
from ..programme import block_containing
from . import selector_event, wake_voice
from .base import AgentLoop

HISTORY_WINDOW = 20
MAX_REQUESTS_PER_TICK = 3


class Selector(AgentLoop):
    def __init__(self, channel_id: int, slug: str) -> None:
        super().__init__(
            f"selector[{slug}]",
            settings.selector_interval_sec,
            wake=selector_event(channel_id),
        )
        self.channel_id = channel_id

    async def tick(self) -> None:
        async with agent_session() as session:
            channel = await session.get(Channel, self.channel_id)
            if channel is None:
                return

            if await self._pending_update(session) is not None:
                return  # voice agent still deciding on the last batch

            # Listener requests come first, regardless of how full the queue is.
            added = await self._drain_requests(session, channel)

            # Then top the queue up to the minimum from the programme.
            while await self._queued_song_count(session) < settings.min_queued_songs:
                if not await self._promote(session):
                    break
                added += 1

            if added:
                await self._open_update(session, channel)
                await session.commit()
                wake_voice(self.channel_id)
            else:
                await session.commit()

    async def _pending_update(self, session: AsyncSession) -> PlaylistUpdate | None:
        return await session.scalar(
            select(PlaylistUpdate)
            .where(
                PlaylistUpdate.channel_id == self.channel_id,
                PlaylistUpdate.status == UpdateStatus.PENDING,
            )
            .limit(1)
        )

    async def _queued_song_count(self, session: AsyncSession) -> int:
        return (
            await session.scalar(
                select(func.count())
                .select_from(PlaylistEntry)
                .where(
                    PlaylistEntry.channel_id == self.channel_id,
                    PlaylistEntry.kind == EntryKind.SONG,
                    PlaylistEntry.status.in_(
                        [EntryStatus.QUEUED, EntryStatus.PLAYING, EntryStatus.DRAFT]
                    ),
                )
            )
            or 0
        )

    async def _recent(
        self, session: AsyncSession, limit: int = HISTORY_WINDOW
    ) -> list[tuple[str | None, str]]:
        rows = (
            await session.execute(
                select(Track.artist, Track.title)
                .join(PlaylistEntry, PlaylistEntry.track_id == Track.id)
                .where(
                    PlaylistEntry.channel_id == self.channel_id,
                    PlaylistEntry.status == EntryStatus.PLAYED,
                )
                .order_by(PlaylistEntry.seq.desc())
                .limit(limit)
            )
        ).all()
        return [(r[0], r[1]) for r in rows]

    async def _drain_requests(self, session: AsyncSession, channel: Channel) -> int:
        """Judge and queue up to MAX_REQUESTS_PER_TICK pending listener requests."""
        added = 0
        for _ in range(MAX_REQUESTS_PER_TICK):
            req = await session.scalar(
                select(ListenerRequest)
                .where(
                    ListenerRequest.channel_id == self.channel_id,
                    ListenerRequest.status == RequestStatus.NEW,
                )
                .order_by(ListenerRequest.created_at)
                .limit(1)
            )
            if req is None:
                break
            handled, queued = await self._handle_request(session, channel, req)
            if queued:
                added += 1
            if not handled:
                break  # transient failure — leave the rest for the next tick
        return added

    async def _handle_request(
        self, session: AsyncSession, channel: Channel, req: ListenerRequest
    ) -> tuple[bool, bool]:
        """Returns (resolved, queued): `resolved` is False only on a transient failure
        that leaves the request pending for a later tick."""
        req.status = RequestStatus.JUDGING
        await session.commit()  # release the writer before the LLM call
        recent = await self._recent(session)
        try:
            verdict = await ask_json(
                prompts.channel_system(channel),
                prompts.request_verdict(req.message, recent),
                RequestVerdict,
            )
        except LLMError as e:
            self.log.warning(
                "verdict failed for request %d: %s — leaving for retry", req.id, e
            )
            req.status = RequestStatus.NEW
            await session.commit()
            return False, False

        if not verdict.eligible or not verdict.title:
            req.status = RequestStatus.REJECTED
            req.verdict_reason = verdict.reason
            req.resolved_at = now()
            await session.commit()
            self.log.info("request %d rejected: %s", req.id, verdict.reason)
            return True, False

        track = await find_track(session, verdict.artist, verdict.title)
        if track is None:
            req.status = RequestStatus.DOWNLOADING
            await session.commit()  # release the writer before the download
            try:
                path, url = await download_song(verdict.artist, verdict.title)
                track = await register_track(
                    session,
                    path,
                    title=verdict.title,
                    artist=verdict.artist,
                    source_url=url,
                )
            except DownloadError as e:
                req.status = RequestStatus.REJECTED
                req.verdict_reason = "Couldn't find a copy of that one to play."
                req.resolved_at = now()
                await session.commit()
                self.log.warning("request %d download failed: %s", req.id, e)
                return True, False

        await self._insert_request_slot(session, track, req)
        req.status = RequestStatus.DONE
        req.matched_track_id = track.id
        req.verdict_reason = verdict.reason
        req.resolved_at = now()
        await session.commit()
        self.log.info(
            "request %d on the programme: %s — %s", req.id, track.artist, track.title
        )
        return True, True

    async def _insert_request_slot(
        self, session: AsyncSession, track: Track, req: ListenerRequest
    ) -> ProgrammeSlot:
        """Put a request in at the earliest slot that is still ours to move.

        Ahead of everything the programme has left in the block but behind
        anything already committed to the timeline, which is as early as a record
        can be made to play without cutting one off. The programmer's re-fit then
        takes a rotation record out of the same block to pay for it, so the mark
        does not move.
        """
        head = await session.scalar(
            select(ProgrammeSlot)
            .where(
                ProgrammeSlot.channel_id == self.channel_id,
                ProgrammeSlot.status == SlotStatus.PLANNED,
            )
            .order_by(ProgrammeSlot.block_start, ProgrammeSlot.position)
            .limit(1)
        )
        # Nothing left on the programme: open the current block for it, and let the
        # programmer fit the rest of the half-hour around it on its next tick.
        block_start = head.block_start if head else block_containing(now())
        slot = ProgrammeSlot(
            channel_id=self.channel_id,
            block_start=block_start,
            position=(head.position - 1) if head else 0,
            track_id=track.id,
            planned_start=head.planned_start if head else None,
            status=SlotStatus.PLANNED,
            origin=SlotOrigin.REQUEST,
            request_id=req.id,
        )
        session.add(slot)
        return slot

    async def _promote(self, session: AsyncSession) -> bool:
        """Draft a playlist entry for the next record on the programme.

        Falls back to the library when the programme has run dry — a station
        whose programmer has not caught up yet still has to have something to
        play, and dead air is worse than a repeat.
        """
        slot = await session.scalar(
            select(ProgrammeSlot)
            .where(
                ProgrammeSlot.channel_id == self.channel_id,
                ProgrammeSlot.status == SlotStatus.PLANNED,
            )
            .order_by(ProgrammeSlot.block_start, ProgrammeSlot.position)
            .limit(1)
        )
        if slot is None:
            track = await self._least_recently_played(session)
            if track is None:
                return False
            self.log.warning("nothing programmed; falling back to %s", track.title)
            await self._draft_entry(session, track)
            return True

        track = await session.get(Track, slot.track_id)
        if track is None:  # the record went out of the library under the programme
            slot.status = SlotStatus.DROPPED
            await session.commit()
            return True

        entry = await self._draft_entry(session, track)
        slot.status = SlotStatus.QUEUED
        slot.entry_id = entry.id
        await session.commit()
        return True

    async def _least_recently_played(self, session: AsyncSession) -> Track | None:
        """Fallback: a song this station has aired before, least recently played first.

        Deliberately restricted to this channel's own history even though the media
        library is shared — the whole point of separate stations is that the jazz
        one never falls back to a grunge record.
        """
        active_track_ids = select(PlaylistEntry.track_id).where(
            PlaylistEntry.channel_id == self.channel_id,
            PlaylistEntry.status.in_(
                [EntryStatus.QUEUED, EntryStatus.PLAYING, EntryStatus.DRAFT]
            ),
            PlaylistEntry.track_id.isnot(None),
        )
        last_played = (
            select(
                PlaylistEntry.track_id, func.max(PlaylistEntry.seq).label("last_seq")
            )
            .where(
                PlaylistEntry.channel_id == self.channel_id,
                PlaylistEntry.status == EntryStatus.PLAYED,
            )
            .group_by(PlaylistEntry.track_id)
            .subquery()
        )
        return await session.scalar(
            select(Track)
            .join(last_played, last_played.c.track_id == Track.id)
            .where(Track.kind == TrackKind.SONG, Track.id.notin_(active_track_ids))
            .order_by(last_played.c.last_seq.asc())
            .limit(1)
        )

    async def _draft_entry(self, session: AsyncSession, track: Track) -> PlaylistEntry:
        next_seq = (
            await session.scalar(
                select(func.max(PlaylistEntry.seq)).where(
                    PlaylistEntry.channel_id == self.channel_id
                )
            )
            or 0
        ) + 1
        entry = PlaylistEntry(
            channel_id=self.channel_id,
            seq=next_seq,
            kind=EntryKind.SONG,
            track_id=track.id,
            status=EntryStatus.DRAFT,
            duration_sec=track.duration_sec,
        )
        session.add(entry)
        await session.commit()
        return entry

    async def _open_update(
        self, session: AsyncSession, channel: Channel
    ) -> PlaylistUpdate:
        """Wrap the loose drafts in a pending playlist update for the voice agent."""
        update = PlaylistUpdate(channel_id=channel.id, status=UpdateStatus.PENDING)
        session.add(update)
        await session.flush()
        drafts = (
            await session.scalars(
                select(PlaylistEntry)
                .where(
                    PlaylistEntry.channel_id == self.channel_id,
                    PlaylistEntry.status == EntryStatus.DRAFT,
                    PlaylistEntry.update_id.is_(None),
                )
                .order_by(PlaylistEntry.seq)
            )
        ).all()
        for entry in drafts:
            entry.update_id = update.id
        self.log.info(
            "opened playlist update %d with %d draft entries", update.id, len(drafts)
        )
        return update
