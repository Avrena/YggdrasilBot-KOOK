"""KOOK bot system events (guild/channel lifecycle notices)."""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from yggdrasil.admin.common import Page
from yggdrasil.db.models import BotEvent
from yggdrasil.db.session import get_session

router = APIRouter(prefix="/events", tags=["events"])

EventStatus = Literal["pending", "approved", "rejected", "expired", "failed"]


class EventOut(BaseModel):
    id: int
    at: datetime
    kind: str
    guild_id: str | None
    channel_id: str | None
    user_id: str | None
    operator_id: str | None
    data: dict[str, Any]
    status: str | None
    handled_by: str | None
    handled_at: datetime | None


def _out(e: BotEvent) -> EventOut:
    return EventOut(
        id=e.id,
        at=e.at,
        kind=e.kind,
        guild_id=e.guild_id,
        channel_id=e.channel_id,
        user_id=e.user_id,
        operator_id=e.operator_id,
        data=dict(e.data),
        status=e.status,
        handled_by=e.handled_by,
        handled_at=e.handled_at,
    )


@router.get("")
async def list_events(
    kind: str | None = Query(None, description="类型前缀，例如 kook.added_channel 或 kook.self_"),
    status: EventStatus | None = None,
    guild_id: str | None = None,
    channel_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[EventOut]:
    cond = []
    if kind:
        cond.append(BotEvent.kind.startswith(kind))
    if status:
        cond.append(BotEvent.status == status)
    if guild_id:
        cond.append(BotEvent.guild_id == guild_id)
    if channel_id:
        cond.append(BotEvent.channel_id == channel_id)
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(BotEvent).where(*cond)) or 0
        rows = await s.scalars(select(BotEvent).where(*cond).order_by(BotEvent.at.desc()).limit(limit).offset(offset))
        return Page[EventOut](items=[_out(e) for e in rows.all()], total=total)
