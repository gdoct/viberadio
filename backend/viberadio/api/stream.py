import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from ..config import settings
from ..stations import get_channel, manager

router = APIRouter(prefix="/stream", tags=["stream"])

SLUG_RE = re.compile(r"^[a-z0-9-]{1,60}$")


def _safe_slug(slug: str) -> str:
    # These slugs index straight into the filesystem, so reject anything that
    # isn't a plain identifier rather than trusting the router's path matching.
    if not SLUG_RE.match(slug):
        raise HTTPException(status_code=404, detail="Unknown station")
    return slug


@router.get("/{slug}/playlist.m3u8")
async def playlist(slug: str) -> Response:
    _safe_slug(slug)
    channel = await get_channel(slug)
    if channel is None:
        raise HTTPException(status_code=404, detail="Unknown station")
    # Listening keeps the station alive even if the UI stops polling.
    await manager.touch(channel)

    path = settings.hls_dir_for(slug) / "playlist.m3u8"
    if not path.exists():
        raise HTTPException(status_code=503, detail="Station is still spinning up")
    return FileResponse(
        path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{slug}/seg{seq}.ts")
async def segment(slug: str, seq: int) -> Response:
    _safe_slug(slug)
    path = settings.hls_dir_for(slug) / f"seg{seq}.ts"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Segment not found")
    return FileResponse(
        path,
        media_type="video/mp2t",
        headers={"Cache-Control": "public, max-age=86400"},
    )
