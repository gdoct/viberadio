from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import ListenerRequest
from ..schemas import RequestCreate, RequestInfo
from ..stations import manager
from .station import resolve_channel

router = APIRouter(prefix="/api/requests", tags=["requests"])


def _info(r: ListenerRequest) -> RequestInfo:
    return RequestInfo(
        id=r.id,
        message=r.message,
        requester=r.requester,
        status=r.status.value,
        verdict_reason=r.verdict_reason,
        created_at=r.created_at,
    )


@router.post("", status_code=201, response_model=RequestInfo)
async def create_request(
    body: RequestCreate,
    channel: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> RequestInfo:
    row = await resolve_channel(session, body.channel or channel)
    req = ListenerRequest(channel_id=row.id, message=body.message, requester=body.name)
    session.add(req)
    await session.commit()
    await session.refresh(req)

    from ..agents import wake_selector

    # A request is also a reason to spin the station up if it went idle.
    await manager.touch(row)
    wake_selector(row.id)
    return _info(req)


@router.get("", response_model=list[RequestInfo])
async def list_requests(
    channel: str | None = Query(default=None),
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[RequestInfo]:
    row = await resolve_channel(session, channel)
    rows = (
        await session.scalars(
            select(ListenerRequest)
            .where(ListenerRequest.channel_id == row.id)
            .order_by(ListenerRequest.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [_info(r) for r in rows]
