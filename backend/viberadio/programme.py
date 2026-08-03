"""Fitting records to the clock.

A station's day is cut into half-hour blocks, and a block has to end where the
next one begins: on the mark. Records are whatever length they are, so hitting a
mark is a small search — take the running order the DJ asked for, then swap, add
or drop until the projected end of the block is close enough to the mark.

Everything here is arithmetic over durations. Nothing touches the database, and
the only reason it knows about the renderer at all is that the renderer decides
what a record actually costs the timeline: a song hands its last `crossfade_sec`
to the one after it, so an hour of four-minute records is not four hours' worth
of minutes.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import settings

# Positions are spaced so a request can be slipped between two records without
# renumbering the ones behind it.
POSITION_STEP = 10

# A second either side of the mark is under the crossfade — searching past it
# buys nothing anyone can hear.
CLOSE_ENOUGH_SEC = 1.0
SEARCH_PASSES = 3


@dataclass(frozen=True)
class Candidate:
    """A record that could go in a block."""

    track_id: int
    artist: str | None
    title: str
    duration_sec: float


@dataclass(frozen=True)
class Fit:
    """A running order and how far it misses the mark by."""

    chosen: tuple[Candidate, ...]
    #  < 0 the block ends early, > 0 it runs over.
    residual_sec: float

    @property
    def error_sec(self) -> float:
        return abs(self.residual_sec)


def advance_sec(duration_sec: float) -> float:
    """How far one record moves the timeline on.

    Less than its length: the crossfade into the next record is played over this
    one's outro, so that much of it never occupies the timeline by itself.
    """
    return duration_sec - settings.crossfade_sec


def break_reserve_sec(song_count: int) -> float:
    """Time set aside for the DJ breaks that will land inside a block.

    Breaks are not planned — the voice agent puts one in front of each batch the
    selector queues, so their number follows from how many records there are.
    What each one costs the timeline is fixed and small (see
    `programme_break_cost_sec`); what it costs to guess wrong is one re-fit.
    """
    if song_count <= 0:
        return 0.0
    breaks = math.ceil(song_count / max(settings.min_queued_songs, 1))
    return breaks * settings.programme_break_cost_sec


def projected_sec(chosen: tuple[Candidate, ...] | list[Candidate]) -> float:
    """How much timeline a running order takes up, breaks included."""
    return sum(advance_sec(c.duration_sec) for c in chosen) + break_reserve_sec(
        len(chosen)
    )


def mark_after(t: datetime) -> datetime:
    """The first :00 or :30 strictly after `t`."""
    block = timedelta(seconds=settings.programme_block_sec)
    epoch = t.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (t - epoch) / block
    return epoch + block * (math.floor(elapsed) + 1)


def block_containing(t: datetime) -> datetime:
    """The mark the block containing `t` opened on."""
    block = timedelta(seconds=settings.programme_block_sec)
    epoch = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return epoch + block * math.floor((t - epoch) / block)


def fit_block(
    order: list[Candidate],
    pool: list[Candidate],
    available_sec: float,
    keep: int = 0,
) -> Fit:
    """Choose which of `order` fills `available_sec`, and how the mark is met.

    `order` is the running order as programmed, and its shape is respected: the
    fit fills from the head, and a record swapped out is replaced where it stood.
    `pool` is everything else that could stand in — the records the DJ listed but
    that did not fit, plus the rest of the library.

    The first `keep` records are pinned: they go in whatever they do to the fit
    and the search may not touch them. That is how a listener request survives
    the block being re-cut around it.

    The search is a few bounded passes of single swaps, then an add and a drop.
    That is enough because the library spans two-minute records and eleven-minute
    ones: there is nearly always one whose length closes the gap on its own. It
    minimises rather than settling for `programme_mark_tolerance_sec`, which is
    the caller's threshold for complaining, not a target to aim at.
    """
    chosen: list[Candidate] = []
    used: set[int] = set()
    fixed = 0  # how many pinned records actually went in, counted not assumed
    # The running order first and the rest of the shelf behind it: a block has to
    # be filled even when the order handed over is shorter than the half hour it
    # has to cover, and the search that follows only makes a handful of moves —
    # it tunes a full block, it cannot build one.
    for index, candidate in enumerate([*order, *pool]):
        if candidate.track_id in used:
            continue
        pinned = index < keep
        if not pinned and projected_sec([*chosen, candidate]) > available_sec:
            continue  # too long for what is left; it may still be swapped in below
        chosen.append(candidate)
        used.add(candidate.track_id)
        fixed += pinned

    spares = [c for c in (*order, *pool) if c.track_id not in used]
    # Same record twice in one block is worse than missing the mark by a second.
    spares = list({c.track_id: c for c in spares}.values())

    best = Fit(tuple(chosen), available_sec - projected_sec(chosen))
    for _ in range(SEARCH_PASSES):
        if best.error_sec <= CLOSE_ENOUGH_SEC:
            break
        improved = _improve(best, spares, available_sec, fixed)
        if improved is None:
            break
        best = improved
    return best


def _improve(
    fit: Fit, spares: list[Candidate], available_sec: float, fixed: int
) -> Fit | None:
    """One pass: the best single swap, add or drop, or None if nothing helps."""
    best = fit
    chosen = list(fit.chosen)
    # `spares` is fixed for the whole search, so a record swapped in on an
    # earlier pass is still sitting in it. Without this the next pass is free to
    # add it a second time, and the block plays the same record twice.
    present = {c.track_id for c in chosen}

    for index in range(fixed, len(chosen)):
        for spare in spares:
            if spare.track_id in present:
                continue
            best = _better(
                best, [*chosen[:index], spare, *chosen[index + 1 :]], available_sec
            )

    for spare in spares:
        if spare.track_id in present:
            continue
        best = _better(best, [*chosen, spare], available_sec)

    for index in range(fixed, len(chosen)):
        best = _better(best, [*chosen[:index], *chosen[index + 1 :]], available_sec)

    return best if best is not fit else None


def _better(best: Fit, trial: list[Candidate], available_sec: float) -> Fit:
    candidate = Fit(tuple(trial), available_sec - projected_sec(trial))
    return candidate if candidate.error_sec < best.error_sec else best


@dataclass(frozen=True)
class Placed:
    """Something in a running order, and what it costs the timeline.

    A record and a bulletin are timed the same way and differ only in where the
    numbers came from: a record's length is a fact, a bulletin's is a
    reservation until it has been spoken.
    """

    duration_sec: float
    advance_sec: float
    is_song: bool = True


def placed_song(candidate: Candidate) -> Placed:
    return Placed(candidate.duration_sec, advance_sec(candidate.duration_sec))


def plan_times(
    block_start: datetime, items: list[Placed]
) -> list[tuple[datetime, datetime]]:
    """Projected airtime of everything in a running order.

    The end is the start plus the item's whole length, so it overlaps the next
    one's start by however much was held back — the same shape
    `PlaylistEntry.actual_start` and `actual_end` take once the renderer has
    committed them. Only records draw down the DJ-break reserve; a news item is
    already a break, and the voice agent does not put another one against it.
    """
    times: list[tuple[datetime, datetime]] = []
    cursor = block_start
    songs = 0
    for item in items:
        times.append((cursor, cursor + timedelta(seconds=item.duration_sec)))
        step = 0.0
        if item.is_song:
            songs += 1
            step = break_reserve_sec(songs) - break_reserve_sec(songs - 1)
        cursor += timedelta(seconds=item.advance_sec + step)
    return times


def lead_into_the_mark(
    chosen: tuple[Candidate, ...], target_sec: float
) -> list[Candidate]:
    """Put the record closest to `target_sec` last, keeping the rest in order.

    The trail for the next bulletin sits before the block's final record, so that
    record is what decides how long before the mark it lands. Reordering the same
    set changes nothing about the total, so the mark is untouched by this — it is
    free, and it is the difference between trailing the news at :55 and trailing
    it at :51.
    """
    if len(chosen) < 2:
        return list(chosen)
    closest = min(chosen, key=lambda c: abs(c.duration_sec - target_sec))
    rest = [c for c in chosen if c.track_id != closest.track_id]
    return [*rest, closest]
