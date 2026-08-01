import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import migrations, models  # noqa: F401 — register tables
from .agents.janitor import Janitor
from .bootstrap import ensure_channels, ensure_fallback_jingle
from .config import settings
from .db import Base, engine, session_scope
from .stations import manager

def _configure_logging() -> None:
    """Log to stdout, and to a rotating file when the log directory is writable.

    Docker's json-file log is deleted along with the container, so an update would
    otherwise take the run history with it. The file handler writes to a mounted
    directory that outlives any single container.
    """
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                settings.log_dir / "viberadio.log",
                maxBytes=settings.log_max_bytes,
                backupCount=settings.log_backup_count,
                encoding="utf-8",
            )
        )
    except OSError as e:  # read-only or missing mount — stdout still works
        print(f"file logging disabled ({e})", flush=True)

    for handler in handlers:
        handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)

    # uvicorn installs its own handlers and stops propagation, which would keep its
    # request lines out of the file. Hand those loggers to the root logger instead:
    # attaching the file handler to each of them would write nested loggers' records
    # once per level on the way up.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_log = logging.getLogger(name)
        uvicorn_log.handlers.clear()
        uvicorn_log.propagate = True


_configure_logging()
log = logging.getLogger("viberadio")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    async with engine.begin() as conn:
        await migrations.apply(conn)
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        channels = await ensure_channels(session)
        await ensure_fallback_jingle(session)
        dial = [c.slug for c in channels]

    # No station agents start here. Stations spin up when a listener asks for one
    # and shut down again when nobody has for `station_idle_timeout_sec`. The
    # janitor is the exception: it collects the audio those stations leave behind,
    # so it has to outlive them.
    manager.start_reaper()
    janitor = Janitor()
    janitor.start()
    log.info("Vibe Radio backend up — dial: %s", ", ".join(dial))
    try:
        yield
    finally:
        await janitor.stop()
        await manager.shutdown()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Vibe Radio", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    from .api import requests, station, stream

    app.include_router(station.router)
    app.include_router(requests.router)
    app.include_router(stream.router)

    # In a container there is no Vite dev server to proxy /api and /stream, so the
    # backend serves the built console itself. Mounted last, at the root, so the
    # API and stream routes above keep their paths.
    if settings.frontend_dist.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=settings.frontend_dist, html=True),
            name="console",
        )
        log.info("Serving frontend from %s", settings.frontend_dist)
    return app


app = create_app()
