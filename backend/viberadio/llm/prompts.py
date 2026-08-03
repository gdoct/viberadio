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


def _fmt_library(records: list[tuple[str | None, str, float]]) -> str:
    lines = []
    for artist, title, duration in records:
        length = f"{int(duration) // 60}:{int(duration) % 60:02d}"
        lines.append(
            f"- {artist} — {title} ({length})" if artist else f"- {title} ({length})"
        )
    return "\n".join(lines) or "- (the library is empty)"


def hour_plan(
    channel: Channel,
    hour_label: str,
    library: list[tuple[str | None, str, float]],
    recent: list[tuple[str | None, str]],
    count: int,
    new_count: int,
) -> str:
    """Programme one hour: which records, in what order.

    The hour is fitted to the clock afterwards — records that do not fit the
    half-hour are dropped from the tail and one may be swapped for a shorter or
    longer one — so this asks for more than will air and says so.
    """
    return (
        f"Programme the hour of {hour_label} on your station.\n\n"
        f"THE LIBRARY — everything you can play\n{_fmt_library(library)}\n\n"
        f"AIRED RECENTLY — do not come straight back to these\n{_fmt_songs(recent)}\n\n"
        f"List {count} of those records in the order they should air. You are "
        "deciding the shape of the hour, not filling a slot: how it opens, where "
        "it lifts and where it sits back, which two records must not sit next to "
        "each other, what this hour of the day wants. Nothing twice, and no artist "
        "twice in a row.\n\n"
        "The hour is cut to the clock after you hand it over — the last record or "
        "two may be dropped, and one may be swapped for a longer or shorter one to "
        "make the half-hour land on time. So put what matters at the front and "
        "treat the tail as spare.\n\n"
        f"You may also name up to {new_count} record(s) the library does not have "
        "and should. Pick real, released recordings that are easy to find; they are "
        "downloaded and go into a later hour, not this one."
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


@dataclass(frozen=True)
class NewsShape:
    """One of the four pieces of copy the newsroom writes off a batch of headlines.

    `words` is a budget for the same reason it is one on `Break`: the copy is
    spoken, and length is the only handle on how long the segment runs.
    """

    direction: str
    words: int


# Written in one call and in this order, so a teaser promises exactly what the
# segment it announces goes on to deliver. Keys are `NewsSegmentKind` values.
NEWS_SHAPES: dict[str, NewsShape] = {
    "news_teaser": NewsShape(
        "Trail the bulletin. Name the first two headlines and the remarkable item, "
        "and stop. No detail, no opinion, no sign-off — you are handing straight "
        "back to a record.",
        words=30,
    ),
    "news": NewsShape(
        "The bulletin itself. One line to open, then the three headlines in the "
        "order they are given with one sentence of substance each, then the "
        "remarkable item to land on, then one line to close.",
        words=110,
    ),
    "gossip_teaser": NewsShape(
        "Trail the gossip. One sentence, the first gossip item only, enough that "
        "somebody stays through the next record for it.",
        words=18,
    ),
    "gossip": NewsShape(
        "The gossip and remarkable spot. One line to open, the first gossip item "
        "with a sentence about it, then the remarkable item with a sentence about "
        "it, then one line to close.",
        words=70,
    ),
}


def news_system(channel: Channel) -> str:
    """Who reads the news on this station.

    The anchor is a second character on the same station, not the DJ in a
    different mood — but a station that has not cast one gets its DJ instead,
    reading the wire in their own voice.
    """
    if channel.news_anchor:
        parts = [
            f"You are the newsreader on {channel.name} — {channel.style}.",
            "",
            "WHO YOU ARE",
            channel.news_anchor,
        ]
    else:
        parts = [
            f"You are {channel.dj_name}, the DJ on {channel.name} — {channel.style} — "
            "and there is nobody else in the building, so you read the news yourself.",
            "",
            "WHO YOU ARE",
            channel.dj_persona,
        ]
    parts += [
        "",
        "HOW YOU READ THE NEWS",
        "The wire is not yours to improve. Report what it says and only what it "
        "says: never invent a name, a number, a cause or a consequence, and never "
        "predict what happens next. Your character is in the phrasing, the emphasis "
        "and what you find worth a raised eyebrow — not in the facts.",
        "",
        "One thought per item. You are reading between records, not filling a "
        "half-hour, so nothing gets a second sentence it did not earn. If an item "
        "arrives thin, stay on the headline rather than padding it out.",
        "",
        "Each piece of copy is read on its own, minutes apart, so none of them may "
        "refer to another one having just happened.",
    ]
    return "\n".join(parts)


def _fmt_items(items: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    for n, (title, summary) in enumerate(items, 1):
        lines.append(f"{n}. {title}")
        if summary:
            lines.append(f"   {summary}")
    return "\n".join(lines) or "(nothing on the wire)"


def news_scripts(
    headlines: list[tuple[str, str]],
    gossip: list[tuple[str, str]],
    remarkable: list[tuple[str, str]],
) -> str:
    """All four newsroom segments off one batch of items, in one call."""
    parts = [
        "THE WIRE, as it came in\n\n"
        f"HEADLINES\n{_fmt_items(headlines)}\n\n"
        f"REMARKABLE\n{_fmt_items(remarkable)}\n\n"
        f"GOSSIP\n{_fmt_items(gossip)}",
        "Write four separate pieces of copy off that wire. Each one is read on air "
        "by itself, so each one stands alone.",
    ]
    for kind, shape in NEWS_SHAPES.items():
        parts.append(f"{kind.upper()} — around {shape.words} words\n{shape.direction}")
    parts.append(
        "The wire is filed in whatever language its source writes in. You broadcast "
        "in the language you speak on air, so translate it into that — keep names of "
        "people and places as they are. Nothing that is not on the wire above goes "
        f"into any of the four. {SPEAKABLE}"
    )
    return "\n\n".join(parts)


def news_handover_system(channel: Channel) -> str:
    """Both people in the room, for the DJ's side of a news handover.

    The DJ prompt on its own would have them writing the news; the anchor prompt
    on its own would have them reading it. This one is the DJ, told who they are
    talking to.
    """
    anchor = channel.news_anchor or (
        f"{channel.dj_name} again — this station has no newsreader, so the DJ "
        "does the bulletin themselves in a flatter voice."
    )
    return "\n".join(
        [
            channel_system(channel),
            "",
            "WHO IS READING THE NEWS",
            anchor,
            "",
            "HOW YOU ARE WITH THEM",
            "You have worked with them for years and it shows, in whichever way "
            "it shows for you: easy, wary, competitive, fond, or all four in one "
            "shift. You are not interviewing them and you are not introducing "
            "them to the audience — everybody knows who they are.",
        ]
    )


def news_handover(
    teaser: str,
    bulletin: str,
    next_song: tuple[str | None, str] | None,
    gossip: bool,
) -> str:
    """The DJ's three lines around one bulletin: throw, hand-off, thanks."""
    what = "the gossip" if gossip else "the news"
    coming = f"{next_song[0]} — {next_song[1]}" if next_song else "whatever comes next"
    return (
        f"You are about to hand over to the newsroom for {what}. Write your three "
        "lines around it. Theirs are already written and are not yours to "
        "change.\n\n"
        f"WHAT THEY SAY WHEN YOU THROW TO THEM\n{teaser}\n\n"
        f"WHAT THEY SAY IN THE BULLETIN ITSELF, a few minutes later\n{bulletin}\n\n"
        "ASK — you throw to them. A handful of words. Ask what they have got, in "
        "your own way; you have done this a thousand times and you are not doing "
        "it formally.\n\n"
        "CLOSE — they have just given the headlines and thrown back to you. React "
        f"to what they actually said, briefly, then put on {coming}. This is the "
        "one that has to name the record.\n\n"
        "THANKS — the bulletin has just finished. Thank them by name and have one "
        "reaction to one thing in it: the item that got to you, or the one you "
        "refuse to take seriously. Do not summarise the bulletin, do not add a "
        "fact to it, and do not correct them on air.\n\n"
        "Fifteen words each, twenty-five at the outside. Three lines from one "
        f"person in one room, so they follow on from each other. {SPEAKABLE}"
    )


def starter_playlist(channel: Channel, count: int) -> str:
    return (
        f"List {count} well-known songs to seed the {channel.name} library. "
        f"They must fit the station style ({channel.style}) and be real, released recordings "
        "that are easy to find. Vary artist, tempo, and era within the style."
    )
