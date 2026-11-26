"""Tests for ``yggdrasil.bot.router.dispatch``: routing, ignored users, bot anti-loop, feature toggles, self skip."""

from collections.abc import Callable
from typing import Any

import pytest

from tests.support.kook import FakeKook, make_incoming
from yggdrasil.bot import router
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot, TriggerRule

GUILD, CH, SELF = "g-1", "ch-1", "bot-self"
MEMBER, OTHER_BOT, LISTED, OWNER = "u-1", "u-bot", "u-ignored", "u-owner"

TLDR = TriggerRule(
    id=1,
    handler="tldr",
    scope="global",
    scope_id=None,
    match_type="keyword_prefix",
    patterns=("tldr",),
    arg_template=None,
    require_mention=False,
    target="quoted_or_self",
    priority=10,
    cooldown_s=0,
    allowed_roles=(),
)


@pytest.fixture
def dispatched_handlers(snapshot: Callable[..., Snapshot], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    dispatched: list[str] = []

    async def fake_run(_kook: Any, _inc: Any, resolved: Any) -> None:
        dispatched.append(resolved.handler)

    monkeypatch.setattr(router, "_invoke_handler", fake_run)
    snapshot(
        guilds={GUILD: GuildInfo(GUILD, "测试服", "", {}, enabled=True)},
        channels={
            CH: ChannelInfo(
                CH,
                GUILD,
                None,
                False,
                0,
                "闲聊",
                True,
                {
                    "interaction": {
                        "ignore_users": [LISTED],
                        "allow_bot_triggers": True,
                        "bot_cooldown_s": 20,
                        "bot_max_consecutive_turns": 1,
                    }
                },
            )
        },
        triggers=(TLDR,),
        superusers=frozenset({OWNER}),
    )
    return dispatched


async def test_router_records_all_non_self_messages_enforces_ignore_and_bot_antiloop(
    dispatched_handlers: list[str], recorded: list[IncomingMessage]
) -> None:
    kook = FakeKook(bot_id=SELF)

    # Normal human triggers tldr
    await router.dispatch(
        kook, make_incoming("tldr https://a.b", channel_id=CH, guild_id=GUILD, user_id=MEMBER, self_id=SELF)
    )
    # Ignored user is stored, not triggered
    await router.dispatch(
        kook, make_incoming("tldr https://a.b", channel_id=CH, guild_id=GUILD, user_id=LISTED, self_id=SELF)
    )
    # Another bot triggers once (allowed=True, consecutive_turns=1), then 2nd consecutive bot trigger is blocked
    await router.dispatch(
        kook,
        make_incoming("tldr https://a.b", channel_id=CH, guild_id=GUILD, user_id=OTHER_BOT, is_bot=True, self_id=SELF),
    )
    await router.dispatch(
        kook,
        make_incoming("tldr https://a.b", channel_id=CH, guild_id=GUILD, user_id=OTHER_BOT, is_bot=True, self_id=SELF),
    )
    # Superuser always bypasses even if tagged [bot]
    await router.dispatch(
        kook,
        make_incoming(
            "tldr https://a.b", channel_id=CH, guild_id=GUILD, user_id=OWNER, username="admin[bot]", self_id=SELF
        ),
    )
    # Self echo is neither stored nor triggered
    await router.dispatch(kook, make_incoming("tldr self", channel_id=CH, guild_id=GUILD, user_id=SELF, self_id=SELF))

    assert dispatched_handlers == ["tldr", "tldr", "tldr"]
    assert [m.user_id for m in recorded] == [MEMBER, LISTED, OTHER_BOT, OTHER_BOT, OWNER]


async def test_router_respects_per_channel_feature_toggles(
    snapshot: Callable[..., Snapshot], dispatched_handlers: list[str], recorded: list[IncomingMessage]
) -> None:
    snapshot(
        guilds={GUILD: GuildInfo(GUILD, "测试服", "", {}, enabled=True)},
        channels={CH: ChannelInfo(CH, GUILD, None, False, 0, "闲聊", True, {"features": {"tldr": False}})},
        triggers=(TLDR,),
    )
    kook = FakeKook(bot_id=SELF)
    await router.dispatch(
        kook, make_incoming("tldr https://a.b", channel_id=CH, guild_id=GUILD, user_id=MEMBER, self_id=SELF)
    )
    assert dispatched_handlers == []
    assert len(recorded) == 1
