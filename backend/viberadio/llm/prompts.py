"""Prompt builders. Stable channel/persona text goes in the system prompt."""

from ..models import Channel


def channel_system(channel: Channel) -> str:
    return (
        f"You are {channel.dj_name}, the DJ of {channel.name}, an internet radio station.\n"
        f"Station style: {channel.style}.\n"
        f"Your voice: {channel.dj_persona}.\n"
        f'Station catchphrase: "{channel.catchphrase}".\n'
        "You make programming decisions for this station and write what you say on air."
    )


def _fmt_songs(songs: list[tuple[str | None, str]]) -> str:
    return (
        "\n".join(f"- {a} — {t}" if a else f"- {t}" for a, t in songs)
        or "- (nothing yet)"
    )


def request_verdict(message: str, recent: list[tuple[str | None, str]]) -> str:
    return (
        "A listener sent this message to the station:\n"
        f'"{message}"\n\n'
        "Decide whether it is a concrete song request you can honor on this station.\n"
        "Reject it if it is not a song request, if the song clashes badly with the station "
        "style, or if it was played very recently.\n\n"
        f"Recently played:\n{_fmt_songs(recent)}\n\n"
        "If eligible, give the artist and title as they would appear on the record. "
        "The reason is read on air, so keep it to one short sentence in your voice."
    )


def song_pick(
    recent: list[tuple[str | None, str]], queued: list[tuple[str | None, str]]
) -> str:
    return (
        "Pick the next song for the station.\n\n"
        f"Recently played (do not repeat these):\n{_fmt_songs(recent)}\n\n"
        f"Already queued up next (do not repeat these either):\n{_fmt_songs(queued)}\n\n"
        "Choose a well-known, real recording that fits the station style and keeps the "
        "hour varied in tempo and mood."
    )


def dj_script(
    channel: Channel,
    prev_song: tuple[str | None, str] | None,
    next_song: tuple[str | None, str] | None,
    request_message: str | None = None,
    requester: str | None = None,
) -> str:
    parts = ["Write a short DJ break to play between songs."]
    if prev_song:
        parts.append(f"Just played: {prev_song[0]} — {prev_song[1]}.")
    if next_song:
        parts.append(f"Coming up next: {next_song[0]} — {next_song[1]}.")
    if request_message:
        who = requester or "a listener"
        parts.append(
            f'This one is a request from {who}, who wrote: "{request_message}". Give them a shout-out.'
        )
    parts.append(
        "Two to four sentences, spoken aloud — no stage directions, no sound-effect notes, "
        "no markdown, no emoji. Write numbers and abbreviations the way you would say them. "
        f'Use the catchphrase "{channel.catchphrase}" only occasionally, not every break.'
    )
    return " ".join(parts)


def starter_playlist(channel: Channel, count: int) -> str:
    return (
        f"List {count} well-known songs to seed the {channel.name} library. "
        f"They must fit the station style ({channel.style}) and be real, released recordings "
        "that are easy to find. Vary artist, tempo, and era within the style."
    )
