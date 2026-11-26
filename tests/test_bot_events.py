"""Tests for KOOK observed event filtering."""

from collections.abc import Callable

import pytest

from yggdrasil import events
from yggdrasil.kook.model import Observed
from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot

BOT, GUILD_ON, GUILD_OFF = "bot-1", "g-on", "g-off"
CH_ON, CH_OFF = "ch-on", "ch-off"


@pytest.fixture(autouse=True)
def _channels(snapshot: Callable[..., Snapshot]) -> None:
    snapshot(
        guilds={
            GUILD_ON: GuildInfo(GUILD_ON, "启用服", "", {}, enabled=True),
            GUILD_OFF: GuildInfo(GUILD_OFF, "停用服", "", {}, enabled=False),
        },
        channels={
            CH_ON: ChannelInfo(CH_ON, GUILD_ON, None, False, 0, "闲聊", True, {}),
            CH_OFF: ChannelInfo(CH_OFF, GUILD_OFF, None, False, 0, "静音", False, {}),
        },
    )


def _kept(kind: str, guild_id: str | None, channel_id: str | None = None, user_id: str | None = None) -> bool:
    return events.kept(Observed(kind=kind, guild_id=guild_id, channel_id=channel_id, user_id=user_id), BOT)


def test_bot_guild_membership_events_are_always_kept_channel_events_follow_enabled_channels() -> None:
    assert _kept("kook.self_joined_guild", GUILD_OFF)
    assert _kept("kook.self_exited_guild", GUILD_OFF)
    assert _kept("kook.deleted_guild", GUILD_OFF)
    assert _kept("kook.added_channel", GUILD_ON, CH_ON)
    assert _kept("kook.pinned_message", GUILD_ON, CH_ON)
    assert not _kept("kook.pinned_message", GUILD_OFF, CH_OFF)
