"""Now and then a sticker in the chat (``Settings.sticker_drop``), after topic segmentation has handled new messages.

The worker decides: cheap gates first (daily cap, gap since the last one, the chat still active), then one call to a
decision model (route ``sticker_pick``, e.g. Jev on OpenRouter's Decisions API) with the recent chat and a weighted
sample of the collected stickers. It answers whether a sticker fits now (a probability, held against ``min_fit``) and
which one; a final dice roll (``chance``) keeps the bot from replying to every lively stretch. The pick goes to a
Redis list that the bot process sends from (the worker has no QQ connection).

Sample weights: ``1 + ln(1 + uses)``, so a sticker sent 200 times weighs about 4x one sent once, and each send by the
bot in the past week multiplies it by ``REPEAT_PENALTY``. Uses are counted across all groups, or only in the group
(which then picks only from its own stickers) with ``group_only``; the bot's own sends never count.
"""

import asyncio
import json
import math
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import exists, select

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message, Sticker, StickerBlob, StickerUsage
from yggdrasil.db.session import get_session
from yggdrasil.kv import redis_client
from yggdrasil.llm import registry
from yggdrasil.memory.common import LOCAL_TZ, format_line, people_for
from yggdrasil.memory.search import VIEW_COLUMNS, MessageView
from yggdrasil.runtime import snapshot

TASK = "sticker_pick"
OUTBOX = "ygg:sticker_drop:outbox"
REPEAT_WINDOW = timedelta(days=7)
REPEAT_PENALTY = 0.2
# A pick older than this when the bot gets to it is stale: the chat has moved on.
MAX_AGE_S = 120
# How long one BRPOP blocks; redis-py's connections time out reads after 5 s by default.
POLL_S = 2
# Short lines and criteria keep the request under ~32 KB of text, past which Jev switches to a slower, less accurate
# long-context mode (UTF-8 Chinese is 3 bytes a character).
LINE_MAX_CHARS = 80


def sent_key(group_id: int) -> str:
    """Sorted set of the group's sends: member ``<sticker id>:<ms>``, score the send time (epoch seconds)."""
    return f"ygg:sticker_drop:sent:{group_id}"


@dataclass(frozen=True, slots=True)
class Candidate:
    id: int
    uses: int  # sightings across groups, or in the group (``group_only``)
    description: str
    usage: str
    emotions: list[str]
    ocr_text: str

    def criterion(self) -> str:
        parts = [self.description[:40], f"适合：{self.usage[:50]}"]
        if self.emotions:
            parts.append(f"情绪：{'、'.join(self.emotions[:4])}")
        if self.ocr_text:
            parts.append(f"图中文字：{self.ocr_text[:20]}")
        return "；".join(parts)


@dataclass(frozen=True, slots=True)
class Outcome:
    outcome: str  # off | no_route | capped | gap | idle | no_stickers | refused | unfit | dice | sent | error
    fit: float | None = None
    sticker_id: int | None = None


def weight(uses: int, recent_sends: int) -> float:
    return (1 + math.log1p(uses)) * REPEAT_PENALTY**recent_sends


def sample[T](items: Sequence[T], weights: Sequence[float], k: int, rng: random.Random) -> list[T]:
    """``k`` items without replacement, each draw proportional to weight (Efraimidis–Spirakis)."""
    keyed = sorted(((rng.random() ** (1 / w), i) for i, w in enumerate(weights) if w > 0), reverse=True)
    return [items[i] for _, i in keyed[:k]]


def _local_midnight(now: datetime) -> datetime:
    return now.astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)


async def _recent_messages(group_id: int, limit: int) -> list[MessageView]:
    async with get_session() as session:
        rows = await session.execute(
            select(*VIEW_COLUMNS)
            .where(Message.group_id == group_id, Message.recalled.is_(False))
            .order_by(Message.id.desc())
            .limit(limit)
        )
        return [MessageView(*r) for r in reversed(rows.all())]


async def _candidates(group_id: int, *, group_only: bool) -> list[Candidate]:
    """Every sendable sticker (tagged, enabled, image kept) with its use count: everywhere, or with ``group_only``
    only the group's own stickers and their count there."""
    query = select(Sticker.id).where(
        Sticker.kind == "image",
        Sticker.status == "ready",
        Sticker.enabled.is_(True),
        exists().where(StickerBlob.sticker_id == Sticker.id),
    )
    if group_only:
        query = query.join(
            StickerUsage, (StickerUsage.sticker_id == Sticker.id) & (StickerUsage.group_id == group_id)
        ).add_columns(StickerUsage.count)
    else:
        query = query.add_columns(Sticker.seen_count)
    query = query.add_columns(Sticker.description, Sticker.usage, Sticker.emotions, Sticker.ocr_text)
    async with get_session() as session:
        return [Candidate(*r) for r in (await session.execute(query)).all()]


def questions(picks: Sequence[Candidate]) -> dict[str, dict[str, Any]]:
    # Asked about the last few messages, not "would the bot fit in": the broader question scored nearly every
    # stretch of chat 0.55-0.8 (prod replay, 2026-11-20); this one puts tech talk and fresh bot answers at 0.15-0.4
    # and banter at 0.8-0.9.
    return {
        "fit": {
            "type": "noul",
            "instructions": "最后几条消息是不是适合用表情包接话的时刻？",
            "criteria": {
                "true": "最后几条是玩梗、吐槽、感叹、调侃、自嘲、发表情或明显的情绪反应，接一张表情很自然",
                "false": "最后几条在讲技术细节、提问、贴链接、讨论具体事情，或者 bot 刚回答完，插一张表情会打断话题",
            },
        },
        "pick": {
            "type": "choice",
            "instructions": "哪张表情包最适合接最近这几条聊天？",
            "criteria": {f"s{c.id}": c.criterion() for c in picks},
        },
    }


async def maybe_drop(group_id: int, *, now: datetime | None = None, rng: random.Random | None = None) -> Outcome:
    """Decide whether to drop a sticker into the group now and queue it for the bot if so."""
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    snap = snapshot()
    settings = snap.settings(group_id)
    cfg = settings.sticker_drop
    if not cfg.enabled or cfg.daily_max == 0:
        return Outcome("off")
    if TASK not in snap.routes:
        return Outcome("no_route")
    r = redis_client()
    key = sent_key(group_id)
    await r.zremrangebyscore(key, "-inf", (now - REPEAT_WINDOW).timestamp())
    sent = cast("list[tuple[str, float]]", await r.zrange(key, 0, -1, withscores=True))
    if sum(at >= _local_midnight(now).timestamp() for _, at in sent) >= cfg.daily_max:
        return Outcome("capped")
    if sent and now.timestamp() - max(at for _, at in sent) < cfg.min_gap_minutes * 60:
        return Outcome("gap")
    recent = await _recent_messages(group_id, cfg.context_messages)
    if not recent or now - recent[-1].sent_at > timedelta(minutes=cfg.active_within_minutes):
        return Outcome("idle")
    candidates = await _candidates(group_id, group_only=cfg.group_only)
    if not candidates:
        return Outcome("no_stickers")
    repeats: dict[int, int] = {}
    for member, _ in sent:
        sid = int(member.split(":", 1)[0])
        repeats[sid] = repeats.get(sid, 0) + 1
    picks = sample(candidates, [weight(c.uses, repeats.get(c.id, 0)) for c in candidates], cfg.candidates, rng)
    bot_name = settings.persona.name
    people = await people_for(group_id, recent, bot_name=bot_name)
    state = {
        "群聊最近的消息": "\n".join(format_line(v, people)[:LINE_MAX_CHARS] for v in recent),
        "发表情的人": f"{bot_name}（群里的 bot，消息里标着「(你)」）",
    }
    result = await registry.decide(TASK, state, questions(picks))
    fit_answer, pick_answer = result.answers.get("fit") or {}, result.answers.get("pick") or {}
    if fit_answer.get("type") != "noul" or pick_answer.get("type") != "choice":
        LOGGER.info("sticker drop in {}: the model refused ({}, {})", group_id, fit_answer, pick_answer)
        return Outcome("refused")
    fit = float(fit_answer["noul"])
    if fit < cfg.min_fit:
        return Outcome("unfit", fit)
    if rng.random() >= cfg.chance:
        return Outcome("dice", fit)
    sticker_id = {f"s{c.id}": c.id for c in picks}.get(str(pick_answer.get("choice")))
    if sticker_id is None:
        LOGGER.warning("sticker drop in {}: the model picked {}, not a candidate", group_id, pick_answer.get("choice"))
        return Outcome("refused", fit)
    await r.zadd(key, {f"{sticker_id}:{int(now.timestamp() * 1000)}": now.timestamp()})
    await r.lpush(OUTBOX, json.dumps({"group_id": group_id, "sticker_id": sticker_id, "at": now.timestamp()}))
    return Outcome("sent", fit, sticker_id)


async def deliver_forever() -> None:
    """Bot process: send the picks the worker queued, while this replica holds the QQ connection."""
    from yggdrasil.qq import client as qq_client

    while True:
        if qq_client.connected() is None:
            await asyncio.sleep(2)
            continue
        try:
            item = await redis_client().brpop([OUTBOX], timeout=POLL_S)
            if item is not None:
                await _deliver(json.loads(item[1]))
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("sticker drop delivery failed")
            await asyncio.sleep(5)


async def _deliver(pick: dict[str, Any]) -> None:
    from yggdrasil.bot.state import identity
    from yggdrasil.delivery.send import send_image
    from yggdrasil.qq import client as qq_client

    group_id, sticker_id = int(pick["group_id"]), int(pick["sticker_id"])
    if time.time() - float(pick["at"]) > MAX_AGE_S:
        LOGGER.info("sticker drop in {}: sticker {} is stale; skipped", group_id, sticker_id)
        return
    qq = qq_client.connected()
    if qq is None or not snapshot().group_enabled(group_id):
        return
    async with get_session() as session:
        data = await session.scalar(select(StickerBlob.data).where(StickerBlob.sticker_id == sticker_id))
    if data is None:
        return
    ident = await identity(qq)
    await send_image(
        qq,
        group_id=group_id,
        user_id=0,
        reply_to=None,
        images=[data],
        text="",
        self_id=ident.self_id,
        nickname=ident.nickname,
        sticker=True,
    )
    LOGGER.info("sticker drop in {}: sent sticker {}", group_id, sticker_id)
