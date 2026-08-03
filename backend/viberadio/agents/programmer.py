"""Programming agent: decides the station's day in advance.

The station used to choose the next record about ninety seconds before playing
it, which meant nothing could ever be made to land anywhere. This agent writes
the running order out to the end of tomorrow instead — songs only, one row per
record in `programme_slots` — and fits every half-hour so it ends on its mark.
The selector then promotes those slots onto the playlist as their turn comes.

Three things happen each tick:

1. **Retire the past.** The programme is a wall-clock grid. A station that was
   asleep for three hours rejoins it at the present; it does not resume where it
   stopped, so slots whose block is over are marked skipped rather than played
   late.
2. **Extend the horizon.** One hour per tick, so building two days never
   monopolises the one-at-a-time LLM lock. The hour the DJ programmes is a
   running order, not a schedule: it is cut to the clock here.
3. **Re-fit the next block.** DJ breaks are not planned, so a block always drifts
   a little from what was projected for it. Re-cutting the earliest block that
   has not been promoted yet, against the real timeline cursor, is what keeps
   that drift from compounding across the day.

The first hours of a cold station are filled by rotation rather than by the DJ:
a station coming on air needs records now, and an LLM call is not the thing to
wait for. Once there is a runway (`programme_min_hours_ahead`), everything
beyond it is programmed properly.

Rotation is only ever a re-shuffle of records this station has already been
given — see `_station_library`. The media library is shared between stations and
choosing off it is the DJ's job, so a station with nothing of its own yet waits
for them rather than opening with the station down the hall's records.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audio import renderer
from ..clock import now
from ..config import settings
from ..db import agent_session
from ..library.downloader import DownloadError, download_song
from ..library.media import find_track, register_track
from ..llm import prompts
from ..llm.client import LLMError, ask_json
from ..llm.schemas import HourPlan
from ..models import (
    Channel,
    EntryKind,
    EntryStatus,
    NewsSegmentKind,
    PlaylistEntry,
    ProgrammeSlot,
    SlotKind,
    SlotOrigin,
    SlotStatus,
    StationRecord,
    Track,
    TrackKind,
)
from ..programme import (
    POSITION_STEP,
    Candidate,
    Placed,
    advance_sec,
    block_containing,
    fit_block,
    lead_into_the_mark,
    mark_after,
    placed_song,
    plan_times,
    projected_sec,
)
from .base import AgentLoop

# How much of the library the DJ is shown when programming an hour. The listing
# is what they choose from, so it is ordered least-recently-programmed first:
# past this many records, what is cut is what they have just had.
LIBRARY_PROMPT_LIMIT = 120

# Aired records fed back into the prompt, so an hour does not open with what the
# last one closed on.
RECENT_WINDOW = 25

# Slots still on the programme are only ever "open" (not yet on the playlist).
OPEN = SlotStatus.PLANNED

# The two origins the planner itself assigns. `SlotOrigin.REQUEST` is the
# selector's to give.
DJ = SlotOrigin.DJ
ROTATION = SlotOrigin.ROTATION


def _key(artist: str | None, title: str) -> str:
    """Match key for a record the DJ named back at us.

    They are echoing a listing we gave them, so this only has to survive
    punctuation and case drifting — "Ain't No Sunshine" against "Aint no sunshine".
    """
    return re.sub(r"[^a-z0-9]+", " ", f"{artist or ''} {title}".lower()).strip()


@dataclass(frozen=True)
class NewsItem:
    """A place kept in a block for something the newsroom will say."""

    kind: SlotKind
    news_kind: NewsSegmentKind
    reserve_sec: float

    @property
    def duration(self) -> float:
        """What the reservation costs the timeline, not how long it is.

        A voice item hands everything past the DJ's opening to the next record's
        intro, so ninety seconds of bulletin only moves the clock on by seventy.
        """
        room = self.reserve_sec - settings.voice_ramp_in_sec - renderer.DRY_SEC
        return self.reserve_sec - max(0.0, min(settings.voice_ramp_out_max_sec, room))


def _bulletin_for(mark: datetime) -> NewsSegmentKind:
    """The news on the hour, the gossip on the half hour."""
    return NewsSegmentKind.NEWS if mark.minute == 0 else NewsSegmentKind.GOSSIP


def _teaser_for(mark: datetime) -> NewsSegmentKind:
    return (
        NewsSegmentKind.NEWS_TEASER
        if mark.minute == 0
        else NewsSegmentKind.GOSSIP_TEASER
    )


def _one_per_record(tracks) -> list[Candidate]:
    """One candidate per record, whichever row it came in on.

    The same song can be in the library twice — downloaded once for a request
    and again for an hour plan, under an artist spelled slightly differently, so
    `find_track`'s exact match missed it. To the programme they are one record,
    and without this the fitter cheerfully puts all three in the same half-hour.
    """
    seen: dict[str, Candidate] = {}
    for track in tracks:
        seen.setdefault(
            _key(track.artist, track.title),
            Candidate(track.id, track.artist, track.title, track.duration_sec),
        )
    return list(seen.values())


class Programmer(AgentLoop):
    def __init__(self, channel_id: int, slug: str) -> None:
        super().__init__(f"programmer[{slug}]", settings.programme_interval_sec)
        self.channel_id = channel_id

    async def tick(self) -> None:
        async with agent_session() as session:
            channel = await session.get(Channel, self.channel_id)
            if channel is None:
                return
            await self._sync_with_playlist(session)
            await self._retire_past(session)
            await session.commit()

            await self._extend(session, channel)

            await self._refit_next_block(session)
            await session.commit()

    # ---- keeping up with the playlist -----------------------------------

    async def _sync_with_playlist(self, session: AsyncSession) -> None:
        """Follow promoted slots to wherever their playlist entry ended up.

        A slot handed to the selector is out of the programme's hands: it airs,
        or the voice agent rejects the update it was in and its entry is
        cancelled. The second case has to come back — the record was programmed
        and has not been played — so the slot returns to the running order.
        """
        promoted = (
            await session.scalars(
                select(ProgrammeSlot)
                .join(PlaylistEntry, PlaylistEntry.id == ProgrammeSlot.entry_id)
                .where(
                    ProgrammeSlot.channel_id == self.channel_id,
                    ProgrammeSlot.status == SlotStatus.QUEUED,
                    PlaylistEntry.status.in_(
                        [EntryStatus.PLAYED, EntryStatus.CANCELLED]
                    ),
                )
            )
        ).all()
        for slot in promoted:
            entry = await session.get(PlaylistEntry, slot.entry_id)
            assert entry is not None
            if entry.status == EntryStatus.PLAYED:
                slot.status = SlotStatus.AIRED
            else:
                slot.status = SlotStatus.PLANNED
                slot.entry_id = None

    async def _retire_past(self, session: AsyncSession) -> None:
        """Blocks that are over are over, whether or not anyone was listening."""
        cutoff = block_containing(now())
        stale = (
            await session.scalars(
                select(ProgrammeSlot).where(
                    ProgrammeSlot.channel_id == self.channel_id,
                    ProgrammeSlot.status == OPEN,
                    ProgrammeSlot.block_start < cutoff,
                )
            )
        ).all()
        for slot in stale:
            slot.status = SlotStatus.SKIPPED
        if stale:
            self.log.info("skipped %d slot(s) whose block has passed", len(stale))

    # ---- the horizon -----------------------------------------------------

    async def _extend(self, session: AsyncSession, channel: Channel) -> None:
        """Programme the next unplanned hour, if there is one to programme."""
        hour = await self._next_open_block(session)
        if hour >= self._horizon():
            return

        library = await self._library(session)
        if not library:
            return  # nothing to programme with yet; bootstrap is still seeding

        # Freshest first, for whoever is choosing: the DJ sees this as the listing
        # they pick from, and rotation takes it as the running order. Without it
        # an hour of rotation is the same hour every time.
        recent = await self._recent(session)
        shared = await self._least_recently_programmed(session, library)
        mine = await self._least_recently_programmed(
            session, await self._station_library(session)
        )

        ahead = (hour - now()).total_seconds() / 3600
        if mine and ahead < settings.programme_min_hours_ahead:
            # No runway: records now beat records chosen well.
            order, note, origin = self._rotation(mine, recent), "rotation", ROTATION
        else:
            # A station with nothing of its own has no rotation to fall back on,
            # and waiting for the DJ is the only honest option — the alternative
            # is opening with whatever the station down the hall downloaded.
            order, note, origin = await self._dj_order(
                session, channel, hour, shared, recent, mine
            )
        if not order:
            return

        blocks = await self._cut_to_clock(session, hour, order, mine, origin)
        if blocks:
            self.log.info(
                "programmed %s (%s): %s",
                hour.strftime("%a %H:%M"),
                note,
                ", ".join(f"{n} records, {r:+.1f}s" for n, r in blocks),
            )

    def _horizon(self) -> datetime:
        """Midnight after tomorrow, in the station's own timezone."""
        zone = ZoneInfo(settings.station_timezone)
        local = now().astimezone(zone)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return (midnight + timedelta(days=2)).astimezone(now().tzinfo)

    async def _next_open_block(self, session: AsyncSession) -> datetime:
        """The first block with nothing programmed in it, from the current one on.

        Counted over every status: a block whose records were all dropped or all
        skipped has had its turn, and re-programming it would mean re-programming
        the past.
        """
        current = block_containing(now())
        last = await session.scalar(
            select(func.max(ProgrammeSlot.block_start)).where(
                ProgrammeSlot.channel_id == self.channel_id
            )
        )
        if last is None:
            return current
        return max(last + timedelta(seconds=settings.programme_block_sec), current)

    async def _library(self, session: AsyncSession) -> list[Candidate]:
        """Every record in the house. Only the DJ gets to choose from this.

        The media library is shared — a track downloaded for one station is
        available to all — and that is deliberate, but it is a shelf, not a
        playlist. Someone with taste has to pick off it.
        """
        rows = (
            await session.scalars(
                select(Track)
                .where(Track.kind == TrackKind.SONG, Track.duration_sec > 0)
                .order_by(Track.id)
            )
        ).all()
        return _one_per_record(rows)

    async def _station_library(self, session: AsyncSession) -> list[Candidate]:
        """The records this station has been *given*, and may rotate.

        Everything that picks a record without the DJ in the room — the rotation
        fallback, the fitter's swaps, a block being re-cut — chooses from here
        and not from the shared shelf. Unfiltered, the jazz station opens with
        Led Zeppelin because the rock station downloaded it last night, which is
        the first thing a listener notices and the last thing they forgive.

        See `StationRecord`: put here by a judgement, never by airplay. Empty
        until the DJ has programmed once, which is the right answer for a station
        that has never been programmed — it waits for them rather than opening
        with the station down the hall's records.
        """
        vetted = select(StationRecord.track_id).where(
            StationRecord.channel_id == self.channel_id
        )
        return await self._tracks_in(session, vetted)

    async def _keep(self, session: AsyncSession, tracks: list[Candidate]) -> int:
        """Take the DJ at their word: everything they named is this station's.

        Including what the clock had no room for. An hour plan is a judgement
        about records, not about half-hours, and keeping only the ones that
        happened to fit would leave rotation with a handful to shuffle.
        """
        known = set(
            await session.scalars(
                select(StationRecord.track_id).where(
                    StationRecord.channel_id == self.channel_id
                )
            )
        )
        added = 0
        for track in tracks:
            if track.track_id in known:
                continue
            known.add(track.track_id)
            session.add(
                StationRecord(channel_id=self.channel_id, track_id=track.track_id)
            )
            added += 1
        return added

    async def _tracks_in(self, session: AsyncSession, subquery) -> list[Candidate]:
        rows = (
            await session.scalars(
                select(Track)
                .where(
                    Track.kind == TrackKind.SONG,
                    Track.duration_sec > 0,
                    Track.id.in_(subquery),
                )
                .order_by(Track.id)
            )
        ).all()
        return _one_per_record(rows)

    async def _dj_order(
        self,
        session: AsyncSession,
        channel: Channel,
        hour: datetime,
        shown: list[Candidate],
        recent: list[tuple[str | None, str]],
        mine: list[Candidate],
    ) -> tuple[list[Candidate], str, SlotOrigin]:
        """Ask the DJ to programme the hour; fall back to rotation if they can't.

        `shown` is the whole library, which is what the DJ is allowed to choose
        from. `mine` is only this station's records — all the fallback may use,
        and empty on a station that has never played anything.
        """
        await session.commit()  # release the reader before the LLM call and downloads
        local = hour.astimezone(ZoneInfo(settings.station_timezone))
        try:
            plan = await ask_json(
                prompts.channel_system(channel),
                prompts.hour_plan(
                    channel,
                    local.strftime("%A %H:%M"),
                    [(c.artist, c.title, c.duration_sec) for c in shown],
                    recent,
                    settings.programme_hour_candidates,
                    settings.programme_new_songs_per_hour,
                ),
                HourPlan,
            )
        except LLMError as e:
            self.log.warning("hour plan failed (%s); falling back to rotation", e)
            return self._rotation(mine, recent), "rotation", ROTATION

        by_key = {_key(c.artist, c.title): c for c in shown}
        order: list[Candidate] = []
        for song in plan.songs:
            match = by_key.get(_key(song.artist, song.title))
            if match is not None and match not in order:
                order.append(match)
        if not order:
            self.log.warning("hour plan named nothing in the library; using rotation")
            return self._rotation(mine, recent), "rotation", ROTATION

        kept = await self._keep(session, order)
        await session.commit()
        if kept:
            self.log.info("%d record(s) added to the station's shelf", kept)

        await self._fetch_new(session, plan)
        return order, plan.note or "programmed", DJ

    async def _fetch_new(self, session: AsyncSession, plan: HourPlan) -> None:
        """Pull records the DJ asked for that the library does not have.

        They go into the library, not into this hour: the hour is being fitted to
        the clock right now and a download that fails halfway would take the
        block's arithmetic with it. They are available from the next hour on.
        """
        for song in plan.new_songs[: settings.programme_new_songs_per_hour]:
            if await find_track(session, song.artist, song.title) is not None:
                continue
            try:
                path, url = await download_song(song.artist, song.title)
                await register_track(
                    session,
                    path,
                    title=song.title,
                    artist=song.artist,
                    source_url=url,
                )
                await session.commit()
                self.log.info("library: added %s — %s", song.artist, song.title)
            except DownloadError as e:
                self.log.warning(
                    "could not add %s — %s: %s", song.artist, song.title, e
                )

    def _rotation(
        self, library: list[Candidate], recent: list[tuple[str | None, str]]
    ) -> list[Candidate]:
        """The fallback running order: the library, freshest first, artists spread out.

        `library` arrives least-recently-programmed first, so this only has to
        push what has just aired towards the back and stop the same artist from
        turning up twice in a row.

        Note *towards the back*, not out. A station whose own shelf is two dozen
        records has aired most of them recently by definition, and dropping those
        would leave nothing to programme with — an hour of the wrong length is
        worse than an hour that repeats.
        """
        heard = {_key(a, t) for a, t in recent}
        fresh = [c for c in library if _key(c.artist, c.title) not in heard]
        stale = [c for c in library if _key(c.artist, c.title) in heard]

        order: list[Candidate] = []
        held: list[Candidate] = []
        last_artist: str | None = None
        for candidate in [*fresh, *stale]:
            if candidate.artist and candidate.artist == last_artist:
                held.append(candidate)
                continue
            order.append(candidate)
            last_artist = candidate.artist
        return [*order, *held][: settings.programme_hour_candidates]

    async def _recent(self, session: AsyncSession) -> list[tuple[str | None, str]]:
        rows = (
            await session.execute(
                select(Track.artist, Track.title)
                .join(PlaylistEntry, PlaylistEntry.track_id == Track.id)
                .where(
                    PlaylistEntry.channel_id == self.channel_id,
                    PlaylistEntry.status == EntryStatus.PLAYED,
                )
                .order_by(PlaylistEntry.seq.desc())
                .limit(RECENT_WINDOW)
            )
        ).all()
        return [(r[0], r[1]) for r in rows]

    async def _least_recently_programmed(
        self, session: AsyncSession, library: list[Candidate]
    ) -> list[Candidate]:
        """The library, the records it has been longest since programming first."""
        rows = (
            await session.execute(
                select(
                    ProgrammeSlot.track_id,
                    func.max(ProgrammeSlot.block_start).label("last"),
                )
                .where(ProgrammeSlot.channel_id == self.channel_id)
                .group_by(ProgrammeSlot.track_id)
            )
        ).all()
        last = {track_id: when for track_id, when in rows}
        never = datetime.min.replace(tzinfo=now().tzinfo)
        ordered = sorted(library, key=lambda c: last.get(c.track_id, never))
        return ordered[:LIBRARY_PROMPT_LIMIT]

    # ---- cutting an hour to the clock ------------------------------------

    async def _cut_to_clock(
        self,
        session: AsyncSession,
        hour: datetime,
        order: list[Candidate],
        library: list[Candidate],
        origin: SlotOrigin,
    ) -> list[tuple[int, float]]:
        """Fit a running order into the hour's blocks and write the slots.

        An hour is two blocks, and each is fitted against what is left of the
        running order — so the DJ's opening records go in the first half-hour and
        what they put at the back fills the second.
        """
        written: list[tuple[int, float]] = []
        remaining = list(order)
        block = hour
        end_of_hour = hour + timedelta(hours=1)
        while block < end_of_hour:
            mark = mark_after(block)
            if await self._has_slots(session, block):
                block = mark
                continue
            # The same cursor the re-fit works from, so a station that came up
            # mid-block — with the selector's fallback records already on the
            # timeline — is planned around them instead of on top of them.
            start = await self._cursor(session, block)
            # The block opens with a bulletin and trails the next one before its
            # last record, so the records only ever get what those two leave.
            news = self._news_items(block, start)
            available = (mark - start).total_seconds() - sum(
                item.duration for item in news
            )
            if available < settings.crossfade_sec:
                block = mark
                continue

            pool = [c for c in library if c not in remaining]
            fit = fit_block(remaining, pool, available)
            if not fit.chosen:
                block = mark
                continue

            chosen_order = lead_into_the_mark(fit.chosen, settings.news_link_lead_sec)
            await self._write_slots(session, block, start, chosen_order, origin, news)
            written.append((len(fit.chosen), fit.residual_sec))
            if abs(fit.residual_sec) > settings.programme_mark_tolerance_sec:
                self.log.warning(
                    "block %s misses its mark by %.1fs",
                    block.strftime("%H:%M"),
                    fit.residual_sec,
                )
            chosen = {c.track_id for c in fit.chosen}
            remaining = [c for c in remaining if c.track_id not in chosen]
            block = mark
        await session.commit()
        return written

    async def _has_slots(self, session: AsyncSession, block_start: datetime) -> bool:
        result = await session.scalar(
            select(func.count())
            .select_from(ProgrammeSlot)
            .where(
                ProgrammeSlot.channel_id == self.channel_id,
                ProgrammeSlot.block_start == block_start,
            )
        )
        return result is not None and result > 0

    def _news_items(self, block_start: datetime, start: datetime) -> list[NewsItem]:
        """What the newsroom has booked in this block.

        A bulletin on the block's own mark, and the trail for the next one. A
        station that woke up mid-block has missed its bulletin — the mark is
        gone — but can still trail the one coming.
        """
        if not settings.news_on_the_hour:
            return []
        items = []
        if start <= block_start + timedelta(seconds=settings.crossfade_sec):
            items.append(
                NewsItem(
                    SlotKind.BULLETIN,
                    _bulletin_for(block_start),
                    settings.news_bulletin_reserve_sec,
                )
            )
        items.append(
            NewsItem(
                SlotKind.LINK,
                _teaser_for(mark_after(block_start)),
                settings.news_link_reserve_sec,
            )
        )
        return items

    @staticmethod
    def _arrange(
        news: list[NewsItem], songs: list[Candidate]
    ) -> list[tuple[NewsItem | Candidate, Placed]]:
        """The running order: bulletin, records, and the trail before the last one.

        `[BULLETIN] song song song [LINK] song |mark|` — the trail sits second
        from the end so the record after it is what carries the station up to the
        mark.
        """
        bulletin = next((i for i in news if i.kind == SlotKind.BULLETIN), None)
        link = next((i for i in news if i.kind == SlotKind.LINK), None)

        order: list[tuple[NewsItem | Candidate, Placed]] = []
        if bulletin is not None:
            order.append(
                (bulletin, Placed(bulletin.reserve_sec, bulletin.duration, False))
            )
        for index, song in enumerate(songs):
            if link is not None and songs and index == len(songs) - 1:
                order.append((link, Placed(link.reserve_sec, link.duration, False)))
            order.append((song, placed_song(song)))
        # No records to hide behind: the trail still goes out, at the end.
        if link is not None and not songs:
            order.append((link, Placed(link.reserve_sec, link.duration, False)))
        return order

    async def _write_slots(
        self,
        session: AsyncSession,
        block_start: datetime,
        first_start: datetime,
        chosen: list[Candidate],
        origin: SlotOrigin,
        news: list[NewsItem],
    ) -> None:
        arranged = self._arrange(news, list(chosen))
        times = plan_times(first_start, [placed for _, placed in arranged])
        for index, ((item, placed), (start, end)) in enumerate(zip(arranged, times)):
            is_news = isinstance(item, NewsItem)
            session.add(
                ProgrammeSlot(
                    channel_id=self.channel_id,
                    block_start=block_start,
                    position=index * POSITION_STEP,
                    kind=item.kind if is_news else SlotKind.SONG,
                    track_id=None if is_news else item.track_id,
                    news_kind=item.news_kind if is_news else None,
                    duration_sec=placed.duration_sec,
                    planned_start=start,
                    planned_end=end,
                    status=SlotStatus.PLANNED,
                    origin=origin,
                )
            )

    # ---- keeping the marks -----------------------------------------------

    async def _refit_next_block(self, session: AsyncSession) -> None:
        """Re-cut the earliest un-promoted block against the real cursor.

        Everything that has already gone to the playlist is untouchable, so this
        works on the tail of a block: what is left to air, in the time that is
        actually left before the mark.
        """
        slots = await self._open_slots(session)
        if not slots:
            return
        block_start = slots[0].block_start
        slots = [s for s in slots if s.block_start == block_start]
        mark = block_start + timedelta(seconds=settings.programme_block_sec)
        start = await self._cursor(session, block_start)

        # The newsroom's places are not the fitter's to take: a bulletin is on
        # its mark because that is the whole point of it, and the trail belongs
        # to the bulletin it announces. Only the records move.
        booked = [s for s in slots if s.kind != SlotKind.SONG]
        songs = [s for s in slots if s.kind == SlotKind.SONG]
        available = (mark - start).total_seconds() - sum(
            self._slot_advance(s) for s in booked
        )

        pinned = [s for s in songs if s.origin == SlotOrigin.REQUEST]
        rest = [s for s in songs if s.origin != SlotOrigin.REQUEST]
        order = [*pinned, *rest]
        candidates = [await self._candidate(session, s) for s in order]

        current = available - projected_sec(candidates)
        if abs(current) <= settings.programme_mark_tolerance_sec:
            # Close enough, and re-cutting a block nobody is unhappy with only
            # churns what the console is already showing.
            await self._retime(session, booked, order, candidates, start)
            return

        # This station's own records only: a re-cut is the fitter choosing, not
        # the DJ, so it may not reach across to another station's shelf.
        library = await self._station_library(session)
        taken = {c.track_id for c in candidates}
        pool = [c for c in library if c.track_id not in taken]
        fit = fit_block(candidates, pool, available, keep=len(pinned))
        if fit.error_sec >= abs(current):
            # The block cannot be made to land — one record left that overruns,
            # or a library with nothing the right length. Re-cutting it into an
            # equally wrong shape every tick would only churn the running order.
            await self._retime(session, booked, order, candidates, start)
            return

        by_track = {c.track_id: c for c in fit.chosen}
        kept: list[ProgrammeSlot] = []
        for slot, candidate in zip(order, candidates):
            if candidate.track_id in by_track:
                kept.append(slot)
                by_track.pop(candidate.track_id)
            else:
                slot.status = SlotStatus.DROPPED
        # Whatever the fit added that was not already in the block.
        position = max((s.position for s in slots), default=0) + POSITION_STEP
        for candidate in fit.chosen:
            if candidate.track_id not in by_track:
                continue
            slot = ProgrammeSlot(
                channel_id=self.channel_id,
                block_start=block_start,
                position=position,
                kind=SlotKind.SONG,
                track_id=candidate.track_id,
                duration_sec=candidate.duration_sec,
                status=SlotStatus.PLANNED,
                origin=SlotOrigin.ROTATION,
            )
            session.add(slot)
            kept.append(slot)
            position += POSITION_STEP

        ordered = lead_into_the_mark(fit.chosen, settings.news_link_lead_sec)
        rank = {c.track_id: i for i, c in enumerate(ordered)}
        await self._retime(
            session,
            booked,
            sorted(kept, key=lambda s: rank[s.track_id]),
            list(ordered),
            start,
        )
        self.log.info(
            "re-cut block %s: %.1fs off the mark → %.1fs (%d records)",
            block_start.strftime("%H:%M"),
            current,
            fit.residual_sec,
            len(fit.chosen),
        )

    async def _retime(
        self,
        session: AsyncSession,
        booked: list[ProgrammeSlot],
        slots: list[ProgrammeSlot],
        candidates: list[Candidate],
        start: datetime,
    ) -> None:
        """Renumber and re-project what is left of a block.

        The newsroom's slots go back in the shape they were cut in — the
        bulletin first, the trail before the last record — around whatever
        records the fit settled on.
        """
        bulletin = next((s for s in booked if s.kind == SlotKind.BULLETIN), None)
        link = next((s for s in booked if s.kind == SlotKind.LINK), None)

        arranged: list[tuple[ProgrammeSlot, Placed]] = []
        if bulletin is not None:
            arranged.append((bulletin, self._slot_placed(bulletin)))
        for index, (slot, candidate) in enumerate(zip(slots, candidates)):
            if link is not None and index == len(slots) - 1:
                arranged.append((link, self._slot_placed(link)))
            arranged.append((slot, placed_song(candidate)))
        if link is not None and not slots:
            arranged.append((link, self._slot_placed(link)))

        times = plan_times(start, [placed for _, placed in arranged])
        for index, ((slot, placed), (begins, ends)) in enumerate(zip(arranged, times)):
            slot.position = index * POSITION_STEP
            slot.duration_sec = placed.duration_sec
            slot.planned_start = begins
            slot.planned_end = ends

    @staticmethod
    def _slot_placed(slot: ProgrammeSlot) -> Placed:
        """A news slot's own length, real once it has been spoken."""
        reserve = slot.duration_sec or settings.news_bulletin_reserve_sec
        room = reserve - settings.voice_ramp_in_sec - renderer.DRY_SEC
        advance = reserve - max(0.0, min(settings.voice_ramp_out_max_sec, room))
        return Placed(reserve, advance, is_song=False)

    def _slot_advance(self, slot: ProgrammeSlot) -> float:
        return self._slot_placed(slot).advance_sec

    async def _open_slots(self, session: AsyncSession) -> list[ProgrammeSlot]:
        rows = await session.scalars(
            select(ProgrammeSlot)
            .where(
                ProgrammeSlot.channel_id == self.channel_id,
                ProgrammeSlot.status == OPEN,
            )
            .order_by(ProgrammeSlot.block_start, ProgrammeSlot.position)
        )
        return list(rows)

    async def _candidate(self, session: AsyncSession, slot: ProgrammeSlot) -> Candidate:
        track = await session.get(Track, slot.track_id)
        assert track is not None, "Slot with no track should not be selected"
        return Candidate(track.id, track.artist, track.title, track.duration_sec)

    async def _cursor(self, session: AsyncSession, block_start: datetime) -> datetime:
        """When the un-promoted part of this block actually starts.

        The end of everything the timeline has already been given, which is two
        things: what the renderer has committed — where the drift lives, since
        that is what the DJ breaks have pushed along — and what has been handed
        to the playlist but not yet rendered.

        The second part is what stops this from oscillating. A record leaves the
        programme the moment the selector promotes it, several minutes before the
        renderer reaches it; without counting it here the block would look four
        minutes emptier than it is, get a record added to fill the gap, and then
        have it taken straight back out once the renderer caught up.
        """
        committed = await session.scalar(
            select(func.max(PlaylistEntry.actual_end)).where(
                PlaylistEntry.channel_id == self.channel_id,
                PlaylistEntry.status.in_([EntryStatus.QUEUED, EntryStatus.PLAYING]),
            )
        )
        edge = max(committed or now(), now())

        promoted = await session.scalars(
            select(PlaylistEntry).where(
                PlaylistEntry.channel_id == self.channel_id,
                PlaylistEntry.status.in_([EntryStatus.DRAFT, EntryStatus.QUEUED]),
                PlaylistEntry.actual_end.is_(None),
            )
        )
        for entry in promoted:
            edge += timedelta(seconds=self._entry_advance(entry))
        # A block that has not been reached yet begins on its mark; one that is
        # already running begins wherever the timeline has got to.
        return max(edge, block_start)

    @staticmethod
    def _entry_advance(entry: PlaylistEntry) -> float:
        """How far a playlist entry will move the timeline once it is rendered.

        The same arithmetic `TimelineProducer._hold_out_sec` does: a song hands
        its crossfade to the record after it, and a break hands everything past
        the DJ's opening to the next song's intro, so a short break costs the
        same few seconds however much is said inside it.
        """
        duration = entry.duration_sec or 0.0
        if entry.kind == EntryKind.VOICE:
            room = duration - settings.voice_ramp_in_sec - renderer.DRY_SEC
            return duration - max(0.0, min(settings.voice_ramp_out_max_sec, room))
        return advance_sec(duration)
