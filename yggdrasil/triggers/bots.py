"""Bot-user detection and anti-loop guard for KOOK bot-to-bot interaction.

Other bots' messages are always recorded in channel history like anyone else's, and when
``interaction.allow_bot_triggers`` is enabled they may even invoke chat / tldr — subject to three independent
anti-loop guards so two bots can never ping-pong endlessly (even with slow 30s+ long-text replies):

1. **Cooldown** (``bot_cooldown_s``): minimum gap between triggers from the same bot in a channel, refreshed both on
   admission and when our reply finishes sending.
2. **Consecutive bot-turn breaker** (``bot_max_consecutive_turns``): counts consecutive bot-triggered runs in a
   channel without any human message in between; resets immediately when any non-bot user speaks in that channel.
3. **Rolling window cap** (``bot_max_per_window`` / ``bot_window_s``): hard cap on replies to a given bot within a
   longer window (default 3 per 10 minutes), defeating slow ping-pong loops regardless of reply latency.
"""

from yggdrasil.kook.model import Incoming
from yggdrasil.kv import redis_client
from yggdrasil.settings import InteractionSettings, Settings

_TAG = "[bot]"
_CHAIN_TTL_S = 1800  # 30 minutes of silence also clears consecutive bot-turn counter


def is_ignored_user(incoming: Incoming, settings: Settings) -> bool:
    """Explicitly ignored user IDs never trigger any handler."""
    return incoming.user_id in settings.interaction.ignore_users


def is_bot_sender(incoming: Incoming) -> bool:
    """Whether KOOK marked the author as a bot (``author.bot == true``) or their name carries ``[bot]``."""
    return bool(incoming.sender.is_bot) or (_TAG in incoming.sender.name.lower())


def sent_by_bot(incoming: Incoming, settings: Settings) -> bool:
    """Backward-compatible helper: True when sender is explicitly ignored or is another bot."""
    return is_ignored_user(incoming, settings) or is_bot_sender(incoming)


async def note_human_message(channel_id: str) -> None:
    """Reset the consecutive bot-turn circuit breaker when a real human speaks in ``channel_id``."""
    try:
        await redis_client().delete(f"ygg:bot_chain:{channel_id}")
    except Exception:
        pass


async def touch_bot_cooldown(channel_id: str | None, bot_user_id: str, cooldown_s: int) -> None:
    """Refresh the per-bot cooldown when our reply finishes sending so slow replies can't bypass cooldown."""
    if channel_id is None or cooldown_s <= 0:
        return
    try:
        await redis_client().set(f"ygg:bot_cd:{channel_id}:{bot_user_id}", "1", ex=cooldown_s)
    except Exception:
        pass


async def check_bot_trigger(
    channel_id: str,
    bot_user_id: str,
    handler: str,
    cfg: InteractionSettings,
) -> str | None:
    """Return None if another bot's trigger is allowed, or a short reason string when blocked."""
    if not cfg.allow_bot_triggers:
        return "bot_triggers_disabled"
    # Other bots may only invoke normal conversation / summary handlers, never admin or privacy commands.
    if handler not in ("chat", "tldr"):
        return f"bot_handler_disallowed:{handler}"

    r = redis_client()

    # 1. Per-bot cooldown (checked first without incrementing other counters).
    cd_key = f"ygg:bot_cd:{channel_id}:{bot_user_id}"
    if cfg.bot_cooldown_s > 0:
        if await r.exists(cd_key):
            return "bot_cooldown"

    # 2. Consecutive bot-turn breaker (defeats slow 30s+ long-text ping-pong loops).
    chain_key = f"ygg:bot_chain:{channel_id}"
    cur_chain = await r.get(chain_key)
    if cur_chain is not None and int(cur_chain) >= cfg.bot_max_consecutive_turns:
        return "bot_consecutive_limit"

    # 3. Longer rolling window cap per (channel, bot).
    win_key = f"ygg:bot_win:{channel_id}:{bot_user_id}"
    cur_win = await r.get(win_key)
    if cur_win is not None and int(cur_win) >= cfg.bot_max_per_window:
        return "bot_window_limit"

    # Admit and update all three guards atomically via pipeline.
    pipe = r.pipeline()
    if cfg.bot_cooldown_s > 0:
        pipe.set(cd_key, "1", ex=cfg.bot_cooldown_s)
    pipe.incr(chain_key)
    pipe.expire(chain_key, _CHAIN_TTL_S)
    pipe.incr(win_key)
    pipe.expire(win_key, cfg.bot_window_s, nx=True)
    await pipe.execute()
    return None
