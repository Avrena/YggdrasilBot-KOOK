"""Import recent group history via ``QQ.history`` (throttled)."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, update

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Group, Message
from yggdrasil.db.session import get_session
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.qq import client as qq_client
from yggdrasil.qq import model as qq_model
from yggdrasil.runtime import snapshot

_PAGE = 50
_PAUSE_S = 1.5
_running: dict[int, asyncio.Task] = {}


def request_backfill(group_id: int, *, before: datetime | None = None) -> bool:
    """Start a background backfill; False if one is already running for the group."""
    task = _running.get(group_id)
    if task and not task.done():
        return False
    _running[group_id] = asyncio.create_task(backfill(group_id, before=before), name=f"backfill-{group_id}")
    return True


async def backfill(group_id: int, *, before: datetime | None = None) -> int:
    """Import up to ``memory.backfill_messages`` of history, skipping what is stored.

    With ``before`` it is a catch-up after a reconnect: only messages sent before then (newer ones
    arrive live), stopping at the first page that reaches stored history.
    """
    qq = qq_client.connected()
    if qq is None:
        LOGGER.warning("backfill {}: no bot connected", group_id)
        return 0
    target = snapshot().settings(group_id).memory.backfill_messages
    # Message ids are protocol-native (Milky seq, OneBot hash): history stored through another protocol (or endpoint)
    # carries other ids, so it is also matched by sender + send second. Our own sends are stamped locally: allow ±1s.
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Message.qq_message_id, Message.user_id, Message.sent_at).where(Message.group_id == group_id)
            )
        ).all()
    known_ids = {r.qq_message_id for r in rows}
    known_at = {(r.user_id, int(r.sent_at.timestamp())) for r in rows}

    def stored(m: qq_model.Message) -> bool:
        drift = (-1, 0, 1) if m.sender.user_id == qq.self_id else (0,)
        return m.message_id in known_ids or any((m.sender.user_id, m.time + d) in known_at for d in drift)

    seen: set[int] = set()
    imported = 0
    cursor: int | None = None
    oldest_time: int | None = None
    while len(seen) < target:
        try:
            history = await qq.history(group_id, before=cursor, limit=_PAGE)
        except qq_model.QQError as e:
            LOGGER.warning("backfill {} page failed: {}", group_id, e)
            break
        batch = [m for m in history if m.message_id not in seen]
        if not batch:
            break
        reached_known = False
        for m in batch:
            seen.add(m.message_id)
            if stored(m):
                reached_known = True
                continue
            if not m.sender.user_id:
                continue
            if before is not None and m.time >= before.timestamp():
                continue
            WRITER.submit(
                IncomingMessage(
                    group_id=group_id,
                    user_id=m.sender.user_id,
                    qq_message_id=m.message_id,
                    message_seq=m.seq,
                    sent_at=datetime.fromtimestamp(m.time, UTC),
                    sender_name=m.sender.name,
                    segments=m.segments,
                    is_self=m.sender.user_id == qq.self_id,
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
                update(Group).where(Group.group_id == group_id).values(backfilled_at=datetime.now(UTC))
            )
    LOGGER.info("backfill {}: imported {} messages ({} scanned)", group_id, imported, len(seen))
    return imported
