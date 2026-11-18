"""Tools that read what was sent in QQ: ``read_file``, ``view_image``, ``expand_forward``."""

import asyncio
from datetime import datetime
from typing import Any

from pydantic_ai import BinaryContent, RunContext, ToolReturn

from yggdrasil.agent.deps import Deps, Handle
from yggdrasil.agent.media import ModelImage, is_image, load_image, media_bytes, media_type, model_image
from yggdrasil.agent.tools.base import ToolResult, chat_tool, people_for, resolve_ref, vision
from yggdrasil.delivery.forward import META_TITLE
from yggdrasil.ingest.normalize import normalize
from yggdrasil.llm.registry import TaskNotConfigured, sees_images
from yggdrasil.people import clean_name
from yggdrasil.qq.model import ForwardNode
from yggdrasil.qq.segments import Segment
from yggdrasil.tools.errors import ToolError
from yggdrasil.transcript.schema import ForwardNode as ShownNode
from yggdrasil.transcript.schema import QQForwardDisplay


@chat_tool("read_file")
async def read_file(ctx: RunContext[Deps], handle: str) -> ToolResult:
    """读取文件内容（PDF/Office/文本/代码等），参数是 file#N 句柄，或聊天记录里的消息编号 #N（那条消息里的文件）。
    扫描版 PDF：能看图的模型直接收到页面图片。图片文件按看图处理。"""
    from yggdrasil.tools.docs import parse_document

    h = await resolve_ref(ctx, handle, "file")
    deps, cfg = ctx.deps, ctx.deps.settings.tools
    blob, name = await media_bytes(deps.qq, deps.group_id, deps.user_id, h, cfg.file_max_mb * 1024 * 1024)
    if is_image(blob):  # a picture sent as a group file
        return await _show_image(ctx, handle, h, await asyncio.to_thread(model_image, blob), "")
    doc = await asyncio.to_thread(parse_document, blob, name, max_pages=cfg.file_max_pages)
    note = "（内容已截断）" if doc.truncated else ""
    header = f"文件：{name}（{doc.kind}，{doc.pages} 页）{note}"
    if doc.scanned_pages and _hands_images(deps):
        pages: list[Any] = []
        for i, page in enumerate(doc.scanned_pages, 1):
            pages += [f"第 {i} 页：", BinaryContent(data=page, media_type=media_type(page))]
        return ToolReturn(
            return_value=f"{header}\n\n扫描版，没有文字层；前 {len(doc.scanned_pages)} 页的图片附在下一条消息里，"
            "直接看图。",
            content=pages,
        )
    text = doc.text
    if doc.scanned_pages:
        pages = [ModelImage(page) for page in doc.scanned_pages]
        text = await vision(ctx, pages, "这是扫描版文档的页面，请按顺序完整转写文字，保留结构。")
    return f"{header}\n\n{text}"


@chat_tool("view_image")
async def view_image(ctx: RunContext[Deps], handle: str, question: str = "") -> ToolResult:
    """查看图片，可附带具体问题。handle 是 img#N 句柄、图片格式的 file#N（当群文件发的图），或聊天记录里的消息编号
    #N（那条消息里的图或文件；有多个时用 #N/2 取第 2 个），所以群里之前发过的图也能看。能看图的模型会直接收到原图，
    否则返回文字描述和转写。"""
    h = await resolve_ref(ctx, handle, "image", "file")
    deps = ctx.deps
    img = await load_image(deps.qq, deps.group_id, deps.user_id, h, made=deps.made_images)
    return await _show_image(ctx, handle, h, img, question)


def _hands_images(deps: Deps) -> bool:
    """Whether images go to the model as images: never for a text-only caller (the tool bridge)."""
    return not deps.text_only and sees_images(deps.task)


async def _show_image(ctx: RunContext[Deps], ref: str, h: Handle, img: ModelImage, question: str) -> ToolResult:
    ctx.deps.notes.image(img.data, img.content.media_type, segment=h.data)
    if _hands_images(ctx.deps):
        return ToolReturn(
            return_value=f"{ref} 的原图附在下一条消息里，直接看图。",
            content=[f"{ref}{img.note}：", img.content],
        )
    try:
        return "（以下是另一个模型写的图片描述）\n" + await vision(ctx, [img], question)
    except TaskNotConfigured as e:
        raise ToolError("没有配置能看图的模型，看不了这张图") from e


# expand_forward: nested forwards are opened this deep, and the listing stops growing after this many nodes.
_FORWARD_MAX_DEPTH = 3
_FORWARD_MAX_NODES = 400


@chat_tool("expand_forward")
async def expand_forward(ctx: RunContext[Deps], handle: str) -> str:
    """展开合并转发（聊天记录），参数是 fwd#N 句柄，或聊天记录里的消息编号 #N（那条合并转发）。嵌套的转发也会展开。"""
    h = await resolve_ref(ctx, handle, "forward")
    if not h.data.get("nodes") and not h.data.get("forward_id"):
        raise ToolError(f"{handle} 打不开：QQ 没有给出这条合并转发的 id")
    # (indent, time, sender id, sender name, segments, handle keys); rendered after one batched name lookup
    nodes: list[tuple[str, str, int, str, list[Segment], list[str]]] = []
    shown: list[ShownNode] = []  # the same nodes for the console

    async def walk(data: dict[str, Any], depth: int) -> None:
        if depth > _FORWARD_MAX_DEPTH or len(nodes) > _FORWARD_MAX_NODES:
            return
        stored = data.get("nodes")  # our own forwards keep their content with the record
        if isinstance(stored, list):
            records = [_stored_node(n) for n in stored]
        elif forward_id := str(data.get("forward_id") or ""):
            records = await ctx.deps.qq.forward(forward_id)
        else:
            return
        for rec in records:
            norm = normalize(rec.segments)
            if norm.text.startswith(META_TITLE + "\n"):  # run metadata, kept in our older forward records
                continue
            sent_at = datetime.fromtimestamp(rec.time).astimezone() if rec.time else None
            keys: list[str] = []
            ts = sent_at.strftime("%m-%d %H:%M") if sent_at else ""
            nodes.append(("  " * depth, ts, rec.user_id, rec.name, rec.segments, keys))
            shown.append(
                ShownNode(
                    depth=depth, user_id=rec.user_id, sender_name=rec.name, sent_at=sent_at, segments=rec.segments
                )
            )
            keys += [k for att in norm.attachments if (k := ctx.deps.handles.add(att.kind, att.data))]
            for att in norm.attachments:
                if att.kind == "forward":
                    await walk(att.data, depth + 1)

    await walk(h.data, 0)
    ctx.deps.notes.show(ctx.tool_call_id, QQForwardDisplay(nodes=shown))
    people = await people_for(ctx, [n[2] for n in nodes] + [u for n in nodes for u in normalize(n[4]).mentions])
    lines = []
    for indent, ts, uid, name, segs, keys in nodes:
        who = people.who(uid, name) if uid else clean_name(name) or "?"
        text = normalize(segs, people.mention).text
        when = f"[{ts}] " if ts else ""
        lines.append(f"{indent}{when}{who}: {text}" + (f" ({', '.join(keys)})" if keys else ""))
    return "\n".join(lines) or "（空的合并转发）"


def _stored_node(node: dict[str, Any]) -> ForwardNode:
    """A node of the ``nodes`` our own forward records keep (``qq.segments.ForwardNodeData``)."""
    return ForwardNode(
        user_id=int(node.get("user_id") or 0),
        name=str(node.get("name") or ""),
        time=int(node.get("time") or 0),
        segments=list(node.get("segments") or []),
    )
