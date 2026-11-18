"""The one-off remap of stored QQ message ids from OneBot's to Milky's, when a deployment switches protocols."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from tests.support.groups import fresh_group
from yggdrasil.db.session import engine
from yggdrasil.qq import remap

pytestmark = pytest.mark.db

GROUP = fresh_group()
RETIRED = remap.RETIRED


def _reply(message_id: int) -> list[dict[str, Any]]:
    return [{"type": "reply", "data": {"message_id": message_id}}, {"type": "text", "data": {"text": "嗯"}}]


# (row id, OneBot id, seq, reply_to, segments)
ROWS = [
    (1, -5000, 100, None, [{"type": "text", "data": {"text": "问题"}}]),  # a negative OneBot hash
    (2, 7000, 101, -5000, _reply(-5000)),
    (3, 9000, None, None, [{"type": "forward", "data": {}}]),  # the bot's own forward: no seq
    (4, 8000, 102, 4242, _reply(4242)),  # quotes a message never stored
    (5, 103, 103, None, [{"type": "text", "data": {"text": "切换后"}}]),  # already a Milky id
]


@pytest.fixture
async def stored() -> AsyncIterator[list[uuid.UUID]]:
    invocations = [uuid.uuid4(), uuid.uuid4()]
    async with engine().begin() as conn:
        for row_id, qq_id, seq, reply_to, segments in ROWS:
            await conn.execute(
                text(
                    "INSERT INTO messages (sent_at, group_id, user_id, qq_message_id, message_seq, reply_to_qq_id,"
                    " sender_name, plain_text, segments, is_self, recalled) VALUES (now(), :g, 20001, :qq, :seq,"
                    " :reply_to, '阿明', :label, CAST(:segments AS jsonb), false, false)"
                ),
                {
                    "g": GROUP,
                    "qq": qq_id,
                    "seq": seq,
                    "reply_to": reply_to,
                    "label": str(row_id),
                    "segments": json.dumps(segments),
                },
            )
        for inv, trigger, reply in ((invocations[0], 7000, 9000), (invocations[1], 103, None)):
            await conn.execute(
                text(
                    "INSERT INTO invocations (id, group_id, user_id, handler, request_text, status, models, tokens_in,"
                    " tokens_out, tokens_cached, tool_calls, trigger_qq_message_id, reply_qq_message_id) VALUES"
                    " (:id, :g, 20001, 'chat', '', 'ok', '[]', 0, 0, 0, '{}', :trigger, :reply)"
                ),
                {"id": inv, "g": GROUP, "trigger": trigger, "reply": reply},
            )
    try:
        yield invocations
    finally:
        async with engine().begin() as conn:
            await conn.execute(text("DELETE FROM messages WHERE group_id = :g"), {"g": GROUP})
            await conn.execute(text("DELETE FROM invocations WHERE group_id = :g"), {"g": GROUP})
            await conn.execute(text("DELETE FROM settings WHERE key = :k"), {"k": remap.MARKER})


async def _messages() -> dict[int, tuple[int, int | None, Any]]:
    async with engine().connect() as conn:
        rows = await conn.execute(
            text("SELECT plain_text, qq_message_id, reply_to_qq_id, segments FROM messages WHERE group_id = :g"),
            {"g": GROUP},
        )
        return {int(label): (qq, reply_to, segments) for label, qq, reply_to, segments in rows}


async def _invocations(ids: list[uuid.UUID]) -> list[tuple[int | None, int | None]]:
    async with engine().connect() as conn:
        rows = await conn.execute(
            text("SELECT id, trigger_qq_message_id, reply_qq_message_id FROM invocations WHERE group_id = :g"),
            {"g": GROUP},
        )
        found = {row_id: (trigger, reply) for row_id, trigger, reply in rows}
    return [found[i] for i in ids]


async def test_group_message_ids_become_their_seq(stored: list[uuid.UUID]) -> None:
    before = await _messages()
    await remap.remap(apply=False)
    assert await _messages() == before  # a dry run changes nothing

    await remap.remap(apply=True)
    after = await _messages()
    assert {k: v[0] for k, v in after.items()} == {1: 100, 2: 101, 3: 9000 - RETIRED, 4: 102, 5: 103}
    assert after[2][1] == 100  # the reply follows its target
    assert after[2][2][0]["data"]["message_id"] == 100
    assert after[4][1] == 4242 - RETIRED  # a target never stored can't match a Milky id any more
    assert after[4][2][0]["data"]["message_id"] == 4242 - RETIRED
    assert await _invocations(stored) == [(101, 9000 - RETIRED), (103, None)]

    with pytest.raises(SystemExit):  # once only
        await remap.remap(apply=True)
