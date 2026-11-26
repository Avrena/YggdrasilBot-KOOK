"""Tests for KOOK bot-to-bot anti-loop guards (cooldown, consecutive-turn breaker, rolling window, human reset)."""

from tests.support.kook import make_incoming
from yggdrasil.kv import redis_client
from yggdrasil.settings import InteractionSettings, Settings
from yggdrasil.triggers.bots import (
    check_bot_trigger,
    is_bot_sender,
    is_ignored_user,
    note_human_message,
    touch_bot_cooldown,
)


def test_is_bot_sender_detects_author_bot_flag_and_tag():
    assert is_bot_sender(make_incoming(is_bot=True)) is True
    assert is_bot_sender(make_incoming(username="Music[bot]")) is True
    assert is_bot_sender(make_incoming(nickname="小助手 [BOT]")) is True
    assert is_bot_sender(make_incoming(username="alice", nickname="爱丽丝")) is False


def test_is_ignored_user():
    cfg = Settings.model_validate({"interaction": {"ignore_users": ["u-ban"]}})
    assert is_ignored_user(make_incoming(user_id="u-ban"), cfg) is True
    assert is_ignored_user(make_incoming(user_id="u-ok"), cfg) is False


async def test_bot_trigger_disabled_or_disallowed_handler():
    cfg_off = InteractionSettings(allow_bot_triggers=False)
    assert await check_bot_trigger("ch-1", "bot-2", "chat", cfg_off) == "bot_triggers_disabled"

    cfg_on = InteractionSettings(allow_bot_triggers=True)
    # Other bots may never run admin or privacy commands
    reason = await check_bot_trigger("ch-1", "bot-2", "admin.ops", cfg_on)
    assert reason == "bot_handler_disallowed:admin.ops"


async def test_cooldown_and_reply_completion_refresh():
    cfg = InteractionSettings(
        allow_bot_triggers=True,
        bot_cooldown_s=20,
        bot_max_consecutive_turns=5,
        bot_max_per_window=10,
    )
    # Turn 1 admitted
    assert await check_bot_trigger("ch-1", "bot-2", "chat", cfg) is None
    # Immediately blocked by cooldown
    assert await check_bot_trigger("ch-1", "bot-2", "chat", cfg) == "bot_cooldown"

    # Simulate cooldown expiry, then our slow reply finishes and calls touch_bot_cooldown -> blocks again
    await redis_client().delete("ygg:bot_cd:ch-1:bot-2")
    await touch_bot_cooldown("ch-1", "bot-2", cfg.bot_cooldown_s)
    assert await check_bot_trigger("ch-1", "bot-2", "chat", cfg) == "bot_cooldown"

    # After cooldown expires, Turn 2 is admitted
    await redis_client().delete("ygg:bot_cd:ch-1:bot-2")
    assert await check_bot_trigger("ch-1", "bot-2", "chat", cfg) is None


async def test_consecutive_turn_breaker_stops_slow_long_text_loops_and_resets_on_human():
    cfg = InteractionSettings(
        allow_bot_triggers=True,
        bot_cooldown_s=20,
        bot_max_consecutive_turns=2,
        bot_max_per_window=10,
    )
    # Slow 35s ping-pong (cooldown has expired before each turn!)
    assert await check_bot_trigger("ch-slow", "bot-2", "chat", cfg) is None
    await redis_client().delete("ygg:bot_cd:ch-slow:bot-2")
    assert await check_bot_trigger("ch-slow", "bot-2", "chat", cfg) is None
    await redis_client().delete("ygg:bot_cd:ch-slow:bot-2")

    # 3rd consecutive bot turn trips the circuit breaker even with no cooldown!
    assert await check_bot_trigger("ch-slow", "bot-2", "chat", cfg) == "bot_consecutive_limit"

    # A human speaks in that channel -> breaker resets immediately
    await note_human_message("ch-slow")
    assert await check_bot_trigger("ch-slow", "bot-2", "chat", cfg) is None


async def test_rolling_window_cap_stops_persistent_bot_chatter():
    cfg = InteractionSettings(
        allow_bot_triggers=True,
        bot_cooldown_s=0,
        bot_max_consecutive_turns=2,
        bot_max_per_window=3,
        bot_window_s=600,
    )
    for _ in range(3):
        await note_human_message("ch-win")
        assert await check_bot_trigger("ch-win", "bot-2", "chat", cfg) is None

    # 4th trigger inside window is blocked even after a human speaks
    await note_human_message("ch-win")
    assert await check_bot_trigger("ch-win", "bot-2", "chat", cfg) == "bot_window_limit"

    # Once window expires, allowed again
    await redis_client().delete("ygg:bot_win:ch-win:bot-2")
    assert await check_bot_trigger("ch-win", "bot-2", "chat", cfg) is None
