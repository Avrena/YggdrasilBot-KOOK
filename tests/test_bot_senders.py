"""Other bots never trigger the bot. In prod a "name[bot]" account replied "tldr" to a message and got a summary; bots
answering bots can loop."""

import warnings
from collections.abc import Callable
from types import ModuleType
from typing import Any

import nonebot
import pytest

from tests.support.groups import fresh_group
from tests.support.qq import FakeQQ, incoming
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.qq.model import Incoming, Member, QQError
from yggdrasil.runtime import GroupInfo, Snapshot, TriggerRule

GROUP, SELF = fresh_group(), 10001
MEMBER, TAGGED, LISTED, OFFICIAL, OWNER, NEWCOMER = 20001, 20002, 20003, 20004, 20005, 20006
# Senders of the lookup tests alone: what QQ answered about a sender is remembered for the whole process.
ROBOT, PERSON, STRANGER, MILKY_SENDER = 20007, 20008, 20009, 20010
TLDR = TriggerRule(
    id=1,
    handler="tldr",
    scope="global",
    group_id=None,
    match_type="keyword_prefix",
    patterns=("tldr",),
    arg_template=None,
    require_mention=False,
    target="quoted_or_self",
    priority=10,
    cooldown_s=0,
    allowed_roles=(),
)


def _connection(robots: frozenset[int] = frozenset(), *, down: bool = False, tells: bool = True) -> FakeQQ:
    """Our own connection; answers member lookups with QQ's robot flag (None: a protocol that can't tell)."""

    def member(*, group_id: int, user_id: int, **_: Any) -> Member:
        if down:
            raise QQError("get_group_member_info timeout")
        return Member(group_id, user_id, "", is_robot=user_id in robots if tells else None)

    return FakeQQ({"member": member}, self_id=SELF)


def _lookups(qq: FakeQQ) -> list[int]:
    return [call["user_id"] for call in qq.called("member")]


@pytest.fixture
def plugin(snapshot: Callable[..., Snapshot]) -> ModuleType:
    with warnings.catch_warnings():  # no adapter registered: routing doesn't need one
        warnings.filterwarnings("ignore", "No adapters found", RuntimeWarning)
        nonebot.require("nonebot_plugin_alconna")
        module = nonebot.require("yggdrasil.bot.plugin")
    group = GroupInfo(GROUP, "g", True, {"interaction": {"ignore_users": [LISTED]}})
    snapshot(groups={GROUP: group}, triggers=(TLDR,), superusers=frozenset({OWNER}))
    return module


def message(user_id: int, name: str, text: str = "tldr https://example.com/post") -> Incoming:
    return incoming(text, group_id=GROUP, user_id=user_id, self_id=SELF, name=name)


async def routed(plugin: ModuleType, qq: FakeQQ, m: Incoming) -> str | None:
    """The command the message starts, if any."""
    state: dict[str, Any] = {}
    await plugin.dispatch(qq, m, state)
    resolved = state.get(plugin.STATE_KEY)
    return resolved.handler if resolved else None


async def test_bots_never_trigger_but_their_messages_are_stored(
    plugin: ModuleType, recorded: list[IncomingMessage]
) -> None:
    qq = _connection(robots=frozenset({OFFICIAL}))
    assert await routed(plugin, qq, message(MEMBER, "阿明")) == "tldr"
    assert await routed(plugin, qq, message(TAGGED, "Mochi[bot]-测试中")) is None  # says it is one
    assert await routed(plugin, qq, message(TAGGED, "mochi[BOT]")) is None
    assert await routed(plugin, qq, message(LISTED, "小助手")) is None  # interaction.ignore_users
    assert await routed(plugin, qq, message(OFFICIAL, "小精灵")) is None  # QQ's own robot flag
    assert await routed(plugin, qq, message(OWNER, "owner[bot]")) == "tldr"  # superusers never count as bots
    assert await routed(plugin, qq, message(NEWCOMER, "robot 爱好者 测试中")) == "tldr"  # only the tag counts
    assert [m.user_id for m in recorded] == [MEMBER, TAGGED, TAGGED, LISTED, OFFICIAL, OWNER, NEWCOMER]


async def test_qq_is_asked_once_per_sender_and_only_about_triggers(
    plugin: ModuleType, recorded: list[IncomingMessage]
) -> None:
    qq = _connection(robots=frozenset({ROBOT}))
    for _ in range(2):
        assert await routed(plugin, qq, message(ROBOT, "小精灵")) is None
        assert await routed(plugin, qq, message(PERSON, "阿明")) == "tldr"
    assert await routed(plugin, qq, message(STRANGER, "新人", "大家好")) is None  # no trigger, no lookup
    assert _lookups(qq) == [ROBOT, PERSON]

    down = _connection(down=True)
    for _ in range(2):  # a failed lookup counts as a person, without delaying each request with another try
        assert await routed(plugin, down, message(STRANGER, "新人")) == "tldr"
    assert _lookups(down) == [STRANGER]


async def test_a_protocol_that_cant_tell_leaves_it_to_names_and_the_ignore_list(
    plugin: ModuleType, recorded: list[IncomingMessage]
) -> None:
    milky = _connection(tells=False)
    for _ in range(2):  # no robot flag: a person, and not asked about on every request
        assert await routed(plugin, milky, message(MILKY_SENDER, "小精灵")) == "tldr"
    assert _lookups(milky) == [MILKY_SENDER]
    assert await routed(plugin, milky, message(TAGGED, "Mochi[bot]")) is None
    assert await routed(plugin, milky, message(LISTED, "小助手")) is None


async def test_the_bots_own_messages_reported_back_are_not_stored_again(
    plugin: ModuleType, recorded: list[IncomingMessage]
) -> None:
    # Yogurt reports the bot's own sends (reportSelfMessage); delivery already recorded them, with their content.
    qq = _connection()
    assert await routed(plugin, qq, message(SELF, "bot", "tldr 自己发的")) is None
    assert recorded == []
