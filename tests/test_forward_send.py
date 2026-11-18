"""Sending the reply forward when QQ doesn't confirm it in time or rejects it."""

import time
from typing import Any

import nonebot
import pytest

from tests.support.groups import fresh_group
from tests.support.qq import FakeQQ
from yggdrasil.agent.schema import BotReply, Section, SourceRef
from yggdrasil.delivery import forward, send
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.qq import segments as seg
from yggdrasil.qq.model import Dropped, ForwardNode, Message, Node, QQError, Sender, Sent, Unconfirmed
from yggdrasil.qq.segments import Segment
from yggdrasil.settings import InteractionSettings

GROUP = fresh_group()
SELF = 10000
FORWARD_ID = 41
TEXT_ID = 42

REPLY = BotReply(
    mode="forward",
    title="解读",
    sections=[Section(content="**第一段**正文"), Section(content="第二段")],
    sources=[SourceRef(label="gist", url="https://example.com/gist")],
)


def _qq(
    *,
    forward: Exception | Sent | None = None,
    history: list[Message] | Exception,
    posted: dict[str, str | Exception] | None = None,
    text_errors: list[Exception] | None = None,
) -> FakeQQ:
    """QQ as the send sees it: a forward goes out as ``FORWARD_ID`` (forward id "res") unless ``forward`` says
    otherwise; the group's latest history page is ``history``; ``posted`` maps the id of each forward there to its
    first node's text; text sends fail with ``text_errors`` in order, then go out as ``TEXT_ID``."""
    posted = posted or {}
    errors = list(text_errors or [])

    def forward_nodes(*, forward_id: str) -> list[ForwardNode] | Exception:
        first = posted[forward_id]
        if isinstance(first, Exception):
            return first
        return [ForwardNode(SELF, "Yggdrasil", 0, [seg.text(first)])]

    def send_text(**_: Any) -> int | Exception:
        return errors.pop(0) if errors else TEXT_ID

    return FakeQQ(
        {
            "send_forward": forward or Sent(FORWARD_ID, "res"),
            "history": history,
            "forward": forward_nodes,
            "send": send_text,
        },
        self_id=SELF,
    )


def _forwards(qq: FakeQQ) -> list[list[Node]]:
    return [call["nodes"] for call in qq.called("send_forward")]


def _texts(qq: FakeQQ) -> list[list[Segment]]:
    return [call["segments"] for call in qq.called("send")]


def _msg(user_id: int, ago: float, segment: Segment, message_id: int = 7) -> Message:
    return Message(message_id, GROUP, Sender(user_id, ""), int(time.time() - ago), [segment])


def _forward_seg(forward_id: str) -> Segment:
    return {"type": "forward", "data": {"forward_id": forward_id, "title": "", "preview": [], "summary": ""}}


TIMEOUT = Unconfirmed("no answer within 120s")
OLD_CHAT = _msg(20001, 600, seg.text("早些时候的话"), message_id=5)
OURS = "第一段正文"  # first node of REPLY as sent (markdown stripped)


def _forward(forward_id: str, message_id: int) -> Message:
    return _msg(SELF, 2, _forward_seg(forward_id), message_id=message_id)


@pytest.fixture(autouse=True)
def _no_check_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forward, "FORWARD_CHECK_DELAYS_S", (0.0, 0.0))


async def _send(qq: FakeQQ, reply: BotReply = REPLY, cfg: InteractionSettings | None = None) -> int | None:
    return await forward.send_forward(
        qq,
        group_id=GROUP,
        user_id=20001,
        reply=reply,
        meta_text="⚙ 元数据",
        cfg=cfg or InteractionSettings(),
        self_id=SELF,
        nickname="Yggdrasil",
    )


async def test_forwards_wait_longer_than_the_adapter_timeout(recorded: list[IncomingMessage]):
    qq = _qq(history=[])
    assert await _send(qq) == FORWARD_ID
    [call] = qq.called("send_forward")
    assert call["reply_timeout"] > nonebot.get_driver().config.api_timeout
    assert [m.qq_message_id for m in recorded] == [FORWARD_ID]


@pytest.mark.parametrize("fetched", ["fwd-1", None])
async def test_a_forward_id_the_send_didnt_return_is_read_back(recorded: list[IncomingMessage], fetched: str | None):
    # Milky answers a forward send with the message seq only: the record learns the forward's id from the sent
    # message, and keeps the nodes either way.
    qq = _qq(forward=Sent(FORWARD_ID), history=[])
    qq.answers["message"] = (
        _msg(SELF, 0, _forward_seg(fetched), message_id=FORWARD_ID) if fetched else QQError("message not found")
    )
    assert await _send(qq) == FORWARD_ID
    [call] = qq.called("message")
    assert (call["group_id"], call["message_id"]) == (GROUP, FORWARD_ID)
    [record] = recorded
    data = record.segments[0]["data"]
    assert data["forward_id"] == (fetched or "")
    assert [n["segments"][0]["data"]["text"] for n in data["nodes"]][:2] == [OURS, "第二段"]


async def test_unconfirmed_forward_found_in_history_counts_as_sent(recorded: list[IncomingMessage]):
    # QQ posted the forward after the call timed out (2c567dde): it's the bot's reply, nothing is sent again.
    qq = _qq(forward=TIMEOUT, history=[OLD_CHAT, _forward("late", 99)], posted={"late": OURS})
    assert await _send(qq) == 99
    assert len(_forwards(qq)) == 1
    assert _texts(qq) == []
    [record] = recorded
    assert record.qq_message_id == 99
    assert record.segments[0]["data"]["forward_id"] == "late"
    assert "第二段" in str(record.segments[0]["data"]["nodes"])


async def test_another_runs_forward_is_not_taken_for_ours(recorded: list[IncomingMessage]):
    # Another answer posted in the same window: ours is the one opening with our first node.
    qq = _qq(
        forward=TIMEOUT,
        history=[OLD_CHAT, _forward("other", 98), _forward("late", 99)],
        posted={"other": "别的回答", "late": OURS},
    )
    assert await _send(qq) == 99
    assert [m.qq_message_id for m in recorded] == [99]

    # Only the other run's forward is there: ours never went out, so the answer goes out as text.
    recorded.clear()
    qq = _qq(forward=TIMEOUT, history=[OLD_CHAT, _forward("other", 98)], posted={"other": "别的回答"})
    assert await _send(qq) == TEXT_ID
    assert len(_texts(qq)) == 1
    assert [m.qq_message_id for m in recorded] == [TEXT_ID]


DROPPED = Dropped("group message delivery was not confirmed: QQ returned no message receipt")


async def test_a_forward_qq_drops_gets_a_notice_not_a_text_copy(recorded: list[IncomingMessage]):
    # 2026-11-01: QQ answered without a group sequence and posted nothing; the text copy of the same answer was
    # dropped the same way, so the run failed and the group saw nothing at all.
    qq = _qq(forward=DROPPED, history=[OLD_CHAT, _forward("late", 99)], posted={"late": OURS})
    with pytest.raises(send.MessageDropped):
        await _send(qq)
    assert len(_forwards(qq)) == 1
    assert [m[0]["data"]["text"] for m in _texts(qq)] == [send.DROPPED_NOTICE]
    assert [m.qq_message_id for m in recorded] == [TEXT_ID]


async def test_a_text_reply_qq_drops_gets_a_notice(recorded: list[IncomingMessage]):
    qq = _qq(history=[], text_errors=[DROPPED])
    with pytest.raises(send.MessageDropped):
        await send.send_text(qq, group_id=GROUP, user_id=20001, reply_to=None, text="答案", self_id=SELF, nickname="Y")
    assert [m[0]["data"]["text"] for m in _texts(qq)] == ["答案", send.DROPPED_NOTICE]
    assert [m.qq_message_id for m in recorded] == [TEXT_ID]


@pytest.mark.parametrize("history", ["unavailable", "busy"])
async def test_unconfirmed_forward_the_history_cant_rule_out_is_not_sent_again(
    recorded: list[IncomingMessage], history: str
):
    # Built here, not at collection: "newer than the send" must hold when the test runs.
    page: list[Message] | Exception = (
        QQError("history unavailable")
        if history == "unavailable"
        # Busy group: the whole page is newer than the send, so the forward may be further back.
        else [_msg(20001, -5, seg.text(f"刷屏{i}"), message_id=i) for i in range(3)]
    )
    qq = _qq(forward=TIMEOUT, history=page)
    assert await _send(qq) is None
    assert len(_forwards(qq)) == 1
    assert _texts(qq) == []
    assert recorded == []


async def test_bot_forward_that_cant_be_checked_is_not_sent_again(recorded: list[IncomingMessage]):
    qq = _qq(forward=TIMEOUT, history=[OLD_CHAT, _forward("late", 99)], posted={"late": QQError("forward gone")})
    assert await _send(qq) is None
    assert _texts(qq) == []
    assert recorded == []


async def test_forward_with_pictures_missing_from_history_is_not_sent_as_text(recorded: list[IncomingMessage]):
    # Its pictures may still be uploading: a text copy now could be followed by the forward.
    qq = _qq(forward=TIMEOUT, history=[OLD_CHAT])
    result = await forward.send_forward(
        qq,
        group_id=GROUP,
        user_id=20001,
        reply=REPLY.model_copy(update={"sections": [Section(content="第一段", images=["img#1"])]}),
        meta_text=None,
        cfg=InteractionSettings(),
        self_id=SELF,
        nickname="Yggdrasil",
        images={"img#1": (b"\x89PNG", "图")},
    )
    assert result is None
    assert len(_forwards(qq)) == 1
    assert _texts(qq) == []


async def test_forward_missing_from_history_falls_back_to_text_once(recorded: list[IncomingMessage]):
    qq = _qq(forward=TIMEOUT, history=[OLD_CHAT])
    assert await _send(qq) == TEXT_ID
    assert len(_forwards(qq)) == 1
    [message] = _texts(qq)
    text = message[0]["data"]["text"]
    assert text.startswith("解读\n\n第一段正文\n\n第二段")  # plain text, markdown stripped
    assert "https://example.com/gist" in text
    assert "元数据" not in text
    assert [m.qq_message_id for m in recorded] == [TEXT_ID]


async def test_rejected_forward_falls_back_to_one_text_within_the_length_limit(recorded: list[IncomingMessage]):
    qq = _qq(forward=QQError("send group message rejected: result=120 err=禁言中"), history=[])
    long = REPLY.model_copy(update={"sections": [Section(content="长" * 150), Section(content="文" * 150)]})
    assert await _send(qq, long, InteractionSettings(node_max_chars=200)) == TEXT_ID
    assert len(_forwards(qq)) == 1
    [message] = _texts(qq)
    text = message[0]["data"]["text"]
    assert len(text) <= 200
    assert text.endswith("（后文过长，已省略）")
