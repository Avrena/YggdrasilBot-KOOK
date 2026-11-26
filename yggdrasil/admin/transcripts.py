"""Transcripts for the console: the stored conversation plus pipeline events, ready to render for KOOK."""

import copy
import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import select, tuple_

from yggdrasil.admin.common import not_found
from yggdrasil.agent.schema import BotReply, OpsReply
from yggdrasil.db.models import Sticker, StickerBlob, Transcript
from yggdrasil.db.session import get_session
from yggdrasil.transcript import store
from yggdrasil.transcript.notes import image_key
from yggdrasil.transcript.schema import (
    ChatContextDisplay,
    ChatMessage,
    Entry,
    OutputPart,
    Part,
    TextPart,
    ToolCallPart,
)

router = APIRouter(prefix="/transcripts", tags=["transcripts"])

BLOB_URL = "/api/transcripts/blobs/{}"
STICKER_URL = "/api/stickers/{}/image"
_REPLY: TypeAdapter[BotReply | OpsReply] = TypeAdapter(BotReply | OpsReply)


class BotOut(BaseModel):
    user_id: str
    name: str


class TranscriptOut(BaseModel):
    available: bool
    source: str
    content: Literal["full", "metadata"]
    live: bool = False
    entries: list[Entry]
    messages: dict[str, ChatMessage] = {}
    names: dict[str, str] = {}
    bot: BotOut | None = None
    updated_at: datetime | None = None


def _sticker_key(seg: dict[str, Any]) -> tuple[str, str] | None:
    data = seg.get("data")
    if not isinstance(data, dict):
        return None
    if seg.get("type") == "emoji" and data.get("id"):
        return ("emoji", str(data["id"]))
    if seg.get("type") == "image" and data.get("sha256"):
        return ("image", str(data["sha256"]))
    return None


async def _stickers(segments: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    keys = {k for seg in segments if (k := _sticker_key(seg)) is not None}
    if not keys:
        return {}
    async with get_session() as session:
        rows = await session.execute(
            select(Sticker.kind, Sticker.ref, Sticker.id)
            .join(StickerBlob, StickerBlob.sticker_id == Sticker.id)
            .where(tuple_(Sticker.kind, Sticker.ref).in_(keys))
        )
        return {(k, r): i for k, r, i in rows.all()}


class _Sources:
    def __init__(self, image_keys: dict[str, str], stickers: dict[tuple[str, str], int]) -> None:
        self._keys, self._stickers = image_keys, stickers

    def segments(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = copy.deepcopy(segments)
        for seg in out:
            data = seg.get("data")
            if not isinstance(data, dict):
                continue
            match seg.get("type"):
                case "emoji":
                    k = _sticker_key(seg)
                    if k is not None and (sid := self._stickers.get(k)) is not None:
                        data["src"] = STICKER_URL.format(sid)
                    elif url := data.get("url"):
                        data["src"] = url
                case "image":
                    key = image_key(data)
                    k = _sticker_key(seg)
                    if key and key in self._keys:
                        data["src"] = BLOB_URL.format(self._keys[key])
                    elif blob := data.get("blob"):
                        data["src"] = BLOB_URL.format(blob)
                    elif k is not None and (sid := self._stickers.get(k)) is not None:
                        data["src"] = STICKER_URL.format(sid)
                    else:
                        data["src"] = data.get("url")
        return out

    def message(self, m: ChatMessage) -> ChatMessage:
        return m.model_copy(update={"segments": self.segments(m.segments)})


def _merge(items: list[Entry], events: list[Entry]) -> list[Entry]:
    out = list(items)
    for event in sorted(events, key=lambda e: (e.at is None, e.at or datetime.min)):
        idx = len(out)
        if event.at is not None:
            idx = next((i for i, e in enumerate(out) if e.at is not None and e.at > event.at), len(out))
        out.insert(idx, event)
    return out


def _mark_output(items: list[Entry]) -> None:
    last = next((e for e in reversed(items) if e.kind == "assistant"), None)
    if last is None or any(isinstance(p, ToolCallPart) for p in last.parts):
        return
    texts = [p for p in last.parts if isinstance(p, TextPart)]
    if not texts or any(p.text is None for p in texts):
        return
    raw = "".join(p.text or "" for p in texts).strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        value = json.loads(raw)
        _REPLY.validate_python(value)
    except (ValueError, ValidationError):
        return
    parts: list[Part] = [p for p in last.parts if not isinstance(p, TextPart)]
    parts.append(OutputPart(value=value, chars=sum(p.chars for p in texts)))
    last.parts = parts


async def build(
    row: Transcript | None, *, events: list[Entry], fallback: list[Entry], live: bool = False
) -> TranscriptOut:
    if row is None:
        return TranscriptOut(
            available=False, source="none", content="full", live=live, entries=_merge(fallback, events)
        )
    items = [Entry.model_validate(e) for e in row.entries]
    if row.source == "pydantic-ai":
        _mark_output(items)
    context = row.context or {}
    messages = {k: ChatMessage.model_validate(v) for k, v in (context.get("messages") or {}).items()}
    shown: list[list[dict[str, Any]]] = [m.segments for m in messages.values()]
    for e in items:
        if isinstance(e.display, ChatContextDisplay):
            shown += [m.segments for m in (e.display.trigger, e.display.quoted, *e.display.chain) if m is not None]
    sources = _Sources(context.get("image_keys") or {}, await _stickers([s for segs in shown for s in segs]))
    for e in items:
        if isinstance(e.display, ChatContextDisplay):
            d = e.display
            e.display = d.model_copy(
                update={
                    "trigger": sources.message(d.trigger) if d.trigger else None,
                    "quoted": sources.message(d.quoted) if d.quoted else None,
                    "chain": [sources.message(m) for m in d.chain],
                }
            )
    return TranscriptOut(
        available=True,
        source=row.source,
        content="metadata" if row.content == "metadata" else "full",
        live=live,
        entries=_merge(items, events),
        messages={k: sources.message(m) for k, m in messages.items()},
        names=context.get("names") or {},
        bot=BotOut.model_validate(context["bot"]) if context.get("bot") else None,
        updated_at=row.updated_at,
    )


@router.get("/blobs/{sha256}", response_class=Response, responses={200: {"content": {"image/*": {}}}})
async def transcript_blob(sha256: str) -> Response:
    found = await store.blob(sha256)
    if found is None:
        raise not_found("图片")
    return Response(
        found.data, media_type=found.mime, headers={"Cache-Control": "private, max-age=31536000, immutable"}
    )
