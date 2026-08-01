import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import migrations, models  # noqa: F401 — register tables
from .bootstrap import ensure_channels, ensure_fallback_jingle
from .config import settings
from .db import Base, engine, session_scope
from .stations import manager

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
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

    # No agents start here. Stations spin up when a listener asks for one and
    # shut down again when nobody has for `station_idle_timeout_sec`.
    manager.start_reaper()
    log.info("Vibe Radio backend up — dial: %s", ", ".join(dial))
    try:
        yield
    finally:
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
