"""Embedding spaces and ``memory_vectors`` maintenance (with ``guild_id`` + optional ``channel_id``)."""

from collections.abc import Sequence
from datetime import date
from typing import Any, cast

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.core.log import LOGGER
from yggdrasil.db.base import EMBED_DIM
from yggdrasil.db.models import DailyDigest, EmbedSpace, GlossaryTerm, MemoryVector, MessageChunk, Topic
from yggdrasil.db.session import get_session
from yggdrasil.llm import registry
from yggdrasil.runtime import EmbedSpaceInfo, publish_change, snapshot

KINDS: tuple[str, ...] = ("chunk", "topic", "glossary", "digest")
EMBED_MAX_CHARS = 3000
_BATCH = 64
_LOCK_KEY = "ygg:embed_space"

# (ref_id, guild_id, channel_id_or_None, text)
type VectorItem = tuple[int, str, str | None, str]


class EmbedDimensionError(RuntimeError):
    pass


def topic_text(title: str, summary: str, keywords: Sequence[str]) -> str:
    out = f"{title}\n{summary}"
    return f"{out}\n关键词：{'、'.join(keywords)}" if keywords else out


def glossary_text(term: str, aliases: Sequence[str], definition: str) -> str:
    head = f"{term}（又作{'、'.join(aliases)}）" if aliases else term
    return f"{head}：{definition}"


def digest_text(day: date, summary: str) -> str:
    return f"{day:%Y-%m-%d} 服务器摘要\n{summary}"


def query_text(model: str, query: str) -> str:
    if "qwen3-embedding" in model.lower():
        return f"Instruct: 给定一个频道聊天记录检索问题，找出相关的聊天内容\nQuery: {query}"
    return query


async def resolve_space() -> EmbedSpaceInfo | None:
    snap = snapshot()
    space = snap.embed_space
    if space is None:
        route = snap.routes.get("embedding")
        if route is None:
            return None
        created = False
        async with get_session() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _LOCK_KEY})
            row = await session.scalar(select(EmbedSpace).where(EmbedSpace.active))
            if row is None:
                row = EmbedSpace(provider_id=route.provider_id, model=route.model, dim=EMBED_DIM, active=True)
                row.status = "ready"
                session.add(row)
                await session.flush()
                created = True
            space = EmbedSpaceInfo(row.id, row.provider_id, row.model, row.dim)
        if created:
            LOGGER.info("created embed space {} ({})", space.id, space.model)
            await publish_change(f"embed_space {space.id} created")
    if space.dim != EMBED_DIM:
        LOGGER.error("embed space {} has dim {} but the schema is fixed at {}", space.id, space.dim, EMBED_DIM)
        return None
    return space


async def store_vectors(space: EmbedSpaceInfo, kind: str, items: Sequence[VectorItem]) -> int:
    if not items:
        return 0
    vectors = await registry.embed(
        [t[:EMBED_MAX_CHARS] for _, _, _, t in items], provider_id=space.provider_id, model=space.model
    )
    rows: list[dict[str, Any]] = []
    for (ref_id, guild_id, channel_id, _), vec in zip(items, vectors, strict=True):
        if len(vec) != EMBED_DIM:
            raise EmbedDimensionError(f"model {space.model} returned dim {len(vec)}, schema expects {EMBED_DIM}")
        rows.append(
            {
                "kind": kind,
                "ref_id": ref_id,
                "space_id": space.id,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "embedding": vec,
            }
        )
    stmt = pg_insert(MemoryVector).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["kind", "ref_id", "space_id"],
        set_={
            "embedding": stmt.excluded.embedding,
            "guild_id": stmt.excluded.guild_id,
            "channel_id": stmt.excluded.channel_id,
        },
    )
    async with get_session() as session:
        await session.execute(stmt)
    return len(rows)


async def delete_vectors(session: AsyncSession, kind: str, ref_ids: Sequence[int]) -> None:
    if ref_ids:
        await session.execute(delete(MemoryVector).where(MemoryVector.kind == kind, MemoryVector.ref_id.in_(ref_ids)))


async def try_embed(kind: str, items: Sequence[VectorItem]) -> None:
    if not items:
        return
    try:
        space = await resolve_space()
        if space is None:
            return
        await store_vectors(space, kind, items)
    except Exception as e:
        LOGGER.warning("embedding {} {} rows deferred: {!r}", len(items), kind, e)


async def _missing(
    session: AsyncSession,
    kind: str,
    space_id: int,
    *,
    guild_id: str | None = None,
    channel_id: str | None = None,
    limit: int,
) -> list[VectorItem]:
    model: Any
    has_channel = kind in ("chunk", "topic")
    match kind:
        case "chunk":
            model, cols = MessageChunk, (MessageChunk.text,)
        case "topic":
            model, cols = Topic, (Topic.title, Topic.summary, Topic.keywords)
        case "glossary":
            model, cols = GlossaryTerm, (GlossaryTerm.term, GlossaryTerm.aliases, GlossaryTerm.definition)
        case "digest":
            model, cols = DailyDigest, (DailyDigest.day, DailyDigest.summary)
        case _:
            raise ValueError(kind)
    has_vector = (
        select(MemoryVector.ref_id)
        .where(MemoryVector.kind == kind, MemoryVector.space_id == space_id, MemoryVector.ref_id == model.id)
        .exists()
    )
    if has_channel:
        q = select(model.id, model.guild_id, model.channel_id, *cols).where(~has_vector).order_by(model.id).limit(limit)
        if channel_id is not None:
            q = q.where(model.channel_id == channel_id)
        elif guild_id is not None:
            q = q.where(model.guild_id == guild_id)
    else:
        if channel_id is not None and guild_id is None:
            return []
        q = select(model.id, model.guild_id, *cols).where(~has_vector).order_by(model.id).limit(limit)
        if guild_id is not None:
            q = q.where(model.guild_id == guild_id)

    out: list[VectorItem] = []
    for row in (await session.execute(q)).all():
        vals = cast("list[Any]", list(row))
        if has_channel:
            ref_id, gid, cid, *rest = vals
        else:
            ref_id, gid, *rest = vals
            cid = None
        match kind:
            case "chunk":
                body = rest[0]
            case "topic":
                body = topic_text(*rest)
            case "glossary":
                body = glossary_text(*rest)
            case _:
                body = digest_text(*rest)
        out.append((ref_id, gid, cid, body))
    return out


async def embed_missing(
    space: EmbedSpaceInfo, *, guild_id: str | None = None, channel_id: str | None = None
) -> dict[str, int]:
    """Fill vectors for rows matching ``guild_id`` / ``channel_id`` (or all rows if both None)."""
    counts = dict.fromkeys(KINDS, 0)
    for kind in KINDS:
        while True:
            async with get_session() as session:
                items = await _missing(session, kind, space.id, guild_id=guild_id, channel_id=channel_id, limit=_BATCH)
            counts[kind] += await store_vectors(space, kind, items)
            if len(items) < _BATCH:
                break
    return counts


async def reembed_space(space_id: int) -> dict[str, int]:
    async with get_session() as session:
        row = await session.get(EmbedSpace, space_id)
        if row is None:
            raise LookupError(f"embed space {space_id} not found")
        if row.dim != EMBED_DIM:
            raise EmbedDimensionError(f"embed space {space_id} has dim {row.dim}, schema expects {EMBED_DIM}")
        if not row.active:
            row.status = "backfilling"
        space = EmbedSpaceInfo(row.id, row.provider_id, row.model, row.dim)

    totals = dict.fromkeys(KINDS, 0)
    while True:
        counts = await embed_missing(space)
        for k, v in counts.items():
            totals[k] += v
        async with get_session() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _LOCK_KEY})
            if any([await _missing(session, kind, space.id, limit=1) for kind in KINDS]):
                continue
            await session.execute(
                update(EmbedSpace).where(EmbedSpace.id == space.id).values(active=True, status="ready")
            )
            removed = await session.scalar(
                select(func.count()).select_from(MemoryVector).where(MemoryVector.space_id != space.id)
            )
            await session.execute(delete(EmbedSpace).where(EmbedSpace.id != space.id))
        break
    LOGGER.info("embed space {} active; embedded {}; dropped {} old vectors", space.id, totals, removed)
    await publish_change(f"embed_space {space.id} activated")
    return totals
