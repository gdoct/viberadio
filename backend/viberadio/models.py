import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .db import Base


class TZDateTime(TypeDecorator):
    """UTC-aware datetimes on SQLite: stored naive-UTC, always returned tz-aware."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


def _enum(e: type[enum.Enum]) -> Enum:
    return Enum(
        e, values_callable=lambda x: [m.value for m in x], native_enum=False, length=20
    )


class TrackKind(enum.Enum):
    SONG = "song"
    JINGLE = "jingle"
    SFX = "sfx"


class UpdateStatus(enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    REJECTED = "rejected"


class EntryKind(enum.Enum):
    SONG = "song"
    VOICE = "voice"


class EntryStatus(enum.Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    PLAYING = "playing"
    PLAYED = "played"
    CANCELLED = "cancelled"


class VoiceKind(enum.Enum):
    INTRO = "intro"
    TRANSITION = "transition"
    NEWS = "news"
    JINGLE = "jingle"
    REPLY = "reply"


class VoiceStatus(enum.Enum):
    PENDING = "pending"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"


class RequestStatus(enum.Enum):
    NEW = "new"
    JUDGING = "judging"
    DOWNLOADING = "downloading"
    DONE = "done"
    REJECTED = "rejected"


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    # URL-safe identifier used for /api and /stream routing.
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    style: Mapped[str] = mapped_column(String(200))
    dj_name: Mapped[str] = mapped_column(String(100))
    # Who the DJ is, in prose — it goes into the system prompt verbatim, so it has
    # to read like a character brief and not like a voice-casting note.
    dj_persona: Mapped[str] = mapped_column(Text)
    # Recurring obsessions the DJ can call back to across breaks. Optional.
    dj_bits: Mapped[str] = mapped_column(Text, default="")
    catchphrase: Mapped[str] = mapped_column(Text)
    # Kokoro voice id — one per station so the DJs don't all sound identical.
    tts_voice: Mapped[str] = mapped_column(String(40), default="am_onyx")
    sort_order: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now()
    )


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[TrackKind] = mapped_column(_enum(TrackKind), default=TrackKind.SONG)
    title: Mapped[str] = mapped_column(String(300), index=True)
    artist: Mapped[str | None] = mapped_column(String(300), index=True)
    file_path: Mapped[str] = mapped_column(Text)
    normalized_path: Mapped[str | None] = mapped_column(Text)
    duration_sec: Mapped[float] = mapped_column(Float)
    source_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now()
    )


class PlaylistUpdate(Base):
    __tablename__ = "playlist_updates"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    status: Mapped[UpdateStatus] = mapped_column(
        _enum(UpdateStatus), default=UpdateStatus.PENDING
    )
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(TZDateTime())

    entries: Mapped[list["PlaylistEntry"]] = relationship(back_populates="update")


class PlaylistEntry(Base):
    __tablename__ = "playlist_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    update_id: Mapped[int | None] = mapped_column(ForeignKey("playlist_updates.id"))
    seq: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[EntryKind] = mapped_column(_enum(EntryKind))
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id"))
    voice_segment_id: Mapped[int | None] = mapped_column(
        ForeignKey("voice_segments.id")
    )
    status: Mapped[EntryStatus] = mapped_column(
        _enum(EntryStatus), default=EntryStatus.DRAFT, index=True
    )
    planned_start: Mapped[datetime | None] = mapped_column(TZDateTime())
    actual_start: Mapped[datetime | None] = mapped_column(TZDateTime())
    actual_end: Mapped[datetime | None] = mapped_column(TZDateTime())
    duration_sec: Mapped[float | None] = mapped_column(Float)

    update: Mapped[PlaylistUpdate | None] = relationship(back_populates="entries")
    track: Mapped[Track | None] = relationship()
    voice_segment: Mapped["VoiceSegment | None"] = relationship()


class VoiceSegment(Base):
    __tablename__ = "voice_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    kind: Mapped[VoiceKind] = mapped_column(
        _enum(VoiceKind), default=VoiceKind.TRANSITION
    )
    # Which shape of break this was — see prompts.BREAK_KINDS. Recorded so the next
    # break can avoid repeating it, and so the log shows what the DJ was doing.
    break_kind: Mapped[str | None] = mapped_column(String(20))
    script: Mapped[str] = mapped_column(Text)
    audio_path: Mapped[str | None] = mapped_column(Text)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    status: Mapped[VoiceStatus] = mapped_column(
        _enum(VoiceStatus), default=VoiceStatus.PENDING
    )
    prev_track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id"))
    next_track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id"))
    request_id: Mapped[int | None] = mapped_column(ForeignKey("listener_requests.id"))
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now()
    )
    ready_at: Mapped[datetime | None] = mapped_column(TZDateTime())


class ListenerRequest(Base):
    __tablename__ = "listener_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    message: Mapped[str] = mapped_column(Text)
    requester: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[RequestStatus] = mapped_column(
        _enum(RequestStatus), default=RequestStatus.NEW, index=True
    )
    verdict_reason: Mapped[str | None] = mapped_column(Text)
    matched_track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id"))
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime())


class HlsSegment(Base):
    __tablename__ = "hls_segments"
    # Each station numbers its own segments from its own timeline epoch.
    __table_args__ = (
        UniqueConstraint("channel_id", "seq", name="uq_segment_per_channel"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    file_path: Mapped[str] = mapped_column(Text)
    start_time: Mapped[datetime] = mapped_column(TZDateTime())
    duration_sec: Mapped[float] = mapped_column(Float)
    discontinuity: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now()
    )


class StationState(Base):
    __tablename__ = "station_state"

    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    timeline_epoch: Mapped[datetime] = mapped_column(TZDateTime())
    next_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    samples_rendered: Mapped[int] = mapped_column(BigInteger, default=0)
    current_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("playlist_entries.id")
    )
    entry_sample_offset: Mapped[int] = mapped_column(BigInteger, default=0)
