import pytest

from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot, TriggerRule
from yggdrasil.triggers.index import MatchInput, TriggerIndex

GUILD = "g-100"
CAT = "cat-100"
CHANNEL = "ch-100"


def rule(id: int, handler: str, match_type: str, patterns=(), **kw) -> TriggerRule:
    base = {
        "scope": "global",
        "scope_id": None,
        "arg_template": None,
        "require_mention": False,
        "target": "quoted_or_self",
        "priority": 100,
        "cooldown_s": 0,
        "allowed_roles": (),
    }
    base.update(kw)
    return TriggerRule(id=id, handler=handler, match_type=match_type, patterns=tuple(patterns), **base)


def handler_of(resolved) -> str | None:
    return resolved.handler if resolved else None


def index(*rules: TriggerRule, channel_settings: dict | None = None) -> TriggerIndex:
    guilds = {GUILD: GuildInfo(GUILD, "g", "", {}, True)}
    channels = {
        CAT: ChannelInfo(CAT, GUILD, None, True, 0, "cat", True, {}),
        CHANNEL: ChannelInfo(CHANNEL, GUILD, CAT, False, 0, "ch", True, channel_settings or {}),
    }
    return TriggerIndex(Snapshot(triggers=rules, guilds=guilds, channels=channels))


def inp(text: str, *, scope="channel", to_me=False, replied=False, role="member") -> MatchInput:
    is_ch = scope == "channel"
    return MatchInput(
        scope=scope,
        channel_id=CHANNEL if is_ch else None,
        guild_id=GUILD if is_ch else None,
        category_id=CAT if is_ch else None,
        text=text,
        to_me=to_me,
        replied_to_bot=replied,
        role=role,
    )


def test_prefix_strips_keyword_and_lower_priority_number_wins():
    idx = index(
        rule(1, "chat", "mention", priority=100),
        rule(2, "tldr", "keyword_prefix", ["tldr", "太长不看"], priority=10),
    )
    got = idx.match(inp("TLDR 这篇文章", to_me=True))
    assert got is not None
    assert (got.handler, got.args) == ("tldr", "这篇文章")


def test_channel_rule_overrides_global_rules_of_same_handler_only():
    idx = index(
        rule(1, "tldr", "keyword_prefix", ["tldr"]),
        rule(2, "tldr", "keyword_prefix", ["总结"], scope="channel", scope_id=CHANNEL),
        rule(3, "chat", "keyword_prefix", ["ygg"]),
    )
    assert idx.match(inp("tldr x")) is None  # global tldr hidden in this channel
    assert handler_of(idx.match(inp("总结 x"))) == "tldr"
    assert handler_of(idx.match(inp("ygg 你好"))) == "chat"  # other handlers still global


def test_require_mention_accepts_name_in_text():
    idx = index(rule(1, "privacy.optout", "keyword_exact", ["不要记我"], require_mention=True))
    assert idx.match(inp("不要记我")) is None
    assert handler_of(idx.match(inp("不要记我", to_me=True))) == "privacy.optout"
    named = index(rule(1, "chat", "name_mention"), channel_settings={"persona": {"name": "小树", "aliases": []}}).match(
        inp("小树你好")
    )
    assert named is not None
    assert named.named


def test_regex_captures_feed_arg_template():
    idx = index(rule(1, "tldr", "regex", [r"总结最近(\d+)条"], arg_template="-n {0} {rest}"))
    got = idx.match(inp("帮我总结最近50条 重点看争论"))
    assert got is not None
    assert got.captures == ("50",)
    assert got.args == "-n 50 帮我 重点看争论"


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("member", False), ("admin", True), ("owner", True), ("superuser", True)],
)
def test_allowed_roles_owner_implies_admin(role: str, allowed: bool):
    idx = index(rule(1, "chat", "keyword_exact", ["重载"], allowed_roles=("admin",)))
    assert (idx.match(inp("重载", role=role)) is not None) is allowed


def test_private_scope_only_sees_private_and_admin_privacy_globals():
    idx = index(
        rule(1, "chat", "keyword_prefix", ["ygg"]),
        rule(2, "admin.status", "keyword_exact", ["状态"]),
        rule(3, "admin.review", "keyword_prefix", ["通过"], scope="private", arg_template="approve {rest}"),
    )
    assert idx.match(inp("ygg hi", scope="private")) is None
    assert handler_of(idx.match(inp("状态", scope="private"))) == "admin.status"
    got = idx.match(inp("通过 R12", scope="private"))
    assert got is not None
    assert got.args == "approve R12"
