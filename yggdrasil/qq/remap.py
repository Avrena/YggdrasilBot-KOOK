"""One-off, when a deployment's QQ account moves from OneBot (SnowLuma) to Milky: stored QQ message ids switch from
OneBot's to Milky's.

OneBot gives a group message a signed 32-bit hash as its id; Milky addresses it by the group's message seq, which
the OneBot backend also stored (``messages.message_seq``). After the switch, replies, recalls, continued answers and
reviews look messages up by Milky ids, so every stored group message id becomes its seq:

- ``messages.qq_message_id`` and ``reply_to_qq_id``, and the ``message_id`` of reply segments;
- ``invocations.trigger_qq_message_id`` / ``reply_qq_message_id`` and ``review_items.trigger_qq_message_id``.

A message stored without a seq, and a reference to a message never stored, moves to ``old id - RETIRED``: below
any OneBot hash and any Milky seq, so it can't match a Milky id by accident, and nothing looks it up again. Private
chats keep their ids (nothing outlives them). Transcripts and recorded events are history and stay as they were.

Run it once, with the OneBot side stopped and before the Milky link connects:
``python -m yggdrasil.qq.remap`` reports what it would change and rolls back; ``--apply`` changes it in one
transaction and records that it ran (``settings`` key ``qq.remap``), so a second run refuses.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from yggdrasil.db.session import dispose_engine, engine

MARKER = "qq.remap"
RETIRED = 1 << 40  # retired ids live below -2^40 + 2^31: clear of OneBot hashes (int32) and Milky seqs (positive)

# (group, old id) -> new id, from every stored group message; indexed both ways for the reference updates.
_MAP = (
    f"""
CREATE TEMP TABLE qq_remap ON COMMIT DROP AS
SELECT DISTINCT ON (group_id, qq_message_id)
       group_id, qq_message_id AS old_id, COALESCE(message_seq, qq_message_id - {RETIRED}) AS new_id
FROM messages
WHERE group_id IS NOT NULL
ORDER BY group_id, qq_message_id, message_seq NULLS LAST
""",
    "CREATE INDEX ON qq_remap (group_id, old_id)",
    "CREATE INDEX ON qq_remap (group_id, new_id)",
    "ANALYZE qq_remap",
)
# Group message ids held elsewhere: (table, column).
_REFERENCES = (
    ("messages", "reply_to_qq_id"),
    ("invocations", "trigger_qq_message_id"),
    ("invocations", "reply_qq_message_id"),
    ("review_items", "trigger_qq_message_id"),
)


async def _count(conn: AsyncConnection, sql: str) -> int:
    return int((await conn.execute(text(sql))).scalar_one() or 0)


async def _update(conn: AsyncConnection, sql: str) -> int:
    return (await conn.execute(text(sql))).rowcount


def _new_id(mapping: dict[tuple[int, int], int], known: set[tuple[int, int]], group_id: int, old: int) -> int:
    if (group_id, old) in mapping:
        return mapping[group_id, old]
    return old if (group_id, old) in known else old - RETIRED


async def _remap_segments(conn: AsyncConnection) -> int:
    """Reply segments carry the quoted message's id too (``normalize`` reads the reply target from them). Runs
    before the column updates, so ids still are the old ones."""
    rows = (
        await conn.execute(
            text(
                "SELECT id, sent_at, group_id, segments FROM messages "
                'WHERE group_id IS NOT NULL AND segments @> \'[{"type": "reply"}]\''
            )
        )
    ).all()
    pairs = (await conn.execute(text("SELECT group_id, old_id, new_id FROM qq_remap"))).all()
    mapping = {(g, o): n for g, o, n in pairs if n != o}
    known = {(g, n) for g, _, n in pairs}  # ids already Milky's (messages stored after a switch)
    changed = 0
    for row_id, sent_at, group_id, segments in rows:
        out: list[dict[str, Any]] = []
        for seg in segments:
            data = seg.get("data") or {}
            old = data.get("message_id")
            if seg.get("type") == "reply" and isinstance(old, int):
                seg = {**seg, "data": {**data, "message_id": _new_id(mapping, known, group_id, old)}}
            out.append(seg)
        if out != segments:
            await conn.execute(
                text("UPDATE messages SET segments = CAST(:segments AS jsonb) WHERE id = :id AND sent_at = :sent_at"),
                {"segments": json.dumps(out, ensure_ascii=False), "id": row_id, "sent_at": sent_at},
            )
            changed += 1
    return changed


async def remap(*, apply: bool) -> dict[str, int]:
    """Remap (or, without ``apply``, count what would change); the counts per column."""
    async with engine().connect() as conn:
        trans = await conn.begin()
        try:
            if await _count(conn, f"SELECT count(*) FROM settings WHERE key = '{MARKER}'"):
                raise SystemExit(f"already remapped (settings key {MARKER}); nothing to do")
            for statement in _MAP:
                await conn.execute(text(statement))
            counts = {
                "map": await _count(conn, "SELECT count(*) FROM qq_remap"),
                "map.without_seq": await _count(conn, f"SELECT count(*) FROM qq_remap WHERE new_id < -{RETIRED // 2}"),
                "messages.segments": await _remap_segments(conn),
            }
            for table, column in _REFERENCES:
                # Not a stored message's old id nor already a Milky id: a message never stored.
                counts[f"{table}.{column}.unknown"] = await _update(
                    conn,
                    f"UPDATE {table} t SET {column} = t.{column} - {RETIRED} "
                    f"WHERE t.group_id IS NOT NULL AND t.{column} > -{RETIRED // 2} "
                    f"AND NOT EXISTS (SELECT 1 FROM qq_remap r "
                    f"WHERE r.group_id = t.group_id AND r.old_id = t.{column}) "
                    f"AND NOT EXISTS (SELECT 1 FROM qq_remap r "
                    f"WHERE r.group_id = t.group_id AND r.new_id = t.{column})",
                )
                counts[f"{table}.{column}"] = await _update(
                    conn,
                    f"UPDATE {table} t SET {column} = r.new_id FROM qq_remap r "
                    f"WHERE t.group_id = r.group_id AND t.{column} = r.old_id AND r.new_id <> r.old_id",
                )
            counts["messages.qq_message_id"] = await _update(
                conn,
                "UPDATE messages m SET qq_message_id = r.new_id FROM qq_remap r "
                "WHERE m.group_id = r.group_id AND m.qq_message_id = r.old_id AND r.new_id <> r.old_id",
            )
            if apply:
                value = {"at": datetime.now(UTC).isoformat(), "counts": counts}
                await conn.execute(
                    text("INSERT INTO settings (key, value) VALUES (:key, CAST(:value AS jsonb))"),
                    {"key": MARKER, "value": json.dumps(value)},
                )
                await trans.commit()
            else:
                await trans.rollback()
        except BaseException:
            if trans.is_active:
                await trans.rollback()
            raise
    return counts


async def _main(apply: bool) -> None:
    try:
        counts = await remap(apply=apply)
    finally:
        await dispose_engine()
    lines = [f"{key}: {n}" for key, n in counts.items()]
    lines.append("applied" if apply else "dry run: rolled back (use --apply to change)")
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Remap stored QQ message ids from OneBot's to Milky's (once).")
    parser.add_argument("--apply", action="store_true", help="change the data (default: report and roll back)")
    asyncio.run(_main(parser.parse_args().apply))
