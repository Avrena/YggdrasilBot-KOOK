"""Per-channel topic segmentation (task ``memory_topic``).

Messages are segmented strictly within each channel so active conversations never cross-contaminate; each topic
stores both ``channel_id`` and ``guild_id`` so guild-level memory (profiles, digests, cross-channel search) can
consume and cite it.
"""

import re
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from pydantic_ai.exceptions import IncompleteToolCall
from sqlalchemy import ColumnElement, and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message, Topic, TopicMessage
from yggdrasil.db.session import get_session
from yggdrasil.memory import llm
from yggdrasil.memory.common import format_line, people_for, pipeline_state, topic_weight
from yggdrasil.memory.search import VIEW_COLUMNS, MessageView
from yggdrasil.memory.vectors import delete_vectors, topic_text, try_embed
from yggdrasil.people import People
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot
from yggdrasil.settings import MemorySettings

PIPELINE = "topic"
TASK = "memory_topic"
MAX_WINDOWS_PER_RUN = 5
MAX_OPEN_TOPICS = 10
MAX_DIRTY_PER_RUN = 10
LINE_MAX_CHARS = 300
TOPIC_CONTEXT_LINES = 3
TITLE_MAX_CHARS = 60
MIN_WINDOW_MESSAGES = 25
_TITLE_SEP = re.compile(r"(\s*(?:：|:(?!\d))\s*)")


class ContinueTopic(BaseModel):
    op: Literal["continue"] = "continue"
    topic_id: int
    message_ids: list[int]
    title: str | None = Field(default=None, description="新标题；不变则省略")
    summary: str | None = Field(default=None, description="覆盖整个话题的新摘要；无新进展则省略")
    keywords: list[str] | None = Field(default=None, description="新关键词；不变则省略")


class NewTopic(BaseModel):
    op: Literal["new"] = "new"
    title: str
    summary: str
    keywords: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list, description="发言者用户 ID")
    message_ids: list[int]


TopicOp = Annotated[ContinueTopic | NewTopic, Field(discriminator="op")]


class TopicOps(BaseModel):
    ops: list[TopicOp]


class TopicRewrite(BaseModel):
    title: str
    summary: str
    keywords: list[str] = Field(default_factory=list)


def clean_title(title: str) -> str:
    pieces = _TITLE_SEP.split(" ".join(title.split()))
    out, seen = "", set()
    for i in range(0, len(pieces), 2):
        part = pieces[i]
        if part and part not in seen:
            seen.add(part)
            out += (pieces[i - 1] if out else "") + part
    return out[:TITLE_MAX_CHARS]


def clean_summary(summary: str, title: str) -> str:
    summary = summary.strip()
    if title:
        lead = re.compile(rf"{re.escape(title)}\s*[：:。，,]\s*")
        while (m := lead.match(summary)) and m.end() < len(summary):
            summary = summary[m.end() :]
    return summary


def validate_ops(
    ops: Sequence[ContinueTopic | NewTopic], window_ids: Collection[int], open_topic_ids: Collection[int]
) -> list[ContinueTopic | NewTopic]:
    window, open_ids = set(window_ids), set(open_topic_ids)
    assigned: set[int] = set()
    out: list[ContinueTopic | NewTopic] = []
    for op in ops:
        if isinstance(op, ContinueTopic) and op.topic_id not in open_ids:
            continue
        if isinstance(op, NewTopic) and not clean_title(op.title):
            continue
        ids = [i for i in dict.fromkeys(op.message_ids) if i in window and i not in assigned]
        if not ids:
            continue
        assigned.update(ids)
        out.append(op.model_copy(update={"message_ids": ids}))
    return out


def participants_of(
    ids: Sequence[int], views: Mapping[int, MessageView], claimed: Sequence[str] = (), *, existing: Sequence[str] = ()
) -> list[str]:
    speakers = {v.user_id for v in views.values() if not v.is_self}
    out = list(existing)
    for uid in [*(views[i].user_id for i in ids if not views[i].is_self), *(str(c) for c in claimed)]:
        if uid in speakers and uid not in out:
            out.append(uid)
    return out


def _line(v: MessageView, people: People) -> str:
    return llm.fence(format_line(v, people, with_id=True)[:LINE_MAX_CHARS])


async def _topic_messages(session: AsyncSession, topic: Topic, *, last: int | None = None) -> list[MessageView]:
    q = (
        select(*VIEW_COLUMNS)
        .join(TopicMessage, TopicMessage.message_id == Message.id)
        .where(
            TopicMessage.topic_id == topic.id,
            Message.channel_id == topic.channel_id,
            Message.recalled.is_(False),
            Message.sent_at.between(topic.start_at, topic.end_at),
        )
    )
    if last is not None:
        rows = (await session.execute(q.order_by(Message.id.desc()).limit(last))).all()
        return [MessageView(*r) for r in reversed(rows)]
    return [MessageView(*r) for r in (await session.execute(q.order_by(Message.id))).all()]


async def _link(session: AsyncSession, topic_id: int, ids: Sequence[int]) -> None:
    await session.execute(
        pg_insert(TopicMessage).values([{"topic_id": topic_id, "message_id": i} for i in ids]).on_conflict_do_nothing()
    )


async def _apply(
    session: AsyncSession,
    channel_id: str,
    guild_id: str,
    ops: Sequence[ContinueTopic | NewTopic],
    views: Mapping[int, MessageView],
) -> list[Topic]:
    changed: list[Topic] = []
    for op in ops:
        msgs = [views[i] for i in op.message_ids]
        first, last = min(m.id for m in msgs), max(m.id for m in msgs)
        start, end = min(m.sent_at for m in msgs), max(m.sent_at for m in msgs)
        if isinstance(op, NewTopic):
            title = clean_title(op.title)
            topic = Topic(
                channel_id=channel_id,
                guild_id=guild_id,
                title=title,
                summary=clean_summary(op.summary, title),
                keywords=[k.strip() for k in op.keywords if k.strip()][:10],
                participants=participants_of(op.message_ids, views, op.participants),
                start_at=start,
                end_at=end,
                first_msg_id=first,
                last_msg_id=last,
                msg_count=len(msgs),
                status="open",
            )
            session.add(topic)
            await session.flush()
            await _link(session, topic.id, op.message_ids)
        else:
            topic = await session.get(Topic, op.topic_id)
            if topic is None or topic.channel_id != channel_id:
                continue
            await _link(session, topic.id, op.message_ids)
            topic.start_at, topic.end_at = min(topic.start_at, start), max(topic.end_at, end)
            topic.first_msg_id, topic.last_msg_id = min(topic.first_msg_id, first), max(topic.last_msg_id, last)
            topic.msg_count = (
                await session.scalar(
                    select(func.count()).select_from(TopicMessage).where(TopicMessage.topic_id == topic.id)
                )
                or 0
            )
            topic.participants = participants_of(op.message_ids, views, existing=topic.participants)
            if op.title and (title := clean_title(op.title)):
                topic.title = title
            if op.summary and (summary := clean_summary(op.summary, topic.title)):
                topic.summary = summary
            if op.keywords:
                topic.keywords = [k.strip() for k in op.keywords if k.strip()][:10]
            topic.status = "open"
        changed.append(topic)
    return changed


async def _segment_window(channel_id: str, guild_id: str, force: bool, limit: int) -> tuple[int, int, int] | None:
    snap = snapshot()
    settings = snap.settings(channel_id=channel_id, guild_id=guild_id)
    mem = settings.memory
    async with get_session() as session:
        st = await pipeline_state(session, PIPELINE, channel_id)
        rows = await session.execute(
            select(*VIEW_COLUMNS)
            .where(Message.channel_id == channel_id, Message.recalled.is_(False), Message.id > st.last_msg_id)
            .order_by(Message.id)
            .limit(limit)
        )
        loaded = [MessageView(*r) for r in rows]
        window = [v for v in loaded if v.text.strip()]
        if not loaded or (len(window) < mem.topic_min_new_messages and len(loaded) < limit and not force):
            return None
        open_rows = (
            await session.scalars(
                select(Topic)
                .where(Topic.channel_id == channel_id, Topic.status == "open")
                .order_by(Topic.end_at.desc())
                .limit(MAX_OPEN_TOPICS)
            )
        ).all()
        tails = {t.id: await _topic_messages(session, t, last=TOPIC_CONTEXT_LINES) for t in open_rows}
    if loaded and not guild_id:
        guild_id = loaded[0].guild_id
    bot_name = settings.persona.name
    people = await people_for(guild_id, [*window, *(v for tail in tails.values() for v in tail)], bot_name=bot_name)
    open_topics = [
        {
            "id": t.id,
            "title": llm.fence(t.title),
            "summary": llm.fence(t.summary),
            "keywords": [llm.fence(k) for k in t.keywords],
            "last_lines": [_line(v, people) for v in tails[t.id]],
        }
        for t in open_rows
    ]
    views = {v.id: v for v in window}
    ops: list[ContinueTopic | NewTopic] = []
    if window:
        prompt = render(
            TASK,
            mode="segment",
            bot_name=bot_name,
            open_topics=open_topics,
            messages="\n".join(_line(v, people) for v in window),
        )
        result = await llm.run_structured(TASK, TopicOps, prompt)
        ops = validate_ops(result.ops, views.keys(), [t["id"] for t in open_topics])
    async with get_session() as session:
        changed = await _apply(session, channel_id, guild_id, ops, views)
        await delete_vectors(session, "topic", [t.id for t in changed])
        st = await pipeline_state(session, PIPELINE, channel_id)
        st.last_msg_id = loaded[-1].id
    await try_embed(
        "topic",
        [(t.id, guild_id, channel_id, topic_text(t.title, t.summary, t.keywords)) for t in changed],
    )
    new = sum(isinstance(op, NewTopic) for op in ops)
    return len(window), new, len(ops) - new


async def _recompute_dirty(channel_id: str, guild_id: str) -> tuple[int, int]:
    snap = snapshot()
    recomputed = deleted = 0
    async with get_session() as session:
        dirty = (
            await session.scalars(
                select(Topic).where(Topic.channel_id == channel_id, Topic.dirty.is_(True)).limit(MAX_DIRTY_PER_RUN)
            )
        ).all()
    for topic in dirty:
        async with get_session() as session:
            remaining = await _topic_messages(session, topic)
        rewrite: TopicRewrite | None = None
        if remaining:
            bot_name = snap.settings(channel_id=channel_id, guild_id=guild_id).persona.name
            people = await people_for(guild_id, remaining, bot_name=bot_name)
            prompt = render(
                TASK,
                mode="recompute",
                bot_name=bot_name,
                topic={"id": topic.id, "title": llm.fence(topic.title), "summary": llm.fence(topic.summary)},
                messages="\n".join(_line(v, people) for v in remaining),
            )
            rewrite = await llm.run_structured(TASK, TopicRewrite, prompt)
        async with get_session() as session:
            await delete_vectors(session, "topic", [topic.id])
            if not remaining or rewrite is None:
                await session.execute(delete(Topic).where(Topic.id == topic.id))
                deleted += 1
                continue
            keep = [v.id for v in remaining]
            await session.execute(
                delete(TopicMessage).where(TopicMessage.topic_id == topic.id, TopicMessage.message_id.notin_(keep))
            )
            speakers = list(dict.fromkeys(v.user_id for v in remaining if not v.is_self))
            participants = [u for u in topic.participants if u in speakers]
            title = clean_title(rewrite.title) or topic.title
            summary = clean_summary(rewrite.summary, title) or topic.summary
            keywords = [k.strip() for k in rewrite.keywords if k.strip()][:10] or topic.keywords
            await session.execute(
                update(Topic)
                .where(Topic.id == topic.id)
                .values(
                    title=title,
                    summary=summary,
                    keywords=keywords,
                    participants=participants,
                    start_at=min(v.sent_at for v in remaining),
                    end_at=max(v.sent_at for v in remaining),
                    first_msg_id=remaining[0].id,
                    last_msg_id=remaining[-1].id,
                    msg_count=len(remaining),
                    dirty=False,
                    weight=topic.weight
                    if topic.importance is None
                    else topic_weight(topic.importance, len(remaining), len(participants)),
                )
            )
            text = topic_text(title, summary, keywords)
        recomputed += 1
        await try_embed("topic", [(topic.id, guild_id, channel_id, text)])
    return recomputed, deleted


async def consolidate_channel(channel_id: str, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    snap = snapshot()
    guild_id = snap.guild_of(channel_id) or ""
    mem = snap.settings(channel_id=channel_id, guild_id=guild_id).memory
    idle_before = now - timedelta(minutes=mem.topic_close_after_minutes)
    async with get_session() as session:
        st = await pipeline_state(session, PIPELINE, channel_id)
        pending = await session.scalar(
            select(func.count()).select_from(
                select(Message.id)
                .where(Message.channel_id == channel_id, Message.recalled.is_(False), Message.id > st.last_msg_id)
                .limit(mem.topic_min_new_messages)
                .subquery()
            )
        )
        idle = await session.scalar(
            select(exists().where(Topic.channel_id == channel_id, Topic.status == "open", Topic.end_at < idle_before))
        )
        dirty = await session.scalar(select(exists().where(Topic.channel_id == channel_id, Topic.dirty.is_(True))))
    stats = {"messages": 0, "new": 0, "continued": 0, "recomputed": 0, "deleted": 0, "closed": 0, "archived": 0}
    if not (pending or 0) >= mem.topic_min_new_messages and not idle and not dirty:
        return stats
    if not llm.available(TASK):
        LOGGER.info("channel {}: task {} not configured; only closing idle topics", channel_id, TASK)
    else:
        force = bool(idle and pending)
        limit = mem.topic_window_messages
        windows = 0
        while windows < MAX_WINDOWS_PER_RUN:
            try:
                done = await _segment_window(channel_id, guild_id, force, limit)
            except IncompleteToolCall:
                if limit <= MIN_WINDOW_MESSAGES:
                    raise
                limit = max(MIN_WINDOW_MESSAGES, limit // 2)
                LOGGER.warning("channel {}: topic ops hit output limit; retrying with {} messages", channel_id, limit)
                continue
            if done is None:
                break
            windows += 1
            force = False
            stats["messages"] += done[0]
            stats["new"] += done[1]
            stats["continued"] += done[2]
        if dirty:
            stats["recomputed"], stats["deleted"] = await _recompute_dirty(channel_id, guild_id)
    idle_open = (Topic.channel_id == channel_id, Topic.status == "open", Topic.end_at < idle_before)
    async with get_session() as session:
        archived = await session.execute(
            update(Topic).where(*idle_open, archivable(mem)).values(status="archived", closed_at=func.now())
        )
        closed = await session.execute(update(Topic).where(*idle_open).values(status="closed", closed_at=func.now()))
        stats["archived"] = archived.rowcount or 0  # type: ignore[attr-defined]
        stats["closed"] = closed.rowcount or 0  # type: ignore[attr-defined]
    return stats


def archivable(mem: MemorySettings) -> ColumnElement[bool]:
    return or_(
        and_(
            Topic.msg_count < mem.topic_archive_below_messages,
            func.cardinality(Topic.participants) < mem.topic_archive_below_participants,
        ),
        Topic.weight < mem.topic_archive_below_weight,
    )


async def archive_closed(session: AsyncSession, guild_id: str) -> int:
    mem = snapshot().settings(guild_id=guild_id).memory
    result = await session.execute(
        update(Topic)
        .where(Topic.guild_id == guild_id, Topic.status == "closed", archivable(mem))
        .values(status="archived")
    )
    return result.rowcount or 0  # type: ignore[attr-defined]
