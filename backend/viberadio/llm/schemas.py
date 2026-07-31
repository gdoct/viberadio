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


class SongRef(BaseModel):
    artist: str
    title: str


class StarterPlaylist(BaseModel):
    songs: list[SongRef]
