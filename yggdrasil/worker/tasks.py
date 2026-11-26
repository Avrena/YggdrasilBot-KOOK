"""Registered worker tasks and their schedules (cron, Asia/Shanghai).

- Channel-scoped steps: chunking & topic segmentation (isolated per channel) and sticker drops.
- Guild-scoped steps: daily digests, curation, user profiles, and glossary (shared across the guild).
"""

from collections.abc import Awaitable, Callable
from typing import Any

from redis.exceptions import LockError
from sqlalchemy import text

from yggdrasil.core.log import LOGGER
from yggdrasil.core.telemetry import TRACER
from yggdrasil.db.partitions import ensure_partitions
from yggdrasil.db.session import engine
from yggdrasil.kv import redis_client
from yggdrasil.memory import vectors
from yggdrasil.memory.chunking import chunk_channel
from yggdrasil.memory.common import memory_channels, memory_guilds
from yggdrasil.memory.curation import curate_guild
from yggdrasil.memory.digest import digest_guild
from yggdrasil.memory.glossary import define_guild, mine_guild
from yggdrasil.memory.profiles import update_guild
from yggdrasil.memory.topics import consolidate_channel
from yggdrasil.runtime import snapshot
from yggdrasil.stickers.drop import maybe_drop
from yggdrasil.stickers.tagging import tag_pending
from yggdrasil.worker.broker import TIMEZONE, broker


def _cron(expr: str) -> list[dict[str, str]]:
    return [{"cron": expr, "cron_offset": TIMEZONE}]


class ScopeFailures(RuntimeError):
    def __init__(self, job: str, scopes: list[str]) -> None:
        super().__init__(f"{job} failed for scopes {scopes}")
        self.scopes = scopes


async def _for_each(
    job: str, kind: str, scopes: list[str], step: Callable[[str], Awaitable[Any]], *, lock_ttl: int = 1800
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    failed: list[str] = []
    for sid in scopes:
        lock = redis_client().lock(f"ygg:job:{job}:{sid}", timeout=lock_ttl, blocking=False)
        if not await lock.acquire():
            LOGGER.info("{}: {} {} is still being processed; skipped", job, kind, sid)
            continue
        try:
            with TRACER.start_as_current_span(f"{job} {kind}", attributes={f"ygg.{kind}_id": sid}):
                results[sid] = await step(sid)
        except Exception:
            failed.append(sid)
            LOGGER.exception("{}: {} {} failed", job, kind, sid)
        finally:
            try:
                await lock.release()
            except LockError:
                LOGGER.warning("{}: lock for {} {} expired before release", job, kind, sid)
    LOGGER.info("{}: {}", job, results)
    if failed:
        raise ScopeFailures(job, failed)
    return results


async def for_each_channel(job: str, step: Callable[[str], Awaitable[Any]], *, lock_ttl: int = 1800) -> dict[str, Any]:
    return await _for_each(job, "channel", memory_channels(snapshot()), step, lock_ttl=lock_ttl)


async def for_each_guild(job: str, step: Callable[[str], Awaitable[Any]], *, lock_ttl: int = 1800) -> dict[str, Any]:
    return await _for_each(job, "guild", memory_guilds(snapshot()), step, lock_ttl=lock_ttl)


@broker.task(task_name="maintain_partitions", schedule=_cron("47 * * * *"))
async def maintain_partitions() -> list[str]:
    async with engine().begin() as conn:
        await conn.execute(text("SET LOCAL lock_timeout = '3s'"))
        names = await ensure_partitions(conn, ahead=3)
    LOGGER.info("message partitions ensured: {}", names)
    return names


@broker.task(task_name="sweep_bot_events", schedule=_cron("20 3 * * *"))
async def sweep_bot_events() -> dict[str, int]:
    from yggdrasil.events import sweep

    stats = await sweep()
    LOGGER.info("bot events swept: {}", stats)
    return stats


async def _chunk_and_embed(channel_id: str) -> dict[str, int]:
    stats = await chunk_channel(channel_id)
    space = await vectors.resolve_space()
    if space is not None:
        stats |= await vectors.embed_missing(space, channel_id=channel_id)
    return stats


@broker.task(task_name="chunk_and_embed", schedule=_cron("*/5 * * * *"))
async def chunk_and_embed() -> dict[str, Any]:
    return await for_each_channel("chunk_and_embed", _chunk_and_embed, lock_ttl=900)


@broker.task(task_name="consolidate_topics", schedule=_cron("*/15 * * * *"))
async def consolidate_topics() -> dict[str, Any]:
    return await for_each_channel("consolidate_topics", consolidate_channel, lock_ttl=3600)


@broker.task(task_name="drop_stickers", schedule=_cron("1-59/5 * * * *"))
async def drop_stickers() -> dict[str, str]:
    lock = redis_client().lock("ygg:job:drop_stickers", timeout=240, blocking=False)
    if not await lock.acquire():
        LOGGER.info("drop_stickers: previous run still going; skipped")
        return {}
    snap = snapshot()
    results: dict[str, str] = {}
    failed: list[str] = []
    try:
        for cid in sorted(snap.channels):
            ch = snap.channels[cid]
            if ch.is_category or not snap.channel_enabled(cid):
                continue
            if not snap.settings(channel_id=cid, guild_id=ch.guild_id).sticker_drop.enabled:
                continue
            try:
                drop = await maybe_drop(cid)
            except Exception:
                failed.append(cid)
                LOGGER.exception("drop_stickers: channel {} failed", cid)
                continue
            results[cid] = drop.outcome if drop.fit is None else f"{drop.outcome} fit={drop.fit:.2f}"
    finally:
        try:
            await lock.release()
        except LockError:
            LOGGER.warning("drop_stickers: lock expired before release")
    if any(outcome != "quiet" for outcome in results.values()):
        LOGGER.info("drop_stickers: {}", results)
    if failed:
        raise ScopeFailures("drop_stickers", failed)
    return results


@broker.task(task_name="daily_digest", schedule=_cron("5 * * * *"))
async def daily_digest() -> dict[str, Any]:
    return await for_each_guild("daily_digest", digest_guild)


@broker.task(task_name="curate_memory", schedule=_cron("20 * * * *"))
async def curate_memory() -> dict[str, Any]:
    return await for_each_guild("curate_memory", curate_guild, lock_ttl=3600)


@broker.task(task_name="update_profiles", schedule=_cron("25 * * * *"))
async def update_profiles() -> dict[str, Any]:
    return await for_each_guild("update_profiles", update_guild, lock_ttl=3600)


@broker.task(task_name="mine_glossary", schedule=_cron("30 4 * * *"))
async def mine_glossary() -> dict[str, Any]:
    return await for_each_guild("mine_glossary", mine_guild)


@broker.task(task_name="define_glossary", schedule=_cron("0 5 * * *"))
async def define_glossary() -> dict[str, Any]:
    return await for_each_guild("define_glossary", define_guild)


@broker.task(task_name="tag_stickers", schedule=_cron("*/5 * * * *"))
async def tag_stickers() -> dict[str, int]:
    lock = redis_client().lock("ygg:job:tag_stickers", timeout=1800, blocking=False)
    if not await lock.acquire():
        LOGGER.info("tag_stickers: previous run still going; skipped")
        return {}
    try:
        stats = await tag_pending()
    finally:
        try:
            await lock.release()
        except LockError:
            LOGGER.warning("tag_stickers: lock expired before release")
    LOGGER.info("tag_stickers: {}", stats)
    return stats


@broker.task(task_name="reembed_space")
async def reembed_space(space_id: int) -> dict[str, int]:
    lock = redis_client().lock("ygg:job:reembed_space", timeout=6 * 3600, blocking=False)
    if not await lock.acquire():
        raise RuntimeError("another re-embedding is running")
    try:
        return await vectors.reembed_space(space_id)
    finally:
        try:
            await lock.release()
        except LockError:
            LOGGER.warning("reembed_space: lock expired before release")
