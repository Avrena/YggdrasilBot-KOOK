"""Guild-shared incremental user profiles (task ``memory_profile``).

Aggregates closed topics across all ``share_in_guild=True`` channels in a KOOK guild so knowledge about a member
learned in one channel is immediately shared across the whole server.
"""

from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select, text, true, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message, Topic, TopicMessage, UserProfile, UserProfileFact
from yggdrasil.db.session import get_session
from yggdrasil.memory import llm
from yggdrasil.memory.common import fact_rank, fact_weight, format_line, people_for
from yggdrasil.memory.curation import clamp_importance
from yggdrasil.memory.search import VIEW_COLUMNS, MessageView
from yggdrasil.people import UNKNOWN_NAME, clean_name
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot

TASK = "memory_profile"
MAX_USERS_PER_RUN = 20
MAX_TOPICS = 20
MAX_MESSAGES = 80
MAX_FACTS_IN_PROMPT = 40
CLOSE_SETTLE = timedelta(minutes=5)

_DUE_SQL = text("""
SELECT m.user_id, count(*) AS n
FROM topics t
JOIN topic_messages tm ON tm.topic_id = t.id
JOIN messages m ON m.id = tm.message_id AND m.channel_id = t.channel_id AND m.sent_at BETWEEN t.start_at AND t.end_at
LEFT JOIN user_profiles p ON p.guild_id = t.guild_id AND p.user_id = m.user_id
WHERE t.guild_id = :g AND t.channel_id = ANY(:chs) AND t.status = 'closed'
  AND t.closed_at < now() - CAST(:settle AS interval)
  AND (p.last_closed_at IS NULL OR (t.closed_at, t.id) > (p.last_closed_at, p.last_topic_id))
  AND NOT m.is_self AND NOT m.recalled AND NOT COALESCE(p.opted_out, false)
GROUP BY m.user_id
HAVING count(*) >= :min
ORDER BY n DESC
LIMIT :lim
""")


class NewFact(BaseModel):
    fact: str
    source_msg_ids: list[int]
    importance: int | None = Field(
        default=None, description="重要度 1–5：5 身份、技能、长期项目、稳定偏好；1 一次性的小事、玩笑"
    )


class ProfileUpdate(BaseModel):
    summary: str
    interests: list[str] = Field(default_factory=list)
    traits: list[str] = Field(default_factory=list)
    new_facts: list[NewFact] = Field(default_factory=list)
    remove_fact_ids: list[int] = Field(default_factory=list)


def validate_update(update: ProfileUpdate, shown_msg_ids: Collection[int], fact_ids: Collection[int]) -> ProfileUpdate:
    shown, known = set(shown_msg_ids), set(fact_ids)
    facts = []
    for f in update.new_facts:
        sources = [i for i in dict.fromkeys(f.source_msg_ids) if i in shown]
        if f.fact.strip() and sources:
            importance = None if f.importance is None else clamp_importance(f.importance)
            facts.append(NewFact(fact=f.fact.strip(), source_msg_ids=sources, importance=importance))
    return update.model_copy(
        update={
            "summary": update.summary.strip(),
            "interests": [s.strip() for s in update.interests if s.strip()][:10],
            "traits": [s.strip() for s in update.traits if s.strip()][:8],
            "new_facts": facts,
            "remove_fact_ids": [i for i in dict.fromkeys(update.remove_fact_ids) if i in known],
        }
    )


async def update_user(guild_id: str, user_id: str) -> bool:
    snap = snapshot()
    shared = snap.shared_channel_ids(guild_id)
    if not shared:
        return False
    mem = snap.settings(guild_id=guild_id).memory
    async with get_session() as session:
        profile = await session.get(UserProfile, (guild_id, user_id))
        if profile is not None and profile.opted_out:
            return False
        participated = (
            select(TopicMessage.topic_id)
            .join(Message, Message.id == TopicMessage.message_id)
            .where(
                TopicMessage.topic_id == Topic.id,
                Message.guild_id == guild_id,
                Message.channel_id.in_(shared),
                Message.user_id == user_id,
                Message.recalled.is_(False),
            )
            .exists()
        )
        unconsumed = (
            tuple_(Topic.closed_at, Topic.id) > tuple_(profile.last_closed_at, profile.last_topic_id)
            if profile is not None and profile.last_closed_at is not None
            else true()
        )
        topics = (
            await session.scalars(
                select(Topic)
                .where(
                    Topic.guild_id == guild_id,
                    Topic.channel_id.in_(shared),
                    Topic.status == "closed",
                    Topic.closed_at < func.now() - CLOSE_SETTLE,
                    unconsumed,
                    participated,
                )
                .order_by(Topic.closed_at, Topic.id)
                .limit(MAX_TOPICS)
            )
        ).all()
        if not topics:
            return False
        rows = await session.execute(
            select(*VIEW_COLUMNS)
            .join(TopicMessage, TopicMessage.message_id == Message.id)
            .where(
                TopicMessage.topic_id.in_([t.id for t in topics]),
                Message.guild_id == guild_id,
                Message.user_id == user_id,
                Message.recalled.is_(False),
                Message.sent_at.between(min(t.start_at for t in topics), max(t.end_at for t in topics)),
            )
            .order_by(Message.id.desc())
            .limit(MAX_MESSAGES)
        )
        messages = [MessageView(*r) for r in reversed(rows.all())]
        facts = (
            await session.execute(
                select(UserProfileFact.id, UserProfileFact.fact)
                .where(
                    UserProfileFact.guild_id == guild_id,
                    UserProfileFact.user_id == user_id,
                    UserProfileFact.archived_at.is_(None),
                )
                .order_by(fact_rank(mem.weight_half_life_days).desc(), UserProfileFact.id.desc())
                .limit(MAX_FACTS_IN_PROMPT)
            )
        ).all()
    if not messages:
        return False
    name = messages[-1].sender_name or user_id
    bot_name = snap.settings(guild_id=guild_id).persona.name
    people = await people_for(guild_id, messages, bot_name=bot_name)
    prompt = render(
        TASK,
        bot_name=bot_name,
        name=llm.fence(clean_name(people.names.get(user_id) or messages[-1].sender_name) or UNKNOWN_NAME),
        user_id=user_id,
        profile={
            "summary": llm.fence(profile.summary) if profile else "",
            "interests": [llm.fence(s) for s in profile.interests] if profile else [],
            "traits": [llm.fence(s) for s in profile.traits] if profile else [],
        },
        facts=[{"id": fid, "fact": llm.fence(f)} for fid, f in facts],
        topics=[
            {
                "title": llm.fence(t.title),
                "channel": snap.channel_name(t.channel_id) or t.channel_id,
                "summary": llm.fence(t.summary),
            }
            for t in topics
        ],
        messages="\n".join(
            llm.fence(format_line(m, people, with_id=True, channel_tag=snap.channel_name(m.channel_id))[:300])
            for m in messages
        ),
    )
    result = validate_update(
        await llm.run_structured(TASK, ProfileUpdate, prompt), [m.id for m in messages], [f[0] for f in facts]
    )
    async with get_session() as session:
        if await session.scalar(
            select(
                exists().where(UserProfile.guild_id == guild_id, UserProfile.user_id == user_id, UserProfile.opted_out)
            )
        ):
            return False
        values = {
            "display_name": name,
            "summary": result.summary or (profile.summary if profile else ""),
            "interests": result.interests,
            "traits": result.traits,
            "last_closed_at": topics[-1].closed_at,
            "last_topic_id": topics[-1].id,
            "updated_at": func.now(),
        }
        stmt = pg_insert(UserProfile).values(guild_id=guild_id, user_id=user_id, **values)
        await session.execute(stmt.on_conflict_do_update(index_elements=["guild_id", "user_id"], set_=values))
        if result.remove_fact_ids:
            await session.execute(
                update(UserProfileFact)
                .where(
                    UserProfileFact.guild_id == guild_id,
                    UserProfileFact.user_id == user_id,
                    UserProfileFact.id.in_(result.remove_fact_ids),
                    UserProfileFact.archived_at.is_(None),
                    UserProfileFact.locked.is_(False),
                )
                .values(archived_at=func.now(), archive_reason="outdated")
            )
        now = datetime.now(UTC)
        for f in result.new_facts:
            low = f.importance is not None and f.importance < mem.fact_archive_below_importance
            session.add(
                UserProfileFact(
                    guild_id=guild_id,
                    user_id=user_id,
                    fact=f.fact,
                    source_msg_ids=f.source_msg_ids,
                    importance=f.importance,
                    weight=None if f.importance is None else fact_weight(f.importance, len(f.source_msg_ids)),
                    scored_at=None if f.importance is None else now,
                    archived_at=now if low else None,
                    archive_reason="low" if low else None,
                )
            )
    return True


async def due_users(guild_id: str) -> Sequence[str]:
    snap = snapshot()
    shared = snap.shared_channel_ids(guild_id)
    if not shared:
        return []
    mem = snap.settings(guild_id=guild_id).memory
    async with get_session() as session:
        rows = await session.execute(
            _DUE_SQL,
            {
                "g": guild_id,
                "chs": shared,
                "min": mem.profile_min_messages,
                "lim": MAX_USERS_PER_RUN,
                "settle": CLOSE_SETTLE,
            },
        )
        return [r.user_id for r in rows]


async def update_guild(guild_id: str) -> dict[str, int]:
    stats = {"updated": 0, "failed": 0}
    if not llm.available(TASK):
        LOGGER.debug("guild {}: task {} not configured", guild_id, TASK)
        return stats
    for user_id in await due_users(guild_id):
        try:
            stats["updated"] += await update_user(guild_id, user_id)
        except Exception:
            stats["failed"] += 1
            LOGGER.exception("guild {}: profile update failed", guild_id)
    return stats
