"""KOOK system events recorded in ``bot_events`` and automatic guild/channel lifecycle policies."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, update

from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import BOT_EVENTS
from yggdrasil.db.models import BotEvent, Channel
from yggdrasil.db.session import get_session
from yggdrasil.kook.client import Kook
from yggdrasil.kook.model import Observed
from yggdrasil.runtime import publish_change, snapshot

AUTO = "auto"


class EventError(Exception):
    pass


def kept(o: Observed, self_id: str) -> bool:
    """Keep bot/guild lifecycle events always; member events only for known guilds."""
    if o.kind in (
        "kook.self_joined_guild",
        "kook.self_exited_guild",
        "kook.deleted_guild",
        "kook.deleted_channel",
        "kook.added_channel",
        "kook.updated_channel",
    ):
        return True
    if o.user_id == self_id:
        return True
    if o.guild_id and (g := snapshot().guilds.get(o.guild_id)) is not None and g.enabled:
        return True
    return o.channel_id is not None and snapshot().channel_enabled(o.channel_id)


async def handle(kook: Kook, o: Observed) -> None:
    try:
        if not kept(o, kook.self_id):
            return
        async with get_session() as s:
            row = BotEvent(
                kind=o.kind,
                guild_id=o.guild_id,
                channel_id=o.channel_id,
                user_id=o.user_id,
                operator_id=o.operator_id,
                data=dict(o.data),
            )
            s.add(row)
            await s.flush()
            event_id = row.id
        BOT_EVENTS.add(1, {"kind": o.kind})
        LOGGER.info("bot event E{}: {} guild={} channel={}", event_id, o.kind, o.guild_id, o.channel_id)
        await _apply_policy(kook, event_id, o)
    except Exception:
        LOGGER.exception("handling KOOK bot event failed")


async def _apply_policy(kook: Kook, event_id: int, o: Observed) -> None:
    from yggdrasil.moderation.review import notify_admins

    snap = snapshot()
    cfg = snap.settings().events
    if o.kind == "kook.deleted_channel" and o.channel_id:
        if cfg.disable_channel_when_deleted and o.channel_id in snap.channels:
            async with get_session() as s:
                await s.execute(update(Channel).where(Channel.channel_id == o.channel_id).values(enabled=False))
            await publish_change(f"channel {o.channel_id} deleted")
    elif o.kind in ("kook.self_exited_guild", "kook.deleted_guild") and o.guild_id:
        if cfg.disable_guild_when_exited:
            async with get_session() as s:
                await s.execute(update(Channel).where(Channel.guild_id == o.guild_id).values(enabled=False))
            await publish_change(f"guild {o.guild_id} exited")
        if cfg.notify_guild_changes:
            gname = snap.guild_name(o.guild_id) or o.guild_id
            await notify_admins(kook, f"E{event_id} bot 已退出/离开 KOOK 服务器 {gname}（{o.guild_id}）。")
    elif o.kind == "kook.self_joined_guild" and o.guild_id:
        if cfg.notify_guild_changes:
            await notify_admins(
                kook,
                f"E{event_id} bot 加入了新 KOOK 服务器 {o.guild_id}，可在管理后台点击「从 KOOK 同步」配置分组与频道。",
            )


async def sweep() -> dict[str, int]:
    cfg = snapshot().settings().events
    now = datetime.now(UTC)
    async with get_session() as s:
        deleted = await s.execute(delete(BotEvent).where(BotEvent.at < now - timedelta(days=cfg.retention_days)))
    return {"expired": 0, "deleted": deleted.rowcount or 0}  # type: ignore[attr-defined]
