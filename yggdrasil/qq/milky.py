"""``QQ`` over Milky (Yogurt): ``MilkyQQ``, the segment translation and the adapter it runs on.

Every action goes through ``bot.call_api(action, _raw=True, ...)`` (``Adapter._call_api``): plain JSON in and out,
so segments stay dicts and ``qq.hooks`` sees each action under its Milky name. Events are the adapter's own
(``nonebot.adapters.milky.event``); its message events arrive with the quoted message already fetched
(``event.reply``) and a leading or trailing @bot stripped from ``event.message``.
"""

import json
import re
from typing import Any, override

import httpx
from nonebot.adapters import Event
from nonebot.adapters.milky import Adapter as MilkyAdapter
from nonebot.adapters.milky import Bot
from nonebot.adapters.milky.event import (
    BotOfflineEvent,
    FriendRequestEvent,
    GroupAdminChangeEvent,
    GroupInvitationEvent,
    GroupInvitedJoinRequestEvent,
    GroupJoinRequestEvent,
    GroupMemberDecreaseEvent,
    GroupMemberIncreaseEvent,
    GroupMuteEvent,
    GroupWholeMuteEvent,
    MessageEvent,
    MessageRecallEvent,
)
from nonebot.adapters.milky.event import Event as MilkyEvent
from nonebot.adapters.milky.exception import ActionFailed, MilkyAdapterException, NetworkError
from nonebot.adapters.milky.utils import handle_api_result, raise_api_response
from nonebot.drivers import Request

from yggdrasil.qq.client import QQ
from yggdrasil.qq.model import (
    Dropped,
    ForwardNode,
    Group,
    Incoming,
    Login,
    Member,
    Message,
    Node,
    Observed,
    QQError,
    Role,
    Sender,
    Sent,
    Unconfirmed,
)
from yggdrasil.qq.ntfile import file_id_of, sha1_of
from yggdrasil.qq.segments import Segment

HISTORY_PAGE = 30  # get_history_messages answers at most 30 messages
_ROLES: tuple[Role, ...] = ("owner", "admin", "member")
# Yogurt answers an action whose QQ packet got no reply in time (10 s) with retcode -500 "Internal error: Timed out
# waiting for 10000 ms": for a send, QQ may have posted the message anyway.
_TIMED_OUT = re.compile(r"timed out", re.IGNORECASE)


class NoAnswer(NetworkError):
    """The Milky endpoint gave no answer in time, or none that decodes: the action may have taken effect."""


class Adapter(MilkyAdapter):
    """The Milky adapter plus raw actions, which ``MilkyQQ`` uses for everything: ``bot.call_api(action, _raw=True,
    _timeout=<seconds or None>, **params)`` posts ``params`` as they are and returns the answer's ``data``. The stock
    API methods take and return the adapter's message and model classes, and its ``call_http`` never honours
    ``_timeout`` (``clean_params`` drops it before it is read). ``__main__`` registers this class."""

    @override
    async def _call_api(self, bot: Bot, api: str, **data: Any) -> Any:
        if not data.pop("_raw", False):
            return await super()._call_api(bot, api, **data)
        timeout = data.pop("_timeout", None) or self.config.api_timeout
        headers = {"Content-Type": "application/json"}
        if bot.info.access_token:
            headers["Authorization"] = f"Bearer {bot.info.access_token}"
        request = Request("POST", bot.info.get_url(api), content=json.dumps(data), headers=headers, timeout=timeout)
        try:
            response = await self.request(request)
        except (TimeoutError, httpx.TimeoutException) as e:
            raise NoAnswer(f"no answer to {api} within {timeout}s") from e
        except Exception as e:
            raise NetworkError(f"HTTP request failed: {e!r}") from e
        body = response.content
        text = body.decode(errors="replace") if isinstance(body, bytes) else (body or "")
        raise_api_response(response.status_code, text)
        if not text:
            raise NoAnswer(f"empty answer to {api}")
        try:
            answer = json.loads(text)
        except ValueError as e:
            raise NoAnswer(f"undecodable answer to {api}: {text[:200]}") from e
        return handle_api_result(answer)


# --- segments -----------------------------------------------------------------------------------------------------


def _incoming(seg: dict[str, Any]) -> Segment:
    kind = str(seg.get("type") or "")
    d: dict[str, Any] = seg.get("data") or {}
    match kind:
        case "text":
            return {"type": "text", "data": {"text": str(d.get("text") or "")}}
        case "mention":
            name = str(d.get("name") or "").removeprefix("@")
            return {"type": "mention", "data": {"user_id": int(d.get("user_id") or 0), "name": name}}
        case "mention_all":
            return {"type": "mention_all", "data": {}}
        case "face":
            return {
                "type": "face",
                "data": {"face_id": str(d.get("face_id") or ""), "is_large": bool(d.get("is_large"))},
            }
        case "reply":
            return {"type": "reply", "data": {"message_id": int(d.get("message_seq") or 0)}}
        case "image":
            ref = str(d.get("resource_id") or "")
            return {
                "type": "image",
                "data": {
                    "ref": ref,
                    "url": str(d.get("temp_url") or ""),
                    "summary": str(d.get("summary") or ""),
                    "sticker": d.get("sub_type") == "sticker",
                    "sha1": sha1_of(ref),
                },
            }
        case "record" | "video":
            data = {"ref": str(d.get("resource_id") or ""), "url": str(d.get("temp_url") or "")}
            return {"type": kind, "data": {**data, "duration": int(d.get("duration") or 0)}}
        case "file":
            data = {
                "file_id": str(d.get("file_id") or ""),
                "file_name": str(d.get("file_name") or ""),
                "file_size": int(d.get("file_size") or 0),
            }
            if d.get("file_hash"):
                data["file_hash"] = str(d["file_hash"])
            return {"type": "file", "data": data}
        case "forward":
            return {
                "type": "forward",
                "data": {
                    "forward_id": str(d.get("forward_id") or ""),
                    "title": str(d.get("title") or ""),
                    "preview": [str(p) for p in d.get("preview") or []],
                    "summary": str(d.get("summary") or ""),
                },
            }
        case "market_face":
            return {
                "type": "market_face",
                "data": {
                    "emoji_package_id": int(d.get("emoji_package_id") or 0),
                    "emoji_id": str(d.get("emoji_id") or ""),
                    "key": str(d.get("key") or ""),
                    "summary": str(d.get("summary") or ""),
                    "url": str(d.get("url") or ""),
                },
            }
        case "light_app":
            data = {"app_name": str(d.get("app_name") or ""), "json_payload": str(d.get("json_payload") or "")}
            return {"type": "light_app", "data": data}
        case "xml":
            data = {"service_id": int(d.get("service_id") or 0), "xml_payload": str(d.get("xml_payload") or "")}
            return {"type": "xml", "data": data}
        case "markdown":
            return {"type": "markdown", "data": {"content": str(d.get("content") or "")}}
        case _:
            return {"type": "text", "data": {"text": f"[{kind}]"}}


def incoming_segments(elements: list[dict[str, Any]]) -> list[Segment]:
    """Milky incoming segments in our schema (``qq.segments``); unknown types become ``[<type>]`` text."""
    return [_incoming(s) for s in elements]


def _outgoing(seg: Segment) -> dict[str, Any]:
    d: dict[str, Any] = seg["data"]
    match seg["type"]:
        case "text":
            return {"type": "text", "data": {"text": d["text"]}}
        case "mention":
            return {"type": "mention", "data": {"user_id": int(d["user_id"])}}
        case "mention_all":
            return {"type": "mention_all", "data": {}}
        case "face":
            return {"type": "face", "data": {"face_id": str(d["face_id"]), "is_large": bool(d.get("is_large"))}}
        case "reply":
            return {"type": "reply", "data": {"message_seq": int(d["message_id"])}}
        case "image":
            return {"type": "image", "data": {"uri": d["uri"], "sub_type": "normal"}}
        case other:
            raise ValueError(f"cannot send a {other} segment")


def outgoing_segments(segments: list[Segment]) -> list[dict[str, Any]]:
    """Outgoing schema segments (text, mention, reply, image ``uri``) as Milky outgoing segments."""
    return [_outgoing(s) for s in segments]


def forward_segment(nodes: list[Node]) -> dict[str, Any]:
    """A merged forward as one outgoing Milky segment. No title/preview/summary: QQ derives them from the nodes."""
    messages = [{"user_id": n.user_id, "sender_name": n.name, "segments": outgoing_segments(n.segments)} for n in nodes]
    return {"type": "forward", "data": {"messages": messages}}


def message_of(raw: dict[str, Any]) -> Message:
    """A Milky ``IncomingMessage`` (history, ``get_message``, an event's data) as ``Message``."""
    group = raw.get("message_scene") == "group"
    member: dict[str, Any] = raw.get("group_member") or {}
    friend: dict[str, Any] = raw.get("friend") or {}
    name = member.get("card") or member.get("nickname") or friend.get("nickname") or ""
    role = member.get("role")
    seq = int(raw["message_seq"])
    return Message(
        message_id=seq,
        group_id=int(raw["peer_id"]) if group else None,
        sender=Sender(int(raw["sender_id"]), str(name), role if role in _ROLES else "member"),
        time=int(raw.get("time") or 0),
        segments=incoming_segments(raw.get("segments") or []),
        seq=seq if group else None,
    )


def _mentions(segments: list[Segment], user_id: int) -> bool:
    return any(s["type"] == "mention" and s["data"]["user_id"] == user_id for s in segments)


# --- events -------------------------------------------------------------------------------------------------------


def _observed(event: MilkyEvent, self_id: int) -> Observed | None:
    def seen(kind: str, group_id: int | None, user_id: int | None, operator_id: int | None, **more: Any) -> Observed:
        raw = {"event_type": event.event_type, **event.model_dump(mode="json")}
        return Observed(kind, "milky", group_id, user_id, operator_id, data=raw, **more)

    match event:
        case FriendRequestEvent(data=d):
            return seen("request.friend", None, d.initiator_id, None, comment=d.comment)
        case GroupInvitationEvent(data=d):
            return seen("request.group.invite", d.group_id, d.initiator_id, None)
        case GroupJoinRequestEvent(data=d):
            return seen("request.group.add", d.group_id, d.initiator_id, None, comment=d.comment)
        case GroupInvitedJoinRequestEvent(data=d):  # a member invited someone: the admins decide
            return seen("request.group.add", d.group_id, d.target_user_id, d.initiator_id)
        case GroupMemberIncreaseEvent(data=d):
            operator = d.operator_id if d.operator_id is not None else d.invitor_id
            if d.user_id == self_id:
                return seen("notice.group_increase.self", d.group_id, self_id, operator)
            how = "invite" if d.invitor_id is not None else "approve"
            return seen(f"notice.group_increase.{how}", d.group_id, d.user_id, operator)
        case GroupMemberDecreaseEvent(data=d):
            kicked = d.operator_id is not None and d.operator_id != d.user_id
            if d.user_id == self_id and kicked:
                return seen("notice.group_decrease.kick_me", d.group_id, self_id, d.operator_id)
            return seen(f"notice.group_decrease.{'kick' if kicked else 'leave'}", d.group_id, d.user_id, d.operator_id)
        case GroupMuteEvent(data=d):
            who = "self" if d.user_id == self_id else "member"
            what = "ban" if d.duration else "lift_ban"
            return seen(f"notice.group_ban.{who}.{what}", d.group_id, d.user_id, d.operator_id, duration=d.duration)
        case GroupWholeMuteEvent(data=d):
            what = "ban" if d.is_mute else "lift_ban"
            return seen(f"notice.group_ban.all.{what}", d.group_id, 0, d.operator_id)
        case GroupAdminChangeEvent(data=d):
            who = "self" if d.user_id == self_id else "member"
            return seen(
                f"notice.group_admin.{who}.{'set' if d.is_set else 'unset'}", d.group_id, d.user_id, d.operator_id
            )
        case BotOfflineEvent(data=d):
            return seen("notice.bot_offline", None, self_id, None, comment=d.reason)
        case _:
            return None


class MilkyQQ(QQ):
    protocol = "milky"

    async def _call(
        self, action: str, params: dict[str, Any], *, reply_timeout: float | None = None, send: bool = False
    ) -> Any:
        """``action``'s answer data. ``send``: no clear answer raises ``Unconfirmed`` instead of ``QQError``."""
        try:
            return await self.bot.call_api(action, _raw=True, _timeout=reply_timeout, **params)
        except NoAnswer as e:
            raise (Unconfirmed if send else QQError)(e.msg or action) from e
        except ActionFailed as e:
            reason = str(e.info.get("message") or f"{action} failed (retcode {e.info.get('retcode')})")
            raise (Unconfirmed if send and _TIMED_OUT.search(reason) else QQError)(reason) from e
        except MilkyAdapterException as e:
            raise QQError(getattr(e, "msg", None) or repr(e)) from e

    async def _dict(self, action: str, params: dict[str, Any], **kw: Any) -> dict[str, Any]:
        data = await self._call(action, params, **kw)
        if not isinstance(data, dict):
            raise QQError(f"{action}: unexpected answer {data!r}")
        return data

    async def _data(self, action: str, params: dict[str, Any], key: str, **kw: Any) -> Any:
        data = await self._dict(action, params, **kw)
        if key not in data:
            raise QQError(f"{action}: no {key} in the answer")
        return data[key]

    # --- events ---------------------------------------------------------------------------------------------------
    @override
    async def incoming(self, event: Event) -> Incoming | None:
        if not isinstance(event, MessageEvent):
            return None
        m = message_of(event.data.model_dump(mode="json"))
        # The message as sent. Not ``event.data.segments``: the adapter's @bot and reply stripping edits the text
        # dicts it shares with them in place; ``original_message`` is a copy taken before that.
        segments = incoming_segments([{"type": s.type, "data": s.data} for s in event.original_message])
        return Incoming(
            message_id=m.message_id,
            group_id=m.group_id,
            sender=m.sender,
            time=m.time,
            segments=segments,
            seq=m.seq,
            self_id=event.self_id,
            text=event.get_plaintext(),
            to_me=event.to_me or _mentions(segments, event.self_id),
            reply=message_of(event.reply.model_dump(mode="json")) if event.reply is not None else None,
            protocol="milky",
        )

    @override
    def recalled(self, event: Event) -> tuple[int, int] | None:
        if isinstance(event, MessageRecallEvent) and event.data.message_scene == "group":
            return event.data.peer_id, event.data.message_seq
        return None

    @override
    def observe(self, event: Event) -> Observed | None:
        return _observed(event, self.self_id) if isinstance(event, MilkyEvent) else None

    # --- identity / info ------------------------------------------------------------------------------------------
    @override
    async def login(self) -> Login:
        data = await self._dict("get_login_info", {})
        return Login(int(data.get("uin") or 0), str(data.get("nickname") or ""))

    @override
    async def group(self, group_id: int) -> Group:
        g = await self._data("get_group_info", {"group_id": group_id}, "group")
        return Group(int(g["group_id"]), str(g.get("group_name") or ""), g.get("member_count"))

    @override
    async def member(self, group_id: int, user_id: int, *, reply_timeout: float | None = None) -> Member:
        params = {"group_id": group_id, "user_id": user_id}
        m = await self._data("get_group_member_info", params, "member", reply_timeout=reply_timeout)
        role = m.get("role")
        return Member(
            group_id=group_id,
            user_id=user_id,
            nickname=str(m.get("nickname") or ""),
            card=str(m.get("card") or ""),
            role=role if role in _ROLES else "member",
        )

    # --- messages -------------------------------------------------------------------------------------------------
    async def _send(
        self, group_id: int | None, user_id: int, message: list[dict[str, Any]], reply_timeout: float | None
    ) -> int:
        if group_id is None:
            action, params = "send_private_message", {"user_id": user_id, "message": message}
        else:
            action, params = "send_group_message", {"group_id": group_id, "message": message}
        data = await self._call(action, params, reply_timeout=reply_timeout, send=True)
        seq = int(data.get("message_seq") or 0) if isinstance(data, dict) else 0
        if seq:
            return seq
        # Yogurt (acidify) answers with the sequence of QQ's send reply, 0 when QQ gave none: QQ took the message
        # without posting it, what SnowLuma reports as "no message receipt" (read from acidify's source, not seen).
        if group_id is not None:
            raise Dropped(f"{action}: QQ accepted the message but gave no message_seq")
        raise Unconfirmed(f"{action}: no message_seq in the answer")

    @override
    async def send(
        self, *, group_id: int | None, user_id: int, segments: list[Segment], reply_timeout: float | None = None
    ) -> int:
        return await self._send(group_id, user_id, outgoing_segments(segments), reply_timeout)

    @override
    async def send_forward(
        self, *, group_id: int | None, user_id: int, nodes: list[Node], reply_timeout: float | None = None
    ) -> Sent:
        # Milky answers a forward like any message: its seq, no forward id (fetch the message to learn it).
        return Sent(await self._send(group_id, user_id, [forward_segment(nodes)], reply_timeout))

    @override
    async def message(self, *, group_id: int | None, user_id: int, message_id: int) -> Message:
        scene, peer = ("group", group_id) if group_id is not None else ("friend", user_id)
        params = {"message_scene": scene, "peer_id": peer, "message_seq": message_id}
        return message_of(await self._data("get_message", params, "message"))

    @override
    async def forward(self, forward_id: str) -> list[ForwardNode]:
        # Milky's forwarded messages carry a sender name and avatar, no QQ number.
        messages = await self._data("get_forwarded_messages", {"forward_id": forward_id}, "messages")
        return [
            ForwardNode(
                0, str(m.get("sender_name") or ""), int(m.get("time") or 0), incoming_segments(m.get("segments") or [])
            )
            for m in messages
        ]

    @override
    async def history(self, group_id: int, *, before: int | None = None, limit: int = 20) -> list[Message]:
        params: dict[str, Any] = {
            "message_scene": "group",
            "peer_id": group_id,
            "limit": max(1, min(limit, HISTORY_PAGE)),
        }
        if before is not None:
            params["start_message_seq"] = before  # that message and older
        messages = [message_of(m) for m in await self._data("get_history_messages", params, "messages")]
        return sorted(messages, key=lambda m: m.message_id)

    @override
    async def react(self, *, group_id: int, message_id: int, emoji: str) -> None:
        params = {"group_id": group_id, "message_seq": message_id, "reaction": emoji, "reaction_type": "face"}
        await self._call("send_group_message_reaction", params)

    @override
    async def recall(self, *, group_id: int | None, user_id: int, message_id: int) -> None:
        if group_id is None:
            await self._call("recall_private_message", {"user_id": user_id, "message_seq": message_id})
        else:
            await self._call("recall_group_message", {"group_id": group_id, "message_seq": message_id})

    # --- media ----------------------------------------------------------------------------------------------------
    @override
    async def media_url(self, segment: Segment, *, group_id: int | None, user_id: int) -> str:
        kind, d = segment["type"], segment["data"]
        if kind in ("image", "record", "video"):
            # The NT file id in the link is the resource id; it is also there in segments OneBot recorded, whose
            # ``ref`` is that protocol's own handle.
            resource = file_id_of(str(d.get("url") or "")) or str(d.get("ref") or "")
            if not resource:
                raise QQError(f"{kind} 消息段没有资源 ID")
            return str(await self._data("get_resource_temp_url", {"resource_id": resource}, "url"))
        if kind == "file":
            if group_id is not None:
                params = {"group_id": group_id, "file_id": d["file_id"]}
                return str(await self._data("get_group_file_download_url", params, "download_url"))
            if not d.get("file_hash"):
                raise QQError("私聊文件没有 file_hash，拿不到下载链接")
            params = {"user_id": user_id, "file_id": d["file_id"], "file_hash": d["file_hash"]}
            return str(await self._data("get_private_file_download_url", params, "download_url"))
        raise QQError(f"{kind} 消息段没有下载链接")

    # --- requests and raw access ----------------------------------------------------------------------------------
    @override
    async def decide_request(self, observed: Observed, *, approve: bool, reason: str | None = None) -> None:
        if observed.protocol != "milky":
            raise QQError("这条请求是通过 OneBot 连接收到的，Milky 连接处理不了")
        event_type, d = observed.data.get("event_type"), observed.data.get("data") or {}
        refusal = {"reason": reason} if reason and not approve else {}
        verb = "accept" if approve else "reject"
        match event_type:
            case "friend_request":
                params = {"initiator_uid": d["initiator_uid"], "is_filtered": bool(d.get("is_filtered"))}
                await self._call(f"{verb}_friend_request", {**params, **refusal})
            case "group_join_request" | "group_invited_join_request":
                params = {
                    "notification_seq": d["notification_seq"],
                    "notification_type": event_type.removeprefix("group_"),
                    "group_id": d["group_id"],
                    "is_filtered": bool(d.get("is_filtered")),
                }
                await self._call(f"{verb}_group_request", {**params, **refusal})
            case "group_invitation":
                await self._call(
                    f"{verb}_group_invitation", {"group_id": d["group_id"], "invitation_seq": d["invitation_seq"]}
                )
            case _:
                raise QQError(f"不是能处理的请求：{event_type}")

    @override
    async def call(self, action: str, params: dict[str, Any]) -> Any:
        return await self._call(action, params)
