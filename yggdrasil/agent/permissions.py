"""Who may make the bot act: decided in code from the KOOK event, never by the model."""

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select

from yggdrasil.db.models import Invocation
from yggdrasil.db.session import get_session
from yggdrasil.kook.model import Incoming

Role = Literal["member", "admin", "owner", "superuser"]
_RECENT = timedelta(days=30)


def speaker_role(incoming: Incoming, superusers: Collection[str]) -> Role:
    if incoming.user_id in superusers:
        return "superuser"
    if incoming.channel_id is not None and incoming.sender.role in ("owner", "admin"):
        return incoming.sender.role
    return "member"


async def bot_message_asker(channel_id: str, reply_msg_id: str) -> str | None:
    """User whose request produced this bot message, if it was a recent answer."""
    async with get_session() as s:
        return await s.scalar(
            select(Invocation.user_id)
            .where(
                Invocation.channel_id == channel_id,
                Invocation.reply_msg_id == reply_msg_id,
                Invocation.created_at > datetime.now(UTC) - _RECENT,
            )
            .order_by(Invocation.created_at.desc())
            .limit(1)
        )


async def may_recall(role: Role, user_id: str, channel_id: str, reply_msg_id: str) -> bool:
    """Guild owner/admins and superusers may recall any bot message; others only answers they asked for."""
    if role != "member":
        return True
    return await bot_message_asker(channel_id, reply_msg_id) == user_id
