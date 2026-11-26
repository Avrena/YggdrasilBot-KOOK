"""Recalling the message that asked the bot stops the run before it answers."""

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
from yggdrasil.pipeline import invoke, recalls
from yggdrasil.settings import Settings
from yggdrasil.triggers.index import Resolved

GUILD, CHANNEL, SELF, ALICE = fresh_guild(), fresh_channel(), "10001", "20001"
TRIGGER = "msg-trigger-7"
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
ANSWER = '{"mode": "reply", "sections": [{"content": "答案"}]}'


class Run:
    """A chat run whose model streams until ``release`` is set, then answers."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.streaming = asyncio.Event()
        self.release = asyncio.Event()
        self.model_cancelled = False
        self.delivered: list[BotReply] = []
        self.reactions: list[str] = []

        async def moderate(stage: str, *_a: Any, **_k: Any) -> Verdict:
            return Verdict(stage, "Skipped", 0.0)

        async def react(_kook: Any, _channel_id: str | None, _message_id: str, emoji: str) -> None:
            self.reactions.append(emoji)

        async def deliver(_inv: invoke.Invocator, reply: BotReply) -> None:
            self.delivered.append(reply)

        monkeypatch.setattr(invoke, "moderate", moderate)
        monkeypatch.setattr(invoke, "react", react)
        monkeypatch.setattr(invoke.Invocator, "deliver", deliver)

    async def stream(self, _messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        self.streaming.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.model_cancelled = True
            raise
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=ANSWER)}

    def _never(self, _messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise AssertionError("agents stream")

    async def start(self, watch: recalls.Watch) -> "asyncio.Task[str]":
        m = make_incoming(
            "问个问题", channel_id=CHANNEL, guild_id=GUILD, user_id=ALICE, self_id=SELF, message_id=TRIGGER, to_me=True
        )
        settings = Settings()
        kook = FakeKook(bot_id=SELF)
        inv = invoke.Invocator(kook, m, CHAT, "chat", "", settings, None, None, watch=watch)
        model = FunctionModel(self._never, stream_function=self.stream)
        agent = Agent(model, output_type=BotReply, deps_type=Deps)
        deps = Deps(kook, None, None, ALICE, settings, Handles(), "chat")
        resolved = ResolvedModel("chat", model, {}, UsageLimits(), "test")
        task = asyncio.create_task(
            inv._execute(agent, "问个问题", deps, resolved, "问个问题")  # pyright: ignore[reportPrivateUsage]
        )
        async with asyncio.timeout(5):
            await self.streaming.wait()
        return task


async def test_recalling_the_trigger_stops_the_streaming_run(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Run(monkeypatch)
    with recalls.watch(CHANNEL, TRIGGER) as watch:
        task = await run.start(watch)
        assert recalls.recall(CHANNEL, TRIGGER)
        async with asyncio.timeout(5):
            status = await task
    assert status == "cancelled"
    assert run.model_cancelled
    assert run.delivered == []
    assert run.reactions == []


async def test_another_recall_leaves_the_run_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Run(monkeypatch)
    with recalls.watch(CHANNEL, TRIGGER) as watch:
        task = await run.start(watch)
        assert not recalls.recall(CHANNEL, f"{TRIGGER}-other")
        assert not recalls.recall(f"{CHANNEL}-other", TRIGGER)
        run.release.set()
        async with asyncio.timeout(5):
            status = await task
    assert status == "ok"
    assert [r.sections[0].content for r in run.delivered] == ["答案"]
    assert not recalls.recall(CHANNEL, TRIGGER)
