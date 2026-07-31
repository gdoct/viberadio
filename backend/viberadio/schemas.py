from datetime import datetime

from pydantic import BaseModel


class ChannelInfo(BaseModel):
    slug: str
    name: str
    style: str
    dj_name: str
    dj_persona: str
    catchphrase: str


class ChannelSummary(BaseModel):
    """One entry on the dial. `status` is off | starting | live."""

    slug: str
    name: str
    style: str
    dj_name: str
    status: str


class TrackInfo(BaseModel):
    id: int
    title: str
    artist: str | None
    duration_sec: float


class NowPlaying(BaseModel):
    kind: str
    track: TrackInfo | None = None
    voice_script: str | None = None
    started_at: datetime | None = None
    elapsed_sec: float | None = None


class QueueItem(BaseModel):
    kind: str
    track: TrackInfo | None = None
    planned_start: datetime | None = None


class HistoryItem(BaseModel):
    kind: str
    track: TrackInfo | None = None
    actual_start: datetime | None = None
    actual_end: datetime | None = None


class StationResponse(BaseModel):
    channel: ChannelInfo
    status: str
    now_playing: NowPlaying | None
    queue: list[QueueItem]
    history: list[HistoryItem]
    stream_url: str


class RequestCreate(BaseModel):
    message: str
    name: str | None = None
    channel: str | None = None


class RequestInfo(BaseModel):
    id: int
    message: str
    requester: str | None
    status: str
    verdict_reason: str | None
    created_at: datetime
