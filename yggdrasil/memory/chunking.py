"""Per-channel conversation chunks: the unit of vector search over raw history."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update

from yggdrasil.db.models import Message, MessageChunk
from yggdrasil.db.session import get_session
from yggdrasil.memory.common import format_line, people_for, pipeline_state
from yggdrasil.memory.search import VIEW_COLUMNS, MessageView
from yggdrasil.memory.vectors import delete_vectors
from yggdrasil.people import People
from yggdrasil.runtime import snapshot

PIPELINE = "chunk"
BATCH = 2000
RECALL_LOOKBACK = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class Chunk:
    messages: tuple[MessageView, ...]
    text: str

    @property
    def first_id(self) -> int:
        return self.messages[0].id

    @property
    def last_id(self) -> int:
        return self.messages[-1].id

    @property
    def start_at(self) -> datetime:
        return min(m.sent_at for m in self.messages)

    @property
    def end_at(self) -> datetime:
        return max(m.sent_at for m in self.messages)


@dataclass(slots=True)
class ChunkPlan:
    chunks: list[Chunk] = field(default_factory=list)
    watermark: int | None = None


def render(messages: Sequence[MessageView], people: People) -> list[str]:
    return [format_line(m, people) for m in messages]


def split_chunks(
    messages: Sequence[MessageView],
    people: People,
    *,
    gap: timedelta,
    max_messages: int,
    max_chars: int,
    now: datetime,
    more_pending: bool = False,
) -> ChunkPlan:
    plan = ChunkPlan()
    current: list[tuple[int, MessageView, str]] = []
    chars = 0

    def close() -> None:
        plan.chunks.append(Chunk(tuple(m for _, m, _ in current), "\n".join(line for _, _, line in current)))

    for i, m in enumerate(messages):
        if not m.text.strip():
            continue
        line = format_line(m, people)[:max_chars]
        if current and (
            m.sent_at - current[-1][1].sent_at > gap
            or len(current) >= max_messages
            or chars + len(line) + 1 > max_chars
        ):
            close()
            current, chars = [], 0
        current.append((i, m, line))
        chars += len(line) + 1
    if current and not more_pending and now - current[-1][1].sent_at > gap:
        close()
        current = []
    if current:
        first_open = current[0][0]
        plan.watermark = messages[first_open - 1].id if first_open > 0 else None
    elif messages:
        plan.watermark = messages[-1].id
    return plan


async def _rechunk_recalled(channel_id: str, guild_id: str, checked_at: datetime) -> int:
    async with get_session() as session:
        q = select(Message.id).where(
            Message.channel_id == channel_id,
            Message.recalled.is_(True),
            Message.recalled_at > checked_at,
            Message.sent_at >= checked_at - RECALL_LOOKBACK,
        )
        recalled_ids = list((await session.scalars(q)).all())
        if not recalled_ids:
            return 0
        chunks: dict[int, MessageChunk] = {}
        for mid in recalled_ids:
            for c in await session.scalars(
                select(MessageChunk).where(
                    MessageChunk.channel_id == channel_id,
                    MessageChunk.first_msg_id <= mid,
                    MessageChunk.last_msg_id >= mid,
                )
            ):
                chunks[c.id] = c
        kept: dict[int, list[MessageView]] = {}
        for c in chunks.values():
            rows = await session.execute(
                select(*VIEW_COLUMNS)
                .where(
                    Message.channel_id == channel_id,
                    Message.recalled.is_(False),
                    Message.id.between(c.first_msg_id, c.last_msg_id),
                    Message.sent_at.between(c.start_at, c.end_at),
                )
                .order_by(Message.id)
            )
            kept[c.id] = [MessageView(*r) for r in rows if r.plain_text.strip()]
    bot_name = snapshot().settings(channel_id=channel_id, guild_id=guild_id).persona.name
    people = await people_for(guild_id, [v for views in kept.values() for v in views], bot_name=bot_name)
    async with get_session() as session:
        for chunk_id, views in kept.items():
            await delete_vectors(session, "chunk", [chunk_id])
            if not views:
                await session.execute(delete(MessageChunk).where(MessageChunk.id == chunk_id))
                continue
            await session.execute(
                update(MessageChunk)
                .where(MessageChunk.id == chunk_id)
                .values(
                    text="\n".join(render(views, people)),
                    first_msg_id=views[0].id,
                    last_msg_id=views[-1].id,
                    msg_count=len(views),
                    start_at=min(v.sent_at for v in views),
                    end_at=max(v.sent_at for v in views),
                )
            )
    return len(kept)


async def chunk_channel(channel_id: str, *, now: datetime | None = None) -> dict[str, int]:
    """Persist newly closed chunks for one channel and rebuild chunks hit by recalls."""
    snap = snapshot()
    guild_id = snap.guild_of(channel_id) or ""
    settings = snap.settings(channel_id=channel_id, guild_id=guild_id)
    mem = settings.memory
    gap = timedelta(minutes=mem.chunk_gap_minutes)
    created = 0
    while True:
        started = now or datetime.now(UTC)
        async with get_session() as session:
            st = await pipeline_state(session, PIPELINE, channel_id)
            watermark = st.last_msg_id
            rows = await session.execute(
                select(*VIEW_COLUMNS)
                .where(Message.channel_id == channel_id, Message.recalled.is_(False), Message.id > watermark)
                .order_by(Message.id)
                .limit(BATCH)
            )
            views = [MessageView(*r) for r in rows]
        if views and not guild_id:
            guild_id = views[0].guild_id
        people = await people_for(guild_id, views, bot_name=settings.persona.name)
        plan = split_chunks(
            views,
            people,
            gap=gap,
            max_messages=mem.chunk_max_messages,
            max_chars=mem.chunk_max_chars,
            now=started,
            more_pending=len(views) == BATCH,
        )
        async with get_session() as session:
            session.add_all(
                MessageChunk(
                    channel_id=channel_id,
                    guild_id=guild_id,
                    first_msg_id=c.first_id,
                    last_msg_id=c.last_id,
                    start_at=c.start_at,
                    end_at=c.end_at,
                    msg_count=len(c.messages),
                    text=c.text,
                )
                for c in plan.chunks
            )
            if plan.watermark is not None:
                st = await pipeline_state(session, PIPELINE, channel_id)
                st.last_msg_id = plan.watermark
        created += len(plan.chunks)
        if len(views) < BATCH or plan.watermark is None:
            break

    async with get_session() as session:
        checked = (await pipeline_state(session, PIPELINE, channel_id)).state.get("recall_checked_at")
    started = datetime.now(UTC)
    rebuilt = await _rechunk_recalled(channel_id, guild_id, datetime.fromisoformat(checked)) if checked else 0
    async with get_session() as session:
        st = await pipeline_state(session, PIPELINE, channel_id)
        st.state = {**st.state, "recall_checked_at": started.isoformat()}
    return {"created": created, "rebuilt": rebuilt}
