"""``QQ`` over NoneBot's OneBot v11 adapter, as SnowLuma speaks it (kept for now; Milky is the main protocol).

SnowLuma specifics relied on: ``message_id`` is a 32-bit hash and ``message_seq`` (an extra field on message events
and records) the group seq; ``get_group_msg_history`` is anchored by ``message_id``; ``get_image`` re-signs a
picture's url by its file name; forward sends answer with ``res_id``; member info carries ``is_robot``; market faces
arrive as ``image`` segments with ``emoji_id``/``emoji_package_id``; a logged-out account reports a ``bot_offline``
notice.
"""

import json
import re
from typing import Any, Literal, cast

from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    ActionFailed,
    FriendRequestEvent,
    GroupAdminNoticeEvent,
    GroupBanNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    GroupRecallNoticeEvent,
    GroupRequestEvent,
    NetworkError,
    NoticeEvent,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11 import Message as V11Message

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
from yggdrasil.qq.segments import ForwardNodeData, Segment

type _Sending = Literal["group", "private"] | None

# SnowLuma's wording when QQ answers a group send with result 0 but no group sequence. On 2026-11-01 both a forward and
# its plain-text copy describing a proxy client were dropped this way within a second: QQ's content filter, as far
# as we can tell, so sending the same content again in another form doesn't help.
_DROPPED = "no message receipt"
# SnowLuma reports a send packet that got no reply, or an answer it couldn't decode, as a failed action although the
# message may be in the chat (it says "check the conversation before retrying"); other failures are definite. An
# answer without a receipt (``_DROPPED``, whose wording also says "not confirmed") is checked first: QQ dropped that
# one. The adapter's own reply timeout is "maybe sent" too: the message almost always goes out anyway (a drawing that
# hit the default timeout did).
_MAYBE_SENT = re.compile(r"message failed: |not confirmed|failed to decode SendMessageResponse")


# --- segments -----------------------------------------------------------------------------------------------------


def to_schema(segments: list[dict[str, Any]]) -> list[Segment]:
    """OneBot segments as SnowLuma sends them (and as rows stored before the schema hold them) in ``qq.segments``.

    Pure, like its helpers ``_node``/``_content``/``_int``: the alembic data migration that converted stored rows keeps
    a frozen copy, so a change here never reaches rows stored before it.
    """
    out: list[Segment] = []
    for seg in segments:
        kind = str(seg.get("type") or "")
        d: dict[str, Any] = seg.get("data") or {}
        match kind:
            case "text":
                out.append({"type": "text", "data": {"text": str(d.get("text") or "")}})
            case "at" if str(d.get("qq")) == "all":
                out.append({"type": "mention_all", "data": {}})
            case "at":
                name = str(d.get("name") or "").removeprefix("@")
                out.append({"type": "mention", "data": {"user_id": _int(d.get("qq")), "name": name}})
            case "face":
                face_id = d.get("id")
                face = "" if face_id is None else str(face_id)
                out.append({"type": "face", "data": {"face_id": face, "is_large": False}})
            case "reply":
                out.append({"type": "reply", "data": {"message_id": _int(d.get("id"))}})
            case "mface" | "image" if kind == "mface" or (d.get("emoji_id") and d.get("emoji_package_id")):
                # SnowLuma sends market faces as images carrying the face's ids.
                face = {
                    "emoji_package_id": _int(d.get("emoji_package_id")),
                    "emoji_id": str(d.get("emoji_id") or ""),
                    "key": str(d.get("key") or ""),
                    "summary": str(d.get("summary") or ""),
                    "url": str(d.get("url") or ""),
                }
                out.append({"type": "market_face", "data": face})
            case "image":
                url, summary = str(d.get("url") or ""), str(d.get("summary") or "")
                image: dict[str, Any] = {
                    "ref": str(d.get("file") or ""),
                    "url": url,
                    "summary": summary,
                    # Custom stickers: sub_type 1, or a summary such as "[动画表情]".
                    "sticker": str(d.get("sub_type")) == "1" or summary.strip("[]") not in ("", "图片"),
                    "sha1": sha1_of(file_id_of(url)),
                }
                if d.get("blob"):
                    image["blob"] = str(d["blob"])
                out.append({"type": "image", "data": image})
            case "record" | "video":
                media = {"ref": str(d.get("file") or ""), "url": str(d.get("url") or ""), "duration": 0}
                out.append({"type": kind, "data": media})
            case "file":
                file: dict[str, Any] = {
                    "file_id": str(d.get("file_id") or ""),
                    "file_name": str(d.get("name") or d.get("file") or ""),
                    "file_size": _int(d.get("file_size") or d.get("size")),
                }
                if d.get("file_hash"):
                    file["file_hash"] = str(d["file_hash"])
                out.append({"type": "file", "data": file})
            case "forward":
                forward: dict[str, Any] = {
                    "forward_id": str(d.get("id") or ""),
                    "title": str(d.get("title") or ""),
                    "preview": [],
                    "summary": "",
                }
                if isinstance(content := d.get("content"), list):  # our record of a forward the bot sent
                    forward["nodes"] = [_node(n) for n in cast(list[Any], content) if isinstance(n, dict)]
                out.append({"type": "forward", "data": forward})
            case "node":
                # A node nested in a recorded forward node (NapCat's nested markdown forwards): its content inline.
                out.extend(_node(seg)["segments"])
            case "json":
                payload = d.get("data") or ""
                if not isinstance(payload, str):
                    payload = json.dumps(payload, ensure_ascii=False)
                try:
                    app = json.loads(payload).get("app")
                except (ValueError, AttributeError):
                    app = None
                out.append({"type": "light_app", "data": {"app_name": str(app or ""), "json_payload": payload}})
            case "xml":
                out.append({"type": "xml", "data": {"service_id": 0, "xml_payload": str(d.get("data") or "")}})
            case "markdown":
                out.append({"type": "markdown", "data": {"content": str(d.get("content") or "")}})
            case "poke":
                out.append({"type": "text", "data": {"text": "[戳一戳]"}})
            case "dice" | "rps":
                label = "骰子" if kind == "dice" else "猜拳"
                out.append({"type": "text", "data": {"text": f"[{label}:{d.get('result', '')}]"}})
            case "inline_keyboard":
                pass  # bot buttons: no content
            case _:
                out.append({"type": "text", "data": {"text": f"[{kind}]"}})
    return out


def _node(item: dict[str, Any]) -> ForwardNodeData:
    """A forward node: ``{"type": "node", "data": {user_id, nickname, time, content}}`` (our record of a forward the
    bot sent) or a ``get_forward_msg`` record ``{sender: {user_id, nickname}, time, message}``."""
    data: dict[str, Any] = (item.get("data") or {}) if item.get("type") == "node" else item
    sender: dict[str, Any] = data.get("sender") or {}
    return {
        "user_id": _int(data.get("user_id") or sender.get("user_id")),
        "name": str(data.get("nickname") or sender.get("nickname") or sender.get("card") or ""),
        "time": _int(data.get("time")),
        "segments": to_schema(_content(data.get("content") or data.get("message"))),
    }


def _content(message: Any) -> list[dict[str, Any]]:
    """A message's segments; OneBot may give a plain string instead."""
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    return [s for s in cast(list[Any], message) if isinstance(s, dict)] if isinstance(message, list) else []


def _int(value: Any) -> int:
    """OneBot ids and sizes come as numbers or numeric strings; 0 for anything else."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_onebot(segments: list[Segment]) -> list[dict[str, Any]]:
    """Outgoing ``qq.segments`` (text, mention, reply, image) as OneBot segments."""
    out: list[dict[str, Any]] = []
    for seg in segments:
        d = seg["data"]
        match seg["type"]:
            case "text":
                out.append({"type": "text", "data": {"text": d["text"]}})
            case "mention":
                out.append({"type": "at", "data": {"qq": str(d["user_id"])}})
            case "reply":
                out.append({"type": "reply", "data": {"id": str(d["message_id"])}})
            case "image":
                out.append({"type": "image", "data": {"file": d["uri"]}})
            case other:
                raise ValueError(f"not an outgoing segment: {other}")
    return out


# --- records ------------------------------------------------------------------------------------------------------


def _role(value: Any) -> Role:
    return value if value in ("owner", "admin") else "member"


def _sender(user_id: int, card: Any, nickname: Any, role: Any) -> Sender:
    return Sender(user_id, str(card or nickname or "").strip(), _role(role))


def _seq(value: Any) -> int | None:
    return _int(value) or None


def _message(record: dict[str, Any], group_id: int | None) -> Message | None:
    """A ``get_msg`` / ``get_group_msg_history`` record; None without an id or a sender."""
    sender: dict[str, Any] = record.get("sender") or {}
    message_id, user_id = record.get("message_id"), record.get("user_id") or sender.get("user_id")
    if message_id is None or user_id is None:
        return None
    role = sender.get("role") if group_id is not None else None
    return Message(
        message_id=_int(message_id),
        group_id=group_id,
        sender=_sender(_int(user_id), sender.get("card"), sender.get("nickname"), role),
        time=_int(record.get("time")),
        segments=to_schema(_content(record.get("message"))),
        seq=_seq(record.get("message_seq")),
    )


def _obj(answer: Any) -> dict[str, Any]:
    if not isinstance(answer, dict):
        raise QQError(f"unexpected answer: {answer!r:.200}")
    return cast(dict[str, Any], answer)


def _message_id(answer: Any) -> int:
    message_id = _obj(answer).get("message_id")
    if message_id is None:
        raise QQError(f"no message_id in the answer: {answer!r:.200}")
    return _int(message_id)


def _error(e: Exception, sending: _Sending) -> QQError:
    """The ``qq.model`` error for a failed action; ``sending``: which kind of send it was (None: not a send)."""
    if isinstance(e, ActionFailed):
        message, wording = str(e.info.get("message") or ""), str(e.info.get("wording") or "")
        reason = message or wording or repr(e)
        if sending == "group" and (_DROPPED in message or _DROPPED in wording):
            return Dropped(reason)
        if sending and (_MAYBE_SENT.search(message) or _MAYBE_SENT.search(wording)):
            return Unconfirmed(reason)
        return QQError(reason)
    if isinstance(e, NetworkError):
        reason = e.msg or repr(e)
        return Unconfirmed(reason) if sending and reason.endswith("timeout") else QQError(reason)
    return QQError(str(e) or type(e).__name__)


def _dicts(message: V11Message) -> list[dict[str, Any]]:
    """An adapter ``Message`` as plain segments."""
    return [{"type": s.type, "data": dict(s.data)} for s in message]


# --- client -------------------------------------------------------------------------------------------------------


class OneBotQQ(QQ):
    protocol = "onebot"

    async def _call(self, action: str, sending: _Sending = None, /, **params: Any) -> Any:
        """``params`` that are None are left out (``_timeout`` None means the adapter's default)."""
        try:
            return await self.bot.call_api(action, **{k: v for k, v in params.items() if v is not None})
        except Exception as e:
            raise _error(e, sending) from e

    # --- events -----------------------------------------------------------------------------------------------
    async def incoming(self, event: Event) -> Incoming | None:
        if isinstance(event, GroupMessageEvent):
            group_id: int | None = event.group_id
        elif isinstance(event, PrivateMessageEvent):
            group_id = None
        else:
            return None
        original = _dicts(event.original_message)
        # The adapter sets ``to_me`` for an @bot at the start or end (and strips it from ``message``); one in the
        # middle stays an ``at`` segment.
        at_me = any(s["type"] == "at" and str(s["data"].get("qq")) == str(self.self_id) for s in original)
        reply = None
        if (quoted := event.reply) is not None:
            role = quoted.sender.role if group_id is not None else None
            reply = Message(
                message_id=quoted.message_id,
                group_id=group_id,
                sender=_sender(quoted.sender.user_id or 0, quoted.sender.card, quoted.sender.nickname, role),
                time=quoted.time,
                segments=to_schema(_dicts(quoted.message)),
                seq=_seq(getattr(quoted, "message_seq", None)),
            )
        sender = event.sender
        return Incoming(
            message_id=event.message_id,
            group_id=group_id,
            sender=_sender(event.user_id, sender.card, sender.nickname, sender.role if group_id is not None else None),
            time=event.time,
            segments=to_schema(original),
            seq=_seq(getattr(event, "message_seq", None)),
            self_id=self.self_id,
            text=event.get_plaintext(),
            to_me=event.to_me or at_me,
            reply=reply,
            protocol="onebot",
        )

    def recalled(self, event: Event) -> tuple[int, int] | None:
        if isinstance(event, GroupRecallNoticeEvent):
            return event.group_id, event.message_id
        return None

    def observe(self, event: Event) -> Observed | None:
        me = self.self_id
        match event:
            case GroupRequestEvent():
                o = Observed(
                    f"request.group.{event.sub_type}",
                    "onebot",
                    event.group_id,
                    event.user_id,
                    comment=event.comment or "",
                )
            case FriendRequestEvent():
                o = Observed("request.friend", "onebot", user_id=event.user_id, comment=event.comment or "")
            case GroupDecreaseNoticeEvent() if event.sub_type == "kick_me" or event.user_id == me:
                o = Observed("notice.group_decrease.kick_me", "onebot", event.group_id, me, event.operator_id)
            case GroupDecreaseNoticeEvent():
                kind = f"notice.group_decrease.{event.sub_type}"
                o = Observed(kind, "onebot", event.group_id, event.user_id, event.operator_id)
            case GroupIncreaseNoticeEvent():
                kind = "notice.group_increase." + ("self" if event.user_id == me else event.sub_type)
                o = Observed(kind, "onebot", event.group_id, event.user_id, event.operator_id)
            case GroupBanNoticeEvent():
                who = "self" if event.user_id == me else "all" if event.user_id == 0 else "member"
                kind = f"notice.group_ban.{who}.{event.sub_type}"
                o = Observed(kind, "onebot", event.group_id, event.user_id, event.operator_id, duration=event.duration)
            case GroupAdminNoticeEvent():
                who = "self" if event.user_id == me else "member"
                o = Observed(f"notice.group_admin.{who}.{event.sub_type}", "onebot", event.group_id, event.user_id)
            case NoticeEvent(notice_type="bot_offline"):
                o = Observed("notice.bot_offline", "onebot", user_id=me)
            case _:
                return None
        o.data = event.model_dump(mode="json", exclude={"original_message", "message"})
        return o

    # --- identity / info --------------------------------------------------------------------------------------
    async def login(self) -> Login:
        info = _obj(await self._call("get_login_info"))
        return Login(_int(info.get("user_id")), str(info.get("nickname") or ""))

    async def group(self, group_id: int) -> Group:
        # no_cache: a group the bot just joined isn't in SnowLuma's cache yet.
        info = _obj(await self._call("get_group_info", group_id=group_id, no_cache=True))
        count = info.get("member_count")
        return Group(group_id, str(info.get("group_name") or ""), None if count is None else _int(count))

    async def member(self, group_id: int, user_id: int, *, reply_timeout: float | None = None) -> Member:
        info = _obj(
            await self._call("get_group_member_info", group_id=group_id, user_id=user_id, _timeout=reply_timeout)
        )
        robot = info.get("is_robot")  # SnowLuma checks Tencent's robot UIN ranges
        return Member(
            group_id=group_id,
            user_id=user_id,
            nickname=str(info.get("nickname") or ""),
            card=str(info.get("card") or ""),
            role=_role(info.get("role")),
            is_robot=None if robot is None else bool(robot),
        )

    # --- messages ---------------------------------------------------------------------------------------------
    async def send(
        self, *, group_id: int | None, user_id: int, segments: list[Segment], reply_timeout: float | None = None
    ) -> int:
        message = _to_onebot(segments)
        if group_id is None:
            answer = await self._call(
                "send_private_msg", "private", user_id=user_id, message=message, _timeout=reply_timeout
            )
        else:
            answer = await self._call(
                "send_group_msg", "group", group_id=group_id, message=message, _timeout=reply_timeout
            )
        return _message_id(answer)

    async def send_forward(
        self, *, group_id: int | None, user_id: int, nodes: list[Node], reply_timeout: float | None = None
    ) -> Sent:
        messages = [
            {"type": "node", "data": {"user_id": str(n.user_id), "nickname": n.name, "content": _to_onebot(n.segments)}}
            for n in nodes
        ]
        if group_id is None:
            answer = await self._call(
                "send_private_forward_msg", "private", user_id=user_id, messages=messages, _timeout=reply_timeout
            )
        else:
            answer = await self._call(
                "send_group_forward_msg", "group", group_id=group_id, messages=messages, _timeout=reply_timeout
            )
        return Sent(_message_id(answer), str(_obj(answer).get("res_id") or "") or None)

    async def message(self, *, group_id: int | None, user_id: int, message_id: int) -> Message:
        record = _obj(await self._call("get_msg", message_id=message_id))
        found = _message({"message_id": message_id, **record}, group_id)
        if found is None:
            raise QQError(f"get_msg {message_id}: no sender in the answer")
        return found

    async def forward(self, forward_id: str) -> list[ForwardNode]:
        answer = await self._call("get_forward_msg", id=forward_id)
        records = answer.get("messages") if isinstance(answer, dict) else answer
        if not isinstance(records, list):
            raise QQError(f"unexpected answer: {answer!r:.200}")
        return [ForwardNode(**_node(r)) for r in cast(list[Any], records) if isinstance(r, dict)]

    async def history(self, group_id: int, *, before: int | None = None, limit: int = 20) -> list[Message]:
        # SnowLuma returns the anchor message and older ones.
        answer = _obj(await self._call("get_group_msg_history", group_id=group_id, message_id=before, count=limit))
        records = answer.get("messages")
        found = [
            m
            for r in (cast(list[Any], records) if isinstance(records, list) else [])
            if isinstance(r, dict) and (m := _message(r, group_id)) is not None
        ]
        return sorted(found, key=lambda m: m.time)

    async def react(self, *, group_id: int, message_id: int, emoji: str) -> None:
        await self._call("set_msg_emoji_like", message_id=message_id, emoji_id=emoji, set=True)

    async def recall(self, *, group_id: int | None, user_id: int, message_id: int) -> None:
        await self._call("delete_msg", message_id=message_id)

    # --- media ------------------------------------------------------------------------------------------------
    async def media_url(self, segment: Segment, *, group_id: int | None, user_id: int) -> str:
        kind, data = segment.get("type"), segment.get("data") or {}
        if kind == "file":
            if not (file_id := str(data.get("file_id") or "")):
                raise QQError("file segment without a file_id")
            if group_id is not None:
                url = _obj(await self._call("get_group_file_url", group_id=group_id, file_id=file_id)).get("url")
            else:
                url = _obj(await self._call("get_file", file_id=file_id)).get("url")
        elif kind == "image" and data.get("ref"):
            # QQ picture urls carry an rkey that expires within hours; SnowLuma re-signs one by the file name.
            url = _obj(await self._call("get_image", file=str(data["ref"]))).get("url")
        elif kind in ("image", "record", "video"):
            url = data.get("url")  # nothing in OneBot refreshes these
        else:
            raise QQError(f"no download link for a {kind} segment")
        if not url:
            raise QQError(f"no download link for this {kind}")
        return str(url)

    # --- requests and raw access ------------------------------------------------------------------------------
    async def decide_request(self, observed: Observed, *, approve: bool, reason: str | None = None) -> None:
        if observed.protocol != "onebot" or not observed.is_request:
            raise QQError(f"not a OneBot request: {observed.kind} ({observed.protocol})")
        flag = str(observed.data.get("flag") or "")
        if observed.kind == "request.friend":
            await self._call("set_friend_add_request", flag=flag, approve=approve, remark="")
        else:
            sub_type = observed.kind.rsplit(".", 1)[1]
            await self._call(
                "set_group_add_request",
                flag=flag,
                sub_type=sub_type,
                approve=approve,
                reason=reason if not approve and reason else None,
            )

    async def call(self, action: str, params: dict[str, Any]) -> Any:
        try:
            return await self.bot.call_api(action, **params)
        except Exception as e:
            raise _error(e, None) from e
