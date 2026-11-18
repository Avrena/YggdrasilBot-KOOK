"""Replies as merged forwards: Markdown stripping and splitting, the nodes (sections, pictures, sources, metadata),
the send, and recovering a forward QQ didn't confirm from the group's history; when it failed, the answer goes out
once as plain text."""

import asyncio
import base64
import re
import time
from datetime import UTC, datetime
from typing import Literal

from yggdrasil.agent.schema import BotReply
from yggdrasil.core.log import LOGGER
from yggdrasil.delivery.send import SEND_IMAGE_TIMEOUT_S, MessageDropped, notify_dropped, record_self, send_text
from yggdrasil.qq import segments as seg
from yggdrasil.qq.client import QQ
from yggdrasil.qq.model import Dropped, Node, QQError, Unconfirmed
from yggdrasil.qq.segments import ForwardNodeData, Segment
from yggdrasil.settings import InteractionSettings

# Code is kept verbatim (only its fences and backticks go): emphasis rules would eat ``__init__`` or ``*args``.
_MD_CODE_BLOCK = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)(?:```|\Z)", re.DOTALL)
_MD_CODE = re.compile(r"`([^`\n]+)`")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
# ``__x__`` is left alone: models bold with ``**``, and in prose ``__`` is a Python dunder far more often.
_MD_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
_MD_ITALIC = re.compile(r"(?<![*\w])\*(?![\s*])(.+?)(?<![\s*])\*(?!\*)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_MD_BULLET = re.compile(r"^(\s*)[*+]\s+", re.MULTILINE)
_CODE_SLOT = re.compile("\x00(\\d+)\x00")
# First line of the metadata node; expand_forward also uses it to skip that node in forwards stored before it was
# left out of our records.
META_TITLE = "⚙ 元数据"


def strip_markdown(text: str) -> str:
    code: list[str] = []

    def stash(m: re.Match[str]) -> str:
        code.append(m.group(1))
        return f"\x00{len(code) - 1}\x00"

    text = _MD_CODE_BLOCK.sub(stash, text.replace("\x00", ""))
    text = _MD_CODE.sub(stash, text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_ITALIC.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1 (\2)", text)
    text = _MD_BULLET.sub(r"\1- ", text)
    return _CODE_SLOT.sub(lambda m: code[int(m.group(1))], text).strip()


def split_text(text: str, limit: int) -> list[str]:
    """Split on paragraph, then line boundaries, hard-cutting only if a single line is too long."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in re.split(r"(\n\s*\n)", text):
        if len(current) + len(para) <= limit:
            current += para
            continue
        if current.strip():
            chunks.append(current.strip())
        current = ""
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            chunks.append(para[:cut].strip())
            para = para[cut:]
        current = para
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]


def build_nodes(
    reply: BotReply,
    meta_text: str | None,
    cfg: InteractionSettings,
    *,
    self_id: int,
    nickname: str,
    images: list[tuple[bytes, str]] | None = None,
) -> list[Node]:
    bodies: list[str] = []
    sections = list(reply.sections)
    if len(sections) > cfg.max_sections:
        # Hard cap behind the prompt's "at most two sections": fold the rest into the last kept one.
        keep, extra = sections[: cfg.max_sections - 1], sections[cfg.max_sections - 1 :]
        merged = "\n\n".join(s.content for s in extra)
        sections = [*keep, extra[0].model_copy(update={"content": merged})]
    for section in sections:
        bodies.extend(split_text(strip_markdown(section.content), cfg.node_max_chars))
    if reply.sources:
        bodies.append(_sources_text(reply))
    limit = cfg.forward_max_nodes - (1 if meta_text else 0)
    if len(bodies) > limit:
        bodies = [*bodies[: limit - 1], "\n\n".join(bodies[limit - 1 :])]

    nodes = [Node(self_id, nickname, [seg.text(b)]) for b in bodies]
    for png, caption in images or []:
        segs = [seg.image("base64://" + base64.b64encode(png).decode())]
        if caption:
            segs.insert(0, seg.text(caption + "\n"))
        # Keep sources last among text nodes but before metadata: insert images after the sections.
        nodes.insert(len(nodes) - (1 if reply.sources else 0), Node(self_id, nickname, segs))
    if meta_text:
        nodes.append(Node(self_id, nickname, [seg.text(meta_text)]))
    return nodes


def _sources_text(reply: BotReply) -> str:
    return "来源\n" + "\n".join(f"- {s.label}" + (f"\n  {s.url}" if s.url else "") for s in reply.sources[:10])


_CUT = "\n……（后文过长，已省略）"


def reply_text(reply: BotReply, cfg: InteractionSettings) -> str:
    """The answer as one plain message, for when the forward can't be sent: cut to ``node_max_chars``."""
    parts = [reply.title or "", *(strip_markdown(s.content) for s in reply.sections)]
    if reply.sources:
        parts.append(_sources_text(reply))
    chunks = split_text("\n\n".join(p for p in parts if p.strip()), cfg.node_max_chars - len(_CUT))
    return chunks[0] + _CUT if len(chunks) > 1 else chunks[0]


# Forwards carry rendered pictures too (render_markdown, render_mermaid), which QQ uploads before it confirms.
SEND_FORWARD_TIMEOUT_S = SEND_IMAGE_TIMEOUT_S
# An unconfirmed forward is looked for in the group's latest history: checks after these pauses (QQ may still be
# posting it), on a page this long, allowing for this much clock difference between us and QQ.
FORWARD_CHECK_DELAYS_S = (0.0, 5.0, 10.0)
_HISTORY_PAGE = 30
_CLOCK_SKEW_S = 5


def _forward_id(segments: list[Segment]) -> str:
    """Id of the forward in a message, or ""."""
    for s in segments:
        if s.get("type") == "forward" and (forward_id := (s.get("data") or {}).get("forward_id")):
            return str(forward_id)
    return ""


def _text_key(segments: list[Segment]) -> str:
    text = "".join(str((s.get("data") or {}).get("text", "")) for s in segments if s.get("type") == "text")
    return "".join(text.split())[:60]


async def _is_ours(qq: QQ, forward_id: str, first: str) -> bool | None:
    """Whether the forward ``forward_id`` opens with our first node (another run may have posted one meanwhile).
    None: can't tell."""
    try:
        nodes = await qq.forward(forward_id)
    except Exception as e:
        LOGGER.warning("fetching forward {} after an unconfirmed send: {}", forward_id, e)
        return None
    theirs = _text_key(nodes[0].segments) if nodes else ""
    if not theirs or not first:
        return None
    return theirs == first


async def _find_own_forward(
    qq: QQ, group_id: int, self_id: int, since: float, nodes: list[Node]
) -> tuple[int, str] | Literal["absent", "unknown"]:
    """(QQ id, forward id) of our forward in the group posted at ``since`` or later. "absent" only when the history
    reaches back to the send and no bot forward on it could be ours; else "unknown"."""
    first = _text_key(nodes[0].segments) if nodes else ""
    checked: dict[str, bool | None] = {}
    covered = False
    for delay in FORWARD_CHECK_DELAYS_S:
        await asyncio.sleep(delay)
        try:
            page = await qq.history(group_id, limit=_HISTORY_PAGE)
        except Exception as e:
            LOGGER.warning("history of {} after an unconfirmed forward: {}", group_id, e)
            covered = False
            continue
        for m in page:
            if m.sender.user_id != self_id or m.time < since - _CLOCK_SKEW_S:
                continue
            forward_id = _forward_id(m.segments)
            if not forward_id or forward_id in checked:
                continue
            checked[forward_id] = mine = await _is_ours(qq, forward_id, first)
            if mine:
                return m.message_id, forward_id
        covered = bool(page) and min(m.time for m in page) <= since - _CLOCK_SKEW_S
    return "absent" if covered and None not in checked.values() else "unknown"


async def _sent_forward_id(qq: QQ, group_id: int, user_id: int, message_id: int) -> str:
    """The id of a forward we just sent, read back from the message (Milky's send returns none); "" when QQ won't
    say. Only our record needs it: the record keeps the nodes, so the forward reads without it."""
    try:
        sent = await qq.message(group_id=group_id, user_id=user_id, message_id=message_id)
    except Exception as e:
        LOGGER.warning("fetching sent forward {} for its id failed: {}", message_id, e)
        return ""
    return _forward_id(sent.segments)


def _record_forward(
    group_id: int,
    self_id: int,
    nickname: str,
    message_id: int,
    forward_id: str,
    title: str,
    nodes: list[Node],
    meta_text: str | None,
) -> None:
    # Keep the node texts with our own record, so a later "reply to the bot's forward" reads what was in it without
    # asking QQ (which may not hand self-sent forwards back).
    data = {
        "forward_id": forward_id,
        "title": title,
        "preview": [],
        "summary": "",
        "nodes": _stored_nodes(nodes, meta_text),
    }
    record_self(group_id, self_id, nickname, message_id, [{"type": "forward", "data": data}])


async def send_forward(
    qq: QQ,
    *,
    group_id: int | None,
    user_id: int,
    reply: BotReply,
    meta_text: str | None,
    cfg: InteractionSettings,
    self_id: int,
    nickname: str,
    images: list[tuple[bytes, str]] | None = None,
) -> int | None:
    """The reply as a merged forward; returns the QQ id of what was sent.

    A group forward QQ didn't confirm (``Unconfirmed``: no answer within ``SEND_FORWARD_TIMEOUT_S``, or a "maybe
    delivered" failure) is looked for in the group's history; found, and opening with our first node, it counts as
    sent. Missing from history that reaches back to the send (and carrying no pictures that may still be uploading),
    or rejected by QQ outright, it failed and the answer goes out once as plain text instead. None: most likely sent
    (an unconfirmed private forward, or history that can't tell), so it must not be sent again; like ``send_image``.
    """
    nodes = build_nodes(reply, meta_text, cfg, self_id=self_id, nickname=nickname, images=images)
    title = (reply.title or "回复")[:30]  # only for our own message record; never sent as a preview
    target = group_id or user_id
    started = time.time()
    try:
        sent = await qq.send_forward(
            group_id=group_id, user_id=user_id, nodes=nodes, reply_timeout=SEND_FORWARD_TIMEOUT_S
        )
    except QQError as e:
        if group_id is not None and isinstance(e, Dropped):
            # Not "maybe delivered": QQ answered and posted nothing. The text copy would carry the same content.
            LOGGER.warning("forward to {} was dropped by QQ: {}", target, e)
            await notify_dropped(qq, group_id, self_id, nickname)
            raise MessageDropped(str(e)) from e
        if not isinstance(e, Unconfirmed):
            LOGGER.warning("forward to {} failed, sending the answer as text: {}", target, e)
        elif group_id is None:
            LOGGER.warning("forward to {} was not confirmed ({}); counting it as sent", target, e)
            return None
        else:
            match await _find_own_forward(qq, group_id, self_id, started, nodes):
                case (message_id, forward_id):
                    LOGGER.info(
                        "forward to {} was not confirmed ({}) but is in the history as {}", target, e, message_id
                    )
                    _record_forward(group_id, self_id, nickname, message_id, forward_id, title, nodes, meta_text)
                    return message_id
                case "unknown":
                    LOGGER.warning(
                        "forward to {} was not confirmed ({}) and the history can't tell; counting it as sent",
                        target,
                        e,
                    )
                    return None
                case "absent" if images:
                    # Its pictures may still be uploading (QQ has no overall limit on that): a text copy now could be
                    # followed by the forward.
                    LOGGER.warning(
                        "forward to {} with pictures was not confirmed ({}) and isn't in the history yet; "
                        "counting it as sent",
                        target,
                        e,
                    )
                    return None
                case "absent":
                    LOGGER.warning(
                        "forward to {} was not confirmed ({}) and isn't in the history, sending the answer as text",
                        target,
                        e,
                    )
    else:
        if group_id is None:
            return sent.message_id
        forward_id = sent.forward_id or await _sent_forward_id(qq, group_id, user_id, sent.message_id)
        _record_forward(group_id, self_id, nickname, sent.message_id, forward_id, title, nodes, meta_text)
        return sent.message_id
    return await send_text(
        qq,
        group_id=group_id,
        user_id=user_id,
        reply_to=None,
        text=reply_text(reply, cfg),
        self_id=self_id,
        nickname=nickname,
    )


def _stored_nodes(nodes: list[Node], meta_text: str | None) -> list[ForwardNodeData]:
    """What our message record keeps of a sent forward: its nodes stamped with the send time, images as "[图片]"
    (no base64 PNGs in the message log), and no metadata node (it describes the run, not the answer, so a later
    expand_forward must not hand it to the model as content)."""
    sent = int(datetime.now(UTC).timestamp())
    meta = [seg.text(meta_text)] if meta_text else None
    return [
        {
            "user_id": n.user_id,
            "name": n.name,
            "time": sent,
            "segments": [seg.text("[图片]") if s["type"] == "image" else s for s in n.segments],
        }
        for n in nodes
        if n.segments != meta
    ]
