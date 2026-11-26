"""Continuing an answer's conversation when a channel member replies to it (isolated per channel)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from sqlalchemy import delete, select

from yggdrasil.agent.deps import Handles
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Invocation, InvocationSession
from yggdrasil.db.session import get_session

MAX_HISTORY_CHARS = 4_000_000
RETENTION = timedelta(days=1)


@dataclass(slots=True)
class Setup:
    handler: str
    instructions: str
    tools: list[str]
    sandbox: bool
    skills: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Session:
    invocation_id: UUID
    setup: Setup
    messages: list[ModelMessage]
    handles: Handles
    nonce: str
    seen_message_id: int | None
    created_at: datetime


async def save(
    invocation_id: UUID,
    setup: Setup,
    messages: Sequence[ModelMessage],
    handles: Handles,
    nonce: str,
    seen_message_id: int | None,
) -> None:
    try:
        payload = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
        size = len(json.dumps(payload, ensure_ascii=False))
        if size > MAX_HISTORY_CHARS:
            LOGGER.info("session of {} not kept: {} chars", invocation_id, size)
            return
        async with get_session() as s:
            expired = InvocationSession.created_at < datetime.now(UTC) - RETENTION
            await s.execute(delete(InvocationSession).where(expired))
            s.add(
                InvocationSession(
                    invocation_id=invocation_id,
                    handler=setup.handler,
                    instructions=setup.instructions,
                    tools=setup.tools,
                    sandbox=setup.sandbox,
                    skills=setup.skills,
                    messages=payload,
                    handles=handles.dump(),
                    nonce=nonce,
                    seen_message_id=seen_message_id,
                )
            )
    except Exception:
        LOGGER.exception("saving the session of {} failed", invocation_id)


async def load(channel_id: str, answer_msg_id: str, max_age: timedelta) -> Session | None:
    async with get_session() as s:
        row = await s.scalar(
            select(InvocationSession)
            .join(Invocation, Invocation.id == InvocationSession.invocation_id)
            .where(
                Invocation.channel_id == channel_id,
                Invocation.reply_msg_id == answer_msg_id,
                Invocation.status == "ok",
                InvocationSession.created_at > datetime.now(UTC) - max_age,
            )
            .order_by(InvocationSession.created_at.desc())
            .limit(1)
        )
    if row is None:
        return None
    try:
        messages = ModelMessagesTypeAdapter.validate_python(row.messages)
    except ValidationError:
        LOGGER.warning("session of {} no longer loads; starting over", row.invocation_id)
        return None
    return Session(
        row.invocation_id,
        Setup(row.handler, row.instructions, list(row.tools), row.sandbox, list(row.skills)),
        messages,
        Handles.load(row.handles),
        row.nonce,
        row.seen_message_id,
        row.created_at,
    )
