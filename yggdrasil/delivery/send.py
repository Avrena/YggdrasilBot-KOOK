"""Sending messages: text and images (each recorded as the bot's own message), reactions, and the notice when QQ
drops a message. Merged forwards are sent by ``delivery.forward``."""

import base64
from datetime import UTC, datetime

from yggdrasil.agent.media import media_type
from yggdrasil.core.log import LOGGER
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.qq import segments as seg
from yggdrasil.qq.client import QQ
from yggdrasil.qq.model import Dropped, QQError, Unconfirmed
from yggdrasil.qq.segments import Segment
from yggdrasil.transcript.notes import Blob, blob_id
from yggdrasil.transcript.store import store_blobs


def record_self(group_id: int, self_id: int, nickname: str, message_id: int, segments: list[Segment]) -> None:
    """Log a message the bot sent as its own (stored-schema ``segments``), so the history (and a later reply to it)
    can read it."""
    WRITER.submit(
        IncomingMessage(
            group_id=group_id,
            user_id=self_id,
            qq_message_id=message_id,
            message_seq=None,
            sent_at=datetime.now(UTC),
            sender_name=nickname,
            segments=segments,
            is_self=True,
        )
    )


async def react(qq: QQ, group_id: int | None, message_id: int, emoji: str) -> None:
    """Put the QQ face ``emoji`` on a group message; nothing in private chats (QQ has no reactions there) or without
    an emoji. Failures are logged, never raised."""
    if group_id is None or not emoji:
        return
    try:
        await qq.react(group_id=group_id, message_id=message_id, emoji=emoji)
    except Exception as e:
        LOGGER.warning("reaction {} on {} failed: {}", emoji, message_id, e)


class MessageDropped(Exception):
    """QQ took the send but never posted it (``qq.model.Dropped``); a short notice went out instead."""


# On 2026-11-01 both a forward and its plain-text copy describing a proxy client were dropped within a second: QQ's
# content filter, as far as we can tell, so sending the same content again in another form doesn't help.
DROPPED_NOTICE = "这条回复没能发出来：QQ 收下了但没有发到群里，可能是内容触发了 QQ 的拦截。"


async def notify_dropped(qq: QQ, group_id: int, self_id: int, nickname: str) -> None:
    """Say in the group that the answer didn't get through (the notice carries none of the dropped content)."""
    segments = [seg.text(DROPPED_NOTICE)]
    try:
        message_id = await qq.send(group_id=group_id, user_id=0, segments=segments)
    except QQError as e:
        LOGGER.warning("the dropped-reply notice to {} didn't go out either: {}", group_id, e)
        return
    record_self(group_id, self_id, nickname, message_id, segments)


async def send_text(
    qq: QQ, *, group_id: int | None, user_id: int, reply_to: int | None, text: str, self_id: int, nickname: str
) -> int:
    """Raises :class:`MessageDropped` (after posting a notice) when QQ swallows the group message."""
    segments = ([seg.reply(reply_to)] if reply_to else []) + [seg.text(text)]
    try:
        message_id = await qq.send(group_id=group_id, user_id=user_id, segments=segments)
    except Dropped as e:
        if group_id is None:
            raise
        LOGGER.warning("text to {} was dropped by QQ: {}", group_id, e)
        await notify_dropped(qq, group_id, self_id, nickname)
        raise MessageDropped(str(e)) from e
    if group_id is not None:
        record_self(group_id, self_id, nickname, message_id, segments)
    return message_id


# Uploading a picture can take QQ well over the adapter's 30 s api_timeout.
SEND_IMAGE_TIMEOUT_S = 120.0


async def send_image(
    qq: QQ,
    *,
    group_id: int | None,
    user_id: int,
    reply_to: int | None,
    images: list[bytes],
    text: str,
    self_id: int,
    nickname: str,
    sticker: bool = False,
) -> int | None:
    """Images (with an optional line of text) as one plain message, replying to the trigger; ``sticker`` sends them
    as stickers.

    None: QQ didn't confirm (``Unconfirmed``, e.g. no answer within ``SEND_IMAGE_TIMEOUT_S``). The message almost
    always went out anyway (it did when the first drawing hit the default timeout), so callers must not send it again.
    """
    segments = [seg.reply(reply_to)] if reply_to else []
    if text:
        segments.append(seg.text(text))
    segments += [seg.image("base64://" + base64.b64encode(png).decode(), sticker=sticker) for png in images]
    try:
        message_id = await qq.send(
            group_id=group_id, user_id=user_id, segments=segments, reply_timeout=SEND_IMAGE_TIMEOUT_S
        )
    except Unconfirmed as e:
        LOGGER.warning("sending images to {} was not confirmed: {}", group_id or user_id, e)
        return None
    if group_id is None:
        return message_id
    # Record the message as QQ stored it (image segments with ref and url, not our base64), so a later reply to it
    # can load the picture again (e.g. "make it bluer").
    try:
        stored = (await qq.message(group_id=group_id, user_id=user_id, message_id=message_id)).segments
    except Exception as e:
        LOGGER.warning("fetching the sent images back failed: {}", e)
        stored = [_unseen_image() if s["type"] == "image" else s for s in segments]
    record_self(group_id, self_id, nickname, message_id, await _keep_pictures(stored, images))
    return message_id


def _unseen_image() -> Segment:
    """A picture we sent but couldn't fetch back from QQ: no ref or url, only the kept copy (``_keep_pictures``)."""
    return {"type": "image", "data": {"ref": "", "url": "", "summary": "[图片]", "sticker": False, "sha1": ""}}


async def _keep_pictures(segments: list[Segment], pictures: list[bytes]) -> list[Segment]:
    """Stamp each image segment with the sha256 of the picture we sent, stored in ``transcript_blobs``: QQ may hand
    our own pictures back without a usable url, which left them unreadable to a later reply."""
    shown = [s for s in segments if s.get("type") == "image"]
    if len(shown) != len(pictures):
        return segments
    blobs = {blob_id(p): Blob(p, media_type(p)) for p in pictures}
    try:
        await store_blobs(blobs)
    except Exception as e:
        LOGGER.warning("keeping sent pictures failed: {}", e)
        return segments
    for s, picture in zip(shown, pictures, strict=True):
        s["data"] = {**(s.get("data") or {}), "blob": blob_id(picture)}
    return segments
