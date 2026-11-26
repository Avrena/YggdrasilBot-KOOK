"""Rows for database tests (marked ``db``; see tests/conftest.py)."""

import math
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete

from yggdrasil.db.base import EMBED_DIM, Base
from yggdrasil.db.models import EmbedSpace, Message, Provider
from yggdrasil.db.session import get_session
from yggdrasil.kook.segments import text as text_seg
from yggdrasil.runtime import EmbedSpaceInfo


async def insert_message(
    channel_id: str,
    text: str = "",
    *,
    guild_id: str | None = None,
    user_id: str = "20001",
    sender_name: str = "频道成员",
    sent_at: datetime | None = None,
    kook_message_id: str | None = None,
    segments: list[dict[str, Any]] | None = None,
    recalled: bool = False,
) -> int:
    """Store a chat message (by default a text message sent now); returns its row id."""
    async with get_session() as s:
        m = Message(
            sent_at=sent_at or datetime.now(UTC),
            channel_id=channel_id,
            guild_id=guild_id or f"guild-{channel_id}",
            user_id=user_id,
            kook_message_id=kook_message_id or f"msg-{uuid.uuid4().hex[:12]}",
            sender_name=sender_name,
            plain_text=text,
            segments=segments if segments is not None else [text_seg(text)] if text else [],
            recalled=recalled,
            recalled_at=datetime.now(UTC) if recalled else None,
        )
        s.add(m)
        await s.flush()
        return m.id


async def purge_channels(*channel_ids: str) -> None:
    """Delete rows keyed by channel_id or guild_id."""
    async with get_session() as s:
        for table in reversed(Base.metadata.sorted_tables):
            if "channel_id" in table.c:
                await s.execute(delete(table).where(table.c.channel_id.in_(channel_ids)))


async def purge_guilds(*guild_ids: str) -> None:
    """Delete rows keyed by guild_id."""
    async with get_session() as s:
        for table in reversed(Base.metadata.sorted_tables):
            if "guild_id" in table.c:
                await s.execute(delete(table).where(table.c.guild_id.in_(guild_ids)))


def unit_vector(cos: float) -> list[float]:
    """A unit vector at cosine ``cos`` to the first axis."""
    vec = [0.0] * EMBED_DIM
    vec[0], vec[1] = cos, math.sqrt(1 - cos * cos)
    return vec


@asynccontextmanager
async def embed_space() -> AsyncIterator[EmbedSpaceInfo]:
    """An embedding space of its own provider, both removed afterwards."""
    async with get_session() as s:
        provider = Provider(name=f"test-embed-{uuid.uuid4().hex[:8]}", base_url="http://embed.example.com/v1")
        s.add(provider)
        await s.flush()
        es = EmbedSpace(provider_id=provider.id, model="test-embed", dim=EMBED_DIM)
        s.add(es)
        await s.flush()
        space = EmbedSpaceInfo(es.id, provider.id, es.model, es.dim)
    try:
        yield space
    finally:
        async with get_session() as s:
            await s.execute(delete(EmbedSpace).where(EmbedSpace.id == space.id))
            await s.execute(delete(Provider).where(Provider.id == space.provider_id))
