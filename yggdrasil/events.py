"""Requests and notices: what is recorded in ``bot_events`` and what the bot does about it.

Never posts to groups. Policy (settings ``events``, global document):
- group invitations / friend requests from superusers are accepted automatically (invited groups
  are also whitelisted and backfilled); others wait in the table and superusers get a private note;
- join requests for groups the bot administers are only recorded (left to the human admins);
- the bot being kicked disables the group; kicks and mutes of the bot are reported privately;
- ``notice.bot_offline`` marks the account logged out (``ygg.qq.logged_in`` → alert).
Member changes are recorded only for enabled groups.

A row's ``data`` is the protocol's raw event plus ``protocol`` (rows without it came through OneBot): only the backend
that recorded a request can answer it.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, update

from yggdrasil.bot import state as bot_state
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import BOT_EVENTS
from yggdrasil.db.models import BotEvent
from yggdrasil.db.session import get_session
from yggdrasil.people import member_label
from yggdrasil.qq import client as qq_client
from yggdrasil.qq.client import QQ
from yggdrasil.qq.model import Observed, Protocol, QQError
from yggdrasil.runtime import snapshot

AUTO = "auto"


class EventError(Exception):
    """User-facing reason a request can't be decided."""


def kept(o: Observed, self_id: int) -> bool:
    """Whether ``o`` is recorded: requests and what happens to the bot itself always, member changes only in
    enabled groups."""
    about_bot = o.user_id == self_id or o.kind == "notice.group_decrease.kick_me"
    return o.is_request or o.group_id is None or about_bot or snapshot().group_enabled(o.group_id)


async def handle(qq: QQ, o: Observed) -> None:
    """Record ``o`` (if kept) and apply the policy. Errors are logged, never raised."""
    try:
        if not kept(o, qq.self_id):
            return
        async with get_session() as s:
            row = BotEvent(
                kind=o.kind,
                group_id=o.group_id,
                user_id=o.user_id,
                operator_id=o.operator_id,
                data={**o.data, "protocol": o.protocol},
                status="pending" if o.is_request else None,
            )
            s.add(row)
            await s.flush()
            event_id = row.id
        BOT_EVENTS.add(1, {"kind": o.kind})
        LOGGER.info("bot event E{}: {} group={} user={}", event_id, o.kind, o.group_id, o.user_id)
        await _apply_policy(qq, event_id, o)
    except Exception:
        LOGGER.exception("handling bot event failed")


async def _apply_policy(qq: QQ, event_id: int, o: Observed) -> None:
    from yggdrasil.moderation.review import notify_admins

    snap = snapshot()
    cfg = snap.settings(None).events
    from_superuser = o.user_id in snap.superusers
    if o.kind == "request.group.invite":
        if from_superuser and cfg.auto_accept_superuser_invites:
            await decide_request(event_id, True, AUTO)
            if cfg.auto_enable_invited_groups and o.group_id is not None:
                await _enable_group(qq, o.group_id)
        elif cfg.notify_invites:
            await notify_admins(
                qq,
                f"E{event_id} 收到加群邀请：{o.user_id} 邀请 bot 加入群 {o.group_id}。回复这条消息「同意」或「拒绝」。",
            )
    elif o.kind == "request.friend":
        if from_superuser and cfg.auto_accept_superuser_friends:
            await decide_request(event_id, True, AUTO)
        elif cfg.notify_friend_requests:
            note = o.comment or "无"
            await notify_admins(
                qq, f"E{event_id} 收到好友申请：{o.user_id}（验证信息：{note}）。回复这条消息「同意」或「拒绝」。"
            )
    elif o.kind == "request.group.add":
        if cfg.notify_join_requests:
            await notify_admins(
                qq, f"E{event_id} {o.user_id} 申请加入群 {o.group_id}（bot 是管理员）。这里只记录，不代为处理。"
            )
    elif o.kind == "notice.group_decrease.kick_me":
        if cfg.disable_group_when_kicked and o.group_id is not None and snap.group_enabled(o.group_id):
            await _disable_group(o.group_id)
        if cfg.notify_kicked:
            await notify_admins(
                qq,
                f"E{event_id} bot 被 {await member_label(o.group_id, o.operator_id)} 踢出了群 {o.group_id}"
                + ("，已停用该群。" if cfg.disable_group_when_kicked else "。"),
            )
    elif o.kind == "notice.group_ban.self.ban":
        if cfg.notify_banned:
            minutes = (o.duration or 0) // 60
            who = await member_label(o.group_id, o.operator_id)
            await notify_admins(qq, f"E{event_id} bot 在群 {o.group_id} 被 {who} 禁言 {minutes} 分钟。")
    elif o.kind == "notice.bot_offline":
        bot_state.offline.add(str(qq.self_id))


async def _enable_group(qq: QQ, group_id: int) -> None:
    from yggdrasil.admin import groups as groups_api

    name = ""
    await asyncio.sleep(3)  # the join takes a moment before group info is available
    try:
        name = (await qq.group(group_id)).name
    except QQError:
        LOGGER.warning("group info for invited group {} unavailable", group_id)
    await groups_api.upsert_group(group_id, groups_api.GroupIn(name=name, enabled=True, settings=None), AUTO)
    try:
        await groups_api.backfill_group(group_id, AUTO)
    except Exception:
        LOGGER.exception("backfill of invited group {} failed to start", group_id)


async def _disable_group(group_id: int) -> None:
    from yggdrasil.admin import groups as groups_api

    group = snapshot().groups.get(group_id)
    body = groups_api.GroupIn(name=group.name if group else "", enabled=False, settings=None)
    await groups_api.upsert_group(group_id, body, AUTO)


_PROTOCOL_NAMES: dict[Protocol, str] = {"milky": "Milky", "onebot": "OneBot"}


async def decide_request(event_id: int, approve: bool, actor: str, reason: str | None = None) -> BotEvent:
    """Answer a pending request through the protocol that recorded it. Raises EventError with a user-facing
    message."""
    async with get_session() as s:
        row = await s.get(BotEvent, event_id, with_for_update=True)
        if row is None or not row.kind.startswith("request."):
            raise EventError(f"E{event_id} 不是申请")
        if row.status != "pending":
            raise EventError(f"E{event_id} 已经是 {row.status}")
        qq = qq_client.connected()
        if qq is None:
            raise EventError("bot 未连接，无法处理")
        data = dict(row.data)
        protocol: Protocol = "milky" if data.pop("protocol", "onebot") == "milky" else "onebot"
        if protocol != qq.protocol:
            raise EventError(
                f"E{event_id} 是通过 {_PROTOCOL_NAMES[protocol]} 收到的，现在连接的是 {_PROTOCOL_NAMES[qq.protocol]}，"
                "处理不了；请在 QQ 里直接处理"
            )
        observed = Observed(
            kind=row.kind,
            protocol=protocol,
            group_id=row.group_id,
            user_id=row.user_id,
            operator_id=row.operator_id,
            comment=str(data.get("comment") or ""),
            data=data,
        )
        try:
            await qq.decide_request(observed, approve=approve, reason=reason)
        except QQError as e:
            row.status, row.handled_by, row.handled_at = "failed", actor, datetime.now(UTC)
            await s.commit()
            raise EventError(f"QQ 拒绝了这次处理（申请可能已失效）：{e}") from e
        row.status = "approved" if approve else "rejected"
        row.handled_by, row.handled_at = actor, datetime.now(UTC)
        await s.flush()
        s.expunge(row)
    return row


async def sweep() -> dict[str, int]:
    """Expire stale requests and drop records past retention (daily worker job)."""
    cfg = snapshot().settings(None).events
    now = datetime.now(UTC)
    async with get_session() as s:
        expired = await s.execute(
            update(BotEvent)
            .where(BotEvent.status == "pending", BotEvent.at < now - timedelta(days=cfg.request_expire_days))
            .values(status="expired", handled_at=now)
        )
        deleted = await s.execute(delete(BotEvent).where(BotEvent.at < now - timedelta(days=cfg.retention_days)))
    return {"expired": expired.rowcount or 0, "deleted": deleted.rowcount or 0}  # type: ignore[attr-defined]
