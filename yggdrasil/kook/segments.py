"""KOOK message segment schema: what ``messages.segments`` stores and every reader understands.

A message is a list of ``{"type": ..., "data": {...}}`` dicts. All IDs (user_id, message_id, channel_id, role_id)
are strings matching KOOK API v3.
"""

from typing import Any, Literal, NotRequired, TypedDict

type Segment = dict[str, Any]
"""``{"type": <SegmentType>, "data": <matching *Data>}``."""

SegmentType = Literal[
    "text",
    "kmarkdown",
    "mention",
    "mention_all",
    "mention_here",
    "mention_role",
    "reply",
    "image",
    "file",
    "video",
    "audio",
    "card",
    "emoji",
]


class TextData(TypedDict):
    text: str


class KMarkdownData(TypedDict):
    content: str


class MentionData(TypedDict):
    user_id: str
    name: str  # without leading "@"; "" when unknown


class MentionRoleData(TypedDict):
    role_id: str
    name: str


class ReplyData(TypedDict):
    message_id: str


class ImageData(TypedDict):
    url: str
    name: str
    sticker: bool
    blob: NotRequired[str]  # sha256 in transcript_blobs for bot-sent images


class FileData(TypedDict):
    url: str
    file_name: str
    file_size: int


class MediaData(TypedDict):
    """``video`` and ``audio``."""

    url: str
    name: str
    duration: int  # seconds; 0 when unknown


class CardData(TypedDict):
    modules: list[dict[str, Any]]
    plain_text: str


class EmojiData(TypedDict):
    id: str  # KOOK guild emoji id, e.g. "123456/abcdef"
    name: str
    guild_id: NotRequired[str]
    url: NotRequired[str]


# --- constructors -----------------------------------------------------------------------------------------------


def text(content: str) -> Segment:
    return {"type": "text", "data": {"text": content}}


def kmarkdown(content: str) -> Segment:
    return {"type": "kmarkdown", "data": {"content": content}}


def mention(user_id: str, name: str = "") -> Segment:
    return {"type": "mention", "data": {"user_id": user_id, "name": name}}


def mention_all() -> Segment:
    return {"type": "mention_all", "data": {}}


def mention_here() -> Segment:
    return {"type": "mention_here", "data": {}}


def mention_role(role_id: str, name: str = "") -> Segment:
    return {"type": "mention_role", "data": {"role_id": role_id, "name": name}}


def reply(message_id: str) -> Segment:
    return {"type": "reply", "data": {"message_id": message_id}}


def image(url: str, *, name: str = "", sticker: bool = False, blob: str | None = None) -> Segment:
    data: dict[str, Any] = {"url": url, "name": name, "sticker": sticker}
    if blob:
        data["blob"] = blob
    return {"type": "image", "data": data}


def file(url: str, *, file_name: str, file_size: int = 0) -> Segment:
    return {"type": "file", "data": {"url": url, "file_name": file_name, "file_size": file_size}}


def card(modules: list[dict[str, Any]], plain_text: str) -> Segment:
    return {"type": "card", "data": {"modules": modules, "plain_text": plain_text}}


def emoji(emoji_id: str, name: str, *, guild_id: str = "", url: str = "") -> Segment:
    data: dict[str, Any] = {"id": emoji_id, "name": name}
    if guild_id:
        data["guild_id"] = guild_id
    if url:
        data["url"] = url
    return {"type": "emoji", "data": data}
