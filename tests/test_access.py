import pytest
from starlette.requests import Request

from yggdrasil.access import can_bypass_channel_privacy, can_use_bot, effective_tools
from yggdrasil.admin.auth import require_admin
from yggdrasil.agent.tools import suite_enabled, tool_names
from yggdrasil.config import APP_CONFIG
from yggdrasil.kook.model import Incoming, Sender
from yggdrasil.kook.parser import parse_sender
from yggdrasil.settings import Settings, merge_settings


def _incoming(*, user_id: str = "u1", role: str = "member", role_ids: tuple[int, ...] = ()) -> Incoming:
    return Incoming(
        message_id="m1",
        channel_id="ch1",
        guild_id="g1",
        sender=Sender(user_id=user_id, name="Alice", role=role, role_ids=role_ids),  # type: ignore[arg-type]
        time=1_700_000_000,
        segments=[],
        self_id="bot1",
        text="hi",
        to_me=True,
    )


def test_parse_sender_extracts_kook_role_ids() -> None:
    s = parse_sender({"id": "100", "username": "Alice", "roles": [101, "202", True, "bad"]})
    assert s.role_ids == (101, 202)


def test_scope_allow_bot_and_four_level_inheritance() -> None:
    # Global allows, guild disables, category re-enables, channel disables again.
    s_guild = merge_settings({}, {"access": {"allow_bot": False}})
    s_cat = merge_settings({}, {"access": {"allow_bot": False}}, {"access": {"allow_bot": True}})
    s_ch = merge_settings(
        {},
        {"access": {"allow_bot": False}},
        {"access": {"allow_bot": True}},
        {"access": {"allow_bot": False}},
    )
    inc = _incoming(role_ids=(10,))
    assert can_use_bot(inc, s_guild, role="member") == (False, "scope_bot_disabled")
    assert can_use_bot(inc, s_cat, role="member") == (True, None)
    assert can_use_bot(inc, s_ch, role="member") == (False, "scope_bot_disabled")
    # Superusers always bypass.
    assert can_use_bot(inc, s_ch, role="superuser") == (True, None)


def test_role_whitelist_blacklist_and_guild_admin_bypass() -> None:
    s = merge_settings(
        {},
        {
            "access": {
                "allowed_roles": ["100", "200"],
                "denied_roles": ["999"],
                "allow_guild_admins": True,
            }
        },
    )
    assert can_use_bot(_incoming(role_ids=(100,)), s, role="member") == (True, None)
    assert can_use_bot(_incoming(role_ids=(300,)), s, role="member") == (False, "role_not_allowed")
    # Guild admin allowed by allow_guild_admins=True
    assert can_use_bot(_incoming(role_ids=(300,)), s, role="admin") == (True, None)
    # Denied role beats whitelist and guild admin
    assert can_use_bot(_incoming(role_ids=(100, 999)), s, role="admin") == (False, "denied_role")

    s_strict = merge_settings(s.model_dump(mode="json"), {"access": {"allow_guild_admins": False}})
    assert can_use_bot(_incoming(role_ids=(300,)), s_strict, role="admin") == (False, "role_not_allowed")


def test_role_rules_allow_bot_override_and_tool_permissions() -> None:
    s = merge_settings(
        {},
        {
            "access": {
                "allow_bot": False,
                "role_rules": {
                    "777": {
                        "allow_bot": True,
                        "tools_allow": ["generate_image"],
                        "tools_deny": ["sandbox", "web_search"],
                    },
                    "888": {"allow_bot": False},
                },
            },
            "tools": {"enabled": ["read_url", "web_search", "sandbox"]},
        },
    )
    vip = _incoming(role_ids=(777,))
    assert can_use_bot(vip, s, role="member") == (True, None)
    # Explicit role_rules.allow_bot=False wins over allow_bot=True when user holds both roles.
    both = _incoming(role_ids=(777, 888))
    assert can_use_bot(both, s, role="member") == (False, "role_rule_denied")

    eff = effective_tools(s, vip, role="member")
    assert "read_url" in eff
    assert "generate_image" in eff
    assert "web_search" not in eff
    assert "sandbox" not in eff
    assert tool_names(eff, "chat") == ["read_url", "generate_image"]
    assert not suite_enabled(s, "chat", "ch1", global_settings=Settings(), configured=True, enabled_tools=eff)

    # Superuser keeps full scope tools even when holding role 777.
    su_eff = effective_tools(s, vip, role="superuser")
    assert su_eff == ["read_url", "web_search", "sandbox"]
    assert suite_enabled(s, "chat", "ch1", global_settings=Settings(), configured=True, enabled_tools=su_eff)


def test_can_bypass_channel_privacy() -> None:
    s = merge_settings(
        {},
        {"access": {"role_rules": {"777": {"bypass_channel_privacy": True}, "888": {"bypass_channel_privacy": False}}}},
    )
    assert can_bypass_channel_privacy(s, ("777",)) is True
    assert can_bypass_channel_privacy(s, ("888",)) is False
    assert can_bypass_channel_privacy(s, ()) is False


async def test_dashboard_auth_disabled_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    req = Request({"type": "http", "headers": []})
    monkeypatch.setattr(APP_CONFIG, "environment", "prod")
    monkeypatch.setattr(APP_CONFIG, "auth_disabled", True)
    monkeypatch.setattr(APP_CONFIG, "dev_admin_email", None)
    monkeypatch.setattr(APP_CONFIG, "bootstrap_admin_emails", [])
    assert await require_admin(req) == "admin@local"

    monkeypatch.setattr(APP_CONFIG, "bootstrap_admin_emails", ["owner@lan"])
    assert await require_admin(req) == "owner@lan"

    req_with_hdr = Request({"type": "http", "headers": [(APP_CONFIG.auth_email_header.lower().encode(), b"ops@lan")]})
    assert await require_admin(req_with_hdr) == "ops@lan"
