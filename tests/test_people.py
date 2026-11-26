"""People labels, mentions, and KOOK context building."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.support.kook import make_incoming
from yggdrasil.agent import context
from yggdrasil.agent.deps import Handles
from yggdrasil.ingest.normalize import normalize
from yggdrasil.kook import segments as seg
from yggdrasil.kook.model import Incoming, Message, Sender
from yggdrasil.kook.segments import Segment
from yggdrasil.memory import search
from yggdrasil.memory.search import MessageView
from yggdrasil.people import People, label, mentioned_ids, self_label
from yggdrasil.settings import ContextSettings, Settings
from yggdrasil.triggers.index import Resolved

SELF = "1000000001"
FRIEND = "1000000002"
GUILD = "g-100"
CHANNEL = "ch-100"
PEOPLE = People("耶梦加得", {FRIEND: "阿明"}, frozenset({SELF}))


def test_label_edge_cases() -> None:
    assert label("阿明", FRIEND) == "阿明(1000000002)"
    assert label(None, "12345") == "成员(12345)"
    assert label("  ", "12345") == "成员(12345)"
    assert label('小(明)（二）<b>"x"\n换行 名', "12345") == "小明二bx换行 名(12345)"
    assert self_label("耶梦加得") == "耶梦加得(你)"


def test_who() -> None:
    assert PEOPLE.who(FRIEND, "旧名片") == "阿明(1000000002)"
    assert PEOPLE.who("10001", "新人") == "新人(10001)"
    assert PEOPLE.who("10001") == "成员(10001)"
    assert PEOPLE.who(SELF, "bot 昵称") == "耶梦加得(你)"
    assert PEOPLE.who("99999", "冒充", is_self=True) == "耶梦加得(你)"


def test_mention_rewrite() -> None:
    text = f"@{SELF} 总结一下 @{FRIEND} 和 @10001 还有 @老名字 @全体成员 mail 1@12345.com"
    assert PEOPLE.mentions(text) == (
        "@耶梦加得(你) 总结一下 @阿明(1000000002) 和 @成员(10001) 还有 @老名字 @全体成员 mail 1@12345.com"
    )
    assert PEOPLE.mentions("@阿明(1000000002) @1234") == "@阿明(1000000002) @1234"
    assert mentioned_ids([f"@{FRIEND} hi @{SELF}", "@老名字"]) == {FRIEND, SELF}


def test_normalize_mentions() -> None:
    segs: list[dict[str, Any]] = [
        seg.mention(SELF),
        seg.text(" 看看"),
        seg.mention(FRIEND),
        seg.mention_all(),
    ]
    assert normalize(segs).text == f"@{SELF}  看看@{FRIEND} @全体成员"
    assert normalize(segs, PEOPLE.mention).text == "@耶梦加得(你) 看看@阿明(1000000002) @全体成员"


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(guild_id: str | None, user_ids: Any, *, bot_name: str, self_id: str | None, texts: Any) -> People:
        assert set(user_ids) >= {FRIEND, SELF}
        return People(bot_name, {FRIEND: "阿明"}, frozenset({self_id} if self_id else ()))

    async def nothing(*_: Any, **__: Any) -> Any:
        return []

    async def no_profiles(*_: Any) -> dict[str, Any]:
        return {}

    async def no_row(*_: Any) -> None:
        return None

    async def no_rows(*_: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(context, "resolve", resolve)
    monkeypatch.setattr(context, "_glossary_hits", nothing)
    monkeypatch.setattr(context.WRITER, "flush", nothing)
    monkeypatch.setattr(search, "recent_messages", nothing)
    monkeypatch.setattr(search, "find_by_msg_id", no_row)
    monkeypatch.setattr(search, "find_by_msg_ids", no_rows)
    monkeypatch.setattr(search, "profiles", no_profiles)
    monkeypatch.setattr(search, "open_topics", nothing)


def _event(segs: list[Segment] | str, reply: Message | None = None, *, message_id: str = "m-1") -> Incoming:
    if isinstance(segs, str):
        return make_incoming(
            segs,
            channel_id=CHANNEL,
            guild_id=GUILD,
            user_id=FRIEND,
            self_id=SELF,
            message_id=message_id,
            username="阿明",
            to_me=True,
            reply=reply,
        )
    return make_incoming(
        normalize(segs).text,
        channel_id=CHANNEL,
        guild_id=GUILD,
        user_id=FRIEND,
        self_id=SELF,
        message_id=message_id,
        username="阿明",
        to_me=True,
        reply=reply,
        segments=segs,
    )


async def test_trigger_renders_self_mention(offline: None) -> None:
    event = _event([seg.mention(SELF), seg.text(" 总结一下")])
    resolved = Resolved("tldr", 1, "", (), "self", 0, False)
    ctx = await context.build_context(event, resolved, Settings(), self_id=SELF, bot_name="耶梦加得", request_text="")
    assert "@耶梦加得(你) 总结一下" in ctx.prompt
    assert SELF not in ctx.prompt
    assert '发送者="阿明(1000000002)"' in ctx.prompt


async def test_context_display_lists_only_the_recent_messages_the_prompt_kept(
    offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    recent = [
        MessageView(
            i,
            CHANNEL,
            GUILD,
            f"k-{i}",
            t0 + timedelta(minutes=i),
            FRIEND,
            "阿明",
            f"第{i}条{'\n续行' * (i % 3)}{'字' * 60}",
            None,
            False,
        )
        for i in range(1, 41)
    ]

    async def recent_messages(*_: Any, **__: Any) -> list[MessageView]:
        return recent

    monkeypatch.setattr(search, "recent_messages", recent_messages)
    settings = Settings(context=ContextSettings(max_context_chars=1000))
    resolved = Resolved("chat", 1, "", (), "self", 0, False)
    ctx = await context.build_context(
        _event("问个问题"), resolved, settings, self_id=SELF, bot_name="耶梦加得", request_text=""
    )
    shown = [m.id for m in recent if f"] #{m.id} " in ctx.prompt]
    assert 1 not in shown
    assert shown[-1] == 40
    assert ctx.display is not None
    assert ctx.display.recent == shown


@pytest.mark.parametrize(
    ("text", "request_block", "focus"),
    [
        (
            "https://example.com/post 这个方案和 nginx 比怎么样",
            "https://example.com/post (url#1)",
            "这个方案和 nginx 比怎么样",
        ),
        ("https://example.com/post 。", "https://example.com/post 。 (url#1)", None),
        ("这个靠谱吗", "这个靠谱吗", None),
    ],
)
async def test_words_after_a_link_in_a_tldr_are_its_focus(
    offline: None, text: str, request_block: str, focus: str | None
) -> None:
    resolved = Resolved("tldr", 1, "", (), "self", 0, False)
    ctx = await context.build_context(
        _event(f"tldr {text}"), resolved, Settings(), self_id=SELF, bot_name="耶梦加得", request_text=text
    )

    def block(tag: str) -> str | None:
        found = re.search(rf"<{tag}-{ctx.nonce}[^>]*>\n(.*?)\n</{tag}-{ctx.nonce}>", ctx.prompt, re.DOTALL)
        return found.group(1) if found else None

    assert block("请求") == request_block
    assert block("关注点") == focus
    assert ("只回答关注点" in ctx.prompt) is (focus is not None)


async def test_a_quoted_reply_brings_the_message_it_replies_to(offline: None, monkeypatch: pytest.MonkeyPatch) -> None:
    t0 = datetime(2026, 10, 30, 4, 0, tzinfo=UTC)
    remark_segs = [seg.reply("k-777"), seg.text("否定与自我否定")]
    remark = MessageView(
        73120, CHANNEL, GUILD, "k-2", t0, FRIEND, "阿明", "否定与自我否定", "k-777", False, remark_segs
    )
    orig_segs = [seg.text("很长一段讨论"), seg.image("https://img.kookapp.cn/a.png")]
    orig = MessageView(
        73117,
        CHANNEL,
        GUILD,
        "k-777",
        t0 - timedelta(minutes=6),
        "10001",
        "老王",
        "很长一段讨论 [图片]",
        None,
        False,
        orig_segs,
    )
    rows = {"k-2": remark, "k-777": orig}

    async def find_by_msg_id(_channel_id: str, kook_message_id: str) -> MessageView | None:
        return rows.get(kook_message_id)

    monkeypatch.setattr(search, "find_by_msg_id", find_by_msg_id)
    reply = Message("k-2", CHANNEL, GUILD, Sender(FRIEND, "阿明"), int(t0.timestamp()), remark_segs)
    event = _event([seg.reply("k-2"), seg.mention(SELF), seg.text(" tldr")], reply)
    resolved = Resolved("tldr", 1, "", (), "quoted", 0, False)
    ctx = await context.build_context(event, resolved, Settings(), self_id=SELF, bot_name="耶梦加得", request_text="")

    assert "#73117 老王(10001): 很长一段讨论 [图片] (img#1)" in ctx.prompt
    assert ctx.prompt.index("#73117") < ctx.prompt.index("否定与自我否定")
    assert '编号="#73120"' in ctx.prompt
    assert ctx.handles.get("img#1") is not None
    assert ctx.display is not None
    assert [m.id for m in ctx.display.chain] == [73117]


async def test_a_follow_up_prompt_has_only_the_chat_since_the_answer(
    offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = datetime(2026, 11, 4, 1, 15, tzinfo=UTC)
    answer = MessageView(86427, CHANNEL, GUILD, "k-501", t0, SELF, "耶梦加得", "回答正文", None, True)
    aside = MessageView(86428, CHANNEL, GUILD, "k-502", t0 + timedelta(seconds=10), "10001", "小红", "？", None, False)
    trigger_row = MessageView(
        86429, CHANNEL, GUILD, "k-7", t0 + timedelta(minutes=3), FRIEND, "阿明", "为什么", "k-501", False
    )
    later = MessageView(
        86430, CHANNEL, GUILD, "k-503", t0 + timedelta(minutes=4), "10001", "小红", "后来的话", None, False
    )
    asked: list[tuple[int | None, datetime]] = []

    async def messages_after(_channel_id: str, after_id: int | None, since: datetime, **_: Any) -> list[MessageView]:
        asked.append((after_id, since))
        return [answer, aside, trigger_row, later]

    monkeypatch.setattr(search, "messages_after", messages_after)
    handles = Handles()
    handles.add("image", {"url": "https://img.kookapp.cn/old.png"})
    image = seg.image("https://img.kookapp.cn/new.png")
    reply = Message("k-501", CHANNEL, GUILD, Sender(SELF, ""), int(t0.timestamp()), [])
    event = _event([seg.reply("k-501"), seg.text("为什么"), image], reply, message_id="k-7")
    ctx = await context.build_followup_context(
        event,
        Settings(),
        self_id=SELF,
        bot_name="耶梦加得",
        request_text="为什么",
        handles=handles,
        nonce="ab12cd",
        answer_msg_id="k-501",
        seen_message_id=86426,
        since=t0,
        now="2026-11-04 09:18 Wednesday",
        recall=False,
    )
    assert asked == [(86426, t0)]
    assert "<中间的新增对话-ab12cd>" in ctx.prompt
    assert "#86428 小红(10001): ？" in ctx.prompt
    for absent in ("#86427", "#86429", "后来的话"):
        assert absent not in ctx.prompt
    assert '<请求-ab12cd 发送者="阿明(1000000002)">\n为什么 (img#2)' in ctx.prompt
    assert "现在是 2026-11-04 09:18 Wednesday" in ctx.prompt
    assert ctx.focus == ["img#2"]
    assert ctx.display is not None
    assert ctx.display.recent == [86428]
