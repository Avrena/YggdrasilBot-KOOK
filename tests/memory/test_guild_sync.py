"""Tests for guild-shared memory with channel isolation and cross-channel citations."""

from collections.abc import Callable

from tests.memory.helpers import mv
from yggdrasil.memory.common import format_line
from yggdrasil.memory.search import accessible_channels
from yggdrasil.people import People
from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot


def test_accessible_channels_shares_across_guild_and_respects_share_in_guild_flag(
    snapshot: Callable[..., Snapshot],
) -> None:
    snapshot(
        guilds={"g1": GuildInfo("g1", "测试服", "", {}, enabled=True)},
        channels={
            "ch-general": ChannelInfo("ch-general", "g1", None, False, 0, "闲聊", True, {}),
            "ch-tech": ChannelInfo("ch-tech", "g1", None, False, 1, "技术讨论", True, {}),
            "ch-private": ChannelInfo(
                "ch-private", "g1", None, False, 2, "私密管理", True, {"memory": {"share_in_guild": False}}
            ),
            "ch-disabled": ChannelInfo("ch-disabled", "g1", None, False, 3, "停用", False, {}),
        },
    )

    # Normal shared channel sees all enabled share_in_guild=True channels in the same guild
    vis = accessible_channels("g1", "ch-general")
    assert set(vis) == {"ch-general", "ch-tech"}
    assert "ch-private" not in vis
    assert "ch-disabled" not in vis

    # current_channel_only restricts to the active channel only
    assert accessible_channels("g1", "ch-general", current_channel_only=True) == ["ch-general"]

    # A channel with share_in_guild=False only reads itself (never leaks or pulls sibling channels)
    assert accessible_channels("g1", "ch-private") == ["ch-private"]


def test_format_line_annotates_cross_channel_citation() -> None:
    people = People("耶梦加得", {"u-1": "阿明"}, frozenset({"bot-1"}))
    m = mv(0, "昨天决定用 KOOK", user="u-1", name="阿明", mid=42, channel_id="ch-tech")
    same = format_line(m, people)
    cross = format_line(m, people, with_id=True, channel_tag="技术讨论")
    assert "[#技术讨论]" not in same
    assert "[#技术讨论]" in cross
    assert "#42" in cross
    assert "昨天决定用 KOOK" in cross
