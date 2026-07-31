"""CLI: ingest a local audio file or download a song into the media library.

Usage:
  uv run python -m viberadio.ingest --file song.mp3 --title "Black Betty" --artist "Ram Jam"
  uv run python -m viberadio.ingest --search "Ram Jam - Black Betty"
  uv run python -m viberadio.ingest --file ident.wav --title "Station Ident" --kind jingle
"""

import argparse
import asyncio
from pathlib import Path

from .config import settings
from .db import session_scope
from .library.downloader import download_song
from .library.media import register_track
from .models import TrackKind


async def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=Path, help="Local audio file to ingest")
    group.add_argument("--search", help='Search+download, e.g. "Ram Jam - Black Betty"')
    parser.add_argument("--title")
    parser.add_argument("--artist")
    parser.add_argument("--kind", choices=[k.value for k in TrackKind], default="song")
    args = parser.parse_args()

    settings.ensure_dirs()

    if args.file:
        if not args.title:
            parser.error("--title is required with --file")
        src, source_url = args.file, None
        title, artist = args.title, args.artist
    else:
        artist, _, title = args.search.partition(" - ")
        if not title:
            artist, title = None, args.search
        src, source_url = await download_song(artist, title)
        title = args.title or title
        artist = args.artist or artist

    async with session_scope() as session:
        track = await register_track(
            session,
            src,
            title=title,
            artist=artist,
            kind=TrackKind(args.kind),
            source_url=source_url,
        )
        print(
            f"track id={track.id} {track.artist} — {track.title} ({track.duration_sec:.1f}s)"
        )
        print(f"normalized: {track.normalized_path}")


if __name__ == "__main__":
    asyncio.run(main())
