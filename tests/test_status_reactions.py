"""Status reactions on the KOOK trigger message."""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ContentFilterError
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse, ThinkingPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.usage import UsageLimits

from tests.support.agent import run_ctx
from tests.support.groups import fresh_channel, fresh_guild
from tests.support.kook import FakeKook, make_incoming, make_sender
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.schema import BotReply
from yggdrasil.agent.skills.kook import react_message
from yggdrasil.agent.tools import web as web_tools
from yggdrasil.agent.tools.web import read_url
from yggdrasil.kook import segments as seg
from yggdrasil.kook.model import Message as KookMessage
from yggdrasil.kook.parser import parse_kook_message_link
from yggdrasil.llm.errors import describe
from yggdrasil.llm.registry import ResolvedModel
from yggdrasil.memory import search as memory_search
from yggdrasil.moderation.guard import Verdict
from yggdrasil.people import People
from yggdrasil.pipeline import invoke
from yggdrasil.pipeline.prompt import instructions
from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot
from yggdrasil.settings import Settings
from yggdrasil.triggers.index import Resolved

GUILD, CHANNEL, SELF, ALICE = fresh_guild(), fresh_channel(), "10001", "20001"
TRIGGER = "msg-trigger-7"
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
ANSWER = '{"mode": "reply", "sections": [{"content": "答案"}]}'
SILENT = '{"mode": "silent", "silent_reason": "不是在问我"}'
SILENT_CUSTOM = '{"mode": "silent", "silent_reason": "收到就好", "silent_reaction": "👍"}'
SILENT_KEEP_EYE = '{"mode": "silent", "silent_reason": "围观", "silent_reaction": "👀"}'
REASON = "This request triggered restrictions on violative cyber content."


def refused() -> ContentFilterError:
    """What pydantic-ai raises for Anthropic's stop_reason=refusal: the whole response dumped as the error's body,
    the thinking signature ahead of the reason."""
    response = ModelResponse(
        parts=[ThinkingPart("想到一半", signature="s" * 3000)],
        finish_reason="content_filter",
        provider_details={"finish_reason": "refusal", "refusal": REASON, "refusal_category": "cyber"},
    )
    body = ModelMessagesTypeAdapter.dump_json([response]).decode()
    return ContentFilterError("Content filter triggered. Finish reason: 'refusal'", body=body)


class Run:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: str, *, think_s: float = 0) -> None:
        self.answer = answer
        self.think_s = think_s
        self.reactions: list[str] = []
        self.unreactions: list[str] = []
        self.delivered: list[BotReply] = []

        async def moderate(stage: str, *_a: Any, **_k: Any) -> Verdict:
            return Verdict(stage, "Skipped", 0.0)

        async def react(_kook: Any, _channel_id: str | None, _message_id: str, emoji: str) -> bool:
            self.reactions.append(emoji)
            return True

        async def unreact(_kook: Any, _channel_id: str | None, _message_id: str, emoji: str) -> bool:
            self.unreactions.append(emoji)
            return True

        async def deliver(_inv: invoke.Invocator, reply: BotReply) -> None:
            self.delivered.append(reply)

        async def add_tokens(*_a: Any) -> None:
            return None

        monkeypatch.setattr(invoke, "moderate", moderate)
        monkeypatch.setattr(invoke, "react", react)
        monkeypatch.setattr(invoke, "unreact", unreact)
        monkeypatch.setattr(invoke.Invocator, "deliver", deliver)
        monkeypatch.setattr(invoke.ratelimit, "add_tokens", add_tokens)

    async def _stream(self, _messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        await asyncio.sleep(self.think_s)
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=self.answer)}

    def _never(self, _messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise AssertionError("agents stream")

    async def go(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        fail: Exception | None = None,
        slow_after_s: float = 30,
        refusal_notice: bool = True,
        silent_emoji: str | None = None,
        silent_reaction_mode: Literal["auto", "fixed", "none"] = "auto",
    ) -> str:
        m = make_incoming(
            "问个问题", channel_id=CHANNEL, guild_id=GUILD, user_id=ALICE, self_id=SELF, message_id=TRIGGER, to_me=True
        )
        settings = Settings()
        patch: dict[str, Any] = {
            "slow_after_s": slow_after_s,
            "refusal_notice": refusal_notice,
            "silent_reaction_mode": silent_reaction_mode,
        }
        if silent_emoji is not None:
            patch["silent_emoji"] = silent_emoji
        settings.interaction = settings.interaction.model_copy(update=patch)
        kook = self.kook = FakeKook(bot_id=SELF)
        inv = invoke.Invocator(kook, m, CHAT, "chat", "", settings, CHANNEL, GUILD)
        model = FunctionModel(self._never, stream_function=self._stream)
        agent = Agent(model, output_type=BotReply, deps_type=Deps)
        deps = Deps(kook, CHANNEL, GUILD, ALICE, settings, Handles(), "chat")
        resolved = ResolvedModel("chat", model, {}, UsageLimits(), "test")

        async def _run(_inv: invoke.Invocator) -> str:
            if fail is not None:
                raise fail
            return await inv._execute(agent, "问个问题", deps, resolved, "问个问题")  # pyright: ignore[reportPrivateUsage]

        monkeypatch.setattr(invoke.Invocator, "_run", _run)
        async with asyncio.timeout(5):
            return await inv.run()


async def test_a_sent_answer_adds_only_received(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Run(monkeypatch, ANSWER)
    assert await run.go(monkeypatch) == "ok"
    assert len(run.delivered) == 1
    assert run.reactions == [Settings().interaction.received_emoji]


async def test_an_outcome_without_a_message_gets_its_reaction(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings().interaction
    silent = Run(monkeypatch, SILENT)
    assert await silent.go(monkeypatch) == "silent"
    assert silent.reactions == [cfg.received_emoji, cfg.silent_emoji]
    assert silent.unreactions == [cfg.received_emoji]

    custom = Run(monkeypatch, SILENT_CUSTOM)
    assert await custom.go(monkeypatch) == "silent"
    assert custom.reactions == [cfg.received_emoji, "👍"]
    assert custom.unreactions == [cfg.received_emoji]

    keep_eye = Run(monkeypatch, SILENT_KEEP_EYE)
    assert await keep_eye.go(monkeypatch) == "silent"
    assert keep_eye.reactions == [cfg.received_emoji]
    assert keep_eye.unreactions == []

    failed = Run(monkeypatch, "")
    assert await failed.go(monkeypatch, fail=RuntimeError("boom")) == "failed"
    assert failed.reactions == [cfg.received_emoji, cfg.failure_emoji]
    assert failed.unreactions == []


async def test_kook_skill_react_message_adds_and_removes_reactions() -> None:
    kook = FakeKook(bot_id=SELF)
    ctx = run_ctx(
        kook=kook,
        channel_id=CHANNEL,
        guild_id=GUILD,
        user_id=ALICE,
        settings=Settings(),
        trigger_message_id=TRIGGER,
        self_id=SELF,
    )
    res_add = await react_message(ctx, "🎉")
    assert "🎉" in str(res_add)
    assert kook.reactions == [(CHANNEL, TRIGGER, "🎉")]
    res_del = await react_message(ctx, "🎉", remove=True)
    assert "取消" in str(res_del)
    assert kook.unreactions == [(CHANNEL, TRIGGER, "🎉", None)]
    assert ctx.deps.actions == ["react 🎉", "unreact 🎉"]


async def test_silent_reaction_mode_distinguishes_auto_fixed_and_none(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings().interaction

    # silent_emoji="" + mode="auto": model can still choose a reaction, or leave empty for total silence
    auto_pick = Run(monkeypatch, SILENT_CUSTOM)
    assert await auto_pick.go(monkeypatch, silent_emoji="", silent_reaction_mode="auto") == "silent"
    assert auto_pick.reactions == [cfg.received_emoji, "👍"]
    assert auto_pick.unreactions == [cfg.received_emoji]

    auto_empty = Run(monkeypatch, SILENT)
    assert await auto_empty.go(monkeypatch, silent_emoji="", silent_reaction_mode="auto") == "silent"
    assert auto_empty.reactions == [cfg.received_emoji]
    assert auto_empty.unreactions == [cfg.received_emoji]

    # mode="fixed": ignores model's custom silent_reaction and uses silent_emoji only
    fixed = Run(monkeypatch, SILENT_CUSTOM)
    assert await fixed.go(monkeypatch, silent_emoji="🤫", silent_reaction_mode="fixed") == "silent"
    assert fixed.reactions == [cfg.received_emoji, "🤫"]
    assert fixed.unreactions == [cfg.received_emoji]

    # mode="none": completely silent even if model returned silent_reaction and silent_emoji is set
    none_mode = Run(monkeypatch, SILENT_CUSTOM)
    assert await none_mode.go(monkeypatch, silent_emoji="🤫", silent_reaction_mode="none") == "silent"
    assert none_mode.reactions == [cfg.received_emoji]
    assert none_mode.unreactions == [cfg.received_emoji]

    # Prompt omits silent_reaction guidance when mode != "auto"
    s_auto = Settings()
    s_none = Settings.model_validate({"interaction": {"silent_reaction_mode": "none"}})
    assert "silent_reaction" in instructions(
        "chat", s_auto, CHANNEL, guild_id=GUILD, tools=[], sandbox=None, report=False, nonce="n"
    )
    assert "silent_reaction" not in instructions(
        "chat", s_none, CHANNEL, guild_id=GUILD, tools=[], sandbox=None, report=False, nonce="n"
    )


async def test_kook_message_anchor_link_parsing_read_url_and_privacy(
    snapshot: Callable[..., Snapshot], monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = (
        "https://www.kookapp.cn/direct/anchor/2827730975763212/3776057497983448/3e514974-cced-4acd-8598-4405617efca5"
    )
    assert parse_kook_message_link(anchor) == (
        "2827730975763212",
        "3776057497983448",
        "3e514974-cced-4acd-8598-4405617efca5",
    )
    assert parse_kook_message_link("https://example.com/direct/anchor/1/2/3") is None

    gid, ch_pub, ch_other, ch_priv = "2827730975763212", "ch-pub", "3776057497983448", "ch-priv"
    mid = "3e514974-cced-4acd-8598-4405617efca5"
    snapshot(
        guilds={gid: GuildInfo(gid, "主服", "", {}, enabled=True)},
        channels={
            ch_pub: ChannelInfo(ch_pub, gid, None, False, 0, "大厅", True, {}),
            ch_other: ChannelInfo(ch_other, gid, None, False, 1, "公告", True, {}),
            ch_priv: ChannelInfo(ch_priv, gid, None, False, 2, "工单", True, {"memory": {"share_in_guild": False}}),
        },
    )

    async def _no_db(_ch: str, _mid: str) -> None:
        return None

    async def _fake_people(*_a: Any, **_k: Any) -> People:
        return People("Yggdrasil", {"u-9": "bob"}, frozenset({SELF}))

    monkeypatch.setattr(memory_search, "find_by_msg_id", _no_db)
    monkeypatch.setattr(web_tools, "people_for", _fake_people)

    kook = FakeKook(
        bot_id=SELF,
        message_map={
            mid: KookMessage(
                message_id=mid,
                channel_id=ch_other,
                guild_id=gid,
                sender=make_sender("u-9", username="bob"),
                time=1_700_000_000,
                segments=[seg.text("周六晚上八点活动"), seg.image("https://img.kookapp.cn/a.png")],
            )
        },
    )
    ctx = run_ctx(
        kook=kook,
        channel_id=ch_pub,
        guild_id=gid,
        user_id=ALICE,
        settings=Settings(),
        trigger_message_id=TRIGGER,
        self_id=SELF,
    )
    out = await read_url(ctx, anchor)
    assert "来源频道：#公告" in str(out)
    assert "周六晚上八点活动" in str(out)
    assert "img#1" in str(out)

    # react_message also accepts KOOK anchor link directly
    await react_message(ctx, "🔥", message_id=anchor)
    assert kook.reactions == [(ch_other, mid, "🔥")]

    # Private channel link is blocked from public channel
    priv_link = f"https://www.kookapp.cn/direct/anchor/{gid}/{ch_priv}/{mid}"
    assert "无权访问" in str(await read_url(ctx, priv_link))
    assert "无权操作" in str(await react_message(ctx, "🔥", message_id=priv_link))


async def test_a_run_still_working_after_slow_after_s_says_so_once(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings().interaction
    slow = Run(monkeypatch, ANSWER, think_s=0.3)
    assert await slow.go(monkeypatch, slow_after_s=0.05) == "ok"
    assert slow.reactions == [cfg.received_emoji, cfg.slow_emoji]

    quick = Run(monkeypatch, ANSWER)
    assert await quick.go(monkeypatch, slow_after_s=0.05) == "ok"
    await asyncio.sleep(0.1)
    assert quick.reactions == [cfg.received_emoji]


async def test_a_refusal_tells_the_chat_why_instead_of_a_failure_reaction(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings().interaction
    told = Run(monkeypatch, "")
    assert await told.go(monkeypatch, fail=refused()) == "failed"
    assert [m.content for m in told.kook.sent] == [f"模型拒绝了这个请求（cyber）：{REASON}"]
    assert told.reactions == [cfg.received_emoji]  # the message is the outcome

    quiet = Run(monkeypatch, "")
    assert await quiet.go(monkeypatch, fail=refused(), refusal_notice=False) == "failed"
    assert quiet.kook.sent == []
    assert quiet.reactions == [cfg.received_emoji, cfg.failure_emoji]


def test_a_recorded_error_leads_with_the_reason() -> None:
    # The first 1000 characters are what an invocation keeps.
    assert describe(refused())[:1000] == (
        "ContentFilterError: Content filter triggered. Finish reason: 'refusal'\n"
        f"refusal_category: cyber\nrefusal: {REASON}"
    )
    every_model_failed = ExceptionGroup("All models failed", [TimeoutError("primary timed out"), refused()])
    assert describe(every_model_failed).splitlines()[:2] == [
        "ExceptionGroup: All models failed",
        "- TimeoutError: primary timed out",
    ]
    assert REASON in describe(every_model_failed)
    assert describe(ValueError("plain")) == "ValueError: plain"
