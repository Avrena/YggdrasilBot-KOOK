"""Tests for guild-shared memory, one-way channel privacy, cross-channel allow/deny, role bypass, and citations."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.memory.helpers import mv
from tests.support.kook import FakeKook, make_incoming
from yggdrasil.agent.deps import Handles
from yggdrasil.memory.common import format_line
from yggdrasil.memory.search import accessible_channels
from yggdrasil.people import People
from yggdrasil.pipeline import invoke
from yggdrasil.pipeline import session as sessions
from yggdrasil.pipeline.prompt import instructions
from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot
from yggdrasil.settings import Settings
from yggdrasil.triggers.index import Resolved


def _setup_guild(snapshot: Callable[..., Snapshot]) -> None:
    """Guild with public channels, an admin category (one-way private), and a ticket category
    readable ONLY by the admin category (or role-lead)."""
    snapshot(
        guilds={"g1": GuildInfo("g1", "测试服", "", {}, enabled=True)},
        channels={
            "ch-general": ChannelInfo("ch-general", "g1", None, False, 0, "大厅", True, {}),
            "ch-tech": ChannelInfo("ch-tech", "g1", None, False, 1, "技术讨论", True, {}),
            "cat-admin": ChannelInfo(
                "cat-admin",
                "g1",
                None,
                True,
                2,
                "管理区",
                True,
                {"memory": {"share_in_guild": False}},
            ),
            "ch-admin": ChannelInfo("ch-admin", "g1", "cat-admin", False, 3, "管理员频道", None, {}),
            "cat-ticket": ChannelInfo(
                "cat-ticket",
                "g1",
                None,
                True,
                4,
                "工单区",
                True,
                {
                    "memory": {
                        "share_in_guild": False,
                        "readable_by_scopes": ["cat-admin"],
                        "readable_by_roles": ["1001"],
                    }
                },
            ),
            "ch-ticket-1": ChannelInfo("ch-ticket-1", "g1", "cat-ticket", False, 5, "工单-001", None, {}),
            "ch-ticket-2": ChannelInfo("ch-ticket-2", "g1", "cat-ticket", False, 6, "工单-002", None, {}),
            "ch-disabled": ChannelInfo("ch-disabled", "g1", None, False, 7, "停用", False, {}),
        },
    )


def test_one_way_privacy_admin_reads_tickets_but_others_cannot(snapshot: Callable[..., Snapshot]) -> None:
    _setup_guild(snapshot)

    # Public channel sees public channels only — never admin or tickets or disabled
    assert set(accessible_channels("g1", "ch-general")) == {"ch-general", "ch-tech"}

    # Admin channel has one-way privacy: hidden from public/tickets, yet reads public + all tickets
    assert set(accessible_channels("g1", "ch-admin")) == {
        "ch-admin",
        "ch-general",
        "ch-tech",
        "ch-ticket-1",
        "ch-ticket-2",
    }

    # Ticket 1 cannot read Ticket 2 or Admin, but (under one-way privacy) can still read public channels
    assert set(accessible_channels("g1", "ch-ticket-1")) == {"ch-ticket-1", "ch-general", "ch-tech"}

    # current_channel_only restricts strictly to self
    assert accessible_channels("g1", "ch-admin", current_channel_only=True) == ["ch-admin"]


def test_cross_channel_read_false_and_allow_deny_lists(snapshot: Callable[..., Snapshot]) -> None:
    snapshot(
        guilds={"g1": GuildInfo("g1", "测试服", "", {}, enabled=True)},
        channels={
            "cat-dev": ChannelInfo("cat-dev", "g1", None, True, 0, "开发组", True, {}),
            "ch-dev-1": ChannelInfo("ch-dev-1", "g1", "cat-dev", False, 1, "后端", None, {}),
            "ch-dev-2": ChannelInfo("ch-dev-2", "g1", "cat-dev", False, 2, "前端", None, {}),
            "ch-general": ChannelInfo(
                "ch-general",
                "g1",
                None,
                False,
                3,
                "大厅",
                True,
                {"memory": {"cross_channel_deny": ["cat-dev"]}},
            ),
            "ch-support": ChannelInfo(
                "ch-support",
                "g1",
                None,
                False,
                4,
                "客服",
                True,
                {"memory": {"cross_channel_allow": ["cat-dev"], "cross_channel_deny": ["ch-dev-2"]}},
            ),
            "ch-isolated": ChannelInfo(
                "ch-isolated",
                "g1",
                None,
                False,
                5,
                "完全隔离",
                True,
                {"memory": {"share_in_guild": False, "cross_channel_read": False}},
            ),
        },
    )

    # Hall denies the entire dev category
    assert set(accessible_channels("g1", "ch-general")) == {"ch-general", "ch-support"}
    # Support allows cat-dev but denies ch-dev-2 (deny beats allow)
    assert set(accessible_channels("g1", "ch-support")) == {"ch-support", "ch-dev-1"}
    # Isolated channel reads only itself
    assert accessible_channels("g1", "ch-isolated") == ["ch-isolated"]


def test_role_readable_and_scoped_bypass_channel_privacy(snapshot: Callable[..., Snapshot]) -> None:
    _setup_guild(snapshot)

    # role 1001 is in cat-ticket.readable_by_roles: can read tickets even from ch-general
    assert set(accessible_channels("g1", "ch-general", role_ids=("1001",))) == {
        "ch-general",
        "ch-tech",
        "ch-ticket-1",
        "ch-ticket-2",
    }
    # role 1001 still cannot read ch-admin (not granted there)
    assert "ch-admin" not in accessible_channels("g1", "ch-general", role_ids=("1001",))

    # Scope-level bypass_channel_privacy: role-boss can bypass inside ch-tech, not inside ch-general
    snapshot(
        guilds={"g1": GuildInfo("g1", "测试服", "", {}, enabled=True)},
        channels={
            "ch-general": ChannelInfo("ch-general", "g1", None, False, 0, "大厅", True, {}),
            "ch-tech": ChannelInfo(
                "ch-tech",
                "g1",
                None,
                False,
                1,
                "技术讨论",
                True,
                {"access": {"role_rules": {"role-boss": {"bypass_channel_privacy": True}}}},
            ),
            "ch-secret": ChannelInfo(
                "ch-secret",
                "g1",
                None,
                False,
                2,
                "机密",
                True,
                {"memory": {"share_in_guild": False, "cross_channel_read": False}},
            ),
        },
    )
    assert set(accessible_channels("g1", "ch-general", role_ids=("role-boss",))) == {"ch-general", "ch-tech"}
    assert set(accessible_channels("g1", "ch-tech", role_ids=("role-boss",))) == {"ch-general", "ch-tech", "ch-secret"}


def test_prompt_instructions_hide_inaccessible_channel_names(snapshot: Callable[..., Snapshot]) -> None:
    _setup_guild(snapshot)
    settings = Settings()

    pub_prompt = instructions(
        "chat", settings, "ch-general", guild_id="g1", tools=["search_history"], sandbox=None, report=False, nonce="abc"
    )
    assert "#技术讨论" in pub_prompt
    assert "#管理员频道" not in pub_prompt
    assert "#工单-001" not in pub_prompt

    admin_prompt = instructions(
        "chat", settings, "ch-admin", guild_id="g1", tools=["search_history"], sandbox=None, report=False, nonce="abc"
    )
    assert "#工单-001" in admin_prompt
    assert "#工单-002" in admin_prompt
    assert "#大厅" in admin_prompt


async def test_session_continuation_rejects_lower_privilege_speaker(
    snapshot: Callable[..., Snapshot], monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_guild(snapshot)
    stored = sessions.Session(
        invocation_id=uuid4(),
        setup=sessions.Setup(
            handler="chat",
            instructions="sys",
            tools=["search_history"],
            sandbox=False,
            skills=[],
            accessible_channels=["ch-general", "ch-tech", "ch-ticket-1", "ch-ticket-2"],
        ),
        messages=[],
        handles=Handles(),
        nonce="abc123",
        seen_message_id=None,
        created_at=datetime.now(UTC),
    )

    async def _load(_ch: str, _ans: str, _age: object) -> sessions.Session:
        return stored

    monkeypatch.setattr(sessions, "load", _load)
    resolved = Resolved("chat", 1, "", (), "quoted_or_self", 0, False)

    # Normal member without role-lead cannot continue role-lead's session that had ticket access
    normal_msg = make_incoming("继续说", channel_id="ch-general", guild_id="g1", user_id="u-normal")
    normal_inv = invoke.Invocator(FakeKook(), normal_msg, resolved, "chat", "", Settings(), "ch-general", "g1")
    assert await normal_inv._session("bot-msg-1") is None  # pyright: ignore[reportPrivateUsage]

    # Another speaker who also holds role 1001 (int KOOK role_id) can continue the session
    lead_base = make_incoming("继续说", channel_id="ch-general", guild_id="g1", user_id="u-lead")
    lead_msg = replace(lead_base, sender=replace(lead_base.sender, role_ids=(1001,)))
    lead_inv = invoke.Invocator(FakeKook(), lead_msg, resolved, "chat", "", Settings(), "ch-general", "g1")
    assert await lead_inv._session("bot-msg-1") is stored  # pyright: ignore[reportPrivateUsage]


def test_format_line_annotates_cross_channel_citation() -> None:
    people = People("耶梦加得", {"u-1": "阿明"}, frozenset({"bot-1"}))
    m = mv(0, "昨天决定用 KOOK", user="u-1", name="阿明", mid=42, channel_id="ch-tech")
    same = format_line(m, people)
    cross = format_line(m, people, with_id=True, channel_tag="技术讨论")
    assert "[#技术讨论]" not in same
    assert "[#技术讨论]" in cross
    assert "#42" in cross
    assert "昨天决定用 KOOK" in cross
