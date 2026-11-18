"""The OneBot v11 backend as SnowLuma speaks it: segments both ways, events, errors and the actions it calls."""

import base64
import hashlib
import json
from typing import Any, cast

import pytest
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    ActionFailed,
    ApiNotAvailable,
    FriendRequestEvent,
    GroupAdminNoticeEvent,
    GroupBanNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupRecallNoticeEvent,
    GroupRequestEvent,
    Message,
    MessageSegment,
    NetworkError,
    NoticeEvent,
)

from tests.support.onebot import ScriptedBot, group_message
from yggdrasil.qq import segments as seg
from yggdrasil.qq.model import Dropped, Node, Observed, QQError, Sender, Sent, Unconfirmed
from yggdrasil.qq.model import Message as QQMessage
from yggdrasil.qq.onebot import OneBotQQ, to_schema

SELF, GROUP, ALICE, BOB = 10000, 20001, 10001, 10002
PICTURE_SHA1 = hashlib.sha1(b"picture").digest()


def _fileid(sha1: bytes) -> str:
    """An NT file id as SnowLuma's picture urls carry it: base64url protobuf, field 2 the content sha1."""
    return base64.urlsafe_b64encode(b"\x12\x14" + sha1 + b"\x50\x80\xbd\xa3\x05").decode().rstrip("=")


PICTURE_URL = f"https://multimedia.nt.qq.com.cn/download?appid=1407&fileid={_fileid(PICTURE_SHA1)}&spec=0&rkey=CAQ"


def _qq(answers: dict[str, Any] | None = None) -> tuple[OneBotQQ, ScriptedBot]:
    bot = ScriptedBot(answers, self_id=SELF)
    return OneBotQQ(cast(Bot, bot)), bot


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def test_snowluma_segments_become_the_schema() -> None:
    card = json.dumps({"app": "com.tencent.miniapp_01", "prompt": "[QQ小程序]分享", "meta": {}}, ensure_ascii=False)
    face = {"emoji_id": "0123456789abcdef0123456789abcdef", "emoji_package_id": 200001, "key": "0123456789abcdef"}
    snowluma = [
        _text("看 "),
        {"type": "at", "data": {"qq": "all"}},
        {"type": "at", "data": {"qq": "10001", "name": "@阿明"}},
        {"type": "at", "data": {"qq": "10002"}},
        {"type": "face", "data": {"id": "14", "raw": {"faceIndex": 14, "faceText": "/微笑"}, "resultId": None}},
        {"type": "reply", "data": {"id": "-707651348"}},
        {"type": "image", "data": {"file": "A1B2.jpg", "url": PICTURE_URL, "summary": "[图片]", "sub_type": 0}},
        {"type": "image", "data": {"file": "own.png", "url": "", "summary": "", "sub_type": 0, "blob": "ab12"}},
        {
            "type": "image",
            "data": {"file": "a6-0123.gif", "url": "https://example.com/raw300.gif", "summary": "[加载中]", **face},
        },
        {
            "type": "mface",
            "data": {"emoji_id": "fedcba98", "emoji_package_id": "230001", "key": "k", "summary": "[吃瓜]"},
        },
        {"type": "record", "data": {"file": "voice-0001.amr", "url": "https://example.com/v", "file_size": "10240"}},
        {"type": "video", "data": {"file": "clip-0001.mp4", "file_size": "2097152"}},
        {
            "type": "file",
            "data": {"file": "benchmark.xlsx", "file_id": "/f0e1-d2c3", "file_size": "18432", "busid": 102},
        },
        {"type": "file", "data": {"name": "notes.txt", "file_id": "f-2", "size": 12, "file_hash": "c0ffee"}},
        {"type": "forward", "data": {"id": "res-1"}},
        {"type": "json", "data": {"data": card}},
        {"type": "xml", "data": {"data": "<?xml version='1.0'?><msg/>"}},
        {"type": "markdown", "data": {"content": "**今日小结**"}},
        {"type": "poke", "data": {"type": "1", "id": "1"}},
        {"type": "dice", "data": {"result": "4"}},
        {"type": "rps", "data": {"result": "2"}},
        {"type": "inline_keyboard", "data": {"rows": []}},
        {"type": "location", "data": {"lat": "0", "lon": "0"}},
    ]
    assert to_schema(snowluma) == [
        _text("看 "),
        {"type": "mention_all", "data": {}},
        {"type": "mention", "data": {"user_id": ALICE, "name": "阿明"}},
        {"type": "mention", "data": {"user_id": BOB, "name": ""}},
        {"type": "face", "data": {"face_id": "14", "is_large": False}},
        {"type": "reply", "data": {"message_id": -707651348}},
        {
            "type": "image",
            "data": {
                "ref": "A1B2.jpg",
                "url": PICTURE_URL,
                "summary": "[图片]",
                "sticker": False,
                "sha1": PICTURE_SHA1.hex(),
            },
        },
        {
            "type": "image",
            "data": {"ref": "own.png", "url": "", "summary": "", "sticker": False, "sha1": "", "blob": "ab12"},
        },
        {
            "type": "market_face",
            "data": {
                "emoji_package_id": 200001,
                "emoji_id": face["emoji_id"],
                "key": face["key"],
                "summary": "[加载中]",
                "url": "https://example.com/raw300.gif",
            },
        },
        {
            "type": "market_face",
            "data": {"emoji_package_id": 230001, "emoji_id": "fedcba98", "key": "k", "summary": "[吃瓜]", "url": ""},
        },
        {"type": "record", "data": {"ref": "voice-0001.amr", "url": "https://example.com/v", "duration": 0}},
        {"type": "video", "data": {"ref": "clip-0001.mp4", "url": "", "duration": 0}},
        {"type": "file", "data": {"file_id": "/f0e1-d2c3", "file_name": "benchmark.xlsx", "file_size": 18432}},
        {"type": "file", "data": {"file_id": "f-2", "file_name": "notes.txt", "file_size": 12, "file_hash": "c0ffee"}},
        {"type": "forward", "data": {"forward_id": "res-1", "title": "", "preview": [], "summary": ""}},
        {"type": "light_app", "data": {"app_name": "com.tencent.miniapp_01", "json_payload": card}},
        {"type": "xml", "data": {"service_id": 0, "xml_payload": "<?xml version='1.0'?><msg/>"}},
        {"type": "markdown", "data": {"content": "**今日小结**"}},
        _text("[戳一戳]"),
        _text("[骰子:4]"),
        _text("[猜拳:2]"),
        _text("[location]"),
    ]


def test_a_recorded_forward_keeps_its_nodes() -> None:
    node = {"user_id": str(SELF), "nickname": "Ygg", "time": 1760000000, "content": [_text("第一段"), _text("[图片]")]}
    record = {"type": "forward", "data": {"id": "res-9", "title": "答", "content": [{"type": "node", "data": node}]}}
    [forward] = to_schema([record])
    assert forward["data"] == {
        "forward_id": "res-9",
        "title": "答",
        "preview": [],
        "summary": "",
        "nodes": [{"user_id": SELF, "name": "Ygg", "time": 1760000000, "segments": [_text("第一段"), _text("[图片]")]}],
    }


@pytest.mark.parametrize(
    ("sub_type", "summary", "sticker"),
    [(1, "", True), ("1", "[图片]", True), (0, "[动画表情]", True), (0, "[图片]", False), (0, "", False)],
)
def test_stickers_are_told_from_photos(sub_type: Any, summary: str, sticker: bool) -> None:
    image = {"file": "x.jpg", "url": "https://example.com/x.jpg", "summary": summary, "sub_type": sub_type}
    [out] = to_schema([{"type": "image", "data": image}])
    assert (out["data"]["sticker"], out["data"]["sha1"]) == (sticker, "")


async def test_a_group_message_with_a_reply_and_a_mention_of_the_bot_inside() -> None:
    quoted = {
        "time": 1759990000,
        "message_type": "group",
        "message_id": -707651348,
        "real_id": -707651348,
        "message_seq": 4401,
        "sender": {"user_id": BOB, "nickname": "老王", "card": "", "role": "admin"},
        "message": [_text("原话")],
    }
    text = MessageSegment.text
    said = Message([MessageSegment.reply(-707651348), text("问一下 "), MessageSegment.at(SELF), text(" 这是什么")])
    event = group_message(
        said,
        group_id=GROUP,
        user_id=ALICE,
        self_id=SELF,
        message_id=-1520000001,
        card=" 阿明 ",
        nickname="ming",
        reply=quoted,
        message_seq=4402,
    )
    qq, _ = _qq()
    got = await qq.incoming(event)
    assert got is not None
    assert (got.message_id, got.group_id, got.seq, got.self_id, got.protocol) == (
        -1520000001,
        GROUP,
        4402,
        SELF,
        "onebot",
    )
    assert (got.sender, got.to_me, got.user_id) == (Sender(ALICE, "阿明", "member"), True, ALICE)
    assert got.segments == [
        {"type": "reply", "data": {"message_id": -707651348}},
        _text("问一下 "),
        {"type": "mention", "data": {"user_id": SELF, "name": ""}},
        _text(" 这是什么"),
    ]
    assert got.reply == QQMessage(-707651348, GROUP, Sender(BOB, "老王", "admin"), 1759990000, [_text("原话")], 4401)

    chat = await qq.incoming(group_message("随便聊聊", group_id=GROUP, user_id=ALICE, self_id=SELF, nickname="ming"))
    assert chat is not None
    assert (chat.to_me, chat.reply, chat.seq, chat.sender.name, chat.text) == (False, None, None, "ming", "随便聊聊")


def _notice(cls: type, notice_type: str, **fields: Any) -> Any:
    return cls.model_validate(
        {"time": 1760000000, "self_id": SELF, "post_type": "notice", "notice_type": notice_type, **fields}
    )


def _request(cls: type, request_type: str, **fields: Any) -> Any:
    return cls.model_validate(
        {"time": 1760000000, "self_id": SELF, "post_type": "request", "request_type": request_type, **fields}
    )


def test_a_group_recall_names_the_message() -> None:
    qq, _ = _qq()
    recall = _notice(
        GroupRecallNoticeEvent, "group_recall", group_id=GROUP, user_id=ALICE, operator_id=ALICE, message_id=-42
    )
    assert qq.recalled(recall) == (GROUP, -42)
    assert qq.recalled(_notice(NoticeEvent, "bot_offline")) is None


def test_requests_and_notices_are_observed_with_their_details() -> None:
    qq, _ = _qq()

    def seen(event: Any) -> tuple[Any, ...]:
        o = qq.observe(event)
        assert o is not None
        assert o.protocol == "onebot"
        return o.kind, o.group_id, o.user_id, o.operator_id

    member = {"group_id": GROUP, "operator_id": BOB}
    kicked = _notice(GroupDecreaseNoticeEvent, "group_decrease", sub_type="kick_me", user_id=SELF, **member)
    assert seen(kicked) == ("notice.group_decrease.kick_me", GROUP, SELF, BOB)
    left = _notice(GroupDecreaseNoticeEvent, "group_decrease", sub_type="leave", user_id=ALICE, **member)
    assert seen(left) == ("notice.group_decrease.leave", GROUP, ALICE, BOB)
    joined = _notice(GroupIncreaseNoticeEvent, "group_increase", sub_type="invite", user_id=SELF, **member)
    assert seen(joined)[0] == "notice.group_increase.self"
    approved = _notice(GroupIncreaseNoticeEvent, "group_increase", sub_type="approve", user_id=ALICE, **member)
    assert seen(approved)[0] == "notice.group_increase.approve"
    admin = _notice(GroupAdminNoticeEvent, "group_admin", sub_type="set", group_id=GROUP, user_id=SELF)
    assert seen(admin) == ("notice.group_admin.self.set", GROUP, SELF, None)

    def ban(user_id: int, sub_type: str = "ban") -> tuple[str, int | None]:
        event = _notice(GroupBanNoticeEvent, "group_ban", sub_type=sub_type, user_id=user_id, duration=600, **member)
        o = qq.observe(event)
        assert o is not None
        return o.kind, o.duration

    assert ban(SELF) == ("notice.group_ban.self.ban", 600)
    assert ban(0) == ("notice.group_ban.all.ban", 600)
    assert ban(ALICE, "lift_ban") == ("notice.group_ban.member.lift_ban", 600)

    assert seen(_notice(NoticeEvent, "bot_offline")) == ("notice.bot_offline", None, SELF, None)

    invite = _request(GroupRequestEvent, "group", sub_type="invite", group_id=GROUP, user_id=ALICE, flag="f-1")
    o = qq.observe(invite)
    assert o is not None
    assert (o.kind, o.is_request, o.data["flag"], o.comment) == ("request.group.invite", True, "f-1", "")
    friend = _request(FriendRequestEvent, "friend", user_id=BOB, flag="f-2", comment="我是老王")
    o = qq.observe(friend)
    assert o is not None
    assert (o.kind, o.user_id, o.comment, o.data["flag"]) == ("request.friend", BOB, "我是老王", "f-2")

    assert qq.observe(group_message("hi", group_id=GROUP, user_id=ALICE, self_id=SELF)) is None


async def test_requests_are_answered_by_flag_and_only_when_onebot_recorded_them() -> None:
    qq, bot = _qq({"set_group_add_request": None, "set_friend_add_request": None})
    invite = qq.observe(
        _request(GroupRequestEvent, "group", sub_type="invite", group_id=GROUP, user_id=ALICE, flag="f-1")
    )
    friend = qq.observe(_request(FriendRequestEvent, "friend", user_id=BOB, flag="f-2"))
    assert invite is not None
    assert friend is not None
    await qq.decide_request(invite, approve=False, reason="不认识")
    await qq.decide_request(friend, approve=True, reason="ignored")
    assert bot.called("set_group_add_request") == [
        {"flag": "f-1", "sub_type": "invite", "approve": False, "reason": "不认识"}
    ]
    assert bot.called("set_friend_add_request") == [{"flag": "f-2", "approve": True, "remark": ""}]
    with pytest.raises(QQError):
        await qq.decide_request(Observed("request.friend", "milky", user_id=BOB, data={"flag": "f-3"}), approve=True)


async def test_sends_carry_onebot_segments_and_the_reply_timeout() -> None:
    qq, bot = _qq({"send_group_msg": {"message_id": -1234}, "send_private_msg": {"message_id": 777}})
    said = [seg.reply(-707651348), seg.mention(ALICE), seg.text(" 好"), seg.image("base64://iVBORw0K")]
    assert await qq.send(group_id=GROUP, user_id=ALICE, segments=said, reply_timeout=120.0) == -1234
    assert bot.called("send_group_msg") == [
        {
            "group_id": GROUP,
            "message": [
                {"type": "reply", "data": {"id": "-707651348"}},
                {"type": "at", "data": {"qq": str(ALICE)}},
                _text(" 好"),
                {"type": "image", "data": {"file": "base64://iVBORw0K"}},
            ],
            "_timeout": 120.0,
        }
    ]
    assert await qq.send(group_id=None, user_id=ALICE, segments=[seg.text("hi")]) == 777
    assert bot.called("send_private_msg") == [{"user_id": ALICE, "message": [_text("hi")]}]


async def test_a_forward_send_returns_its_res_id() -> None:
    qq, bot = _qq(
        {
            "send_group_forward_msg": {"message_id": 41, "res_id": "Fq7h/x+Ab2"},
            "send_private_forward_msg": {"message_id": 42},
        }
    )
    nodes = [Node(SELF, "Ygg", [seg.text("第一段")]), Node(SELF, "Ygg", [seg.image("base64://iVBORw0K")])]
    assert await qq.send_forward(group_id=GROUP, user_id=ALICE, nodes=nodes, reply_timeout=120.0) == Sent(
        41, "Fq7h/x+Ab2"
    )
    assert bot.called("send_group_forward_msg") == [
        {
            "group_id": GROUP,
            "messages": [
                {"type": "node", "data": {"user_id": str(SELF), "nickname": "Ygg", "content": [_text("第一段")]}},
                {
                    "type": "node",
                    "data": {
                        "user_id": str(SELF),
                        "nickname": "Ygg",
                        "content": [{"type": "image", "data": {"file": "base64://iVBORw0K"}}],
                    },
                },
            ],
            "_timeout": 120.0,
        }
    ]
    assert await qq.send_forward(group_id=None, user_id=ALICE, nodes=nodes[:1]) == Sent(42, None)


RECEIPT = (
    "group message delivery was not confirmed: QQ returned no message receipt; check the conversation before retrying"
)


def _failed(message: str = "", wording: str = "") -> ActionFailed:
    return ActionFailed(status="failed", retcode=100, data=None, message=message, wording=wording)


@pytest.mark.parametrize(
    ("error", "in_group", "expected"),
    [
        (_failed(wording=RECEIPT), True, Dropped),
        (_failed(wording=RECEIPT), False, Unconfirmed),  # no group seq to miss in a private chat
        (_failed("send group message failed: no response"), True, Unconfirmed),
        (_failed("failed to decode SendMessageResponse"), True, Unconfirmed),
        (NetworkError("WebSocket call api send_msg timeout"), True, Unconfirmed),
        (NetworkError("WebSocket call api send_msg timeout"), False, Unconfirmed),
        (_failed("group not found"), True, QQError),
        (NetworkError("WebSocket call api send_msg failed"), True, QQError),
        (ApiNotAvailable(), False, QQError),
    ],
)
@pytest.mark.parametrize("forward", [False, True])
async def test_send_failures_become_dropped_unconfirmed_or_plain_errors(
    error: Exception, in_group: bool, expected: type[QQError], forward: bool
) -> None:
    action = f"send_{'group' if in_group else 'private'}_{'forward_msg' if forward else 'msg'}"
    qq, _ = _qq({action: error})
    group_id = GROUP if in_group else None
    sending = (
        qq.send_forward(group_id=group_id, user_id=ALICE, nodes=[Node(SELF, "Ygg", [seg.text("hi")])])
        if forward
        else qq.send(group_id=group_id, user_id=ALICE, segments=[seg.text("hi")])
    )
    with pytest.raises(QQError) as raised:
        await sending
    assert type(raised.value) is expected


async def test_other_actions_fail_plainly() -> None:
    qq, _ = _qq({"get_msg": NetworkError("WebSocket call api get_msg timeout"), "get_status": _failed(wording=RECEIPT)})
    with pytest.raises(QQError) as raised:
        await qq.message(group_id=GROUP, user_id=ALICE, message_id=5)
    assert type(raised.value) is QQError
    with pytest.raises(QQError) as raised:
        await qq.call("get_status", {})
    assert type(raised.value) is QQError


async def test_history_is_anchored_by_message_id_and_comes_oldest_first() -> None:
    def record(message_id: int, seq: int, at: int, text: str) -> dict[str, Any]:
        sender = {"user_id": ALICE, "nickname": "ming", "card": "阿明", "role": "owner"}
        return {
            "message_id": message_id,
            "message_seq": seq,
            "time": at,
            "user_id": ALICE,
            "group_id": GROUP,
            "sender": sender,
            "message": [_text(text)],
        }

    page = {"messages": [record(-7, 302, 1760000060, "后"), record(91, 301, 1760000000, "前")]}
    qq, bot = _qq({"get_group_msg_history": page})
    got = await qq.history(GROUP, before=-7, limit=30)
    assert [(m.message_id, m.seq, m.sender, m.segments) for m in got] == [
        (91, 301, Sender(ALICE, "阿明", "owner"), [_text("前")]),
        (-7, 302, Sender(ALICE, "阿明", "owner"), [_text("后")]),
    ]
    await qq.history(GROUP)
    assert bot.called("get_group_msg_history") == [
        {"group_id": GROUP, "message_id": -7, "count": 30},
        {"group_id": GROUP, "count": 20},
    ]


async def test_download_links_come_from_the_matching_action() -> None:
    qq, bot = _qq(
        {
            "get_image": {"url": PICTURE_URL},
            "get_group_file_url": {"url": "https://example.com/group-file"},
            "get_file": {"url": "https://example.com/private-file"},
        }
    )
    [picture] = to_schema([{"type": "image", "data": {"file": "A1B2.jpg", "url": "", "summary": "[图片]"}}])
    file = {"type": "file", "data": {"file_id": "/f0e1", "file_name": "a.txt", "file_size": 1}}
    assert await qq.media_url(picture, group_id=GROUP, user_id=ALICE) == PICTURE_URL
    assert await qq.media_url(file, group_id=GROUP, user_id=ALICE) == "https://example.com/group-file"
    assert await qq.media_url(file, group_id=None, user_id=ALICE) == "https://example.com/private-file"
    assert bot.calls == [
        ("get_image", {"file": "A1B2.jpg"}),
        ("get_group_file_url", {"group_id": GROUP, "file_id": "/f0e1"}),
        ("get_file", {"file_id": "/f0e1"}),
    ]
