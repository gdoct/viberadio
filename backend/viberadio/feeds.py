"""Reading RSS, kept deliberately small.

Four URLs polled once an hour do not justify a dependency, so this is stdlib: one
GET, one parse, a list of entries. RSS 2.0 and Atom look different enough at the
top and similar enough at the item that matching on local tag names covers both
without either being special-cased.
"""

import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree

from .config import settings

log = logging.getLogger("feeds")

USER_AGENT = "viberadio/0.1 (radio station; one poll per hour)"

_TAGS_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class FeedError(Exception):
    """The feed could not be fetched or made sense of."""


@dataclass(frozen=True)
class FeedEntry:
    guid: str
    title: str
    summary: str
    link: str | None
    published_at: datetime | None


def _local(tag: str) -> str:
    """Tag name without its namespace — Atom has one, RSS does not."""
    return tag.rpartition("}")[2]


def _child(element: ElementTree.Element, *names: str) -> ElementTree.Element | None:
    for child in element:
        if _local(child.tag) in names:
            return child
    return None


def _text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    # Summaries arrive as escaped HTML; the anchor reads what we store, so tags and
    # entities have to come off here rather than in the prompt.
    raw = "".join(element.itertext())
    return _WS_RE.sub(" ", unescape(_TAGS_RE.sub(" ", raw))).strip()


def _timestamp(value: str) -> datetime | None:
    """RFC 822 (RSS) or ISO 8601 (Atom), always returned as UTC."""
    value = value.strip()
    if not value:
        return None
    for parse in (parsedate_to_datetime, datetime.fromisoformat):
        try:
            parsed = parse(value)
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _headline(title: str) -> str:
    """Drop a section label a feed has filed in front of the headline.

    Papers put their rubric in the title — "NU+ | ...", "Schermtijd | ..." — which
    is a shelf the item sits on, not something anyone would say out loud. Only a
    single short word is taken as a label, so a sentence that happens to contain
    a pipe keeps all of itself: dropping words off a real headline would be the
    worse mistake of the two.
    """
    label, sep, rest = title.partition(" | ")
    if sep and rest and len(label) <= 20 and " " not in label:
        return rest.strip()
    return title


def _link(item: ElementTree.Element) -> str | None:
    link = _child(item, "link")
    if link is None:
        return None
    return (link.text or "").strip() or link.get("href")


def parse(payload: bytes) -> list[FeedEntry]:
    """Every item in a feed document, in the order it was published in."""
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as e:
        raise FeedError(f"not parseable as XML: {e}") from e

    entries: list[FeedEntry] = []
    for item in (el for el in root.iter() if _local(el.tag) in ("item", "entry")):
        title = _headline(_text(_child(item, "title")))
        link = _link(item)
        # A feed without guids is legal; the link, then the headline, identify the
        # item well enough to keep it from being stored twice.
        guid = _text(_child(item, "guid", "id")) or link or title
        if not title or not guid:
            continue
        entries.append(
            FeedEntry(
                guid=guid[:500],
                title=title,
                summary=_text(_child(item, "description", "summary", "content")),
                link=link,
                published_at=_timestamp(
                    _text(_child(item, "pubDate", "published", "updated"))
                ),
            )
        )
    return entries


def fetch(url: str) -> list[FeedEntry]:
    """Blocking: one GET, parsed. Call it from a thread."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(
            request, timeout=settings.news_fetch_timeout_sec
        ) as response:
            payload = response.read()
    except (urllib.error.URLError, OSError) as e:
        raise FeedError(f"{url}: {e}") from e
    return parse(payload)
