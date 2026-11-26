"""Repeated tldr requests: the same target asked for again within ``ratelimit.repeat_window_s`` is answered once."""

import hashlib

from yggdrasil.ingest.normalize import normalize
from yggdrasil.kook.model import Incoming
from yggdrasil.kv import redis_client
from yggdrasil.triggers.index import Resolved


def key(incoming: Incoming, resolved: Resolved, handler: str, request_text: str) -> str | None:
    if handler != "tldr" or incoming.channel_id is None:
        return None
    if normalize(incoming.segments).attachments:
        return None
    words = " ".join(request_text.split())
    reply = incoming.reply if resolved.target in ("quoted_or_self", "quoted") else None
    target = reply.message_id if reply is not None else ""
    if not target and not words:
        return None
    return f"ygg:repeat:{incoming.channel_id}:{target}:{hashlib.blake2b(words.encode(), digest_size=16).hexdigest()}"


async def first(key: str, seconds: int) -> bool:
    if seconds <= 0:
        return True
    return bool(await redis_client().set(key, "1", ex=seconds, nx=True))


async def forget(key: str) -> None:
    await redis_client().delete(key)
