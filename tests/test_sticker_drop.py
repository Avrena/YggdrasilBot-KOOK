"""Sticker drops: the weighted sample, the gates and the decision (``stickers.drop.maybe_drop``), and the bot sending
the queued pick as a sticker."""

import json
import random
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis
import pytest
from sqlalchemy import delete, update

from tests.support.db import insert_message, purge_groups
from tests.support.files import png
from tests.support.groups import fresh_group
from tests.support.qq import FakeQQ
from yggdrasil.db.models import Message, Sticker, StickerBlob, StickerUsage
from yggdrasil.db.session import get_session
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.llm import registry
from yggdrasil.qq import client as qq_client
from yggdrasil.qq.model import Login, QQError
from yggdrasil.runtime import GroupInfo, Route, Snapshot
from yggdrasil.stickers import drop

NOW = datetime(2026, 11, 19, 4, 0, tzinfo=UTC)  # 12:00 in Asia/Shanghai


def test_the_sample_favours_used_stickers_but_reaches_rare_ones_and_skips_repeats() -> None:
    rng = random.Random(1)
    draws = [drop.sample(["popular", "rare"], [drop.weight(200, 0), drop.weight(0, 0)], 1, rng)[0] for _ in range(4000)]
    share = draws.count("popular") / len(draws)
    assert 0.8 < share < 0.92  # weight 1 + ln(201) ≈ 6.3 against 1: about 86 %, not 99.5 % as raw counts would give

    # Sent twice by the bot this week: now rarer than a sticker nobody ever used.
    draws = [drop.sample(["repeat", "fresh"], [drop.weight(200, 2), drop.weight(0, 0)], 1, rng)[0] for _ in range(4000)]
    assert draws.count("repeat") < draws.count("fresh")

    picked = drop.sample(list(range(10)), [1.0] * 10, 6, rng)
    assert len(picked) == len(set(picked)) == 6


GROUP = fresh_group()
ON = {
    "enabled": True,
    "chance": 1.0,
    "min_fit": 0.5,
    "daily_max": 2,
    "min_gap_minutes": 15,
    "min_new_messages": 1,
    "group_only": True,
}


@pytest.fixture
async def stickers() -> AsyncIterator[list[int]]:
    """Two sendable stickers the group has used, plus one disabled; their ids (sendable first)."""
    refs = [uuid.uuid4().hex for _ in range(3)]
    async with get_session() as session:
        rows = [
            Sticker(kind="image", ref=ref, status="ready", description=f"表情{i}", usage="接梗", enabled=i < 2)
            for i, ref in enumerate(refs)
        ]
        session.add_all(rows)
        await session.flush()
        for st in rows:
            session.add(StickerBlob(sticker_id=st.id, data=png(size=(4, 3))))
            session.add(StickerUsage(sticker_id=st.id, group_id=GROUP, count=5))
        ids = [st.id for st in rows]
    yield ids
    async with get_session() as session:
        await session.execute(delete(Sticker).where(Sticker.ref.in_(refs)))
    await purge_groups(GROUP)


class Decisions:
    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []  # the model's answers, one per call, consumed in order
        self.requests: list[dict[str, Any]] = []


@pytest.fixture
def decisions(monkeypatch: pytest.MonkeyPatch) -> Decisions:
    made = Decisions()

    async def decide(task: str, state: Any, questions: dict[str, Any], **_: Any) -> registry.Decisions:
        assert task == drop.TASK
        made.requests.append({"state": state, "questions": questions})
        return registry.Decisions(made.answers.pop(0), "openrouter/typesafe/jev-1.14", 100, 0.00001)

    monkeypatch.setattr(registry, "decide", decide)
    return made


def _answers(fit: float, choice: str) -> dict[str, Any]:
    return {
        "fit": {"type": "noul", "noul": fit},
        "pick": {"type": "choice", "choice": choice, "confidence": 0.8, "probabilities": {choice: 0.9}},
    }


def _install(snapshot: Callable[..., Snapshot], **sticker_drop: Any) -> None:
    route = Route(drop.TASK, 1, "typesafe/jev-1.14", {}, None, None, {})
    snapshot(
        routes={drop.TASK: route},
        groups={GROUP: GroupInfo(GROUP, "test", True, {})},
        global_settings_doc={"sticker_drop": ON | sticker_drop},
    )


async def _queued(r: fakeredis.FakeAsyncRedis) -> list[dict[str, Any]]:
    return [json.loads(x) for x in await r.lrange(drop.OUTBOX, 0, -1)]


@pytest.mark.db
async def test_a_fitting_moment_queues_a_pick_within_the_gap_and_the_daily_cap(
    stickers: list[int],
    decisions: Decisions,
    snapshot: Callable[..., Snapshot],
    fake_redis: fakeredis.FakeAsyncRedis,
) -> None:
    _install(snapshot, active_within_minutes=60)
    await insert_message(GROUP, "哈哈哈笑死", sent_at=NOW - timedelta(minutes=1))
    decisions.answers += [_answers(0.9, f"s{stickers[0]}"), _answers(0.9, f"s{stickers[1]}")]

    first = await drop.maybe_drop(GROUP, now=NOW)
    assert (first.outcome, first.sticker_id) == ("sent", stickers[0])
    [request] = decisions.requests
    assert sorted(request["questions"]["pick"]["criteria"]) == sorted(f"s{i}" for i in stickers[:2])  # not disabled
    assert "哈哈哈笑死" in request["state"]["群聊最近的消息"]

    assert (await drop.maybe_drop(GROUP, now=NOW + timedelta(minutes=10))).outcome == "gap"
    await insert_message(GROUP, "绷不住了", sent_at=NOW + timedelta(minutes=19))
    assert (await drop.maybe_drop(GROUP, now=NOW + timedelta(minutes=20))).outcome == "sent"
    assert (await drop.maybe_drop(GROUP, now=NOW + timedelta(minutes=40))).outcome == "capped"
    assert [(q["group_id"], q["sticker_id"]) for q in reversed(await _queued(fake_redis))] == [
        (GROUP, stickers[0]),
        (GROUP, stickers[1]),
    ]


@pytest.mark.db
async def test_a_decision_waits_for_enough_new_messages_from_members(
    stickers: list[int],
    decisions: Decisions,
    snapshot: Callable[..., Snapshot],
) -> None:
    _install(snapshot, min_new_messages=2, chance=0.0, active_within_minutes=60)
    await insert_message(GROUP, "一", sent_at=NOW - timedelta(minutes=3))
    await insert_message(GROUP, "[bot 的回答]", user_id=10000, sent_at=NOW - timedelta(minutes=2))
    async with get_session() as session:  # the bot's own message doesn't count
        bot_message = (Message.group_id == GROUP) & (Message.user_id == 10000)
        await session.execute(update(Message).where(bot_message).values(is_self=True))
    assert (await drop.maybe_drop(GROUP, now=NOW)).outcome == "quiet"

    await insert_message(GROUP, "二", sent_at=NOW - timedelta(minutes=1))
    decisions.answers.append(_answers(0.9, f"s{stickers[0]}"))
    assert (await drop.maybe_drop(GROUP, now=NOW)).outcome == "dice"  # decided (chance 0): no send, no gap
    assert (await drop.maybe_drop(GROUP, now=NOW + timedelta(minutes=5))).outcome == "quiet"
    assert len(decisions.requests) == 1


@pytest.mark.db
@pytest.mark.parametrize(
    ("answers", "outcome"),
    [
        (_answers(0.3, "s0"), "unfit"),
        ({"fit": {"type": "refusal"}, "pick": {"type": "refusal"}}, "refused"),
        (_answers(0.9, "s999999999"), "refused"),  # not one of the candidates
    ],
)
async def test_nothing_is_queued_when_the_model_says_no(
    answers: dict[str, Any],
    outcome: str,
    stickers: list[int],
    decisions: Decisions,
    snapshot: Callable[..., Snapshot],
    fake_redis: fakeredis.FakeAsyncRedis,
) -> None:
    _install(snapshot)
    await insert_message(GROUP, "在吗", sent_at=NOW - timedelta(minutes=1))
    decisions.answers.append(answers)

    assert (await drop.maybe_drop(GROUP, now=NOW)).outcome == outcome
    assert await _queued(fake_redis) == []


@pytest.mark.db
async def test_a_quiet_chat_or_a_disabled_feature_costs_no_model_call(
    stickers: list[int],
    decisions: Decisions,
    snapshot: Callable[..., Snapshot],
    fake_redis: fakeredis.FakeAsyncRedis,
) -> None:
    await insert_message(GROUP, "晚安", sent_at=NOW - timedelta(minutes=30))
    _install(snapshot)
    assert (await drop.maybe_drop(GROUP, now=NOW)).outcome == "idle"
    _install(snapshot, enabled=False)
    assert (await drop.maybe_drop(GROUP, now=NOW)).outcome == "off"
    assert decisions.requests == []
    assert await _queued(fake_redis) == []


@pytest.mark.db
async def test_the_bot_sends_a_fresh_pick_as_a_sticker_and_skips_a_stale_one(
    stickers: list[int],
    snapshot: Callable[..., Snapshot],
    recorded: list[IncomingMessage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(snapshot)
    qq = FakeQQ({"login": Login(10000, "Ygg"), "message": QQError("not found")})
    monkeypatch.setattr(qq_client, "connected", lambda: qq)

    await drop._deliver({"group_id": GROUP, "sticker_id": stickers[0], "at": time.time() - drop.MAX_AGE_S - 1})
    assert qq.called("send") == []

    await drop._deliver({"group_id": GROUP, "sticker_id": stickers[0], "at": time.time()})
    [sent] = qq.called("send")
    assert sent["group_id"] == GROUP
    [segment] = sent["segments"]
    assert segment["type"] == "image"
    assert segment["data"]["sticker"] is True
    assert segment["data"]["uri"].startswith("base64://")
    assert [m.is_self for m in recorded] == [True]
