"""Tests for 4-level scope inheritance (global -> guild -> category -> channel), feature toggles, and triggers."""

from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot, TriggerRule
from yggdrasil.settings import merge_settings
from yggdrasil.triggers.index import MatchInput, TriggerIndex


def _sample_snapshot() -> Snapshot:
    return Snapshot(
        global_settings_doc={
            "persona": {"name": "GlobalBot"},
            "features": {"tldr": True, "sticker_collect": True},
            "interaction": {"quote_reply": "always"},
        },
        guilds={
            "g-1": GuildInfo(
                "g-1",
                "测试服",
                "",
                {"persona": {"name": "GuildBot"}, "features": {"sticker_collect": False}},
                enabled=True,
            ),
            "g-off": GuildInfo("g-off", "停用服", "", {}, enabled=False),
        },
        channels={
            "cat-1": ChannelInfo("cat-1", "g-1", None, True, 0, "日常分组", True, {"features": {"tldr": False}}),
            "cat-off": ChannelInfo("cat-off", "g-1", None, True, 1, "静音分组", False, {}),
            "ch-inherit": ChannelInfo("ch-inherit", "g-1", "cat-1", False, 0, "闲聊", None, {}),
            "ch-override": ChannelInfo(
                "ch-override",
                "g-1",
                "cat-1",
                False,
                1,
                "私密管理",
                True,
                {"features": {"tldr": True}, "memory": {"share_in_guild": False}},
            ),
            "ch-forced-off": ChannelInfo("ch-forced-off", "g-1", "cat-1", False, 2, "公告", False, {}),
            "ch-in-off-cat": ChannelInfo("ch-in-off-cat", "g-1", "cat-off", False, 0, "日志", None, {}),
            "ch-override-on": ChannelInfo("ch-override-on", "g-1", "cat-off", False, 1, "特例", True, {}),
            "ch-ungrouped": ChannelInfo("ch-ungrouped", "g-1", None, False, 0, "无分组", None, {}),
            "ch-in-off-guild": ChannelInfo("ch-in-off-guild", "g-off", None, False, 0, "停用服频道", True, {}),
        },
    )


def test_four_level_settings_inheritance_and_feature_toggles():
    snap = _sample_snapshot()

    # Guild overrides global; category overrides guild; channel overrides category
    s_inherit = snap.settings("ch-inherit")
    assert s_inherit.persona.name == "GuildBot"
    assert s_inherit.features.sticker_collect is False  # from guild
    assert s_inherit.features.tldr is False  # from category
    assert s_inherit.memory.share_in_guild is True

    s_override = snap.settings("ch-override")
    assert s_override.features.tldr is True  # channel overrides category
    assert s_override.features.sticker_collect is False  # still inherited from guild
    assert s_override.memory.share_in_guild is False  # excluded from guild-shared memory

    # merge_settings ignores empty/None sparse layers cleanly
    merged = merge_settings({"persona": {"name": "A"}}, None, {}, {"persona": {"name": "B"}})
    assert merged.persona.name == "B"


def test_tri_state_channel_enabled_and_shared_channels():
    snap = _sample_snapshot()
    assert snap.channel_enabled("ch-inherit") is True  # inherits cat-1=True
    assert snap.channel_enabled("ch-forced-off") is False  # explicit False beats cat-1=True
    assert snap.channel_enabled("ch-in-off-cat") is False  # inherits cat-off=False
    assert snap.channel_enabled("ch-override-on") is True  # explicit True beats cat-off=False
    assert snap.channel_enabled("ch-ungrouped") is False  # uncategorized None defaults False
    assert snap.channel_enabled("ch-in-off-guild") is False  # guild.enabled=False gates everything

    # Guild-shared memory excludes disabled channels and share_in_guild=False channels
    shared = snap.shared_channel_ids("g-1")
    assert "ch-inherit" in shared
    assert "ch-override-on" in shared
    assert "ch-override" not in shared  # share_in_guild=False
    assert "ch-forced-off" not in shared


def test_four_level_trigger_scope_precedence_and_custom_prefix():
    rules = (
        TriggerRule(1, "chat", "global", None, "keyword_prefix", ("ygg",), None, False, "quoted_or_self", 100, 0, ()),
        TriggerRule(2, "chat", "guild", "g-1", "keyword_prefix", ("小树",), None, False, "quoted_or_self", 100, 0, ()),
        TriggerRule(
            3, "chat", "category", "cat-1", "keyword_prefix", ("ask",), None, False, "quoted_or_self", 100, 0, ()
        ),
        TriggerRule(
            4, "chat", "channel", "ch-override", "keyword_prefix", ("ops",), None, False, "quoted_or_self", 100, 0, ()
        ),
        TriggerRule(5, "tldr", "global", None, "keyword_prefix", ("tldr",), None, False, "quoted_or_self", 50, 0, ()),
    )
    snap = Snapshot(triggers=rules, guilds=_sample_snapshot().guilds, channels=_sample_snapshot().channels)
    idx = TriggerIndex(snap)

    # ch-override uses channel rule ("ops"), hides category/guild/global chat rules, keeps global tldr
    assert idx.match(MatchInput("channel", "ch-override", "cat-1", "g-1", "ops 帮我查下", False, False, "member"))
    assert idx.match(MatchInput("channel", "ch-override", "cat-1", "g-1", "ygg 你好", False, False, "member")) is None
    tldr = idx.match(MatchInput("channel", "ch-override", "cat-1", "g-1", "tldr", False, False, "member"))
    assert tldr is not None
    assert tldr.handler == "tldr"

    # ch-inherit uses category rule ("ask"), hides guild ("小树") and global ("ygg")
    got_cat = idx.match(MatchInput("channel", "ch-inherit", "cat-1", "g-1", "ask 问题", False, False, "member"))
    assert got_cat is not None
    assert got_cat.args == "问题"
    assert idx.match(MatchInput("channel", "ch-inherit", "cat-1", "g-1", "小树 你好", False, False, "member")) is None

    # ch-override-on is in cat-off (no category rule), so it falls back to guild rule ("小树")
    got_guild = idx.match(
        MatchInput("channel", "ch-override-on", "cat-off", "g-1", "小树 晚安", False, False, "member")
    )
    assert got_guild is not None
    assert got_guild.args == "晚安"
    assert (
        idx.match(MatchInput("channel", "ch-override-on", "cat-off", "g-1", "ygg 晚安", False, False, "member")) is None
    )
