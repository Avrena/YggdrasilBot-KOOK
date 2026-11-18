"""qq segment schema

Stored QQ messages switch from OneBot v11 segments to the bot's own schema (``yggdrasil.qq.segments``, Milky-named):
``messages.segments`` and the QQ messages kept in ``transcripts`` (context snapshots, the prompt context and forward
displays of the entries; ``image_keys`` follow the new image identity). Rows already in the schema are left as they
are.

Stickers follow the new identity: an image sticker's ``ref`` becomes the content sha1 (of the saved image, else of
the NT file id in the newest stored message with that md5 file name; rows with neither keep their md5 ref and are
counted), and SnowLuma market faces collected as image stickers become ``mface`` rows ``"<package>:<emoji>"``
(merged into an existing one). ``invocation_sessions`` are dropped: their handles hold OneBot segment data (at most an
hour's worth).

The OneBot mapping is a frozen copy: the app's protocol backend may change, this must not. Irreversible.

Revision ID: e2a9c4f7b130
Revises: b7f2c9d4e815
Create Date: 2026-11-17 12:00:00.000000
"""

import base64
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

revision: str = "e2a9c4f7b130"
down_revision: str | Sequence[str] | None = "b7f2c9d4e815"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH = 1000
_MD5_FILE = re.compile(r"^([0-9A-Fa-f]{32})(?:\.\w+)?$")
# Segment types only the schema has, and for the types both have, a data key only the schema uses.
_SCHEMA_ONLY = {"mention", "mention_all", "market_face", "light_app"}
_SCHEMA_KEY = {
    "face": "face_id",
    "reply": "message_id",
    "image": "sha1",
    "record": "duration",
    "video": "duration",
    "file": "file_name",
    "forward": "forward_id",
    "xml": "xml_payload",
}

type Segment = dict[str, Any]


# --- conversion (pure) ----------------------------------------------------------------------------------------------


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def sha1_of(file_id: str) -> str:
    """The content sha1 in an NT file id (base64url protobuf, field 2 = 20 bytes); "" when it is not one."""
    try:
        raw = base64.urlsafe_b64decode(file_id + "=" * (-len(file_id) % 4))
    except ValueError:
        return ""
    if len(raw) >= 22 and raw[0] == 0x12 and raw[1] == 20:
        return raw[2:22].hex()
    return ""


def file_id_of(url: str) -> str:
    try:
        return parse_qs(urlsplit(url).query).get("fileid", [""])[0]
    except ValueError:
        return ""


def _image(d: dict[str, Any]) -> dict[str, Any]:
    url, summary = str(d.get("url") or ""), str(d.get("summary") or "")
    image: dict[str, Any] = {
        "ref": str(d.get("file") or ""),
        "url": url,
        "summary": summary,
        "sticker": str(d.get("sub_type")) == "1" or summary.strip("[]") not in ("", "图片"),
        "sha1": sha1_of(file_id_of(url)),
    }
    if d.get("blob"):
        image["blob"] = str(d["blob"])
    return image


def _node(item: dict[str, Any]) -> dict[str, Any]:
    """A forward node: ``{"type": "node", "data": {user_id, nickname, time, content}}`` (our record of a forward the
    bot sent) or a ``get_forward_msg`` record ``{sender: {user_id, nickname, card}, time, message}``."""
    data = _dict(item.get("data")) if item.get("type") == "node" else item
    sender = _dict(data.get("sender"))
    return {
        "user_id": _int(data.get("user_id") or sender.get("user_id")),
        "name": str(data.get("nickname") or sender.get("nickname") or sender.get("card") or ""),
        "time": _int(data.get("time")),
        "segments": convert(data.get("content") or data.get("message")),
    }


def _segment(seg: dict[str, Any]) -> list[Segment]:
    kind = str(seg.get("type") or "")
    d = _dict(seg.get("data"))
    if kind in _SCHEMA_ONLY or (kind in _SCHEMA_KEY and _SCHEMA_KEY[kind] in d):
        return [{"type": kind, "data": d}]  # already converted
    match kind:
        case "text":
            return [{"type": "text", "data": {"text": str(d.get("text") or "")}}]
        case "at" if str(d.get("qq")) == "all":
            return [{"type": "mention_all", "data": {}}]
        case "at":
            name = str(d.get("name") or "").removeprefix("@")
            return [{"type": "mention", "data": {"user_id": _int(d.get("qq")), "name": name}}]
        case "face":
            face_id = "" if d.get("id") is None else str(d["id"])
            return [{"type": "face", "data": {"face_id": face_id, "is_large": False}}]
        case "reply":
            return [{"type": "reply", "data": {"message_id": _int(d.get("id"))}}]
        case "mface" | "image" if kind == "mface" or (d.get("emoji_id") and d.get("emoji_package_id")):
            # SnowLuma sends market faces as images carrying the face's ids.
            face = {
                "emoji_package_id": _int(d.get("emoji_package_id")),
                "emoji_id": str(d.get("emoji_id") or ""),
                "key": str(d.get("key") or ""),
                "summary": str(d.get("summary") or ""),
                "url": str(d.get("url") or ""),
            }
            return [{"type": "market_face", "data": face}]
        case "image":
            return [{"type": "image", "data": _image(d)}]
        case "record" | "video":
            return [
                {"type": kind, "data": {"ref": str(d.get("file") or ""), "url": str(d.get("url") or ""), "duration": 0}}
            ]
        case "file":
            file: dict[str, Any] = {
                "file_id": str(d.get("file_id") or ""),
                "file_name": str(d.get("name") or d.get("file") or ""),
                "file_size": _int(d.get("file_size") or d.get("size")),
            }
            if d.get("file_hash"):
                file["file_hash"] = str(d["file_hash"])
            return [{"type": "file", "data": file}]
        case "forward":
            forward: dict[str, Any] = {
                "forward_id": str(d.get("id") or ""),
                "title": str(d.get("title") or ""),
                "preview": [],
                "summary": "",
            }
            if isinstance(content := d.get("content"), list):  # our record of a forward the bot sent
                forward["nodes"] = [_node(n) for n in content if isinstance(n, dict)]
            return [{"type": "forward", "data": forward}]
        case "node":
            # A node nested in a recorded forward node (NapCat's nested markdown forwards): its content inline.
            return _node(seg)["segments"]
        case "json":
            payload = d.get("data") or ""
            if not isinstance(payload, str):
                payload = json.dumps(payload, ensure_ascii=False)
            try:
                app = json.loads(payload).get("app")
            except (ValueError, AttributeError, RecursionError):
                app = None
            return [{"type": "light_app", "data": {"app_name": str(app or ""), "json_payload": payload}}]
        case "xml":
            return [{"type": "xml", "data": {"service_id": 0, "xml_payload": str(d.get("data") or "")}}]
        case "markdown":
            return [{"type": "markdown", "data": {"content": str(d.get("content") or "")}}]
        case "poke":
            return [{"type": "text", "data": {"text": "[戳一戳]"}}]
        case "dice" | "rps":
            label = "骰子" if kind == "dice" else "猜拳"
            return [{"type": "text", "data": {"text": f"[{label}:{d.get('result', '')}]"}}]
        case "inline_keyboard":
            return []  # bot buttons: no content
        case _:
            return [{"type": "text", "data": {"text": f"[{kind}]"}}]


def convert(segments: Any) -> list[Segment]:
    """Stored segments in the schema (OneBot ones converted, schema ones kept); never raises. A plain string (OneBot
    may give one) is one text segment; entries that are not segments are dropped."""
    if isinstance(segments, str):
        return [{"type": "text", "data": {"text": segments}}]
    if not isinstance(segments, list):
        return []
    return [out for seg in segments if isinstance(seg, dict) for out in _segment(seg)]


# --- transcripts ----------------------------------------------------------------------------------------------------


def _old_image_key(seg: Any) -> str | None:
    """What the app keyed an OneBot image by (``transcript.notes.image_key`` before this revision)."""
    if not isinstance(seg, dict) or seg.get("type") != "image" or not isinstance(seg.get("data"), dict):
        return None
    data = seg["data"]
    if "sha1" in data or (data.get("emoji_id") and data.get("emoji_package_id")):
        return None
    key = data.get("file_id") or data.get("file") or data.get("url")
    return str(key) if key else None


def _convert_tracked(segments: Any, keys: dict[str, str]) -> list[Segment]:
    """``convert``, noting each OneBot image's old key -> its new one (``sha1``, else ``ref``, else ``url``)."""
    for seg in segments if isinstance(segments, list) else []:
        if (old := _old_image_key(seg)) is not None:
            data = _image(seg["data"])
            keys[old] = str(data["sha1"] or data["ref"] or data["url"] or old)
    return convert(segments)


def convert_transcript(entries: Any, context: Any) -> tuple[Any, Any]:
    """A transcript's ``entries`` and ``context`` with every QQ message's segments converted."""
    keys: dict[str, str] = {}

    def message(m: Any) -> Any:
        if isinstance(m, dict) and "segments" in m:
            return {**m, "segments": _convert_tracked(m["segments"], keys)}
        return m

    new_entries = entries
    if isinstance(entries, list):
        new_entries = []
        for entry in entries:
            display = entry.get("display") if isinstance(entry, dict) else None
            if isinstance(display, dict) and display.get("kind") == "qq_context":
                display = dict(display)
                for name in ("trigger", "quoted"):
                    if isinstance(display.get(name), dict):
                        display[name] = message(display[name])
                if isinstance(display.get("chain"), list):
                    display["chain"] = [message(m) for m in display["chain"]]
                entry = {**entry, "display": display}
            elif isinstance(display, dict) and display.get("kind") == "qq_forward":
                nodes = display.get("nodes")
                entry = {**entry, "display": {**display, "nodes": [message(n) for n in nodes or []]}}
            new_entries.append(entry)
    new_context = context
    if isinstance(context, dict):
        new_context = dict(context)
        if isinstance(context.get("messages"), dict):
            new_context["messages"] = {k: message(m) for k, m in context["messages"].items()}
        if isinstance(context.get("image_keys"), dict):
            new_context["image_keys"] = {keys.get(k, k): v for k, v in context["image_keys"].items()}
    return new_entries, new_context


# --- stickers -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketFace:
    package_id: str
    emoji_id: str
    key: str
    summary: str


@dataclass(slots=True)
class Clues:
    """What stored messages say about image stickers' md5 file names (upper-case): the content sha1 (from the url's
    NT file id) or, for SnowLuma market faces sent as images, the market face. The newest message wins."""

    sha1s: dict[str, str] = field(default_factory=dict)
    faces: dict[str, MarketFace] = field(default_factory=dict)

    def collect(self, segments: Any) -> None:
        for seg in segments if isinstance(segments, list) else []:
            if not isinstance(seg, dict) or seg.get("type") != "image" or not isinstance(seg.get("data"), dict):
                continue
            data = seg["data"]
            match = _MD5_FILE.match(str(data.get("file") or data.get("ref") or ""))
            if match is None:
                continue
            md5 = match.group(1).upper()
            if data.get("emoji_id") and data.get("emoji_package_id"):
                self.faces[md5] = MarketFace(
                    str(data["emoji_package_id"]),
                    str(data["emoji_id"]),
                    str(data.get("key") or ""),
                    str(data.get("summary") or "").strip("[]"),
                )
            elif sha1 := str(data.get("sha1") or "") or sha1_of(file_id_of(str(data.get("url") or ""))):
                self.sha1s[md5] = sha1


def sticker_target(md5: str, blob: bytes | None, clues: Clues) -> tuple[str, str, MarketFace | None] | None:
    """The ``(kind, ref, market face)`` an md5-keyed image sticker becomes; None when nothing tells."""
    md5 = md5.upper()
    if (face := clues.faces.get(md5)) is not None:
        return "mface", f"{face.package_id}:{face.emoji_id}", face
    if blob is not None:
        return "image", hashlib.sha1(blob).hexdigest(), None
    if sha1 := clues.sha1s.get(md5):
        return "image", sha1, None
    return None


# --- database -------------------------------------------------------------------------------------------------------


def _messages(conn: Connection, clues: Clues, before: Counter[str], after: Counter[str]) -> tuple[int, int]:
    select = sa.text("SELECT id, sent_at, segments FROM messages WHERE id > :last ORDER BY id LIMIT :n").columns(
        sa.column("id", sa.BigInteger), sa.column("sent_at", sa.DateTime(timezone=True)), sa.column("segments", JSONB)
    )
    update = sa.text("UPDATE messages SET segments = :segments WHERE id = :id AND sent_at = :sent_at").bindparams(
        sa.bindparam("segments", type_=JSONB)
    )
    last, seen, changed = 0, 0, 0
    while rows := conn.execute(select, {"last": last, "n": BATCH}).all():
        updates = []
        for mid, sent_at, segments in rows:
            clues.collect(segments)
            new = convert(segments)
            old = segments if isinstance(segments, list) else []
            before.update(str(s.get("type") or "") if isinstance(s, dict) else "?" for s in old)
            after.update(s["type"] for s in new)
            if new != segments:
                updates.append({"id": mid, "sent_at": sent_at, "segments": new})
        if updates:
            conn.execute(update, updates)
        seen, changed, last = seen + len(rows), changed + len(updates), rows[-1][0]
    return seen, changed


def _transcripts(conn: Connection) -> tuple[int, int]:
    select = sa.text("SELECT id, entries, context FROM transcripts WHERE id > :last ORDER BY id LIMIT :n").columns(
        sa.column("id", sa.BigInteger), sa.column("entries", JSONB), sa.column("context", JSONB)
    )
    update = sa.text("UPDATE transcripts SET entries = :entries, context = :context WHERE id = :id").bindparams(
        sa.bindparam("entries", type_=JSONB), sa.bindparam("context", type_=JSONB)
    )
    last, seen, changed = 0, 0, 0
    while rows := conn.execute(select, {"last": last, "n": 100}).all():
        updates = []
        for tid, entries, context in rows:
            new_entries, new_context = convert_transcript(entries, context)
            if (new_entries, new_context) != (entries, context):
                updates.append({"id": tid, "entries": new_entries, "context": new_context})
        if updates:
            conn.execute(update, updates)
        seen, changed, last = seen + len(rows), changed + len(updates), rows[-1][0]
    return seen, changed


def _merge_sticker(conn: Connection, source: int, target: int) -> None:
    """Fold sticker ``source`` (counts, per-group usage) into ``target`` and delete it."""
    params = {"s": source, "t": target}
    conn.execute(
        sa.text(
            "INSERT INTO sticker_usage (sticker_id, group_id, count, last_seen)"
            " SELECT :t, group_id, count, last_seen FROM sticker_usage WHERE sticker_id = :s"
            " ON CONFLICT (sticker_id, group_id) DO UPDATE SET count = sticker_usage.count + EXCLUDED.count,"
            " last_seen = GREATEST(sticker_usage.last_seen, EXCLUDED.last_seen)"
        ),
        params,
    )
    conn.execute(
        sa.text(
            "UPDATE stickers t SET seen_count = t.seen_count + s.seen_count,"
            " first_seen = LEAST(t.first_seen, s.first_seen), last_seen = GREATEST(t.last_seen, s.last_seen)"
            " FROM stickers s WHERE t.id = :t AND s.id = :s"
        ),
        params,
    )
    conn.execute(sa.text("DELETE FROM stickers WHERE id = :s"), params)


def _stickers(conn: Connection, clues: Clues) -> Counter[str]:
    out = Counter[str]()
    rows = conn.execute(
        sa.text("SELECT id, ref FROM stickers WHERE kind = 'image' AND ref ~* '^[0-9a-f]{32}$' ORDER BY id")
    ).all()
    for sid, md5 in rows:
        blob = conn.execute(sa.text("SELECT data FROM sticker_blobs WHERE sticker_id = :id"), {"id": sid}).scalar()
        target = sticker_target(md5, blob, clues)
        if target is None:
            out["unresolved"] += 1
            continue
        kind, ref, face = target
        existing = conn.execute(
            sa.text("SELECT id FROM stickers WHERE kind = :k AND ref = :r"), {"k": kind, "r": ref}
        ).scalar()
        if existing is not None and existing != sid:
            _merge_sticker(conn, sid, existing)
            out[f"{kind} merged"] += 1
        elif face is not None:
            conn.execute(
                sa.text(
                    "UPDATE stickers SET kind = 'mface', ref = :r, emoji_id = :e, package_id = :p, key = :key,"
                    " summary = COALESCE(NULLIF(:summary, ''), summary),"
                    " status = CASE WHEN status IN ('fetching', 'failed') THEN 'ready' ELSE status END,"
                    " error = CASE WHEN status IN ('fetching', 'failed') THEN '' ELSE error END WHERE id = :id"
                ),
                {
                    "id": sid,
                    "r": ref,
                    "e": face.emoji_id,
                    "p": face.package_id,
                    "key": face.key,
                    "summary": face.summary,
                },
            )
            out["mface"] += 1
        else:
            conn.execute(sa.text("UPDATE stickers SET ref = :r WHERE id = :id"), {"id": sid, "r": ref})
            out["image"] += 1
    return out


def upgrade() -> None:
    conn = op.get_bind()
    clues, before, after = Clues(), Counter[str](), Counter[str]()
    seen, changed = _messages(conn, clues, before, after)
    transcripts, transcripts_changed = _transcripts(conn)
    stickers = _stickers(conn, clues)
    op.execute("DELETE FROM invocation_sessions")
    sys.stderr.write(
        f"qq segment schema: {changed}/{seen} messages converted\n"
        f"  segment types before: {dict(before.most_common())}\n"
        f"  segment types after: {dict(after.most_common())}\n"
        f"  {transcripts_changed}/{transcripts} transcripts converted\n"
        f"  image stickers re-keyed: {dict(stickers)}\n"
    )


def downgrade() -> None:
    raise NotImplementedError("irreversible: the OneBot segments are gone")
