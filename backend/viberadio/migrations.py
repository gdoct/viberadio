"""Tiny forward-only schema patches.

The project uses `create_all` rather than Alembic (see README), which happily
creates new tables but never alters existing ones. These patches bridge that gap
for databases created before multi-station support, so an existing station keeps
its library and playback history.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger("migrations")


async def _columns(conn: AsyncConnection, table: str) -> set[str]:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {row[1] for row in result}


async def _table_exists(conn: AsyncConnection, table: str) -> bool:
    result = await conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": table},
    )
    return result.first() is not None


async def apply(conn: AsyncConnection) -> None:
    """Run before `create_all`, so newly-shaped tables are then created cleanly."""
    if not await _table_exists(conn, "channels"):
        return  # fresh database — create_all builds the current schema

    cols = await _columns(conn, "channels")
    if "slug" not in cols:
        # Nullable + backfilled rather than NOT NULL: SQLite cannot add a NOT NULL
        # column without a default, and the unique index is what actually matters.
        await conn.execute(text("ALTER TABLE channels ADD COLUMN slug VARCHAR(60)"))
        await conn.execute(
            text("UPDATE channels SET slug = 'kgor' WHERE slug IS NULL AND id = 1")
        )
        await conn.execute(
            text("UPDATE channels SET slug = 'station-' || id WHERE slug IS NULL")
        )
        log.info("migrated: channels.slug added")
    if "tts_voice" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE channels ADD COLUMN tts_voice VARCHAR(40) "
                "NOT NULL DEFAULT 'am_onyx'"
            )
        )
    if "sort_order" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE channels ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )
        )

    if await _table_exists(conn, "hls_segments"):
        seg_cols = await _columns(conn, "hls_segments")
        if "channel_id" not in seg_cols:
            # Segment rows are a throwaway index over files with a 30-minute TTL,
            # and `seq` was globally unique — which two stations cannot share.
            # Dropping is cheaper and safer than rewriting the table in place.
            await conn.execute(text("DROP TABLE hls_segments"))
            log.info("migrated: hls_segments rebuilt for per-station sequences")
