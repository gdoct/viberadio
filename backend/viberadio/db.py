from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    pass


settings.data_dir.mkdir(parents=True, exist_ok=True)
engine = create_async_engine(settings.database_url)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


SessionMaker = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One transaction for the whole block. Only for short, write-only work.

    SQLite allows a single writer, so agents that interleave slow work (LLM calls,
    downloads, ffmpeg) must use `agent_session` and commit in short bursts instead.
    """
    async with SessionMaker() as session:
        async with session.begin():
            yield session


@asynccontextmanager
async def agent_session() -> AsyncIterator[AsyncSession]:
    """Session for agent loops: the caller commits after each logical step so no write
    transaction is ever held across an await on slow I/O."""
    async with SessionMaker() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionMaker() as session:
        yield session
