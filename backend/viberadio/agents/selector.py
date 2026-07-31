"""Song selector agent.

Per the spec: check listener requests first; eligible requests are verified against the
media library, downloaded if missing, and queued. Otherwise pick a new song fitting the
channel. Keep going until at least `min_queued_songs` songs are queued, then hand the
playlist update to the voice agent.
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
from ..llm.schemas import RequestVerdict, SongPick
from ..models import (
    Channel,
    EntryKind,
    EntryStatus,
    ListenerRequest,
    PlaylistEntry,
    PlaylistUpdate,
    RequestStatus,
    Track,
    TrackKind,
    UpdateStatus,
)
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

            # Then top the queue up to the minimum with the DJ's own picks.
            while await self._queued_song_count(session) < settings.min_queued_songs:
                if not await self._pick_song(session, channel):
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

    async def _queued(self, session: AsyncSession) -> list[tuple[str | None, str]]:
        rows = (
            await session.execute(
                select(Track.artist, Track.title)
                .join(PlaylistEntry, PlaylistEntry.track_id == Track.id)
                .where(
                    PlaylistEntry.channel_id == self.channel_id,
                    PlaylistEntry.status.in_(
                        [EntryStatus.QUEUED, EntryStatus.PLAYING, EntryStatus.DRAFT]
                    ),
                )
                .order_by(PlaylistEntry.seq)
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

        await self._draft_entry(session, channel, track)
        req.status = RequestStatus.DONE
        req.matched_track_id = track.id
        req.verdict_reason = verdict.reason
        req.resolved_at = now()
        await session.commit()
        self.log.info("request %d queued: %s — %s", req.id, track.artist, track.title)
        return True, True

    async def _pick_song(self, session: AsyncSession, channel: Channel) -> bool:
        recent = await self._recent(session)
        queued = await self._queued(session)
        track: Track | None = None
        try:
            pick = await ask_json(
                prompts.channel_system(channel),
                prompts.song_pick(recent, queued),
                SongPick,
            )
            track = await find_track(session, pick.artist, pick.title)
            if track is None:
                path, url = await download_song(pick.artist, pick.title)
                track = await register_track(
                    session, path, title=pick.title, artist=pick.artist, source_url=url
                )
            self.log.info("picked %s — %s (%s)", pick.artist, pick.title, pick.reason)
        except (LLMError, DownloadError) as e:
            self.log.warning("pick failed (%s); falling back to library rotation", e)
            track = await self._least_recently_played(session)
            if track is None:
                return False

        await self._draft_entry(session, channel, track)
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

    async def _draft_entry(
        self, session: AsyncSession, channel: Channel, track: Track
    ) -> PlaylistEntry:
        next_seq = (
            await session.scalar(
                select(func.max(PlaylistEntry.seq)).where(
                    PlaylistEntry.channel_id == self.channel_id
                )
            )
            or 0
        ) + 1
        entry = PlaylistEntry(
            channel_id=channel.id,
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
