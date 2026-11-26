"""Status reactions on the KOOK trigger message."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.usage import UsageLimits

from tests.support.groups import fresh_channel, fresh_guild
from tests.support.kook import FakeKook, make_incoming
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.schema import BotReply
from yggdrasil.llm.registry import ResolvedModel
from yggdrasil.moderation.guard import Verdict
from yggdrasil.pipeline import invoke
from yggdrasil.settings import Settings
from yggdrasil.triggers.index import Resolved

GUILD, CHANNEL, SELF, ALICE = fresh_guild(), fresh_channel(), "10001", "20001"
TRIGGER = "msg-trigger-7"
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
ANSWER = '{"mode": "reply", "sections": [{"content": "答案"}]}'
SILENT = '{"mode": "silent", "silent_reason": "不是在问我"}'


class Run:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: str, *, think_s: float = 0) -> None:
        self.answer = answer
        self.think_s = think_s
        self.reactions: list[str] = []
        self.delivered: list[BotReply] = []

        async def moderate(stage: str, *_a: Any, **_k: Any) -> Verdict:
            return Verdict(stage, "Skipped", 0.0)

        async def react(_kook: Any, _channel_id: str | None, _message_id: str, emoji: str) -> None:
            self.reactions.append(emoji)

        async def deliver(_inv: invoke.Invocator, reply: BotReply) -> None:
            self.delivered.append(reply)

        async def add_tokens(*_a: Any) -> None:
            return None

        monkeypatch.setattr(invoke, "moderate", moderate)
        monkeypatch.setattr(invoke, "react", react)
        monkeypatch.setattr(invoke.Invocator, "deliver", deliver)
        monkeypatch.setattr(invoke.ratelimit, "add_tokens", add_tokens)

    async def _stream(self, _messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        await asyncio.sleep(self.think_s)
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=self.answer)}

    def _never(self, _messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise AssertionError("agents stream")

    async def go(self, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False, slow_after_s: float = 30) -> str:
        m = make_incoming(
            "问个问题", channel_id=CHANNEL, guild_id=GUILD, user_id=ALICE, self_id=SELF, message_id=TRIGGER, to_me=True
        )
        settings = Settings()
        settings.interaction = settings.interaction.model_copy(update={"slow_after_s": slow_after_s})
        kook = FakeKook(bot_id=SELF)
        inv = invoke.Invocator(kook, m, CHAT, "chat", "", settings, CHANNEL, GUILD)
        model = FunctionModel(self._never, stream_function=self._stream)
        agent = Agent(model, output_type=BotReply, deps_type=Deps)
        deps = Deps(kook, CHANNEL, GUILD, ALICE, settings, Handles(), "chat")
        resolved = ResolvedModel("chat", model, {}, UsageLimits(), "test")

        async def _run(_inv: invoke.Invocator) -> str:
            if fail:
                raise RuntimeError("boom")
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

    failed = Run(monkeypatch, "")
    assert await failed.go(monkeypatch, fail=True) == "failed"
    assert failed.reactions == [cfg.received_emoji, cfg.failure_emoji]


async def test_a_run_still_working_after_slow_after_s_says_so_once(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings().interaction
    slow = Run(monkeypatch, ANSWER, think_s=0.3)
    assert await slow.go(monkeypatch, slow_after_s=0.05) == "ok"
    assert slow.reactions == [cfg.received_emoji, cfg.slow_emoji]

    quick = Run(monkeypatch, ANSWER)
    assert await quick.go(monkeypatch, slow_after_s=0.05) == "ok"
    await asyncio.sleep(0.1)
    assert quick.reactions == [cfg.received_emoji]
