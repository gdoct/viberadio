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


class SlotStatus(enum.Enum):
    PLANNED = "planned"
    QUEUED = "queued"  # promoted to a playlist entry
    AIRED = "aired"
    SKIPPED = "skipped"  # its block passed while the station was down
    DROPPED = "dropped"  # a re-fit or a request took its place


class SlotKind(enum.Enum):
    """What occupies a place in the running order.

    `link` is the exchange that trails a bulletin — the DJ asks, the anchor
    answers, the DJ hands off to a record — and `bulletin` is the bulletin
    itself. Both are one voice item on the playlist, however many people speak
    inside them.
    """

    SONG = "song"
    LINK = "link"
    BULLETIN = "bulletin"


class SlotOrigin(enum.Enum):
    """How a record came to be in a station's running order.

    `dj` and `request` are decisions — someone judged this record right for this
    station — and they are what make it one of the station's own. `rotation` is
    derived from those, so it grants nothing: otherwise a station would slowly
    take ownership of whatever a bad hour happened to put on air.
    """

    DJ = "dj"
    ROTATION = "rotation"
    REQUEST = "request"


class NewsCategory(enum.Enum):
    NEWS = "news"
    GOSSIP = "gossip"
    REMARKABLE = "remarkable"


class NewsSegmentKind(enum.Enum):
    """The four pieces of copy the newsroom writes from one batch of headlines."""

    NEWS_TEASER = "news_teaser"  # "Coming up in the news: ..."
    NEWS = "news"  # the bulletin itself
    GOSSIP_TEASER = "gossip_teaser"
    GOSSIP = "gossip"


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
    # Who reads the news on this station, in prose. Optional: a station without one
    # has its own DJ read the wire in character.
    news_anchor: Mapped[str] = mapped_column(Text, default="")
    # The anchor's own Kokoro voice. They talk to the DJ, so it has to be a
    # different one — the same voice twice is not a conversation.
    news_tts_voice: Mapped[str] = mapped_column(String(40), default="af_heart")
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


class StationRecord(Base):
    """A record this station has been given.

    The media library is shared between stations; this is what makes one of its
    records *this* station's. It is put here by a judgement — the DJ named it
    when programming an hour, or a listener asked for it and the DJ agreed it
    fitted — and everything that picks without the DJ in the room afterwards
    (rotation, the fitter's swaps) may only choose from here.

    Deliberately separate from the running order. A DJ who names eighteen
    records for an hour with room for seven has still said all eighteen belong
    here, and the eleven that did not fit are the station's just as much as the
    seven that did — that is what keeps rotation from starving.
    """

    __tablename__ = "station_records"
    __table_args__ = (
        UniqueConstraint("channel_id", "track_id", name="uq_record_per_station"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id"), index=True)
    added_at: Mapped[datetime] = mapped_column(TZDateTime(), server_default=func.now())


class ProgrammeSlot(Base):
    """One item in one half-hour block of a station's day.

    The programme is what the station *intends* to broadcast, decided hours ahead
    and fitted so each block ends on its mark; `playlist_entries` is what has been
    committed to the audio timeline. Keeping them apart is what lets a block be
    re-shuffled right up until the moment it is promoted, without ever touching
    anything that has aired or been rendered.

    Mostly records, but a block also carries the two news items it is built
    around: the bulletin that opens it on the mark, and the link that trails the
    next one before its last record.
    """

    __tablename__ = "programme_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    # The mark this block opens on: every :00 and :30.
    block_start: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    # Order within the block. Assigned in steps so a request can be slipped in
    # between two records without renumbering the ones behind it.
    position: Mapped[int] = mapped_column()
    kind: Mapped[SlotKind] = mapped_column(_enum(SlotKind), default=SlotKind.SONG)
    # Null for the two news kinds: what they play is written, not recorded.
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id"))
    # Which piece of the newsroom's copy this reads, for `link` and `bulletin`.
    news_kind: Mapped[NewsSegmentKind | None] = mapped_column(_enum(NewsSegmentKind))
    # A record's length is known; a news item's is a reservation until it has
    # been spoken, and is replaced by the real one when it has.
    duration_sec: Mapped[float | None] = mapped_column(Float)
    planned_start: Mapped[datetime | None] = mapped_column(TZDateTime())
    planned_end: Mapped[datetime | None] = mapped_column(TZDateTime())
    status: Mapped[SlotStatus] = mapped_column(
        _enum(SlotStatus), default=SlotStatus.PLANNED, index=True
    )
    origin: Mapped[SlotOrigin] = mapped_column(
        _enum(SlotOrigin), default=SlotOrigin.ROTATION
    )
    entry_id: Mapped[int | None] = mapped_column(ForeignKey("playlist_entries.id"))
    request_id: Mapped[int | None] = mapped_column(ForeignKey("listener_requests.id"))
    # What a news slot will actually broadcast, once the studio has spoken it.
    # Set well before airtime — until it is, the slot is only a reservation.
    voice_segment_id: Mapped[int | None] = mapped_column(
        ForeignKey("voice_segments.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now()
    )

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
    # `[{"speaker": "Kyle", "voice": "am_onyx", "text": "..."}, ...]` when more
    # than one person is in the room. Null for an ordinary break, which is the
    # DJ alone and is fully described by `script`.
    turns: Mapped[str | None] = mapped_column(Text)
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


class NewsFeed(Base):
    """One RSS source, and when it was last asked for anything.

    Not per station: the feeds belong to the process, and this row is what keeps
    three active stations from turning one hourly poll into three.
    """

    __tablename__ = "news_feeds"

    url: Mapped[str] = mapped_column(String(500), primary_key=True)
    category: Mapped[NewsCategory] = mapped_column(_enum(NewsCategory))
    # Last attempt, not last success — the cap is on requests made.
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime())
    ok: Mapped[bool] = mapped_column(default=True)
    error: Mapped[str | None] = mapped_column(Text)


class NewsItem(Base):
    """A headline off the wire, shared by every station."""

    __tablename__ = "news_items"
    # An item can be in both the general feed and a themed one; it is a different
    # item to the newsroom in each, so the category is part of its identity.
    __table_args__ = (
        UniqueConstraint("guid", "category", name="uq_news_item_per_category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[NewsCategory] = mapped_column(_enum(NewsCategory), index=True)
    guid: Mapped[str] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TZDateTime(), index=True)
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)


class NewsSegment(Base):
    """Spoken copy written for one station from one batch of items.

    Per channel, because the wire is shared but the voice reading it is not: the
    same four headlines become four different scripts on four stations.
    """

    __tablename__ = "news_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    kind: Mapped[NewsSegmentKind] = mapped_column(_enum(NewsSegmentKind))
    script: Mapped[str] = mapped_column(Text)
    # Fingerprint of the items this batch was written from. The four segments of a
    # batch share one, which is both how they are grouped and how the agent knows
    # the wire has not moved since it last wrote.
    digest: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now()
    )
    # Set by whoever puts this on air. Nothing does yet.
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime())


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
