"""Read API over channel history and guild-shared memory.

- Live conversation readers (``recent_messages``, ``messages_after``, ``find_by_msg_id``) stay isolated per channel.
- Long-term memory (``search_history``, ``messages_around``, ``message_segments``, ``open_topics``,
  ``lookup_glossary``, ``profiles``) is shared across all ``share_in_guild=True`` channels of the same KOOK guild,
  carrying ``channel_id`` so the model can cite and expand messages from sibling channels.
"""

import math
import re
from collections.abc import Collection, Hashable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import Row, bindparam, func, or_, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.access import can_bypass_channel_privacy
from yggdrasil.core.log import LOGGER
from yggdrasil.db.base import EMBED_DIM
from yggdrasil.db.models import GlossaryTerm, Message, Topic, UserProfile, UserProfileFact
from yggdrasil.db.session import get_session
from yggdrasil.llm import registry
from yggdrasil.memory.common import decay, fact_rank
from yggdrasil.memory.vectors import query_text
from yggdrasil.runtime import snapshot

RRF_K = 60
BM25_TOP = 50
VECTOR_TOP = 30
RERANK_POOL = 40
RERANK_MAX_CHARS = 1200
RECENT_CAP = 300
MSG_ID_LOOKBACK = timedelta(days=30)
GLOSSARY_MAX_DISTANCE = 0.45
PROFILE_FACTS = 10
SEMANTIC_MIN_SIMILARITY = 0.6
HIT_MAX_CHARS = 800
EXCERPT_MESSAGE_CHARS = 300
_CHUNK_LINE = re.compile(r"^\[\d{2}-\d{2} \d{2}:\d{2}\] ", re.MULTILINE)
TOPIC_WEIGHT_PIVOT = 5.0
TOPIC_WEIGHT_SPREAD = 0.05
# Subtle boost for hits from the active channel during guild-wide search.
SAME_CHANNEL_BOOST = 1.05


@dataclass(frozen=True, slots=True)
class MessageView:
    id: int
    channel_id: str
    guild_id: str
    kook_message_id: str
    sent_at: datetime
    user_id: str
    sender_name: str
    text: str
    reply_to_msg_id: str | None
    is_self: bool
    segments: list[dict[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class HistoryHit:
    kind: str  # message | chunk | topic | digest
    ref_id: int
    text: str
    when: datetime
    score: float
    channel_id: str | None = None
    msg_id: int | None = None
    user_id: str | None = None
    sender_name: str | None = None
    media: bool = False
    span: tuple[int, int, datetime, datetime] | None = None


@dataclass(frozen=True, slots=True)
class TopicView:
    id: int
    channel_id: str
    title: str
    summary: str
    keywords: list[str]
    participants: list[str]
    start_at: datetime
    end_at: datetime
    msg_count: int


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    id: int
    term: str
    aliases: list[str]
    definition: str
    confidence: float
    locked: bool


@dataclass(frozen=True, slots=True)
class ProfileView:
    user_id: str
    display_name: str
    summary: str
    interests: list[str]
    traits: list[str]
    facts: list[str]
    updated_at: datetime


VIEW_COLUMNS = (
    Message.id,
    Message.channel_id,
    Message.guild_id,
    Message.kook_message_id,
    Message.sent_at,
    Message.user_id,
    Message.sender_name,
    Message.plain_text,
    Message.reply_to_msg_id,
    Message.is_self,
)
CHAT_COLUMNS = (*VIEW_COLUMNS, Message.segments)


def _view(row: Row[Any] | Sequence[Any]) -> MessageView:
    return MessageView(*row)


def _visible_channel(channel_id: str) -> tuple[Any, ...]:
    return (Message.channel_id == channel_id, Message.recalled.is_(False))


def _clean_ids(values: Sequence[str]) -> set[str]:
    return {v.strip() for v in values if v and v.strip()}


def _matches_scope(channel_id: str, parent_id: str | None, scopes: set[str]) -> bool:
    return channel_id in scopes or (parent_id is not None and parent_id in scopes)


def accessible_channels(
    guild_id: str,
    channel_id: str | None,
    *,
    current_channel_only: bool = False,
    role_ids: Collection[str] = (),
) -> list[str]:
    """Channel IDs visible to a memory query originating from ``(guild_id, channel_id)``.

    One-way privacy model (all inherited along ``global -> guild -> category -> channel``):
    - ``share_in_guild=False`` hides a channel from other channels by default (and keeps it out of guild-wide
      digests/glossary/profiles), while the channel itself may still read public or explicitly authorized channels
      unless its own ``cross_channel_read`` is ``False``.
    - Target-side grants (``readable_by_scopes``, ``readable_by_roles``) let a private channel/category expose itself
      to specific caller channels/categories (e.g. ticket category -> admin category) or KOOK roles.
    - Caller-side ``cross_channel_deny`` / ``cross_channel_allow`` restrict which target channels/categories can be
      cited from the current channel (deny wins over allow and sharing).
    - ``access.role_rules[role_id].bypass_channel_privacy`` lets a role bypass cross-channel restrictions within the
      configured scope.
    """
    snap = snapshot()
    if current_channel_only and channel_id:
        return [channel_id]

    caller_settings = snap.settings(channel_id=channel_id, guild_id=guild_id)
    caller_mem = caller_settings.memory
    holder_roles = {str(r).strip() for r in role_ids if str(r).strip()}
    candidates = snap.guild_channels(guild_id, enabled_only=True)

    if can_bypass_channel_privacy(caller_settings, holder_roles):
        visible = {c.channel_id for c in candidates}
        if channel_id:
            visible.add(channel_id)
        return sorted(visible)

    if channel_id and not caller_mem.cross_channel_read:
        return [channel_id]

    caller_scopes: set[str] = set()
    if channel_id:
        caller_scopes.add(channel_id)
        caller_info = snap.channels.get(channel_id)
        if caller_info is not None and caller_info.parent_id:
            caller_scopes.add(caller_info.parent_id)

    allow_scopes = _clean_ids(caller_mem.cross_channel_allow)
    deny_scopes = _clean_ids(caller_mem.cross_channel_deny)

    visible: set[str] = {channel_id} if channel_id else set()
    for target in candidates:
        if target.channel_id == channel_id:
            continue
        t_mem = snap.settings(channel_id=target.channel_id, guild_id=guild_id).memory
        if not t_mem.enabled:
            continue
        if deny_scopes and _matches_scope(target.channel_id, target.parent_id, deny_scopes):
            continue
        if allow_scopes and not _matches_scope(target.channel_id, target.parent_id, allow_scopes):
            continue
        if t_mem.share_in_guild:
            visible.add(target.channel_id)
            continue
        readable_scopes = _clean_ids(t_mem.readable_by_scopes)
        readable_roles = _clean_ids(t_mem.readable_by_roles)
        if (caller_scopes & readable_scopes) or (holder_roles & readable_roles):
            visible.add(target.channel_id)
    return sorted(visible)


# --- raw messages -----------------------------------------------------------------------------------------------


async def recent_messages(channel_id: str, *, limit: int = 40, minutes: int = 0) -> list[MessageView]:
    """The last ``limit`` messages in ``channel_id``, extended to the last ``minutes`` minutes (capped at 300)."""
    newest_first = (
        select(*CHAT_COLUMNS).where(*_visible_channel(channel_id)).order_by(Message.sent_at.desc(), Message.id.desc())
    )
    async with get_session() as session:
        rows = list((await session.execute(newest_first.limit(limit))).all()) if limit > 0 else []
        if minutes > 0:
            since = datetime.now(UTC) - timedelta(minutes=minutes)
            windowed = await session.execute(newest_first.where(Message.sent_at >= since).limit(RECENT_CAP))
            seen = {r[0] for r in rows}
            rows.extend(r for r in windowed.all() if r[0] not in seen)
    views = sorted((_view(r) for r in rows), key=lambda v: (v.sent_at, v.id))
    return views[-max(limit, RECENT_CAP) :]


async def messages_around(
    guild_id: str,
    message_id: int,
    before: int = 10,
    after: int = 10,
    *,
    allowed_channels: Sequence[str] | None = None,
) -> tuple[str | None, list[MessageView]]:
    """Context window around ``#message_id`` inside its own channel (must belong to ``guild_id`` and
    ``allowed_channels``). Returns ``(source_channel_id, messages)``."""
    async with get_session() as session:
        anchor_q = select(Message.sent_at, Message.channel_id).where(
            Message.id == message_id, Message.guild_id == guild_id
        )
        if allowed_channels is not None:
            anchor_q = anchor_q.where(Message.channel_id.in_(list(allowed_channels)))
        anchor = (await session.execute(anchor_q)).first()
        if anchor is None:
            return None, []
        sent_at, source_channel = anchor
        key = tuple_(Message.sent_at, Message.id)
        base = select(*CHAT_COLUMNS).where(*_visible_channel(source_channel))
        older = await session.execute(
            base.where(key < tuple_(sent_at, message_id))
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(before)
        )
        newer = await session.execute(
            base.where(key >= tuple_(sent_at, message_id)).order_by(Message.sent_at, Message.id).limit(after + 1)
        )
        rows = [*reversed(older.all()), *newer.all()]
    return source_channel, [_view(r) for r in rows]


async def messages_after(
    channel_id: str, after_id: int | None, since: datetime, *, limit: int = RECENT_CAP
) -> list[MessageView]:
    query = select(*CHAT_COLUMNS).where(*_visible_channel(channel_id), Message.sent_at >= since)
    if after_id is not None:
        query = query.where(Message.id > after_id)
    async with get_session() as session:
        rows = await session.execute(query.order_by(Message.sent_at.desc(), Message.id.desc()).limit(limit))
    return sorted(map(_view, rows), key=lambda v: (v.sent_at, v.id))


async def find_by_msg_id(channel_id: str, kook_message_id: str) -> MessageView | None:
    since = datetime.now(UTC) - MSG_ID_LOOKBACK
    async with get_session() as session:
        row = (
            await session.execute(
                select(*CHAT_COLUMNS)
                .where(
                    *_visible_channel(channel_id),
                    Message.kook_message_id == kook_message_id,
                    Message.sent_at >= since,
                )
                .order_by(Message.sent_at.desc())
                .limit(1)
            )
        ).first()
    return _view(row) if row else None


async def find_by_msg_ids(channel_id: str, kook_message_ids: Collection[str]) -> dict[str, MessageView]:
    if not kook_message_ids:
        return {}
    since = datetime.now(UTC) - MSG_ID_LOOKBACK
    async with get_session() as session:
        rows = await session.execute(
            select(*CHAT_COLUMNS)
            .where(
                *_visible_channel(channel_id),
                Message.kook_message_id.in_(list(kook_message_ids)),
                Message.sent_at >= since,
            )
            .order_by(Message.sent_at)
        )
    return {v.kook_message_id: v for v in map(_view, rows)}


async def message_segments(
    guild_id: str, ids: Sequence[int], *, allowed_channels: Sequence[str] | None = None
) -> dict[int, list[dict[str, Any]]]:
    """Raw segments of messages belonging to ``guild_id`` (and ``allowed_channels`` when given)."""
    if not ids:
        return {}
    q = select(Message.id, Message.segments).where(
        Message.guild_id == guild_id, Message.recalled.is_(False), Message.id.in_(list(ids))
    )
    if allowed_channels is not None:
        q = q.where(Message.channel_id.in_(list(allowed_channels)))
    async with get_session() as session:
        rows = await session.execute(q)
    return {r[0]: r[1] for r in rows}


# --- hybrid history search --------------------------------------------------------------------------------------


def rrf_fuse[K: Hashable](rankings: Sequence[Sequence[K]], k: int = RRF_K) -> list[tuple[K, float]]:
    scores: dict[K, float] = {}
    for ranking in rankings:
        seen: set[K] = set()
        for rank, key in enumerate(ranking, start=1):
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


_MEDIA_PATH = (
    '$[*] ? (@.type == "image" || @.type == "file" || @.type == "card" || @.type == "video" || @.type == "audio")'
)


async def _bm25(
    session: AsyncSession,
    guild_id: str,
    channels: Sequence[str],
    query: str,
    since: datetime | None,
    until: datetime | None,
    user_id: str | None,
) -> list[HistoryHit]:
    if not channels:
        return []
    conds = ["plain_text ||| :q", "guild_id = :g", "channel_id = ANY(:chs)", "recalled = false"]
    params: dict[str, Any] = {"q": query, "g": guild_id, "chs": list(channels), "lim": BM25_TOP}
    if since is not None:
        conds.append("sent_at >= :since")
        params["since"] = since
    if until is not None:
        conds.append("sent_at < :until")
        params["until"] = until
    if user_id is not None:
        conds.append("user_id = :u")
        params["u"] = user_id
    sql = (
        "SELECT id, channel_id, sent_at, user_id, sender_name, plain_text, pdb.score(id) AS score, "
        f"jsonb_path_exists(segments, '{_MEDIA_PATH}') AS media FROM messages "
        f"WHERE {' AND '.join(conds)} ORDER BY score DESC, id DESC LIMIT :lim"
    )
    rows = (await session.execute(text(sql), params)).all()
    return [
        HistoryHit(
            kind="message",
            ref_id=r.id,
            text=r.plain_text,
            when=r.sent_at,
            score=float(r.score),
            channel_id=r.channel_id,
            msg_id=r.id,
            user_id=r.user_id,
            sender_name=r.sender_name,
            media=bool(r.media),
        )
        for r in rows
    ]


async def _query_vector(query: str) -> tuple[int, list[float]] | None:
    space = snapshot().embed_space
    if space is None or space.dim != EMBED_DIM:
        return None
    [vec] = await registry.embed([query_text(space.model, query)], provider_id=space.provider_id, model=space.model)
    return space.id, vec


_VECTOR_SQL = """
SELECT v.kind, v.ref_id, v.channel_id, v.embedding <=> CAST(:qv AS halfvec) AS dist,
       COALESCE(c.text, t.title || E'\\n' || t.summary, d.summary) AS body,
       COALESCE(c.start_at, t.start_at, d.day::timestamp AT TIME ZONE 'Asia/Shanghai') AS at,
       t.weight, c.first_msg_id, c.last_msg_id, c.end_at
FROM memory_vectors v
LEFT JOIN message_chunks c ON v.kind = 'chunk' AND c.id = v.ref_id
LEFT JOIN topics t ON v.kind = 'topic' AND t.id = v.ref_id AND t.status <> 'archived'
LEFT JOIN daily_digests d ON v.kind = 'digest' AND d.id = v.ref_id
WHERE v.guild_id = :g AND v.space_id = :s AND v.kind IN ('chunk', 'topic', 'digest')
  AND (v.channel_id = ANY(:chs) OR (v.kind = 'digest' AND :include_digests))
  AND COALESCE(c.id, t.id, d.id) IS NOT NULL
"""


async def _vector(
    session: AsyncSession,
    guild_id: str,
    channels: Sequence[str],
    include_digests: bool,
    space_id: int,
    vec: list[float],
    since: datetime | None,
    until: datetime | None,
    user_id: str | None,
) -> list[HistoryHit]:
    if not channels and not include_digests:
        return []
    conds: list[str] = []
    params: dict[str, Any] = {
        "qv": vec,
        "g": guild_id,
        "chs": list(channels),
        "include_digests": include_digests,
        "s": space_id,
        "lim": VECTOR_TOP,
    }
    if since is not None:
        conds.append("COALESCE(c.end_at, t.end_at, (d.day + 1)::timestamp AT TIME ZONE 'Asia/Shanghai') >= :since")
        params["since"] = since
    if until is not None:
        conds.append("COALESCE(c.end_at, t.start_at, d.day::timestamp AT TIME ZONE 'Asia/Shanghai') < :until")
        params["until"] = until
    if user_id is not None:
        conds.append(
            "((v.kind = 'chunk' AND EXISTS (SELECT 1 FROM messages m"
            " WHERE m.channel_id = c.channel_id AND m.user_id = :u"
            " AND NOT m.recalled AND m.id BETWEEN c.first_msg_id AND c.last_msg_id"
            " AND m.sent_at BETWEEN c.start_at AND c.end_at))"
            " OR (v.kind = 'topic' AND :u = ANY(t.participants)))"
        )
        params["u"] = user_id
    sql = _VECTOR_SQL + "".join(f"  AND {c}\n" for c in conds) + "ORDER BY dist LIMIT :lim"
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    stmt = text(sql).bindparams(bindparam("qv", type_=HALFVEC(EMBED_DIM)))
    rows = (await session.execute(stmt, params)).all()
    half_life = snapshot().settings(guild_id=guild_id).memory.weight_half_life_days
    now = datetime.now(UTC)
    hits = [
        HistoryHit(
            kind=r.kind,
            ref_id=r.ref_id,
            text=r.body,
            when=r.at,
            score=1.0 - float(r.dist),
            channel_id=r.channel_id,
            span=(r.first_msg_id, r.last_msg_id, r.at, r.end_at) if r.kind == "chunk" else None,
        )
        for r in rows
    ]
    factors = [_topic_factor(r.weight, now - r.at, half_life) for r in rows]
    order = sorted(range(len(hits)), key=lambda i: -hits[i].score * factors[i])
    return [hits[i] for i in order]


def _topic_factor(weight: float | None, age: timedelta, half_life_days: float) -> float:
    if weight is None:
        return 1.0
    ranked = max(weight * decay(age, half_life_days), 0.1)
    return 1 + TOPIC_WEIGHT_SPREAD * math.log2(ranked / TOPIC_WEIGHT_PIVOT)


async def _rerank(query: str, hits: list[HistoryHit], top_k: int) -> list[HistoryHit] | None:
    try:
        ranked = await registry.rerank(query, [h.text[:RERANK_MAX_CHARS] for h in hits], top_n=top_k)
    except registry.TaskNotConfigured:
        return None
    except Exception as e:
        LOGGER.warning("rerank failed, keeping fused order: {!r}", e)
        return None
    return [replace(hits[i], score=score) for i, score in ranked[:top_k] if 0 <= i < len(hits)]


_QUESTION_WORDS = frozenset(
    "是谁 谁 谁说 什么 啥 怎么 怎么样 怎样 咋 为什么 为啥 为何 哪个 哪些 哪里 哪儿 多少 几 吗 呢 吧 啊 呀 了 的"
    " 是 有 有没有 没有 说 说过 提过 提到 讲 讲过 过 一下 知道 记得 有人 谁提".split()
)


async def _query_words(session: AsyncSession, query: str) -> list[str]:
    rows = await session.scalars(text("SELECT unnest(CAST(CAST(:q AS pdb.jieba) AS text[]))"), {"q": query})
    words = list(dict.fromkeys(w for w in rows if any(c.isalnum() for c in w)))
    return [w for w in words if w not in _QUESTION_WORDS] or words


def _terms(words: Sequence[str]) -> list[str]:
    return [w for w in words if len(w) > 1] or list(words)


def _covered(hits: list[HistoryHit]) -> list[HistoryHit]:
    chunks = [(h.channel_id, h.span, {line[_said(line) :] for line in h.text.split("\n")}) for h in hits if h.span]

    def shown(m: HistoryHit) -> bool:
        said = m.text.strip()
        return any(
            ch == m.channel_id and lo <= m.ref_id <= hi and start <= m.when <= end and said in lines
            for ch, (lo, hi, start, end), lines in chunks
        )

    return [h for h in hits if h.kind != "message" or h.media or not shown(h)]


def _clip(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _around(s: str, terms: Sequence[str], limit: int, start: int = 0) -> str:
    if len(s) <= limit:
        return s
    low = s.lower()
    found = [(i, len(w)) for w in terms if (i := low.find(w, start)) >= 0]
    room = limit - start - 2
    if not found or room < 20 or sum(min(found)) <= limit - 1:
        return _clip(s, limit)
    pos = min(found)[0]
    end = min(len(s), max(start, pos - room // 3) + room)
    begin = max(start, end - room)
    return s[:start] + ("…" if begin > start else "") + s[begin:end] + ("…" if end < len(s) else "")


def _said(line: str) -> int:
    return line.find(": ") + 2 if ": " in line else 0


def _excerpt(chunk: str, terms: Sequence[str]) -> str | None:
    starts = [m.start() for m in _CHUNK_LINE.finditer(chunk)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    full = [chunk[a:b].strip() for a, b in zip(starts, [*starts[1:], len(chunk)])]
    words = [sum(w in line[_said(line) :].lower() for w in terms) for line in full]
    lines = [_around(line, terms, EXCERPT_MESSAGE_CHARS, _said(line)) for line in full]
    ranked = sorted((i for i, n in enumerate(words) if n), key=lambda i: -words[i])
    keep: set[int] = set()
    size = 0
    for i in ranked:
        added = {j for j in (i - 1, i, i + 1) if 0 <= j < len(lines)} - keep
        cost = sum(len(lines[j]) + 1 for j in added)
        if keep and size + cost > HIT_MAX_CHARS:
            break
        keep |= added
        size += cost
    if not keep:
        return None
    out: list[str] = []
    for j in sorted(keep):
        if out and j - 1 not in keep:
            out.append("…")
        out.append(lines[j])
    return _clip("\n".join(out), HIT_MAX_CHARS)


def _shown(hit: HistoryHit, terms: Sequence[str]) -> HistoryHit | None:
    if hit.kind == "chunk":
        excerpt = _excerpt(hit.text, terms)
        if excerpt is not None:
            return replace(hit, text=excerpt)
    elif any(w in hit.text.lower() for w in terms):
        return replace(hit, text=_around(hit.text, terms, HIT_MAX_CHARS))
    if hit.score < SEMANTIC_MIN_SIMILARITY:
        return None
    return replace(hit, text=_clip(hit.text, HIT_MAX_CHARS))


async def search_history(
    guild_id: str,
    query: str,
    *,
    channel_id: str | None = None,
    current_channel_only: bool = False,
    role_ids: Collection[str] = (),
    since: datetime | None = None,
    until: datetime | None = None,
    user_id: str | None = None,
    top_k: int = 12,
) -> list[HistoryHit]:
    """Hybrid BM25 + vector search over guild memory (honoring channel privacy, scope rules, and role rules)."""
    query = query.strip()
    if not query or top_k <= 0:
        return []
    channels = accessible_channels(guild_id, channel_id, current_channel_only=current_channel_only, role_ids=role_ids)
    if not channels:
        return []
    shared_ids = set(snapshot().shared_channel_ids(guild_id))
    include_digests = not current_channel_only and bool(shared_ids) and shared_ids.issubset(channels)
    try:
        qv = await _query_vector(query)
    except Exception as e:
        LOGGER.warning("query embedding failed, BM25 only: {!r}", e)
        qv = None
    async with get_session() as session:
        words = await _query_words(session, query)
        lexical = await _bm25(session, guild_id, channels, " ".join(words) or query, since, until, user_id)
        terms = _terms(words)
        semantic = await _vector(session, guild_id, channels, include_digests, *qv, since, until, user_id) if qv else []
    lexical = [replace(h, text=_around(h.text, terms, HIT_MAX_CHARS)) for h in lexical]
    semantic = [s for h in semantic if (s := _shown(h, terms))]
    by_key = {(h.kind, h.ref_id): h for h in (*semantic, *lexical)}
    fused = rrf_fuse([[(h.kind, h.ref_id) for h in lexical], [(h.kind, h.ref_id) for h in semantic]])
    scored = []
    for key, score in fused:
        hit = by_key[key]
        if channel_id and hit.channel_id == channel_id and not current_channel_only:
            score *= SAME_CHANNEL_BOOST
        scored.append(replace(hit, score=score))
    scored.sort(key=lambda h: -h.score)
    pool = scored[: max(RERANK_POOL, top_k)]
    if len(pool) > 1:
        reranked = await _rerank(query, pool, top_k)
        if reranked is not None:
            return _covered(reranked)
    return _covered(pool[:top_k])


# --- topics, glossary, profiles ---------------------------------------------------------------------------------


def _topic_view(t: Topic) -> TopicView:
    return TopicView(
        t.id,
        t.channel_id,
        t.title,
        t.summary,
        list(t.keywords),
        list(t.participants),
        t.start_at,
        t.end_at,
        t.msg_count,
    )


async def open_topics(
    guild_id: str,
    *,
    channel_id: str | None = None,
    limit: int = 5,
    include_other_channels: bool = True,
    role_ids: Collection[str] = (),
) -> list[TopicView]:
    """Open topics: current channel first, optionally followed by sibling accessible channels in the guild."""
    channels = accessible_channels(
        guild_id, channel_id, current_channel_only=not include_other_channels, role_ids=role_ids
    )
    if not channels:
        return []
    async with get_session() as session:
        rows = list(
            (
                await session.scalars(
                    select(Topic)
                    .where(Topic.guild_id == guild_id, Topic.channel_id.in_(channels), Topic.status == "open")
                    .order_by(Topic.end_at.desc())
                    .limit(limit * 2)
                )
            ).all()
        )
    if channel_id:
        rows.sort(key=lambda t: (t.channel_id != channel_id, -t.end_at.timestamp()))
    return [_topic_view(t) for t in rows[:limit]]


def _entry(g: GlossaryTerm) -> GlossaryEntry:
    return GlossaryEntry(g.id, g.term, list(g.aliases), g.definition, g.confidence, g.locked)


async def glossary_terms(guild_id: str) -> list[GlossaryEntry]:
    async with get_session() as session:
        rows = await session.scalars(select(GlossaryTerm).where(GlossaryTerm.guild_id == guild_id))
        return [_entry(g) for g in rows]


def _contains(column: Any, term: str) -> Any:
    if term.isascii() and any(c.isalpha() for c in term):
        return column.regexp_match(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", flags="i")
    return column.ilike(f"%{term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%")


async def lookup_glossary(guild_id: str, term: str, limit: int = 3) -> list[GlossaryEntry]:
    term = term.strip()
    if not term:
        return []
    lowered = term.lower()
    async with get_session() as session:
        scoped = select(GlossaryTerm).where(GlossaryTerm.guild_id == guild_id)
        exact = (
            await session.scalars(
                scoped.where(
                    or_(
                        func.lower(GlossaryTerm.term) == lowered,
                        text("lower(:t) = ANY(SELECT lower(a) FROM unnest(glossary_terms.aliases) a)").bindparams(
                            t=term
                        ),
                    )
                )
            )
        ).all()
        if exact:
            return [_entry(g) for g in exact]
        fuzzy = (
            await session.scalars(
                scoped.where(or_(_contains(GlossaryTerm.term, term), _contains(GlossaryTerm.definition, term)))
                .order_by(func.length(GlossaryTerm.term))
                .limit(limit)
            )
        ).all()
        if fuzzy:
            return [_entry(g) for g in fuzzy]
    try:
        qv = await _query_vector(term)
    except Exception as e:
        LOGGER.warning("glossary query embedding failed: {!r}", e)
        return []
    if qv is None:
        return []
    space_id, vec = qv
    async with get_session() as session:
        await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        stmt = text(
            "SELECT v.ref_id FROM memory_vectors v WHERE v.guild_id = :g AND v.space_id = :s"
            " AND v.kind = 'glossary' AND (v.embedding <=> CAST(:qv AS halfvec)) <= :maxd"
            " ORDER BY v.embedding <=> CAST(:qv AS halfvec) LIMIT :lim"
        ).bindparams(bindparam("qv", type_=HALFVEC(EMBED_DIM)))
        ids = list(
            (
                await session.scalars(
                    stmt,
                    {"g": guild_id, "s": space_id, "qv": vec, "maxd": GLOSSARY_MAX_DISTANCE, "lim": limit},
                )
            ).all()
        )
        if not ids:
            return []
        rows = {g.id: g for g in await session.scalars(select(GlossaryTerm).where(GlossaryTerm.id.in_(ids)))}
    return [_entry(rows[i]) for i in ids if i in rows]


async def profiles(guild_id: str, user_ids: Sequence[str]) -> dict[str, ProfileView]:
    if not user_ids:
        return {}
    async with get_session() as session:
        rows = (
            await session.scalars(
                select(UserProfile).where(
                    UserProfile.guild_id == guild_id,
                    UserProfile.user_id.in_(set(user_ids)),
                    UserProfile.opted_out.is_(False),
                )
            )
        ).all()
        if not rows:
            return {}
        half_life = snapshot().settings(guild_id=guild_id).memory.weight_half_life_days
        ranked = (
            select(
                UserProfileFact.user_id,
                UserProfileFact.fact,
                func.row_number()
                .over(
                    partition_by=UserProfileFact.user_id,
                    order_by=(fact_rank(half_life).desc(), UserProfileFact.id.desc()),
                )
                .label("rn"),
            )
            .where(
                UserProfileFact.guild_id == guild_id,
                UserProfileFact.user_id.in_([p.user_id for p in rows]),
                UserProfileFact.archived_at.is_(None),
            )
            .subquery()
        )
        facts: dict[str, list[str]] = {}
        for uid, fact in (
            await session.execute(
                select(ranked.c.user_id, ranked.c.fact).where(ranked.c.rn <= PROFILE_FACTS).order_by(ranked.c.rn)
            )
        ).all():
            facts.setdefault(uid, []).append(fact)
    return {
        p.user_id: ProfileView(
            p.user_id,
            p.display_name,
            p.summary,
            list(p.interests),
            list(p.traits),
            facts.get(p.user_id, []),
            p.updated_at,
        )
        for p in rows
    }


async def get_profile(guild_id: str, user_id: str) -> ProfileView | None:
    return (await profiles(guild_id, [user_id])).get(user_id)
