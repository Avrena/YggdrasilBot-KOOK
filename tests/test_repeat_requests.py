"""One answer per tldr target in a KOOK channel."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.support.groups import fresh_channel, fresh_guild
from tests.support.kook import FakeKook, make_incoming
from yggdrasil.kook import segments as seg
from yggdrasil.kook.model import Incoming, Message, Sender
from yggdrasil.pipeline import invoke
from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot
from yggdrasil.settings import InteractionSettings
from yggdrasil.triggers.index import Resolved

GUILD, CHANNEL, SELF = fresh_guild(), fresh_channel(), "10001"
ALICE, BOB, OWNER = "20001", "20002", "20003"
QUOTED, OTHER_QUOTED = "q-500", "q-501"
TLDR = Resolved("tldr", 1, "", (), "quoted_or_self", 0, False)
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
SILENT = InteractionSettings().silent_emoji
KOOK = FakeKook(bot_id=SELF)


@dataclass
class ChannelRun:
    started: list[str] = field(default_factory=list)
    reactions: list[tuple[str, str]] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)


@pytest.fixture
def channel(monkeypatch: pytest.MonkeyPatch, snapshot: Callable[..., Snapshot]) -> ChannelRun:
    ch = ChannelRun()
    no_limits = {"ratelimit": {"user_per_minute": 0, "channel_per_minute": 0}}
    snapshot(
        guilds={GUILD: GuildInfo(GUILD, "g", "", {}, enabled=True)},
        channels={CHANNEL: ChannelInfo(CHANNEL, GUILD, None, False, 0, "ch", True, no_limits)},
        superusers=frozenset({OWNER}),
    )

    async def react(_kook: Any, _channel_id: str | None, message_id: str, emoji: str) -> None:
        ch.reactions.append((message_id, emoji))

    async def run(self: invoke.Invocator) -> str:
        assert self.trigger_message_id is not None
        ch.started.append(self.trigger_message_id)
        return ch.outcomes.pop(0) if ch.outcomes else "ok"

    monkeypatch.setattr(invoke, "react", react)
    monkeypatch.setattr(invoke.Invocator, "run", run)
    return ch


def message(message_id: str, user_id: str, text: str, *, quoting: str | None = None, image: bool = False) -> Incoming:
    segs = [seg.reply(quoting)] if quoting is not None else []
    segs.append(seg.text(text))
    if image:
        segs.append(seg.image("https://img.kookapp.cn/cat.png"))
    quoted = (
        Message(quoting, CHANNEL, GUILD, Sender("20009", "老王"), 1760000000, [seg.text("很长的一段话")])
        if quoting
        else None
    )
    return make_incoming(
        text,
        channel_id=CHANNEL,
        guild_id=GUILD,
        user_id=user_id,
        self_id=SELF,
        message_id=message_id,
        username="群友",
        reply=quoted,
        segments=segs,
    )


async def tldr(m: Incoming, words: str = "") -> None:
    await invoke.run(KOOK, m, TLDR, "tldr", words)


async def test_one_answer_per_target(channel: ChannelRun) -> None:
    await tldr(message("m1", ALICE, "tldr", quoting=QUOTED))
    await tldr(message("m2", BOB, "tldr", quoting=QUOTED))
    await tldr(message("m3", ALICE, "tldr https://example.com/post"), "https://example.com/post")
    await tldr(message("m4", ALICE, "tldr  https://example.com/post "), " https://example.com/post ")
    assert channel.started == ["m1", "m3"]
    assert channel.reactions == [("m2", SILENT), ("m4", SILENT)]


async def test_links_that_differ_only_in_case_are_different_targets(channel: ChannelRun) -> None:
    await tldr(message("m1", ALICE, "tldr https://example.com/s/aBc12"), "https://example.com/s/aBc12")
    await tldr(message("m2", BOB, "tldr https://example.com/s/AbC12"), "https://example.com/s/AbC12")
    assert channel.started == ["m1", "m2"]
    assert channel.reactions == []


async def test_other_requests_still_run(channel: ChannelRun) -> None:
    await tldr(message("m1", ALICE, "tldr", quoting=QUOTED))
    await tldr(message("m2", BOB, "tldr 只说价格", quoting=QUOTED), "只说价格")
    await tldr(message("m3", BOB, "tldr", quoting=OTHER_QUOTED))
    await tldr(message("m4", ALICE, "tldr"))
    await tldr(message("m5", ALICE, "tldr"))
    await tldr(message("m6", BOB, "tldr", image=True))
    await tldr(message("m7", BOB, "tldr", image=True))
    await tldr(message("m8", OWNER, "tldr", quoting=QUOTED))
    for i in ("m9", "m10"):
        await invoke.run(KOOK, message(i, ALICE, "早", quoting=QUOTED), CHAT, "chat", "早")
    assert channel.started == [f"m{i}" for i in range(1, 11)]
    assert channel.reactions == []


async def test_a_failed_run_can_be_asked_for_again(channel: ChannelRun) -> None:
    channel.outcomes = ["failed"]
    for i in ("m1", "m2", "m3"):
        await tldr(message(i, ALICE, "tldr", quoting=QUOTED))
    assert channel.started == ["m1", "m2"]
    assert channel.reactions == [("m3", SILENT)]
