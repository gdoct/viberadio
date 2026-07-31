"""Song download skill: search YouTube with yt-dlp and fetch bestaudio."""

import asyncio
import logging
from pathlib import Path

import yt_dlp

from ..config import settings

log = logging.getLogger(__name__)


class DownloadError(Exception):
    pass


def _download_sync(query: str) -> tuple[Path, str]:
    """Search YouTube for `query`, download bestaudio, return (path, source_url)."""
    outtmpl = str(settings.media_dir / "%(id)s.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        # Skip live streams and overly long results (> 15 min is not a radio song)
        "match_filter": yt_dlp.utils.match_filter_func("!is_live & duration < 900"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=True)
        if info is None:
            raise DownloadError(f"No results for {query!r}")
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise DownloadError(f"No results for {query!r}")
            info = entries[0]
        path = Path(ydl.prepare_filename(info))
        if not path.exists():
            raise DownloadError(f"Download produced no file for {query!r}")
        return path, info.get("webpage_url", "")


async def download_song(artist: str | None, title: str) -> tuple[Path, str]:
    query = f"{artist} - {title}" if artist else title
    log.info("Downloading %r via yt-dlp", query)
    try:
        return await asyncio.to_thread(_download_sync, query)
    except DownloadError:
        raise
    except Exception as e:
        raise DownloadError(f"yt-dlp failed for {query!r}: {e}") from e
