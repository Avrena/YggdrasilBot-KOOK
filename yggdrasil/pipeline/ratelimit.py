"""Fixed-window rate limits, daily token budgets (Redis) and per-process concurrency slots (per channel)."""

import asyncio
import contextlib
from collections import Counter
from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

from yggdrasil.core.metrics import RATELIMIT_REJECTED
from yggdrasil.kv import redis_client
from yggdrasil.settings import RateLimitSettings

_TZ = ZoneInfo("Asia/Shanghai")
_semaphores: dict[tuple[str, str | None, int], asyncio.Semaphore] = {}
_waiting: Counter[tuple[str | None, str]] = Counter()


async def _hit(key: str, window_s: int, limit: int) -> bool:
    r = redis_client()
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_s, nx=True)
    count, _ = await pipe.execute()
    return int(count) <= limit


def _day() -> str:
    return datetime.now(_TZ).strftime("%Y%m%d")


async def check(channel_id: str | None, user_id: str, cfg: RateLimitSettings, *, superuser: bool) -> str | None:
    if superuser:
        return None
    ch = channel_id or "dm"
    checks = [
        (f"ygg:rl:u:{user_id}:m", 60, cfg.user_per_minute, "user_minute"),
        (f"ygg:rl:u:{user_id}:d:{_day()}", 86400, cfg.user_per_day, "user_day"),
        (f"ygg:rl:c:{ch}:m", 60, cfg.channel_per_minute, "channel_minute"),
    ]
    for key, window, limit, scope in checks:
        if limit > 0 and not await _hit(key, window, limit):
            RATELIMIT_REJECTED.add(1, {"scope": scope})
            return scope
    if await tokens_exhausted(channel_id, cfg):
        RATELIMIT_REJECTED.add(1, {"scope": "channel_tokens"})
        return "channel_tokens"
    return None


async def tokens_exhausted(channel_id: str | None, cfg: RateLimitSettings) -> bool:
    if channel_id is None or cfg.channel_daily_tokens <= 0:
        return False
    used = await redis_client().get(f"ygg:tok:c:{channel_id}:{_day()}")
    return bool(used) and int(used) >= cfg.channel_daily_tokens


async def add_tokens(channel_id: str | None, tokens: int) -> None:
    if channel_id is None or tokens <= 0:
        return
    key = f"ygg:tok:c:{channel_id}:{_day()}"
    pipe = redis_client().pipeline()
    pipe.incrby(key, tokens)
    pipe.expire(key, 2 * 86400, nx=True)
    await pipe.execute()


def _sem(kind: str, key: str | None, limit: int) -> asyncio.Semaphore:
    k = (kind, key, max(limit, 1))
    sem = _semaphores.get(k)
    if sem is None:
        sem = _semaphores[k] = asyncio.Semaphore(k[2])
    return sem


@contextlib.asynccontextmanager
async def slot(channel_id: str | None, user_id: str, cfg: RateLimitSettings) -> AsyncIterator[bool]:
    user_key = (channel_id, user_id)
    if _waiting[user_key] >= cfg.max_queued_per_user:
        RATELIMIT_REJECTED.add(1, {"scope": "user_queue"})
        yield False
        return
    user_sem = _sem("user", user_id, cfg.max_concurrent_per_user)
    channel_sem = _sem("channel", channel_id, cfg.max_concurrent_per_channel)
    _waiting[user_key] += 1
    acquired: list[asyncio.Semaphore] = []
    try:
        try:
            async with asyncio.timeout(cfg.queue_timeout_s):
                for sem in (user_sem, channel_sem):
                    await sem.acquire()
                    acquired.append(sem)
        except TimeoutError:
            RATELIMIT_REJECTED.add(1, {"scope": "queue_timeout"})
            yield False
            return
        finally:
            _waiting[user_key] -= 1
        yield True
    finally:
        for sem in acquired:
            sem.release()


async def cooldown(rule_id: int, user_id: str, seconds: int) -> bool:
    if seconds <= 0:
        return True
    return bool(await redis_client().set(f"ygg:cd:{rule_id}:{user_id}", "1", ex=seconds, nx=True))
