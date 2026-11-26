"""Daily per-guild digest (task ``memory_digest``) aggregating all shared channels' topics."""

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import DailyDigest, Message, Topic
from yggdrasil.db.session import get_session
from yggdrasil.memory import llm
from yggdrasil.memory.common import LOCAL_TZ, local
from yggdrasil.memory.vectors import digest_text, try_embed
from yggdrasil.people import clean_name, resolve
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot

TASK = "memory_digest"
_WEEKDAYS = "一二三四五六日"
MAX_EVENTS = 8
MAX_EVENT_PEOPLE = 5
_MARKUP = re.compile(r"\*\*|__|`|^#+\s*")


class DigestEvent(BaseModel):
    title: str = Field(description="事件标题，不超过 20 字")
    topic_ids: list[int] = Field(description="这件事对应的话题 id（方括号里的数字）")
    participants: list[str] = Field(
        default_factory=list, description="主要参与者的用户 ID（名字后括号里的数字），最多 5 个，按参与程度排"
    )
    what: str = Field(description="发生了什么，一两句话")
    conclusion: str = Field(default="", description="结论或后续；没有就留空")


class DigestDraft(BaseModel):
    overview: str = Field(description="一句话概括当天服务器里的整体情况，不写日期和消息数")
    events: list[DigestEvent] = Field(default_factory=list, description="主要事件，按重要程度排")


def due_day(now: datetime, digest_hour: int) -> date | None:
    local_now = local(now)
    if local_now.hour < digest_hour:
        return None
    return local_now.date() - timedelta(days=1)


def _sentence(text: str) -> str:
    text = _MARKUP.sub("", " ".join(text.split())).strip().rstrip("，,；;：:")
    return text if not text or text[-1] in "。！？!?…" else f"{text}。"


def render_digest(
    day: date,
    message_count: int,
    draft: DigestDraft,
    topics: Mapping[int, Topic],
    names: Mapping[str, str],
    channel_names: Mapping[str, str] | None = None,
) -> str | None:
    channel_names = channel_names or {}
    overview = _sentence(draft.overview)
    lines = [f"{day:%Y-%m-%d}（周{_WEEKDAYS[day.weekday()]}）共 {message_count} 条消息。{overview}"]
    for event in draft.events:
        ids = [i for i in dict.fromkeys(event.topic_ids) if i in topics]
        title = _sentence(event.title).rstrip("。")
        what = _sentence(event.what)
        if not ids or not title or not what:
            continue
        speakers = list(dict.fromkeys(u for i in ids for u in topics[i].participants))
        chosen = [str(u) for u in dict.fromkeys(event.participants) if str(u) in speakers] or speakers
        ch_tags = list(
            dict.fromkeys(f"#{channel_names.get(topics[i].channel_id) or topics[i].channel_id}" for i in ids)
        )
        ch_prefix = f"[{'/'.join(ch_tags)}] " if ch_tags else ""
        line = f"{len(lines)}. {ch_prefix}{title}（{sum(topics[i].msg_count for i in ids)} 条）：{what}"
        if conclusion := _sentence(event.conclusion):
            line += f"结论：{conclusion}"
        if chosen:
            line += f"参与：{'、'.join(names.get(u) or u for u in chosen[:MAX_EVENT_PEOPLE])}。"
        lines.append(line)
        if len(lines) > MAX_EVENTS:
            break
    if len(lines) == 1 and not overview:
        return None
    return "\n".join(lines)


async def build_digest(guild_id: str, day: date) -> int | None:
    snap = snapshot()
    shared = snap.shared_channel_ids(guild_id)
    if not shared:
        return None
    start = datetime.combine(day, time(), LOCAL_TZ)
    end = start + timedelta(days=1)
    async with get_session() as session:
        if await session.scalar(select(DailyDigest.id).where(DailyDigest.guild_id == guild_id, DailyDigest.day == day)):
            return None
        topics = (
            await session.scalars(
                select(Topic)
                .where(
                    Topic.guild_id == guild_id,
                    Topic.channel_id.in_(shared),
                    Topic.status != "archived",
                    Topic.start_at < end,
                    Topic.end_at >= start,
                )
                .order_by(Topic.start_at)
            )
        ).all()
        if not topics:
            return None
        day_messages = (
            Message.guild_id == guild_id,
            Message.channel_id.in_(shared),
            Message.recalled.is_(False),
            Message.sent_at >= start,
            Message.sent_at < end,
        )
        count = await session.scalar(select(func.count()).select_from(Message).where(*day_messages)) or 0
    bot_name = snap.settings(guild_id=guild_id).persona.name
    people = await resolve(guild_id, {u for t in topics for u in t.participants}, bot_name=bot_name)
    ch_names = {c.channel_id: c.name for c in snap.channels.values() if c.guild_id == guild_id}
    prompt = render(
        TASK,
        day=day.isoformat(),
        weekday=f"周{_WEEKDAYS[day.weekday()]}",
        bot_name=bot_name,
        message_count=count,
        topics=[
            {
                "id": t.id,
                "channel": ch_names.get(t.channel_id) or t.channel_id,
                "title": llm.fence(t.title),
                "summary": llm.fence(t.summary),
                "keywords": [llm.fence(k) for k in t.keywords],
                "start": f"{local(t.start_at):%H:%M}",
                "end": f"{local(t.end_at):%H:%M}",
                "msg_count": t.msg_count,
                "participants": [llm.fence(people.who(u)) for u in t.participants],
            }
            for t in topics
        ],
    )
    draft = await llm.run_structured(TASK, DigestDraft, prompt)
    names = {u: clean_name(n) for u, n in people.names.items()}
    summary = render_digest(day, count, draft, {t.id: t for t in topics}, names, ch_names)
    if summary is None:
        LOGGER.warning("guild {}: empty digest for {}", guild_id, day)
        return None
    async with get_session() as session:
        digest_id = await session.scalar(
            pg_insert(DailyDigest)
            .values(guild_id=guild_id, day=day, summary=summary, topic_ids=[t.id for t in topics])
            .on_conflict_do_nothing(index_elements=["guild_id", "day"])
            .returning(DailyDigest.id)
        )
    if digest_id is not None:
        await try_embed("digest", [(digest_id, guild_id, None, digest_text(day, summary))])
    return digest_id


async def digest_guild(guild_id: str, *, now: datetime | None = None) -> int | None:
    day = due_day(now or datetime.now(UTC), snapshot().settings(guild_id=guild_id).memory.digest_hour)
    if day is None:
        return None
    if not llm.available(TASK):
        LOGGER.debug("guild {}: task {} not configured", guild_id, TASK)
        return None
    return await build_digest(guild_id, day)
