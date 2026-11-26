"""The normalized conversation format, shared by pydantic-ai runs and omp jobs."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

VERSION = 2


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str | None = None
    chars: int = 0


class ThinkingPart(BaseModel):
    type: Literal["thinking"] = "thinking"
    text: str | None = None
    chars: int = 0
    redacted: bool = False


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    blob: str | None = None
    url: str | None = None
    mime: str = ""
    bytes: int = 0


class ToolCallPart(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    args: dict[str, Any] | None = None
    chars: int = 0


class OutputPart(BaseModel):
    type: Literal["output"] = "output"
    value: dict[str, Any]
    chars: int = 0


Part = Annotated[TextPart | ThinkingPart | ImagePart | ToolCallPart | OutputPart, Field(discriminator="type")]


class Usage(BaseModel):
    input: int = 0
    output: int = 0
    cached: int = 0


class ChatMessage(BaseModel):
    """A KOOK message snapshot as seen by a run."""

    id: int | None = None
    kook_message_id: str | None = None
    channel_id: str | None = None
    user_id: str
    sender_name: str = ""
    sent_at: datetime | None = None
    segments: list[dict[str, Any]] = []
    is_self: bool = False
    recalled: bool = False


class ChatContextDisplay(BaseModel):
    kind: Literal["chat_context"] = "chat_context"
    recent: list[int] = []
    quoted: ChatMessage | None = None
    trigger: ChatMessage | None = None
    blocks: list[str] = []
    chain: list[ChatMessage] = []


class ChatMessagesDisplay(BaseModel):
    kind: Literal["chat_messages"] = "chat_messages"
    message_ids: list[int] = []


class PageDisplay(BaseModel):
    kind: Literal["page"] = "page"
    url: str
    final_url: str = ""
    title: str = ""
    via: str = ""
    page: int = 1
    pages: int = 1


class Hit(BaseModel):
    title: str = ""
    url: str = ""
    snippet: str = ""


class HitsDisplay(BaseModel):
    kind: Literal["hits"] = "hits"
    query: str = ""
    hits: list[Hit] = []


class ShownImage(BaseModel):
    blob: str
    caption: str = ""


class ImagesDisplay(BaseModel):
    kind: Literal["images"] = "images"
    images: list[ShownImage] = []


Display = Annotated[
    ChatContextDisplay | ChatMessagesDisplay | PageDisplay | HitsDisplay | ImagesDisplay,
    Field(discriminator="kind"),
]

Outcome = Literal["ok", "error", "retry", "cancelled"]


class BridgeCall(BaseModel):
    name: str
    at: datetime | None = None
    args: dict[str, Any] | None = None
    chars: int = 0
    parts: list[Part] = []
    outcome: Outcome = "ok"
    duration_ms: int | None = None
    display: Display | None = None


class Entry(BaseModel):
    kind: Literal["system", "user", "assistant", "tool", "event"]
    at: datetime | None = None
    parts: list[Part] = []
    display: Display | None = None
    model: str | None = None
    duration_ms: int | None = None
    usage: Usage | None = None
    finish: str | None = None
    error: str | None = None
    call_id: str | None = None
    name: str | None = None
    outcome: Outcome | None = None
    bridge: list[BridgeCall] | None = None
    text: str | None = None
    level: Literal["info", "warn", "error"] | None = None
