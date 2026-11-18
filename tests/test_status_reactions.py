"""Status reactions on the trigger message: QQ shows the requester a gray tip for each one, so a quick answer gets
none, a slow one only "received", and a result reaction is added only when no message carries the outcome."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.usage import UsageLimits

from tests.support.groups import fresh_group
from tests.support.qq import FakeQQ, incoming
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.schema import BotReply
from yggdrasil.llm.registry import ResolvedModel
from yggdrasil.moderation.guard import Verdict
from yggdrasil.pipeline import invoke
from yggdrasil.settings import Settings
from yggdrasil.triggers.index import Resolved

GROUP, SELF, ALICE = fresh_group(), 10001, 20001
TRIGGER = 7
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
FORWARD = '{"mode": "forward", "sections": [{"content": "答案"}]}'
SILENT = '{"mode": "silent", "silent_reason": "不是在问我"}'
DELAY_S = 0.05


class Run:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: str, *, after_s: float = 0.0) -> None:
        self.answer, self.after_s = answer, after_s
        self.reactions: list[str] = []
        self.delivered: list[BotReply] = []

        async def moderate(stage: str, *_a: Any, **_k: Any) -> Verdict:
            return Verdict(stage, "Skipped", 0.0)

        async def react(_qq: Any, _group_id: int | None, _message_id: int, emoji: str) -> None:
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
        await asyncio.sleep(self.after_s)
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=self.answer)}

    def _never(self, _messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise AssertionError("agents stream")

    async def go(self, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> str:
        m = incoming("问个问题", group_id=GROUP, user_id=ALICE, self_id=SELF, message_id=TRIGGER, to_me=True)
        settings = Settings()
        settings.interaction.received_delay_s = DELAY_S
        qq = FakeQQ(self_id=SELF)
        inv = invoke.Invocator(qq, m, CHAT, "chat", "", settings, GROUP)
        model = FunctionModel(self._never, stream_function=self._stream)
        agent = Agent(model, output_type=BotReply, deps_type=Deps)
        deps = Deps(qq, GROUP, ALICE, settings, Handles(), "chat")
        resolved = ResolvedModel("chat", model, {}, UsageLimits(), "test")

        async def _run(_inv: invoke.Invocator) -> str:
            if fail:
                await asyncio.sleep(self.after_s)
                raise RuntimeError("boom")
            return await inv._execute(agent, "问个问题", deps, resolved, "问个问题")  # pyright: ignore[reportPrivateUsage]

        monkeypatch.setattr(invoke.Invocator, "_run", _run)
        async with asyncio.timeout(5):
            return await inv.run()


async def test_a_quick_answer_adds_no_reaction(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Run(monkeypatch, FORWARD)
    assert await run.go(monkeypatch) == "ok"
    assert len(run.delivered) == 1
    await asyncio.sleep(DELAY_S * 3)  # the pending "received" was dropped, not just late
    assert run.reactions == []


async def test_a_slow_answer_adds_only_received(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Run(monkeypatch, FORWARD, after_s=DELAY_S * 4)
    assert await run.go(monkeypatch) == "ok"
    assert run.reactions == [Settings().interaction.received_emoji]


async def test_an_outcome_without_a_message_gets_its_reaction(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings().interaction
    quick = Run(monkeypatch, SILENT)
    assert await quick.go(monkeypatch) == "silent"
    await asyncio.sleep(DELAY_S * 3)
    assert quick.reactions == [cfg.silent_emoji]

    slow = Run(monkeypatch, "", after_s=DELAY_S * 4)
    assert await slow.go(monkeypatch, fail=True) == "failed"
    assert slow.reactions == [cfg.received_emoji, cfg.failure_emoji]
