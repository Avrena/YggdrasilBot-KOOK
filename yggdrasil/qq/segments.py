"""The bot's own QQ message segment schema: what ``messages.segments`` stores and every reader understands.

A message is a list of ``{"type": ..., "data": {...}}`` dicts. Names and structure follow Milky's incoming segments
(https://milky.ntqqrev.org/struct/IncomingSegment); each protocol backend (``qq.milky``, ``qq.onebot``) translates
to and from this schema, so nothing outside ``yggdrasil.qq`` sees a protocol's own shapes. Deviations from Milky:

- ``reply.message_id`` instead of ``message_seq``: the protocol's own id of the quoted message (``qq_message_id``),
  a seq under Milky and a hash under OneBot.
- media (``image``/``record``/``video``) carry ``ref`` (the protocol's handle to refresh ``url``, Milky
  ``resource_id``) and ``url`` (Milky ``temp_url``); ``image.sticker`` replaces ``sub_type``, and ``image.sha1`` is
  the content hash QQ embeds in the NT file id (the same picture has the same sha1 under every protocol).
- ours only: ``image.blob`` (sha256 of a picture the bot sent, kept in ``transcript_blobs``) and ``forward.nodes``
  (the content of a forward the bot sent, kept with its own record).

Types with no counterpart here (OneBot ``poke``/``dice``/``rps``) become ``text`` placeholders; content-free ones
(``inline_keyboard``) are dropped.
"""

from typing import Any, Literal, NotRequired, TypedDict

type Segment = dict[str, Any]
"""``{"type": <SegmentType>, "data": <the matching *Data>}``."""

SegmentType = Literal[
    "text",
    "mention",
    "mention_all",
    "face",
    "reply",
    "image",
    "record",
    "video",
    "file",
    "forward",
    "market_face",
    "light_app",
    "xml",
    "markdown",
]


class TextData(TypedDict):
    text: str


class MentionData(TypedDict):
    user_id: int
    name: str  # without the leading "@"; "" when the protocol didn't say


class FaceData(TypedDict):
    face_id: str
    is_large: bool


class ReplyData(TypedDict):
    message_id: int


class ImageData(TypedDict):
    ref: str  # refreshes ``url`` (``QQ.media_url``); "" when there is none
    url: str  # may have expired
    summary: str  # QQ's preview text: "[图片]", "[动画表情]", ...
    sticker: bool  # a custom sticker (QQ "表情"), not a photo
    sha1: str  # content hash from the NT file id (lowercase hex); "" when the protocol gave none
    blob: NotRequired[str]  # ours: sha256 of a picture the bot sent itself, in ``transcript_blobs``


class MediaData(TypedDict):
    """``record`` and ``video``."""

    ref: str
    url: str
    duration: int  # seconds; 0 when unknown


class FileData(TypedDict):
    file_id: str
    file_name: str
    file_size: int
    file_hash: NotRequired[str]  # TriSHA1, private files only (Milky needs it for the download link)


class ForwardNodeData(TypedDict):
    user_id: int  # 0 when the protocol didn't say
    name: str
    time: int  # unix seconds
    segments: list[Segment]


class ForwardData(TypedDict):
    forward_id: str  # "" when unknown (a forward we sent that QQ didn't confirm)
    title: str
    preview: list[str]
    summary: str
    nodes: NotRequired[list[ForwardNodeData]]  # ours: what a forward the bot sent contained (images as "[图片]")


class MarketFaceData(TypedDict):
    emoji_package_id: int
    emoji_id: str
    key: str
    summary: str
    url: str


class LightAppData(TypedDict):
    app_name: str
    json_payload: str


class XmlData(TypedDict):
    service_id: int
    xml_payload: str


class MarkdownData(TypedDict):
    content: str


# --- outgoing ---------------------------------------------------------------------------------------------------
# What the bot sends: text, mention, reply and image (``uri`` instead of ``ref``/``url``); forwards go through
# ``QQ.send_forward`` with ``Node``s. Backends translate these, like the incoming ones.


def text(content: str) -> Segment:
    return {"type": "text", "data": {"text": content}}


def mention(user_id: int) -> Segment:
    return {"type": "mention", "data": {"user_id": user_id, "name": ""}}


def reply(message_id: int) -> Segment:
    return {"type": "reply", "data": {"message_id": message_id}}


def image(uri: str) -> Segment:
    """``uri``: ``base64://...``, ``http(s)://...`` or ``file://...``."""
    return {"type": "image", "data": {"uri": uri}}
