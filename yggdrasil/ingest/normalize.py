"""Turn ``qq.segments`` segments into placeholder text (the memory/LLM view of a message).

Non-text content is never expanded at ingest time: images, files, forwards and
cards become short placeholders, and the segments are kept in ``messages.segments``
so tools can fetch the real content on demand.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from yggdrasil.qq import faces

# A link ends before trailing ASCII punctuation ("see https://a.example/x." or "…/x/;"): it belongs to the sentence.
URL_RE = re.compile(r"https?://[^\s\u3000，。！？、）」』\])>\"']+(?<![.,;:!?])")
_RECORD_TYPES = {"record": "语音", "video": "视频"}
# Titles QQ gives every forward of a kind; they say nothing about the content.
_GENERIC_FORWARD_TITLES = {"群聊的聊天记录", "聊天记录"}


@dataclass(slots=True)
class Attachment:
    kind: str  # image | file | forward | record | video | card | mface
    data: dict[str, Any]


@dataclass(slots=True)
class Normalized:
    text: str
    reply_to: int | None = None
    mentions: list[int] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


def _card(raw: str) -> tuple[str, str | None]:
    """Title and URL of a light app card (mini-program, share link, news)."""
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return "卡片", None
    if not isinstance(doc, dict):
        return "卡片", None
    title = doc.get("prompt") or ""
    url = None
    meta = doc.get("meta") or {}
    for item in meta.values() if isinstance(meta, dict) else []:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("desc") or title
        url = item.get("qqdocurl") or item.get("jumpUrl") or item.get("url") or url
        if url:
            break
    return str(title).strip() or "卡片", url


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize(segments: list[dict[str, Any]], mention: Callable[[int], str] | None = None) -> Normalized:
    """``mention`` renders a mention target for a model (``名字(QQ)``); without it the raw ``@<name or qq>`` is
    kept, which is what ingest stores in ``plain_text``."""
    out = Normalized(text="")
    parts: list[str] = []
    rendered_at = False  # previous part is a mention rendered through ``mention``
    # QQ follows a markdown segment or a market face with a plain-text fallback of the same content (the markdown
    # source, the ``[summary]``); that copy is dropped so the content shows once.
    echo: str | None = None
    for seg in segments:
        after_at, rendered_at = rendered_at, False
        fallback, echo = echo, None
        kind = seg.get("type", "")
        data = seg.get("data") or {}
        match kind:
            case "text":
                text = str(data.get("text", ""))
                if text == fallback:
                    continue
                if after_at and text[:1].isspace():
                    text = text[1:]  # the mention already ends with a space (the stored form keeps both)
                parts.append(text)
            case "mention":
                uid = _int(data.get("user_id"))
                if uid is not None:
                    out.mentions.append(uid)
                if uid is not None and mention is not None:
                    parts.append(f"@{mention(uid)} ")
                    rendered_at = True
                    continue
                name = str(data.get("name") or "").lstrip("@") or str(data.get("user_id", ""))
                parts.append(f"@{name} ")
            case "mention_all":
                parts.append("@全体成员")
            case "reply":
                out.reply_to = _int(data.get("message_id"))
            case "face":
                face_id = str(data.get("face_id", ""))
                parts.append(f"[表情:{faces.name(face_id) or f'表情{face_id}'}]")
            case "market_face":
                summary = str(data.get("summary") or "").strip("[]") or "表情包"
                parts.append(f"[表情包:{summary}]")
                out.attachments.append(Attachment("mface", data))
                echo = data.get("summary")
            case "image":
                summary = str(data.get("summary") or "").strip("[]")
                parts.append(f"[表情包:{summary or '动画表情'}]" if data.get("sticker") else "[图片]")
                out.attachments.append(Attachment("image", data))
            case "record" | "video":
                parts.append(f"[{_RECORD_TYPES[kind]}]")
                out.attachments.append(Attachment(kind, data))
            case "file":
                parts.append(f"[文件:{data.get('file_name') or '未命名'}]")
                out.attachments.append(Attachment("file", data))
            case "forward":
                # The bot's own forwards are titled after the answer; QQ's generic titles add nothing.
                title = str(data.get("title") or "").strip()
                parts.append(f"[合并转发:{title}]" if title not in _GENERIC_FORWARD_TITLES and title else "[合并转发]")
                out.attachments.append(Attachment("forward", data))
            case "light_app":
                title, url = _card(str(data.get("json_payload", "")))
                parts.append(f"[卡片:{title}]")
                out.attachments.append(Attachment("card", {"title": title, "url": url}))
                if url:
                    out.urls.append(url)
            case "xml":
                parts.append("[卡片]")
            case "markdown":
                content = str(data.get("content", ""))
                parts.append(f"[markdown]{content}")
                echo = content
            case _:
                parts.append(f"[{kind}]")
    out.text = "".join(parts).strip()
    texts = [str(s.get("data", {}).get("text", "")) for s in segments if s.get("type") == "text"]
    for url in URL_RE.findall(" ".join(texts)):
        if url not in out.urls:
            out.urls.append(url)
    return out
