"""Import recent KOOK channel history via ``Kook.history`` (``/message/list`` pagination)."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, update

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Channel, Message
from yggdrasil.db.session import get_session
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.kook import client as kook_client
from yggdrasil.kook import model as kook_model
from yggdrasil.runtime import snapshot

_PAGE = 50
_PAUSE_S = 1.0
_running: dict[str, asyncio.Task] = {}


def request_backfill(channel_id: str, *, before: datetime | None = None) -> bool:
    task = _running.get(channel_id)
    if task and not task.done():
        return False
    _running[channel_id] = asyncio.create_task(backfill(channel_id, before=before), name=f"backfill-{channel_id}")
    return True


async def backfill(channel_id: str, *, before: datetime | None = None) -> int:
    kook = kook_client.connected()
    if kook is None:
        LOGGER.warning("backfill {}: no KOOK bot connected", channel_id)
        return 0
    snap = snapshot()
    guild_id = snap.guild_of(channel_id) or ""
    if not guild_id:
        try:
            guild_id = (await kook.channel(channel_id)).guild_id
        except kook_model.KookError:
            pass
    target = snap.settings(channel_id=channel_id, guild_id=guild_id).memory.backfill_messages
    async with get_session() as session:
        known_ids = set(
            (await session.scalars(select(Message.kook_message_id).where(Message.channel_id == channel_id))).all()
        )

    seen: set[str] = set()
    imported = 0
    cursor: str | None = None
    oldest_time: int | None = None
    while len(seen) < target:
        try:
            history = await kook.history(channel_id, guild_id=guild_id, before=cursor, limit=_PAGE)
        except kook_model.KookError as e:
            LOGGER.warning("backfill {} page failed: {}", channel_id, e)
            break
        batch = [m for m in history if m.message_id not in seen]
        if not batch:
            break
        reached_known = False
        for m in batch:
            seen.add(m.message_id)
            if m.message_id in known_ids:
                reached_known = True
                continue
            if not m.sender.user_id:
                continue
            if before is not None and m.time >= before.timestamp():
                continue
            WRITER.submit(
                IncomingMessage(
                    channel_id=channel_id,
                    guild_id=guild_id,
                    user_id=m.sender.user_id,
                    kook_message_id=m.message_id,
                    sent_at=datetime.fromtimestamp(m.time, UTC),
                    sender_name=m.sender.name,
                    segments=m.segments,
                    is_self=m.sender.user_id == kook.self_id,
                )
            )
            imported += 1
        oldest = min(batch, key=lambda m: m.time)
        if (oldest_time is not None and oldest.time >= oldest_time) or (before is not None and reached_known):
            break
        cursor, oldest_time = oldest.message_id, oldest.time
        await WRITER.flush()
        await asyncio.sleep(_PAUSE_S)
    await WRITER.flush()
    if before is None:
        async with get_session() as session:
            await session.execute(
                update(Channel).where(Channel.channel_id == channel_id).values(backfilled_at=datetime.now(UTC))
            )
    LOGGER.info("backfill {}: imported {} messages ({} scanned)", channel_id, imported, len(seen))
    return imported
