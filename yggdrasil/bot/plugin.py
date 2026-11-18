"""NoneBot plugin: ingest every whitelisted group message, route triggers, run handlers.

Every QQ event goes through ``route`` (an ``event_preprocessor``) once: the protocol's client (``qq.client``) turns
it into a recall, a request/notice to record (``events``) or an ``Incoming`` message. Messages are routed against the
DB-driven trigger index; the resolved command is rewritten into an internal ``ygg:<handler>`` header so Alconna only
parses arguments (see ``TriggerExt``), and the handlers take the ``Incoming`` from the state.
"""

import asyncio
from datetime import UTC, datetime

from arclet.alconna import AllParam
from nonebot import get_driver
from nonebot.adapters import Bot, Event
from nonebot.message import event_preprocessor
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot.typing import T_State
from nonebot_plugin_alconna import Alconna, Args, Arparma, Option, Subcommand, UniMessage, on_alconna
from nonebot_plugin_alconna.extension import Extension

from yggdrasil.backfill import request_backfill
from yggdrasil.bot import state as bot_state
from yggdrasil.bot.inflight import spawn as _spawn
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import TRIGGERS_MATCHED
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.pipeline import ratelimit, recalls
from yggdrasil.qq.client import QQ, client
from yggdrasil.qq.model import Incoming
from yggdrasil.runtime import snapshot
from yggdrasil.triggers.bots import sent_by_bot
from yggdrasil.triggers.index import MatchInput, Resolved, current

__plugin_meta__ = PluginMetadata(
    name="yggdrasil",
    description="Group-chat TL;DR, replies and background memory",
    usage="see dashboard",
    type="application",
    supported_adapters={"~milky", "~onebot.v11"},
)

STATE_KEY = "ygg.cmd"
INCOMING_KEY = "ygg.incoming"


def _qq(bot: Bot) -> QQ | None:
    try:
        return client(bot)
    except TypeError:
        return None


def _mentions_me(incoming: Incoming) -> bool:
    """@bot anywhere in the message: ``to_me`` covers an @ at the start or end, one in the middle stays a
    ``mention`` segment."""
    return incoming.to_me or any(
        seg["type"] == "mention" and seg["data"].get("user_id") == incoming.self_id for seg in incoming.segments
    )


# --- routing -----------------------------------------------------------------


@event_preprocessor
async def route(bot: Bot, event: Event, state: T_State) -> None:
    qq = _qq(bot)
    if qq is None:
        return
    # Milky has no heartbeat events: a live connection that delivers events is alive.
    if qq.protocol == "milky" or event.get_event_name() == "meta_event.heartbeat":
        bot_state.heartbeat(bot.self_id)
    if (recalled := qq.recalled(event)) is not None:
        _recall(*recalled)
        return
    if (observed := qq.observe(event)) is not None:
        from yggdrasil import events

        # Requests and notices: recorded in bot_events, policy in yggdrasil.events (never posts to groups).
        _spawn(events.handle(qq, observed))
        return
    incoming = await qq.incoming(event)
    if incoming is not None:
        await dispatch(qq, incoming, state)


async def dispatch(qq: QQ, incoming: Incoming, state: T_State) -> None:
    """Store a group message, and route it (or a private message) to the trigger it starts, if any."""
    snap = snapshot()
    group_id = incoming.group_id
    if incoming.user_id == incoming.self_id:
        # The bot records what it sends itself (``delivery.send.record_self``, with the forward's nodes and the
        # pictures' blobs); Yogurt also reports those messages back (``reportSelfMessage``), which stored them twice.
        return
    if group_id is not None:
        if not snap.group_enabled(group_id):
            return
        WRITER.submit(
            IncomingMessage(
                group_id=group_id,
                user_id=incoming.user_id,
                qq_message_id=incoming.message_id,
                message_seq=incoming.seq,
                sent_at=datetime.fromtimestamp(incoming.time, UTC),
                sender_name=incoming.sender.name.strip(),
                segments=incoming.segments,
            )
        )
        bot_state.last_group_message[group_id] = incoming.message_id
        scope, role = "group", incoming.sender.role
    else:
        scope, role = "private", "member"
    if incoming.user_id in snap.superusers:
        role = "superuser"
    reply = incoming.reply
    resolved = current().match(
        MatchInput(
            scope=scope,
            group_id=group_id,
            text=incoming.text,
            to_me=_mentions_me(incoming),
            replied_to_bot=reply is not None and reply.sender.user_id == incoming.self_id,
            role=role,
        )
    )
    if resolved is None:
        return
    if group_id is not None and role != "superuser" and await sent_by_bot(qq, incoming, snap.settings(group_id)):
        LOGGER.info("ignored {} trigger from bot {} in group {}", resolved.handler, incoming.user_id, group_id)
        return
    if not await ratelimit.cooldown(resolved.rule_id, incoming.user_id, resolved.cooldown_s):
        return
    TRIGGERS_MATCHED.add(1, {"handler": resolved.handler, "scope": scope})
    state[STATE_KEY] = resolved
    state[INCOMING_KEY] = incoming


def _recall(group_id: int, message_id: int) -> None:
    recalls.recall(group_id, message_id)  # a run it triggered stops before answering
    if snapshot().group_enabled(group_id):
        _spawn(WRITER.mark_recalled(group_id, message_id))


class TriggerExt(Extension):
    """Feed Alconna the routed command (``ygg:<handler> <args>``) instead of the raw message."""

    @property
    def priority(self) -> int:
        return 10

    @property
    def id(self) -> str:
        return "yggdrasil.trigger"

    async def message_provider(self, event, state, bot, use_origin: bool = False) -> UniMessage | None:
        resolved: Resolved | None = state.get(STATE_KEY)
        if resolved is None:
            return None
        return UniMessage(f"ygg:{resolved.handler} {resolved.args}".strip())


def _routed(handler: str) -> Rule:
    async def check(state: T_State) -> bool:
        resolved = state.get(STATE_KEY)
        return resolved is not None and resolved.handler == handler

    return Rule(check)


def _matcher(alc: Alconna, handler: str):
    return on_alconna(
        alc,
        rule=_routed(handler),
        extensions=[TriggerExt],
        skip_for_unmatch=True,
        auto_send_output=False,
        use_cmd_start=False,
        priority=10,
        block=True,
    )


def _rest(result: Arparma) -> str:
    rest = result.all_matched_args.get("rest")
    if rest is None:
        return ""
    if isinstance(rest, list | tuple):
        return " ".join(str(x) for x in rest)
    return str(rest)


def _routed_message(bot: Bot, state: T_State) -> tuple[QQ, Incoming] | None:
    """The client and message ``route`` resolved the command for."""
    qq, incoming = _qq(bot), state.get(INCOMING_KEY)
    if qq is None or not isinstance(incoming, Incoming):
        return None
    return qq, incoming


# --- handlers ----------------------------------------------------------------

tldr_cmd = _matcher(Alconna("ygg:tldr", Args["rest?", AllParam]), "tldr")
chat_cmd = _matcher(Alconna("ygg:chat", Args["rest?", AllParam]), "chat")
optout_cmd = _matcher(Alconna("ygg:privacy.optout"), "privacy.optout")
optin_cmd = _matcher(Alconna("ygg:privacy.optin"), "privacy.optin")
forget_cmd = _matcher(Alconna("ygg:privacy.forget"), "privacy.forget")
review_cmd = _matcher(
    Alconna(
        "ygg:admin.review",
        # ids optional: 「拒绝」 alone (e.g. replying to a friend-request note) goes to the ops agent
        Subcommand("approve", Args["id?", str]),
        Subcommand("reject", Args["id?", str]),
        Subcommand("edit", Args["id?", str], Args["text?", AllParam]),
    ),
    "admin.review",
)
status_cmd = _matcher(Alconna("ygg:admin.status", Option("-v")), "admin.status")
ops_cmd = _matcher(Alconna("ygg:admin.ops", Args["rest?", AllParam]), "admin.ops")


@tldr_cmd.handle()
async def _tldr(bot: Bot, state: T_State, result: Arparma) -> None:
    from yggdrasil.pipeline.invoke import run

    if routed := _routed_message(bot, state):
        _spawn(run(*routed, state[STATE_KEY], "tldr", _rest(result)))


@chat_cmd.handle()
async def _chat(bot: Bot, state: T_State, result: Arparma) -> None:
    from yggdrasil.pipeline.invoke import run

    if routed := _routed_message(bot, state):
        _spawn(run(*routed, state[STATE_KEY], "chat", _rest(result)))


def _group_message(bot: Bot, state: T_State) -> tuple[QQ, Incoming, int] | None:
    """``_routed_message`` with its group, for group-only commands."""
    routed = _routed_message(bot, state)
    if routed is None or (group_id := routed[1].group_id) is None:
        return None
    return routed[0], routed[1], group_id


async def _privacy_reply(qq: QQ, incoming: Incoming, group_id: int, text: str) -> None:
    from yggdrasil.delivery.send import send_text

    ident = await bot_state.identity(qq)
    await send_text(
        qq,
        group_id=group_id,
        user_id=incoming.user_id,
        reply_to=incoming.message_id
        if bot_state.should_quote(group_id, incoming.message_id, snapshot().settings(group_id).interaction.quote_reply)
        else None,
        text=text,
        self_id=ident.self_id,
        nickname=ident.nickname,
    )


@optout_cmd.handle()
async def _optout(bot: Bot, state: T_State) -> None:
    from yggdrasil.memory.privacy import set_opt_out

    if (routed := _group_message(bot, state)) is None:
        return
    qq, incoming, group_id = routed
    await set_opt_out(group_id, incoming.user_id, True)
    await _privacy_reply(
        qq, incoming, group_id, "好，之后不再整理你的画像（聊天记录仍按群规则保存）。想恢复就说「记得我」。"
    )


@optin_cmd.handle()
async def _optin(bot: Bot, state: T_State) -> None:
    from yggdrasil.memory.privacy import set_opt_out

    if (routed := _group_message(bot, state)) is None:
        return
    qq, incoming, group_id = routed
    await set_opt_out(group_id, incoming.user_id, False)
    await _privacy_reply(qq, incoming, group_id, "好，重新开始记住你。")


@forget_cmd.handle()
async def _forget(bot: Bot, state: T_State) -> None:
    from yggdrasil.memory.privacy import forget_user, set_opt_out

    if (routed := _group_message(bot, state)) is None:
        return
    qq, incoming, group_id = routed
    await forget_user(group_id, incoming.user_id)
    await set_opt_out(group_id, incoming.user_id, True)
    await _privacy_reply(qq, incoming, group_id, "已删除你的画像，并且之后不再整理。")


@review_cmd.handle()
async def _review(bot: Bot, state: T_State, result: Arparma) -> None:
    from yggdrasil.moderation.review import ReviewError, decide, parse_review_id

    routed = _routed_message(bot, state)
    if routed is None or routed[1].group_id is not None:
        return
    qq, incoming = routed
    action = next((s for s in ("approve", "reject", "edit") if result.find(s)), None)
    raw_id = result.query[str](f"{action}.id") or "" if action else ""
    try:
        review_id = parse_review_id(raw_id)
    except ReviewError:
        review_id = None
    if action is None or review_id is None:
        # No R<id>: not a plain review command (e.g. 「同意」/「拒绝」 to a request note); let ops read it.
        from yggdrasil.ops.agent import run_ops

        _spawn(run_ops(qq, incoming, incoming.text))
        return
    text = result.query[list](f"{action}.text")
    if isinstance(text, list | tuple):
        text = " ".join(str(x) for x in text)
    try:
        item = await decide(review_id, action, f"qq:{incoming.user_id}", str(text) if text else None)
    except ReviewError as e:
        await review_cmd.finish(str(e))
    await review_cmd.finish(f"R{item.id} → {item.status}")


@status_cmd.handle()
async def _status() -> None:
    from yggdrasil.moderation.review import pending_count

    snap = snapshot()
    enabled = [g for g in snap.groups.values() if g.enabled]
    await status_cmd.finish(
        f"config v{snap.version}\n群：{', '.join(f'{g.name}({g.group_id})' for g in enabled) or '无'}\n"
        f"路由：{', '.join(sorted(snap.routes)) or '无'}\n待审核：{pending_count}"
    )


@ops_cmd.handle()
async def _ops(bot: Bot, state: T_State, result: Arparma) -> None:
    from yggdrasil.ops.agent import run_ops

    routed = _routed_message(bot, state)
    # The trigger's allowed_roles is editable; this is not.
    if routed is not None and routed[1].user_id in snapshot().superusers:
        _spawn(run_ops(*routed, _rest(result)))


# --- lifecycle ---------------------------------------------------------------

driver = get_driver()


@driver.on_bot_connect
async def _connected(bot: Bot) -> None:
    qq = _qq(bot)
    if qq is None:
        return
    bot_state.mark_connected(bot.self_id)
    ident = await bot_state.identity(qq)
    LOGGER.info("bot connected via {}: {} ({})", qq.protocol, ident.nickname, ident.self_id)
    _spawn(_catch_up(datetime.now(UTC)))


async def _catch_up(connected_at: datetime) -> None:
    """Store what was said while no pod held the connection (rolling update, protocol restart).

    Triggers sent in that gap are not answered; only the history is filled in.
    """
    await asyncio.sleep(10)  # let the old pod's last ingest flush land, so nothing is stored twice
    for gid, group in snapshot().groups.items():
        if group.enabled:
            request_backfill(gid, before=connected_at)


@driver.on_bot_disconnect
async def _disconnected(bot: Bot) -> None:
    if _qq(bot) is None:
        return
    bot_state.mark_disconnected(bot.self_id)
    LOGGER.warning("bot disconnected: {}", bot.self_id)
