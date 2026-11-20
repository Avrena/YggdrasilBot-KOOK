"""The Milky backend as Yogurt speaks it: segments both ways, events, errors and the actions it calls.

``milky_payloads.json`` holds real Yogurt answers (trimmed, ids and names replaced); the segment types the test group
never had (faces, files, market faces, ...) follow the Milky 1.3 spec."""

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import httpx
import nonebot
import pytest
from nonebot.adapters import Bot
from nonebot.adapters.milky import Bot as MilkyBot
from nonebot.adapters.milky.bot import _check_at_me, _check_reply  # pyright: ignore[reportPrivateUsage]
from nonebot.adapters.milky.config import ClientInfo
from nonebot.adapters.milky.event import MessageEvent
from nonebot.adapters.milky.exception import ActionFailed, InvalidAccessToken, NetworkError
from nonebot.adapters.milky.model.message import IncomingMessage
from nonebot.drivers import Request, Response

from yggdrasil.qq import segments as seg
from yggdrasil.qq.milky import Adapter, MilkyQQ, NoAnswer, incoming_segments
from yggdrasil.qq.model import (
    Announcement,
    Dropped,
    GroupFile,
    GroupFiles,
    GroupFolder,
    Node,
    Observed,
    QQError,
    Sender,
    Sent,
    Unconfirmed,
)

SELF, GROUP, OWNER, MEMBER = 10000, 20001, 10001, 10002
PAYLOADS: dict[str, Any] = json.loads((Path(__file__).parent / "milky_payloads.json").read_text(encoding="utf-8"))
PICTURE_SHA1 = hashlib.sha1(b"pytest picture").hexdigest()  # what the fixture's resource id carries


class ScriptedBot:
    """The Milky ``Bot`` as ``MilkyQQ`` uses it: ``call_api`` answered from ``answers`` (data to return, an exception
    to raise, or a function of the params returning either). Calls are kept without the raw-call flag."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.self_id = str(SELF)
        self.answers = answers or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, api: str, **data: Any) -> Any:
        assert data.pop("_raw") is True
        self.calls.append((api, data))
        answer = self.answers[api]
        if callable(answer):
            answer = answer(**data)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def called(self, api: str) -> list[dict[str, Any]]:
        return [data for name, data in self.calls if name == api]


def _qq(answers: dict[str, Any] | None = None) -> tuple[MilkyQQ, ScriptedBot]:
    bot = ScriptedBot(answers)
    return MilkyQQ(cast(Bot, bot)), bot


def _event(raw: dict[str, Any]) -> Any:
    event = Adapter.json_to_event(json.loads(json.dumps(raw)))
    assert event is not None
    return event


async def _delivered(raw: dict[str, Any], quoted: dict[str, Any] | None = None) -> MessageEvent:
    """A message event as the adapter hands it to the plugin: the quote fetched, a leading/trailing @bot stripped."""

    class _Fetching:
        async def get_message(self, **_: Any) -> IncomingMessage:
            assert quoted is not None
            return IncomingMessage.model_validate(quoted)

    event = _event(raw)
    await _check_reply(cast(MilkyBot, _Fetching()), event)
    _check_at_me(cast(MilkyBot, _Fetching()), event)
    return event


# --- segments -----------------------------------------------------------------------------------------------------


async def test_yogurt_history_comes_oldest_first_in_the_schema() -> None:
    page = PAYLOADS["get_history_messages"]
    newest_first = {**page, "messages": page["messages"][::-1]}
    qq, bot = _qq({"get_history_messages": newest_first})

    messages = await qq.history(GROUP, limit=50)

    assert bot.called("get_history_messages") == [
        {"_timeout": None, "message_scene": "group", "peer_id": GROUP, "limit": 30}
    ]
    assert [m.message_id for m in messages] == [3394, 3408, 3416, 3420, 3423]
    forward, image, reply, mention, own = messages
    assert forward.segments == [
        {
            "type": "forward",
            "data": {
                "forward_id": "jughq1/7HAc3example+forwardid/wIoEQWr2bITi1AgxkVMd",
                "title": "群聊的聊天记录",
                "preview": ["10000: 这个帖子我打不开，所以总结不了。", "10000: 来源", "10000: ⚙ 元数据"],
                "summary": "查看3条转发消息",
            },
        }
    ]
    picture = image.segments[0]["data"]
    assert picture["ref"]
    assert picture["ref"] in picture["url"]
    assert (picture["summary"], picture["sticker"], picture["sha1"]) == ("[图片]", False, PICTURE_SHA1)
    assert reply.segments == [
        {"type": "reply", "data": {"message_id": 3415}},
        {"type": "mention", "data": {"user_id": MEMBER, "name": "成员乙"}},
        seg.text(" "),
    ]
    assert mention.segments[0] == {"type": "mention", "data": {"user_id": MEMBER, "name": "成员乙"}}
    assert (reply.group_id, reply.seq, reply.sender) == (GROUP, 3416, Sender(OWNER, "群主", "owner"))
    assert own.sender == Sender(SELF, "测试bot", "admin")

    await qq.history(GROUP, before=3400, limit=5)
    assert bot.called("get_history_messages")[-1]["start_message_seq"] == 3400


def test_the_other_milky_segments_keep_only_the_schema_fields() -> None:
    milky = [
        {"type": "mention_all", "data": {}},
        {"type": "face", "data": {"face_id": "76", "is_large": False}},
        {
            "type": "image",
            "data": {"resource_id": "x", "temp_url": "u", "width": 1, "height": 1, "sub_type": "sticker"},
        },
        {"type": "record", "data": {"resource_id": "r", "temp_url": "https://example.com/r", "duration": 3}},
        {
            "type": "video",
            "data": {"resource_id": "v", "temp_url": "https://example.com/v", "width": 1, "height": 1, "duration": 9},
        },
        {"type": "file", "data": {"file_id": "/f", "file_name": "a.txt", "file_size": 12, "file_hash": None}},
        {"type": "file", "data": {"file_id": "/g", "file_name": "b.txt", "file_size": 1, "file_hash": "abc"}},
        {
            "type": "market_face",
            "data": {
                "emoji_package_id": 1,
                "emoji_id": "e",
                "key": "k",
                "summary": "[哈哈]",
                "url": "https://example.com/m",
            },
        },
        {"type": "light_app", "data": {"app_name": "com.tencent.miniapp_01", "json_payload": "{}"}},
        {"type": "xml", "data": {"service_id": 35, "xml_payload": "<msg/>"}},
        {"type": "markdown", "data": {"content": "# hi"}},
        {"type": "poke_v2", "data": {"x": 1}},
    ]
    assert incoming_segments(milky) == [
        {"type": "mention_all", "data": {}},
        {"type": "face", "data": {"face_id": "76", "is_large": False}},
        {"type": "image", "data": {"ref": "x", "url": "u", "summary": "", "sticker": True, "sha1": ""}},
        {"type": "record", "data": {"ref": "r", "url": "https://example.com/r", "duration": 3}},
        {"type": "video", "data": {"ref": "v", "url": "https://example.com/v", "duration": 9}},
        {"type": "file", "data": {"file_id": "/f", "file_name": "a.txt", "file_size": 12}},
        {"type": "file", "data": {"file_id": "/g", "file_name": "b.txt", "file_size": 1, "file_hash": "abc"}},
        {
            "type": "market_face",
            "data": {
                "emoji_package_id": 1,
                "emoji_id": "e",
                "key": "k",
                "summary": "[哈哈]",
                "url": "https://example.com/m",
            },
        },
        {"type": "light_app", "data": {"app_name": "com.tencent.miniapp_01", "json_payload": "{}"}},
        {"type": "xml", "data": {"service_id": 35, "xml_payload": "<msg/>"}},
        {"type": "markdown", "data": {"content": "# hi"}},
        seg.text("[poke_v2]"),
    ]


async def test_sends_carry_milky_segments_and_the_reply_timeout() -> None:
    qq, bot = _qq({"send_group_message": {"message_seq": 3500, "time": 1}, "send_private_message": {"message_seq": 7}})
    segments = [seg.reply(3416), seg.mention(OWNER), seg.text(" 好"), seg.image("base64://AAAA")]
    segments.append(seg.image("base64://BBBB", sticker=True))

    assert await qq.send(group_id=GROUP, user_id=OWNER, segments=segments, reply_timeout=120) == 3500
    assert await qq.send(group_id=None, user_id=OWNER, segments=[seg.text("hi")]) == 7

    assert bot.called("send_group_message") == [
        {
            "_timeout": 120,
            "group_id": GROUP,
            "message": [
                {"type": "reply", "data": {"message_seq": 3416}},
                {"type": "mention", "data": {"user_id": OWNER}},
                {"type": "text", "data": {"text": " 好"}},
                {"type": "image", "data": {"uri": "base64://AAAA", "sub_type": "normal"}},
                {"type": "image", "data": {"uri": "base64://BBBB", "sub_type": "sticker"}},
            ],
        }
    ]
    assert bot.called("send_private_message") == [
        {"_timeout": None, "user_id": OWNER, "message": [{"type": "text", "data": {"text": "hi"}}]}
    ]


async def test_a_forward_goes_out_as_one_forward_segment() -> None:
    qq, bot = _qq({"send_group_message": {"message_seq": 3501, "time": 1}})
    nodes = [Node(SELF, "测试bot", [seg.text("一")]), Node(SELF, "测试bot", [seg.text("二"), seg.image("base64://AA")])]

    assert await qq.send_forward(group_id=GROUP, user_id=OWNER, nodes=nodes, reply_timeout=60) == Sent(3501, None)
    (call,) = bot.called("send_group_message")
    assert call["_timeout"] == 60
    assert call["message"] == [
        {
            "type": "forward",
            "data": {
                "messages": [
                    {"user_id": SELF, "sender_name": "测试bot", "segments": [{"type": "text", "data": {"text": "一"}}]},
                    {
                        "user_id": SELF,
                        "sender_name": "测试bot",
                        "segments": [
                            {"type": "text", "data": {"text": "二"}},
                            {"type": "image", "data": {"uri": "base64://AA", "sub_type": "normal"}},
                        ],
                    },
                ]
            },
        }
    ]


async def test_a_fetched_forward_has_names_but_no_qq_numbers() -> None:
    qq, bot = _qq({"get_forwarded_messages": PAYLOADS["get_forwarded_messages"]})
    nodes = await qq.forward("fwd-1")
    assert bot.called("get_forwarded_messages") == [{"_timeout": None, "forward_id": "fwd-1"}]
    assert len(nodes) == 3
    assert (nodes[0].user_id, nodes[0].name, nodes[0].segments) == (
        0,
        "10000",
        [seg.text("这个帖子我打不开，所以总结不了。")],
    )


# --- events -------------------------------------------------------------------------------------------------------


async def test_a_group_message_with_a_reply_and_a_mention_of_the_bot() -> None:
    quoted = PAYLOADS["get_message"]["message"]
    event = await _delivered(PAYLOADS["message_receive"], quoted)
    qq, _ = _qq()

    msg = await qq.incoming(event)

    assert msg is not None
    assert (msg.message_id, msg.seq, msg.group_id, msg.self_id, msg.protocol) == (3416, 3416, GROUP, SELF, "milky")
    assert msg.sender == Sender(OWNER, "群主", "owner")
    assert (msg.text, msg.to_me) == ("解释一下", True)
    # The stored segments are the message as sent: the quote and the @bot stay.
    assert msg.segments == [
        {"type": "reply", "data": {"message_id": 3415}},
        {"type": "mention", "data": {"user_id": SELF, "name": "测试bot"}},
        seg.text(" 解释一下"),
    ]
    assert msg.reply is not None
    assert (msg.reply.message_id, msg.reply.sender, msg.reply.segments) == (
        3415,
        Sender(OWNER, "群主", "owner"),
        [{"type": "reply", "data": {"message_id": 3414}}, seg.text("这是无栈协程吧")],
    )


async def test_a_mention_of_the_bot_inside_the_text_is_addressed_to_it() -> None:
    raw = json.loads(json.dumps(PAYLOADS["message_receive"]))
    raw["data"]["segments"] = [
        seg.text("问一下 "),
        {"type": "mention", "data": {"user_id": SELF, "name": "测试bot"}},
        seg.text(" 在吗"),
    ]
    event = await _delivered(raw)
    qq, _ = _qq()

    msg = await qq.incoming(event)

    assert msg is not None
    assert (event.to_me, msg.to_me, msg.reply) == (False, True, None)


def test_a_group_recall_names_the_message() -> None:
    qq, _ = _qq()
    data = {"peer_id": GROUP, "sender_id": OWNER, "message_seq": 3416, "operator_id": OWNER, "display_suffix": ""}
    group = _event(
        {"event_type": "message_recall", "time": 1, "self_id": SELF, "data": {"message_scene": "group", **data}}
    )
    friend = _event(
        {"event_type": "message_recall", "time": 1, "self_id": SELF, "data": {"message_scene": "friend", **data}}
    )
    assert qq.recalled(group) == (GROUP, 3416)
    assert qq.recalled(friend) is None


_OBSERVED = [
    (
        "friend_request",
        {"initiator_id": OWNER, "initiator_uid": "u_x", "comment": "加个好友", "via": "群聊"},
        ("request.friend", None, OWNER, None, "加个好友", None),
    ),
    (
        "group_invitation",
        {"group_id": GROUP, "invitation_seq": 5, "initiator_id": OWNER},
        ("request.group.invite", GROUP, OWNER, None, "", None),
    ),
    (
        "group_join_request",
        {"group_id": GROUP, "notification_seq": 9, "is_filtered": True, "initiator_id": MEMBER, "comment": "想进群"},
        ("request.group.add", GROUP, MEMBER, None, "想进群", None),
    ),
    (
        "group_invited_join_request",
        {"group_id": GROUP, "notification_seq": 10, "initiator_id": OWNER, "target_user_id": MEMBER},
        ("request.group.add", GROUP, MEMBER, OWNER, "", None),
    ),
    (
        "group_member_increase",
        {"group_id": GROUP, "user_id": SELF, "invitor_id": OWNER},
        ("notice.group_increase.self", GROUP, SELF, OWNER, "", None),
    ),
    (
        "group_member_increase",
        {"group_id": GROUP, "user_id": MEMBER, "invitor_id": OWNER},
        ("notice.group_increase.invite", GROUP, MEMBER, OWNER, "", None),
    ),
    (
        "group_member_increase",
        {"group_id": GROUP, "user_id": MEMBER, "operator_id": OWNER},
        ("notice.group_increase.approve", GROUP, MEMBER, OWNER, "", None),
    ),
    (
        "group_member_decrease",
        {"group_id": GROUP, "user_id": SELF, "operator_id": OWNER},
        ("notice.group_decrease.kick_me", GROUP, SELF, OWNER, "", None),
    ),
    (
        "group_member_decrease",
        {"group_id": GROUP, "user_id": MEMBER, "operator_id": OWNER},
        ("notice.group_decrease.kick", GROUP, MEMBER, OWNER, "", None),
    ),
    (
        "group_member_decrease",
        {"group_id": GROUP, "user_id": MEMBER},
        ("notice.group_decrease.leave", GROUP, MEMBER, None, "", None),
    ),
    (
        "group_mute",
        {"group_id": GROUP, "user_id": SELF, "duration": 600, "operator_id": OWNER},
        ("notice.group_ban.self.ban", GROUP, SELF, OWNER, "", 600),
    ),
    (
        "group_mute",
        {"group_id": GROUP, "user_id": MEMBER, "duration": 0, "operator_id": OWNER},
        ("notice.group_ban.member.lift_ban", GROUP, MEMBER, OWNER, "", 0),
    ),
    (
        "group_whole_mute",
        {"group_id": GROUP, "operator_id": OWNER, "is_mute": True},
        ("notice.group_ban.all.ban", GROUP, 0, OWNER, "", None),
    ),
    (
        "group_admin_change",
        {"group_id": GROUP, "user_id": SELF, "operator_id": OWNER, "is_set": True},
        ("notice.group_admin.self.set", GROUP, SELF, OWNER, "", None),
    ),
    (
        "bot_offline",
        {"reason": "账号在其他设备登录"},
        ("notice.bot_offline", None, SELF, None, "账号在其他设备登录", None),
    ),
]


@pytest.mark.parametrize(("event_type", "data", "expected"), _OBSERVED)
def test_requests_and_notices_are_observed_with_their_details(
    event_type: str, data: dict[str, Any], expected: tuple[Any, ...]
) -> None:
    qq, _ = _qq()
    raw = {"event_type": event_type, "time": 1793323582, "self_id": SELF, "data": data}

    o = qq.observe(_event(raw))

    assert o is not None
    assert (o.kind, o.group_id, o.user_id, o.operator_id, o.comment, o.duration) == expected
    assert o.protocol == "milky"
    assert o.data["event_type"] == event_type
    assert o.data["data"].items() >= data.items()


def test_messages_and_other_notices_are_not_observed() -> None:
    qq, _ = _qq()
    nudge = {
        "group_id": GROUP,
        "sender_id": OWNER,
        "receiver_id": SELF,
        "display_action": "戳了戳",
        "display_suffix": "",
    }
    nudge["display_action_img_url"] = ""
    assert qq.observe(_event(PAYLOADS["message_receive"])) is None
    assert qq.observe(_event({"event_type": "group_nudge", "time": 1, "self_id": SELF, "data": nudge})) is None


async def test_requests_are_answered_with_what_their_event_carried() -> None:
    answers = {
        a: {}
        for a in (
            "accept_friend_request",
            "reject_friend_request",
            "reject_group_request",
            "accept_group_request",
            "accept_group_invitation",
        )
    }
    qq, bot = _qq(answers)

    def observed(event_type: str) -> Observed:
        data = next(d for t, d, _ in _OBSERVED if t == event_type)
        o = qq.observe(_event({"event_type": event_type, "time": 1, "self_id": SELF, "data": data}))
        assert o is not None
        return o

    await qq.decide_request(observed("friend_request"), approve=True)
    await qq.decide_request(observed("friend_request"), approve=False, reason="不认识")
    await qq.decide_request(observed("group_join_request"), approve=False, reason="满了")
    await qq.decide_request(observed("group_invited_join_request"), approve=True, reason="ignored")
    await qq.decide_request(observed("group_invitation"), approve=True)

    assert bot.calls == [
        ("accept_friend_request", {"_timeout": None, "initiator_uid": "u_x", "is_filtered": False}),
        ("reject_friend_request", {"_timeout": None, "initiator_uid": "u_x", "is_filtered": False, "reason": "不认识"}),
        (
            "reject_group_request",
            {
                "_timeout": None,
                "notification_seq": 9,
                "notification_type": "join_request",
                "group_id": GROUP,
                "is_filtered": True,
                "reason": "满了",
            },
        ),
        (
            "accept_group_request",
            {
                "_timeout": None,
                "notification_seq": 10,
                "notification_type": "invited_join_request",
                "group_id": GROUP,
                "is_filtered": False,
            },
        ),
        ("accept_group_invitation", {"_timeout": None, "group_id": GROUP, "invitation_seq": 5}),
    ]

    with pytest.raises(QQError):
        await qq.decide_request(Observed("request.friend", "onebot", data={"flag": "x"}), approve=True)
    assert len(bot.calls) == 5


# --- errors -------------------------------------------------------------------------------------------------------


def _failed(retcode: int, message: str) -> ActionFailed:
    return ActionFailed(status="failed", retcode=retcode, message=message)


@pytest.mark.parametrize(
    ("group_id", "answer", "error"),
    [
        (GROUP, {"message_seq": 0, "time": 1}, Dropped),
        (None, {"message_seq": 0, "time": 1}, Unconfirmed),
        (GROUP, _failed(-500, "Internal error: Timed out waiting for 10000 ms"), Unconfirmed),
        (GROUP, NoAnswer("no answer to send_group_message within 30.0s"), Unconfirmed),
        (GROUP, _failed(-404, "Group not found"), QQError),
        (None, InvalidAccessToken("401"), QQError),
    ],
)
async def test_send_failures_become_dropped_unconfirmed_or_plain_errors(
    group_id: int | None, answer: Any, error: type[QQError]
) -> None:
    qq, _ = _qq({"send_group_message": answer, "send_private_message": answer})
    with pytest.raises(QQError) as caught:
        await qq.send(group_id=group_id, user_id=OWNER, segments=[seg.text("x")])
    assert type(caught.value) is error


@pytest.mark.parametrize(
    "answer",
    [
        _failed(-404, "Message not found"),
        _failed(-500, "Internal error: Timed out waiting for 10000 ms"),
        NoAnswer("no answer"),
        NetworkError("HTTP request failed: ConnectError()"),
    ],
)
async def test_other_actions_fail_plainly(answer: Exception) -> None:
    qq, _ = _qq({"get_message": answer})
    with pytest.raises(QQError) as caught:
        await qq.message(group_id=GROUP, user_id=OWNER, message_id=1)
    assert type(caught.value) is QQError


async def test_messages_reactions_and_recalls_address_the_chat_by_seq() -> None:
    answers = {
        "get_message": PAYLOADS["get_message"],
        "send_group_message_reaction": {},
        "recall_group_message": {},
        "recall_private_message": {},
    }
    qq, bot = _qq(answers)

    quoted = await qq.message(group_id=GROUP, user_id=OWNER, message_id=3415)
    await qq.react(group_id=GROUP, message_id=3415, emoji="76")
    await qq.react(group_id=GROUP, message_id=3415, emoji="127881")  # 🎉: an emoji, not a QQ face
    await qq.recall(group_id=GROUP, user_id=OWNER, message_id=3415)
    await qq.recall(group_id=None, user_id=OWNER, message_id=12)

    assert quoted.message_id == 3415
    assert [(a, {k: v for k, v in d.items() if k != "_timeout"}) for a, d in bot.calls] == [
        ("get_message", {"message_scene": "group", "peer_id": GROUP, "message_seq": 3415}),
        (
            "send_group_message_reaction",
            {"group_id": GROUP, "message_seq": 3415, "reaction": "76", "reaction_type": "face"},
        ),
        (
            "send_group_message_reaction",
            {"group_id": GROUP, "message_seq": 3415, "reaction": "127881", "reaction_type": "emoji"},
        ),
        ("recall_group_message", {"group_id": GROUP, "message_seq": 3415}),
        ("recall_private_message", {"user_id": OWNER, "message_seq": 12}),
    ]


async def test_download_links_come_from_the_matching_action() -> None:
    answers = {
        "get_resource_temp_url": {"url": "https://example.com/fresh"},
        "get_group_file_download_url": {"download_url": "https://example.com/g"},
        "get_private_file_download_url": {"download_url": "https://example.com/p"},
    }
    qq, bot = _qq(answers)
    picture = PAYLOADS["get_history_messages"]["messages"][1]["segments"][0]["data"]
    # A picture OneBot recorded: its ref is SnowLuma's file name, the link carries the NT file id.
    onebot_picture = seg.image("")
    onebot_picture["data"] = {"ref": "abc.png", "url": picture["temp_url"], "summary": "", "sticker": False, "sha1": ""}

    assert await qq.media_url(onebot_picture, group_id=GROUP, user_id=OWNER) == "https://example.com/fresh"
    file = {"type": "file", "data": {"file_id": "/f", "file_name": "a.txt", "file_size": 1}}
    assert await qq.media_url(file, group_id=GROUP, user_id=OWNER) == "https://example.com/g"
    hashed = {"type": "file", "data": {**file["data"], "file_hash": "abc"}}
    assert await qq.media_url(hashed, group_id=None, user_id=OWNER) == "https://example.com/p"
    with pytest.raises(QQError):
        await qq.media_url(file, group_id=None, user_id=OWNER)
    with pytest.raises(QQError):
        await qq.media_url(seg.text("x"), group_id=GROUP, user_id=OWNER)

    assert [(a, {k: v for k, v in d.items() if k != "_timeout"}) for a, d in bot.calls] == [
        ("get_resource_temp_url", {"resource_id": picture["resource_id"]}),
        ("get_group_file_download_url", {"group_id": GROUP, "file_id": "/f"}),
        ("get_private_file_download_url", {"user_id": OWNER, "file_id": "/f", "file_hash": "abc"}),
    ]


# --- group info and files ----------------------------------------------------------------------------------------


def _member(**fields: Any) -> dict[str, Any]:
    base = {
        "user_id": MEMBER,
        "nickname": "ming",
        "sex": "unknown",
        "group_id": GROUP,
        "card": "阿明",
        "title": "摸鱼王",
        "level": 42,
        "role": "admin",
        "join_time": 1700000000,
        "last_sent_time": 1760000000,
    }
    return {"member": {**base, **fields}}


async def test_member_info_carries_title_level_times_and_a_running_mute() -> None:
    muted, unmuted, expired = _member(shut_up_end_time=4102444800), _member(), _member(shut_up_end_time=1600000000)
    answers = iter([muted, unmuted, expired, _member(shut_up_end_time=0, join_time=0, last_sent_time=0)])
    qq, bot = _qq({"get_group_member_info": lambda **_: next(answers)})

    member = await qq.member(GROUP, MEMBER)
    assert (member.card, member.role, member.title, member.level) == ("阿明", "admin", "摸鱼王", 42)
    assert (member.joined_at, member.last_spoke_at, member.muted_until) == (1700000000, 1760000000, 4102444800)
    assert (await qq.member(GROUP, MEMBER)).muted_until is None
    assert (await qq.member(GROUP, MEMBER)).muted_until is None
    never = await qq.member(GROUP, MEMBER)
    assert (never.joined_at, never.last_spoke_at, never.muted_until) == (None, None, None)
    assert bot.called("get_group_member_info")[0] == {"group_id": GROUP, "user_id": MEMBER, "_timeout": None}


async def test_announcements_come_newest_first_with_their_picture() -> None:
    def notice(announcement_id: str, at: int, content: str, image_url: str | None = None) -> dict[str, Any]:
        out = {"group_id": GROUP, "announcement_id": announcement_id, "user_id": OWNER, "time": at, "content": content}
        return out if image_url is None else {**out, "image_url": image_url}

    answer = {
        "announcements": [
            notice("a-old", 1750000000, "群规：别刷屏"),
            notice("a-new", 1760000000, "周五开黑", "https://example.com/notice.jpg"),
        ]
    }
    qq, bot = _qq({"get_group_announcements": answer})

    assert await qq.announcements(GROUP) == [
        Announcement("a-new", OWNER, 1760000000, "周五开黑", ("https://example.com/notice.jpg",)),
        Announcement("a-old", OWNER, 1750000000, "群规：别刷屏"),
    ]
    assert bot.called("get_group_announcements") == [{"group_id": GROUP, "_timeout": None}]


def _essence(seq: int, sent: int, set_at: int, segments: list[Any]) -> dict[str, Any]:
    return {
        "group_id": GROUP,
        "message_seq": seq,
        "message_time": sent,
        "sender_id": MEMBER,
        "sender_name": "阿明",
        "operator_id": OWNER,
        "operator_name": "群主",
        "operation_time": set_at,
        "segments": segments,
    }


async def test_essence_pages_until_the_limit_and_comes_newest_first_in_the_schema() -> None:
    picture = {
        "type": "image",
        "data": {
            "resource_id": "EhQ-res",
            "temp_url": "https://example.com/essence.jpg",
            "width": 640,
            "height": 480,
            "summary": "[图片]",
            "sub_type": "normal",
        },
    }
    pages = [
        {"messages": [_essence(301, 1750000000, 1750000100, [seg.text("名言")])], "is_end": False},
        {"messages": [_essence(305, 1760000000, 1760000100, [picture])], "is_end": False},
        {"messages": [_essence(299, 1740000000, 1740000100, [])], "is_end": True},
    ]
    qq, bot = _qq({"get_group_essence_messages": lambda page_index, **_: pages[page_index]})

    two = await qq.essence(GROUP, limit=2)
    assert [(e.message_id, e.user_id, e.sender_name, e.time, e.operator_id, e.operator_time) for e in two] == [
        (305, MEMBER, "阿明", 1760000000, OWNER, 1760000100),
        (301, MEMBER, "阿明", 1750000000, OWNER, 1750000100),
    ]
    assert two[0].segments == incoming_segments([picture])
    assert two[0].segments[0]["data"]["url"] == "https://example.com/essence.jpg"
    assert two[1].segments == [seg.text("名言")]
    assert [(d["page_index"], d["page_size"]) for d in bot.called("get_group_essence_messages")] == [(0, 2), (1, 2)]

    bot.calls.clear()
    assert [e.message_id for e in await qq.essence(GROUP, limit=10)] == [305, 301, 299]
    assert [d["page_index"] for d in bot.called("get_group_essence_messages")] == [0, 1, 2]


async def test_group_files_list_a_folder_and_uploads_go_inline_as_base64() -> None:
    listing = {
        "files": [
            {
                "group_id": GROUP,
                "file_id": "/f-0001",
                "file_name": "规则.pdf",
                "parent_folder_id": "/",
                "file_size": 18432,
                "uploaded_time": 1750000000,
                "uploader_id": OWNER,
                "downloaded_times": 7,
            },
            {
                "group_id": GROUP,
                "file_id": "/f-0002",
                "file_name": "temp.zip",
                "parent_folder_id": "/",
                "file_size": 1024,
                "uploaded_time": 1760000000,
                "expire_time": 1760600000,
                "uploader_id": MEMBER,
                "downloaded_times": 0,
            },
        ],
        "folders": [
            {
                "group_id": GROUP,
                "folder_id": "/d-0001",
                "parent_folder_id": "/",
                "folder_name": "资料",
                "created_time": 1700000000,
                "last_modified_time": 1750000000,
                "creator_id": OWNER,
                "file_count": 3,
            }
        ],
    }
    answers = {
        "get_group_files": listing,
        "upload_group_file": {"file_id": "/f-0003"},
        "send_group_nudge": {},
    }
    qq, bot = _qq(answers)

    root = await qq.group_files(GROUP)
    await qq.group_files(GROUP, folder_id="/d-0001")
    assert root == GroupFiles(
        [
            GroupFile("/f-0001", "规则.pdf", 18432, 1750000000, OWNER, downloads=7),
            GroupFile("/f-0002", "temp.zip", 1024, 1760000000, MEMBER, downloads=0, expires_at=1760600000),
        ],
        [GroupFolder("/d-0001", "资料", 3)],
    )
    assert await qq.upload_group_file(GROUP, data=b"hello\x00world", name="a.bin") == "/f-0003"
    await qq.upload_group_file(GROUP, data=b"x", name="b.txt", folder_id="/d-0001")
    await qq.nudge(group_id=GROUP, user_id=MEMBER)

    assert [(a, {k: v for k, v in d.items() if k != "_timeout"}) for a, d in bot.calls] == [
        ("get_group_files", {"group_id": GROUP, "parent_folder_id": "/"}),
        ("get_group_files", {"group_id": GROUP, "parent_folder_id": "/d-0001"}),
        (
            "upload_group_file",
            {"group_id": GROUP, "parent_folder_id": "/", "file_uri": "base64://aGVsbG8Ad29ybGQ=", "file_name": "a.bin"},
        ),
        (
            "upload_group_file",
            {"group_id": GROUP, "parent_folder_id": "/d-0001", "file_uri": "base64://eA==", "file_name": "b.txt"},
        ),
        ("send_group_nudge", {"group_id": GROUP, "user_id": MEMBER}),
    ]


async def test_a_failed_upload_is_a_plain_error() -> None:
    qq, _ = _qq({"upload_group_file": _failed(-500, "upload failed: file too large")})
    with pytest.raises(QQError, match="file too large") as caught:
        await qq.upload_group_file(GROUP, data=b"x", name="big.bin")
    assert type(caught.value) is QQError


# --- the adapter's raw actions -------------------------------------------------------------------------------------


async def test_raw_actions_post_the_params_as_they_are_and_wait_as_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = Adapter(nonebot.get_driver())
    bot = MilkyBot(adapter, str(SELF), ClientInfo(host="yogurt.example", port=3000, access_token="t0ken"))
    sent: list[Request] = []
    answers: list[Response | Exception] = [
        Response(200, content=b'{"status":"ok","retcode":0,"data":{"message_seq":3600,"time":1}}'),
        httpx.ReadTimeout("slow"),
        Response(200, content=b'{"status":"failed","retcode":-404,"message":"Group not found"}'),
    ]

    async def request(setup: Request) -> Response:
        sent.append(setup)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(adapter, "request", request)
    message = [{"type": "text", "data": {"text": "hi"}}]

    data = await bot.call_api("send_group_message", _raw=True, _timeout=120, group_id=GROUP, message=message)
    with pytest.raises(NoAnswer):
        await bot.call_api("get_login_info", _raw=True, _timeout=None)
    with pytest.raises(ActionFailed):
        await bot.call_api("get_group_info", _raw=True, group_id=GROUP)

    assert data == {"message_seq": 3600, "time": 1}
    first = sent[0]
    assert str(first.url) == "http://yogurt.example:3000/api/send_group_message"
    assert first.headers["Authorization"] == "Bearer t0ken"
    assert json.loads(cast(str, first.content)) == {"group_id": GROUP, "message": message}
    assert first.timeout == 120
    assert sent[1].timeout == nonebot.get_driver().config.api_timeout
