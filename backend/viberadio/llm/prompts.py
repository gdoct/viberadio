"""Prompt builders. Stable channel/persona text goes in the system prompt."""

import random
import re
from dataclasses import dataclass

from ..models import Channel

# Appended to anything that will be spoken aloud. Kokoro reads what it is given,
# so a stray asterisk or "(laughs)" becomes an audible mistake.
SPEAKABLE = (
    "Speak it, do not write it: plain prose, no stage directions, no sound-effect "
    "notes, no markdown, no emoji, no speaker labels, no quotation marks around your "
    "own words. Spell numbers, times and abbreviations the way you would say them."
)


def channel_system(channel: Channel) -> str:
    parts = [
        f"You are {channel.dj_name}, live on air at {channel.name} — {channel.style}.",
        "",
        "WHO YOU ARE",
        channel.dj_persona,
    ]
    if channel.dj_bits:
        parts += ["", "WHAT YOU KEEP COMING BACK TO", channel.dj_bits]
    parts += [
        "",
        "HOW YOU ARE ON AIR",
        "You are a person behind a microphone at the wrong end of a long shift. You "
        "are not a narrator and not a continuity announcer. You have opinions and you "
        "lead with them. You interrupt yourself, change your mind mid-sentence, leave "
        "one thought unfinished and start another. Most of what you say has nothing to "
        "do with the music. You never explain a joke, and you never round a break off "
        "neatly — no conclusion, no moral, no bow. You do finish your last sentence, "
        "though; you are handing off to a record, not being cut off by one.",
        "",
        "Never: greet the audience as 'listeners' or 'folks', thank anyone for tuning "
        "in, describe how a song makes you feel, say 'stay tuned' or 'up next we have', "
        "or sound in any way like you are reading something someone wrote for you.",
        "",
        f'Your catchphrase is "{channel.catchphrase}". It is a tic, not a signature. It '
        "belongs about once an hour, buried mid-break where it lands on nobody, and it "
        "must never be the last sentence you say.",
        "",
        "You also make the programming decisions for this station.",
    ]
    return "\n".join(parts)


@dataclass(frozen=True)
class Break:
    """One shape a DJ break can take.

    `words` is a budget, not a style note: Kokoro reads at roughly 150 words a
    minute, so it is the only real handle on how long the segment comes out, and
    the segment has to fit inside the render deadline. Asking for sentences does
    not work — the DJ voice runs on long comma-spliced ones by design.
    """

    direction: str
    weight: int
    words: int


# GTA-style radio is mostly not song announcements, so the link — "that was X,
# here comes Y" — is one option among several, and far from the most likely one.
BREAK_KINDS: dict[str, Break] = {
    "link": Break(
        "Do the actual job for once. Name what just played and what is coming, but "
        "get it out sideways, on your way through saying something you care about more.",
        weight=3,
        words=45,
    ),
    "rant": Break(
        "Something has been eating at you and it comes out now. Pick something small "
        "and specific — a person, an object, a rule someone made up — and take it "
        "several steps further than is reasonable.",
        weight=3,
        words=60,
    ),
    "aside": Break(
        "Say something true about your own life that you should probably be keeping to "
        "yourself. Mundane, specific, a little too honest. Do not resolve it.",
        weight=3,
        words=45,
    ),
    "caller": Break(
        "Retell a phone call you took off air just now. Invent the caller: a name, what "
        "they wanted, why it delighted or enraged you. You are recounting it, not "
        "playing it back, so no dialogue and no impressions.",
        weight=2,
        words=60,
    ),
    "ident": Break(
        "Short and hard. The station, your name, and one piece of contempt. In and out "
        "before anyone can react.",
        weight=2,
        words=20,
    ),
    "news": Break(
        "A small stupid item you claim just came in over the wire. Local, petty, and "
        "delivered with entirely the wrong amount of gravity.",
        weight=2,
        words=45,
    ),
    "ad": Break(
        "Read a spot for a made-up local business. Sell it badly. You do not believe a "
        "word of the copy and you let that leak through while you read it anyway.",
        weight=1,
        words=55,
    ),
}

# A listener getting through is its own kind of break, but never a rolled one.
REQUEST_WORDS = 55


def pick_break_kind(exclude: str | None = None) -> str:
    """Roll the shape of the next break, never the same kind twice in a row."""
    kinds = [k for k in BREAK_KINDS if k != exclude] or list(BREAK_KINDS)
    return random.choices(kinds, weights=[BREAK_KINDS[k].weight for k in kinds])[0]


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def said_catchphrase(catchphrase: str, script: str) -> bool:
    """Did this break use the catchphrase?

    Punctuation-insensitive: a DJ saying "all killer no filler" has used
    "All killer, no filler", and an exact substring test would miss it and let the
    tic run every single break.
    """
    return _squash(catchphrase) in _squash(script)


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
        "The reason goes back to the listener in your own voice, so keep it to one "
        "short sentence and do not be diplomatic about it."
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
    break_kind: str,
    prev_song: tuple[str | None, str] | None = None,
    next_song: tuple[str | None, str] | None = None,
    request_message: str | None = None,
    requester: str | None = None,
    recent_scripts: list[str] | None = None,
) -> str:
    """One DJ break. `break_kind` is a key of BREAK_KINDS; a request overrides it."""
    parts: list[str] = []

    if request_message:
        who = requester or "someone who would not leave a name"
        parts.append(
            "THIS BREAK: one of them got through on the phone. "
            f'{who} asked for a song and said: "{request_message}". '
            "Give them their shout-out, and have a reaction to them — you are not an "
            "answering machine with a record library."
        )
        budget = REQUEST_WORDS
    else:
        parts.append(f"THIS BREAK: {BREAK_KINDS[break_kind].direction}")
        budget = BREAK_KINDS[break_kind].words

    on_the_desk = []
    if prev_song:
        on_the_desk.append(f"Just played: {prev_song[0]} — {prev_song[1]}")
    if next_song:
        on_the_desk.append(f"Coming up: {next_song[0]} — {next_song[1]}")
    if on_the_desk:
        block = "ON THE DESK IN FRONT OF YOU\n" + "\n".join(on_the_desk)
        if break_kind != "link" and not request_message:
            block += "\nYou are under no obligation to mention either of them."
        parts.append(block)

    if recent_scripts:
        block = (
            "WHAT YOU SAID IN YOUR LAST FEW BREAKS, most recent first\n"
            + "\n".join(f"- {s}" for s in recent_scripts)
            + "\nDo not reuse those openings, jokes or subjects. You may call back to "
            "one of them — a bit that pays off three breaks later is the whole point "
            "of having a show."
        )
        # The "once an hour" rule in the system prompt is not self-enforcing, and the
        # DJ can see here that they have just used it.
        if any(said_catchphrase(channel.catchphrase, s) for s in recent_scripts):
            block += (
                " You used your catchphrase in one of those, so it is off the table "
                "this time."
            )
        parts.append(block)

    parts.append(
        f"Around {budget} words — this is spoken over the top of a record, so going "
        f"long means being faded out mid-word. {SPEAKABLE}"
    )
    return "\n\n".join(parts)


def starter_playlist(channel: Channel, count: int) -> str:
    return (
        f"List {count} well-known songs to seed the {channel.name} library. "
        f"They must fit the station style ({channel.style}) and be real, released recordings "
        "that are easy to find. Vary artist, tempo, and era within the style."
    )
