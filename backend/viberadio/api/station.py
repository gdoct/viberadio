from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..clock import now
from ..db import get_session
from ..models import Channel, EntryStatus, PlaylistEntry
from ..schemas import (
    ChannelInfo,
    ChannelSummary,
    HistoryItem,
    NowPlaying,
    QueueItem,
    StationResponse,
    TrackInfo,
)
from ..stations import manager

router = APIRouter(prefix="/api", tags=["station"])


async def resolve_channel(session: AsyncSession, slug: str | None) -> Channel:
    """The requested station, or the first on the dial when none is named."""
    q = select(Channel).order_by(Channel.sort_order, Channel.id)
    if slug:
        q = q.where(Channel.slug == slug)
    channel = await session.scalar(q.limit(1))
    if channel is None:
        raise HTTPException(status_code=404, detail=f"No such station: {slug}")
    return channel


def _track_info(entry: PlaylistEntry) -> TrackInfo | None:
    if entry.track is None:
        return None
    t = entry.track
    return TrackInfo(
        id=t.id, title=t.title, artist=t.artist, duration_sec=t.duration_sec
    )


def _channel_info(channel: Channel) -> ChannelInfo:
    return ChannelInfo(
        slug=channel.slug,
        name=channel.name,
        style=channel.style,
        dj_name=channel.dj_name,
        dj_persona=channel.dj_persona,
        catchphrase=channel.catchphrase,
    )


async def _on_air_channel_ids(session: AsyncSession) -> set[int]:
    """Channels with a real entry actually on air.

    A station that has only just woken starts streaming its holding jingle within
    seconds and may already have songs queued, but those are scheduled behind the
    jingle — until one is `playing`, the listener is still waiting.
    """
    rows = await session.scalars(
        select(PlaylistEntry.channel_id)
        .where(PlaylistEntry.status == EntryStatus.PLAYING)
        .distinct()
    )
    return set(rows.all())


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "running": manager.running_slugs()}


@router.get("/channels", response_model=list[ChannelSummary])
async def channels(
    session: AsyncSession = Depends(get_session),
) -> list[ChannelSummary]:
    """The dial. `status` is off | starting | live — a cold station spins up on
    first request, so the UI can show that it is waking rather than broken."""
    rows = (
        await session.scalars(select(Channel).order_by(Channel.sort_order, Channel.id))
    ).all()
    on_air = await _on_air_channel_ids(session)
    return [
        ChannelSummary(
            slug=c.slug,
            name=c.name,
            style=c.style,
            dj_name=c.dj_name,
            status=manager.status(c.id, c.id in on_air),
        )
        for c in rows
    ]


@router.get("/station", response_model=StationResponse)
async def station(
    channel: str | None = Query(default=None, description="Station slug"),
    session: AsyncSession = Depends(get_session),
) -> StationResponse:
    row = await resolve_channel(session, channel)
    # Asking about a station counts as listening to it: this is what starts a
    # cold station and what keeps a live one from being reaped.
    await manager.touch(row)

    playing = await session.scalar(
        select(PlaylistEntry)
        .options(
            selectinload(PlaylistEntry.track), selectinload(PlaylistEntry.voice_segment)
        )
        .where(
            PlaylistEntry.channel_id == row.id,
            PlaylistEntry.status == EntryStatus.PLAYING,
        )
        .order_by(PlaylistEntry.seq.desc())
        .limit(1)
    )

    queued = (
        await session.scalars(
            select(PlaylistEntry)
            .options(selectinload(PlaylistEntry.track))
            .where(
                PlaylistEntry.channel_id == row.id,
                PlaylistEntry.status == EntryStatus.QUEUED,
            )
            .order_by(PlaylistEntry.seq)
            .limit(20)
        )
    ).all()

    played = (
        await session.scalars(
            select(PlaylistEntry)
            .options(selectinload(PlaylistEntry.track))
            .where(
                PlaylistEntry.channel_id == row.id,
                PlaylistEntry.status == EntryStatus.PLAYED,
            )
            .order_by(PlaylistEntry.seq.desc())
            .limit(10)
        )
    ).all()

    now_playing = None
    if playing is not None:
        elapsed = None
        if playing.actual_start is not None:
            elapsed = (now() - playing.actual_start).total_seconds()
        now_playing = NowPlaying(
            kind=playing.kind.value,
            track=_track_info(playing),
            voice_script=playing.voice_segment.script
            if playing.voice_segment
            else None,
            started_at=playing.actual_start,
            elapsed_sec=elapsed,
        )

    return StationResponse(
        channel=_channel_info(row),
        status=manager.status(row.id, playing is not None),
        now_playing=now_playing,
        queue=[
            QueueItem(
                kind=e.kind.value, track=_track_info(e), planned_start=e.planned_start
            )
            for e in queued
        ],
        history=[
            HistoryItem(
                kind=e.kind.value,
                track=_track_info(e),
                actual_start=e.actual_start,
                actual_end=e.actual_end,
            )
            for e in played
        ],
        stream_url=f"/stream/{row.slug}/playlist.m3u8",
    )


@router.get("/history", response_model=list[HistoryItem])
async def history(
    channel: str | None = Query(default=None),
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[HistoryItem]:
    row = await resolve_channel(session, channel)
    played = (
        await session.scalars(
            select(PlaylistEntry)
            .options(selectinload(PlaylistEntry.track))
            .where(
                PlaylistEntry.channel_id == row.id,
                PlaylistEntry.status == EntryStatus.PLAYED,
            )
            .order_by(PlaylistEntry.seq.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [
        HistoryItem(
            kind=e.kind.value,
            track=_track_info(e),
            actual_start=e.actual_start,
            actual_end=e.actual_end,
        )
        for e in played
    ]


@router.get("/queue", response_model=list[QueueItem])
async def queue(
    channel: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> list[QueueItem]:
    row = await resolve_channel(session, channel)
    queued = (
        await session.scalars(
            select(PlaylistEntry)
            .options(selectinload(PlaylistEntry.track))
            .where(
                PlaylistEntry.channel_id == row.id,
                PlaylistEntry.status == EntryStatus.QUEUED,
            )
            .order_by(PlaylistEntry.seq)
        )
    ).all()
    return [
        QueueItem(
            kind=e.kind.value, track=_track_info(e), planned_start=e.planned_start
        )
        for e in queued
    ]
