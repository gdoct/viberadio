import asyncio
import logging
import subprocess

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .clock import now
from .config import settings
from .models import Channel, StationState, Track, TrackKind
from .presets import load_stations

log = logging.getLogger(__name__)

PRESET_FIELDS = (
    "name",
    "style",
    "dj_name",
    "dj_persona",
    "dj_bits",
    "news_anchor",
    "catchphrase",
    "tts_voice",
)


async def ensure_channels(session: AsyncSession) -> list[Channel]:
    """Bring `channels` in line with `stations/*.md`, in dial order.

    The files are the source of truth, so editing one and restarting is all it
    takes to change a station. A station is matched by slug, and everything it has
    accumulated — playlist, timeline, history — belongs to that row, so renaming a
    station keeps its past while changing its slug starts a new one.
    """
    channels: list[Channel] = []
    for order, preset in enumerate(load_stations()):
        channel = await session.scalar(
            select(Channel).where(Channel.slug == preset.slug)
        )
        is_new = channel is None
        if channel is None:
            channel = Channel(slug=preset.slug)
            session.add(channel)
        changed = [f for f in PRESET_FIELDS if getattr(channel, f) != getattr(preset, f)]
        for field in changed:
            setattr(channel, field, getattr(preset, field))
        channel.sort_order = order
        await session.flush()
        if is_new:
            log.info(
                "Created station %s — %r (%s)", preset.slug, preset.name, preset.style
            )
        elif changed:
            log.info(
                "Station %s updated from its file: %s", preset.slug, ", ".join(changed)
            )
        channels.append(channel)
        await ensure_station_state(session, channel.id)
    return channels


async def default_channel(session: AsyncSession) -> Channel | None:
    return await session.scalar(
        select(Channel).order_by(Channel.sort_order, Channel.id).limit(1)
    )


def _synth_jingle(dst_path: str) -> None:
    """Synthesize a short, inoffensive station ident chord with ffmpeg (no external assets)."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=415:duration=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=494:duration=8",
            "-filter_complex",
            "amix=inputs=3,vibrato=f=5:d=0.2,afade=t=in:d=0.6,afade=t=out:st=5.5:d=2.5,volume=0.4",
            "-ar",
            str(settings.sample_rate),
            "-ac",
            str(settings.channels),
            dst_path,
        ],
        check=True,
        capture_output=True,
    )


async def ensure_fallback_jingle(session: AsyncSession) -> Track:
    """The renderer loops this when the queue is empty — the stream must never stall."""
    jingle = await session.scalar(
        select(Track).where(Track.kind == TrackKind.JINGLE).limit(1)
    )
    if jingle is not None:
        return jingle
    from .library.media import register_track

    raw = settings.media_dir / "station-ident.wav"
    await asyncio.to_thread(_synth_jingle, str(raw))
    jingle = await register_track(
        session, raw, title="Station Ident", artist=None, kind=TrackKind.JINGLE
    )
    log.info("Synthesized fallback station ident jingle")
    return jingle


FALLBACK_STARTER_SONGS = [
    ("Creedence Clearwater Revival", "Bad Moon Rising"),
    ("The Rolling Stones", "Gimme Shelter"),
    ("Lynyrd Skynyrd", "Sweet Home Alabama"),
    ("The Beatles", "Come Together"),
    ("Fleetwood Mac", "Go Your Own Way"),
    ("Deep Purple", "Smoke on the Water"),
    ("The Doors", "Roadhouse Blues"),
    ("Steppenwolf", "Born to Be Wild"),
]


async def seed_library(session: AsyncSession, channel: Channel, count: int = 8) -> int:
    """Download a starter set in the station's style so its first minutes aren't jingles."""
    from .library.downloader import DownloadError, download_song
    from .library.media import find_track, register_track
    from .llm import prompts
    from .llm.client import LLMError, ask_json
    from .llm.schemas import StarterPlaylist

    try:
        result = await ask_json(
            prompts.channel_system(channel),
            prompts.starter_playlist(channel, count),
            StarterPlaylist,
        )
        wanted = [(s.artist, s.title) for s in result.songs][:count]
    except LLMError as e:
        log.warning(
            "starter playlist generation failed (%s); using the built-in list", e
        )
        wanted = FALLBACK_STARTER_SONGS[:count]

    added = 0
    for artist, title in wanted:
        if await find_track(session, artist, title) is not None:
            continue
        try:
            path, url = await download_song(artist, title)
            await register_track(
                session, path, title=title, artist=artist, source_url=url
            )
            await session.commit()
            added += 1
        except DownloadError as e:
            log.warning("seed download failed for %s — %s: %s", artist, title, e)
    return added


async def ensure_station_state(session: AsyncSession, channel_id: int) -> StationState:
    state = await session.get(StationState, channel_id)
    if state is None:
        state = StationState(
            channel_id=channel_id,
            timeline_epoch=now(),
            next_seq=0,
            samples_rendered=0,
            entry_sample_offset=0,
        )
        session.add(state)
        await session.flush()
        log.info("Initialized station state, epoch=%s", state.timeline_epoch)
    return state


async def _main() -> None:
    """CLI: `uv run python -m viberadio.bootstrap --seed [--count N]`"""
    import argparse

    from . import migrations
    from .db import Base, engine, session_scope

    parser = argparse.ArgumentParser(description="Vibe Radio bootstrap")
    parser.add_argument(
        "--seed", action="store_true", help="Download a starter set of songs"
    )
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument(
        "--station", help="Slug to seed (default: every station on the dial)"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    settings.ensure_dirs()
    async with engine.begin() as conn:
        # Same order as the app's startup: patch existing tables, then create new ones.
        await migrations.apply(conn)
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        await ensure_channels(session)
        await ensure_fallback_jingle(session)
    if args.seed:
        async with session_scope() as session:
            targets = [
                c
                for c in await ensure_channels(session)
                if args.station in (None, c.slug)
            ]
            for channel in targets:
                added = await seed_library(session, channel, args.count)
                print(f"{channel.slug}: seeded {added} song(s).")


if __name__ == "__main__":
    asyncio.run(_main())
