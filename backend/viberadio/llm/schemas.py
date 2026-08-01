from pydantic import BaseModel, Field


class RequestVerdict(BaseModel):
    """Is a listener request playable on this channel?"""

    eligible: bool = Field(
        description="True if this is a concrete song request fitting the channel"
    )
    artist: str | None = Field(default=None, description="Artist name, if identifiable")
    title: str | None = Field(default=None, description="Song title, if identifiable")
    reason: str = Field(description="Short explanation for the listener")


class SongPick(BaseModel):
    """A song the DJ wants to play next."""

    artist: str
    title: str
    reason: str = Field(description="Why it fits the channel right now")


class DJScript(BaseModel):
    """Spoken DJ segment, plain prose ready for TTS."""

    script: str = Field(
        description="What the DJ says out loud — no stage directions or markup"
    )


class NewsScripts(BaseModel):
    """One batch of newsroom copy: four segments off the same headlines.

    Written in a single call so the teasers promise exactly what the segments
    they announce go on to deliver. Field names match `NewsSegmentKind` values.
    """

    news_teaser: str = Field(
        description="One or two sentences announcing the headlines that are coming up"
    )
    news: str = Field(description="The bulletin itself, with an intro and an outro")
    gossip_teaser: str = Field(
        description="One sentence announcing the gossip item that is coming up"
    )
    gossip: str = Field(
        description="The gossip and remarkable segment, with an intro and an outro"
    )


class SongRef(BaseModel):
    artist: str
    title: str


class StarterPlaylist(BaseModel):
    songs: list[SongRef]
