"""Newsroom agent: turns an RSS wire into spoken copy for one station.

It runs per station, and only while that station is up — a dial nobody is
listening to does not need to know what happened. What it produces is text: four
scripts per batch, sitting in `news_segments` waiting for something to read them.
Nothing puts them on air yet.

Two rates, deliberately different. The **wire** is shared: the same feeds serve
every station, so they are polled process-wide and never more than once an hour
each (`news_min_fetch_interval_sec`), no matter how many stations are up or how
often they tick. The row in `news_feeds` is what enforces that across stations
and across restarts, and it is stamped before the request goes out, so a hang or
a crash still counts as this hour's attempt. The **copy** is not shared: each
station's anchor writes their own from the same headlines, once per batch, and
only when the wire has actually moved (`digest`).

The four segments come out of a single LLM call, which is what keeps the teasers
honest — a teaser is written next to the bulletin it announces. Kinds:

## news_teaser
An answer to the DJ asking "what is in the news?":
"Coming up in the news: [headline 1], and [headline 2]. And, [remarkable headline]."

## news
The bulletin: the top three headlines, one sentence each, intro and outro.
 "Here are the headlines: [headline 1]. [1 more sentence about 1].
 [headline 2]. [1 more sentence about 2].
 And [headline 3]. [1 more sentence about 3].
 Then something remarkable: [remarkable headline]. [1 more sentence about it].
 That's all for the news today. Stay tuned for more updates."

## gossip_teaser
"Coming up in the gossip: [gossip headline]."

## gossip
 "Here's what is really keeping people occupied today: [gossip headline].
 [1 more sentence]. [remarkable headline]. [1 more sentence].
 That's all for the gossip today."

Sources are `news_sources`, `news_gossip_sources` and `news_remarkable_sources`
in the settings; they default to the NU.nl feeds.
"""

import asyncio
import hashlib
import time
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import now
from ..config import settings
from ..db import agent_session
from ..feeds import FeedEntry, FeedError, fetch
from ..llm import prompts
from ..llm.client import LLMError, ask_json
from ..llm.schemas import NewsScripts
from ..models import (
    Channel,
    NewsCategory,
    NewsFeed,
    NewsItem,
    NewsSegment,
    NewsSegmentKind,
)
from .base import AgentLoop

# One poll at a time for the whole process. The `news_feeds` row is the real
# hourly gate, but three stations spinning up together would otherwise all read
# it before any of them had written it back.
_poll_lock = asyncio.Lock()


def _sources() -> list[tuple[NewsCategory, str]]:
    return [
        *((NewsCategory.NEWS, url) for url in settings.news_sources),
        *((NewsCategory.GOSSIP, url) for url in settings.news_gossip_sources),
        *((NewsCategory.REMARKABLE, url) for url in settings.news_remarkable_sources),
    ]


def _digest(items: list[NewsItem]) -> str:
    """Fingerprint of the batch an anchor was handed.

    Order matters: the same items in a different order are a different bulletin,
    because the bulletin reads them top down.
    """
    joined = "\n".join(f"{i.category.value}:{i.guid}" for i in items)
    return hashlib.sha256(joined.encode()).hexdigest()[:32]


def _wire(items: list[NewsItem]) -> list[tuple[str, str]]:
    return [(i.title, i.summary) for i in items]


class News(AgentLoop):
    def __init__(self, channel_id: int, slug: str) -> None:
        super().__init__(f"news[{slug}]", settings.news_interval_sec)
        self.channel_id = channel_id
        self._up_since = time.monotonic()

    async def tick(self) -> None:
        await self._poll_wire()

        # A station coming up has the selector and the voice agent queued on the
        # same one-at-a-time LLM lock, and they are the ones holding up the first
        # record. News can wait a minute; the first song cannot.
        if time.monotonic() - self._up_since < settings.news_generate_grace_sec:
            return

        async with agent_session() as session:
            channel = await session.get(Channel, self.channel_id)
            if channel is None:
                return
            headlines = await self._top(
                session, NewsCategory.NEWS, settings.news_headline_count
            )
            if not headlines:
                return  # nothing on the wire yet — the first poll may still be out
            gossip = await self._top(
                session, NewsCategory.GOSSIP, settings.news_gossip_count
            )
            remarkable = await self._top(
                session, NewsCategory.REMARKABLE, settings.news_remarkable_count
            )

            batch = headlines + remarkable + gossip
            digest = _digest(batch)
            if not await self._due(session, digest):
                return
            await self._write(session, channel, digest, headlines, gossip, remarkable)

    # ---- the wire --------------------------------------------------------

    async def _poll_wire(self) -> None:
        """Bring the shared feeds up to date, at most once an hour each."""
        async with _poll_lock:
            polled = added = 0
            for category, url in _sources():
                fetched, new = await self._poll_feed(category, url)
                polled += int(fetched)
                added += new
            if polled:
                self.log.info("polled %d feed(s), %d new item(s)", polled, added)
                async with agent_session() as session:
                    await self._prune(session)
                    await session.commit()

    async def _poll_feed(self, category: NewsCategory, url: str) -> tuple[bool, int]:
        """Returns (did we go out to the network, how many items were new)."""
        async with agent_session() as session:
            feed = await session.get(NewsFeed, url)
            if feed is not None and feed.fetched_at > now() - timedelta(
                seconds=settings.news_min_fetch_interval_sec
            ):
                return False, 0
            if feed is None:
                feed = NewsFeed(url=url, category=category)
                session.add(feed)
            feed.category = category
            # Stamped before the request, not after it: the cap is on requests
            # made, so a timeout or a kill still spends this hour's poll.
            feed.fetched_at = now()
            await session.commit()  # release the writer before the network call

        try:
            entries = await asyncio.to_thread(fetch, url)
        except FeedError as e:
            self.log.warning("feed %s failed: %s", url, e)
            entries = None

        async with agent_session() as session:
            feed = await session.get(NewsFeed, url)
            assert feed is not None, "NewsFeed should exist after commit"
            feed.ok = entries is not None
            feed.error = None if entries is not None else "fetch failed"
            added = 0 if entries is None else await self._store(session, feed, entries)
            await session.commit()
        return True, added

    async def _store(
        self, session: AsyncSession, feed: NewsFeed, entries: list[FeedEntry]
    ) -> int:
        """Insert the items this feed has not filed under this category before."""
        guids = [e.guid for e in entries]
        if not guids:
            return 0
        known = set(
            await session.scalars(
                select(NewsItem.guid).where(
                    NewsItem.category == feed.category, NewsItem.guid.in_(guids)
                )
            )
        )
        stamp = now()
        added = 0
        for entry in entries:
            if entry.guid in known:
                continue
            known.add(entry.guid)  # a feed can carry the same item twice
            session.add(
                NewsItem(
                    category=feed.category,
                    guid=entry.guid,
                    source=feed.url,
                    title=entry.title,
                    summary=entry.summary,
                    link=entry.link,
                    published_at=entry.published_at,
                    fetched_at=stamp,
                )
            )
            added += 1
        return added

    async def _prune(self, session: AsyncSession) -> None:
        """Drop what has aged out: items after a long gap, copy far sooner.

        Copy is written against one hour's headlines, so a segment nobody read is
        worthless long before the item it was written from is.
        """
        await session.execute(
            delete(NewsItem).where(
                NewsItem.fetched_at
                < now() - timedelta(hours=settings.news_retention_hours)
            )
        )
        await session.execute(
            delete(NewsSegment).where(
                NewsSegment.created_at
                < now() - timedelta(seconds=settings.news_segment_ttl_sec)
            )
        )

    # ---- the copy --------------------------------------------------------

    async def _top(
        self, session: AsyncSession, category: NewsCategory, limit: int
    ) -> list[NewsItem]:
        """The freshest items of one kind. Newest first — the bulletin reads top down."""
        rows = await session.scalars(
            select(NewsItem)
            .where(NewsItem.category == category)
            .order_by(NewsItem.published_at.desc(), NewsItem.id.desc())
            .limit(limit)
        )
        return list(rows)

    async def _due(self, session: AsyncSession, digest: str) -> bool:
        last = await session.scalar(
            select(NewsSegment)
            .where(NewsSegment.channel_id == self.channel_id)
            .order_by(NewsSegment.id.desc())
            .limit(1)
        )
        if last is None:
            return True
        if last.digest == digest:
            return False  # the wire has not moved; the copy still stands
        # Backstop against a feed that churns its top items faster than it is
        # polled: one batch of copy per station per hour, whatever the wire does.
        return last.created_at <= now() - timedelta(
            seconds=settings.news_min_fetch_interval_sec
        )

    async def _write(
        self,
        session: AsyncSession,
        channel: Channel,
        digest: str,
        headlines: list[NewsItem],
        gossip: list[NewsItem],
        remarkable: list[NewsItem],
    ) -> None:
        await session.commit()  # release the reader before the LLM call
        try:
            result = await ask_json(
                prompts.news_system(channel),
                prompts.news_scripts(
                    _wire(headlines), _wire(gossip), _wire(remarkable)
                ),
                NewsScripts,
            )
        except LLMError as e:
            # Nothing to fall back to: a bulletin the anchor did not write would
            # be a bulletin nobody checked. The next tick tries again.
            self.log.warning("news copy failed: %s", e)
            return

        scripts = {
            kind: getattr(result, kind.value).strip() for kind in NewsSegmentKind
        }
        missing = [kind.value for kind, script in scripts.items() if not script]
        if missing:
            # All four or none: a batch with an empty bulletin in it would air as
            # silence where the news was announced.
            self.log.warning("news copy came back without %s", ", ".join(missing))
            return

        for kind, script in scripts.items():
            session.add(
                NewsSegment(
                    channel_id=channel.id, kind=kind, script=script, digest=digest
                )
            )
        await session.commit()
        self.log.info(
            "wrote %d segment(s) [%s] from %d item(s), top story: %s",
            len(NewsSegmentKind),
            digest[:8],
            len(headlines) + len(gossip) + len(remarkable),
            headlines[0].title[:80],
        )
