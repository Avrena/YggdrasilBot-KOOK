"""Shared helpers for agent tools: ``chat_tool`` decorator, accounting, ref resolution, vision, name lookup."""

import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any

from pydantic_ai import Agent, RunContext, ToolReturn

from yggdrasil.agent.deps import Deps, Handle
from yggdrasil.agent.media import ModelImage
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import TOOL_CALLS, TOOL_DURATION
from yggdrasil.ingest.normalize import normalize
from yggdrasil.llm.registry import TaskNotConfigured, chat_model
from yggdrasil.llm.streaming import drain_events
from yggdrasil.people import People, resolve
from yggdrasil.prompts import render
from yggdrasil.tools.errors import ToolError

type ToolResult = str | ToolReturn


@asynccontextmanager
async def accounting(deps: Deps, name: str) -> AsyncIterator[None]:
    deps.tool_counts[name] += 1
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        elapsed = time.perf_counter() - started
        deps.tool_seconds[name] += elapsed
        TOOL_CALLS.add(1, {"tool": name, "outcome": outcome})
        TOOL_DURATION.record(elapsed, {"tool": name})


def chat_tool[**P](name: str) -> Callable[[Callable[P, Awaitable[ToolResult]]], Callable[P, Awaitable[ToolResult]]]:
    def deco(fn: Callable[P, Awaitable[ToolResult]]) -> Callable[P, Awaitable[ToolResult]]:
        @wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
            ctx = args[0]
            assert isinstance(ctx, RunContext)
            try:
                async with accounting(ctx.deps, name):
                    return await fn(*args, **kwargs)
            except ToolError as e:
                ctx.deps.notes.failed(ctx.tool_call_id)
                return f"[工具错误] {e}"
            except TaskNotConfigured as e:
                ctx.deps.notes.failed(ctx.tool_call_id)
                return f"[工具不可用] {e}"
            except Exception as e:
                ctx.deps.notes.failed(ctx.tool_call_id)
                LOGGER.opt(exception=e).warning("tool {} failed", name)
                return f"[工具错误] {type(e).__name__}: {str(e)[:200]}"

        return wrapper

    return deco


_KIND_LABELS = {"image": "图片", "file": "文件", "card": "卡片", "url": "链接"}
_MESSAGE_REF = re.compile(r"#(\d+)(?:/(\d+))?")


async def resolve_ref(ctx: RunContext[Deps], ref: str, *kinds: str) -> Handle:
    """Resolve a handle (``img#1``, ``file#1``, ...) or ``#<message id>[/<k>]`` from accessible guild history."""
    from yggdrasil.memory.search import accessible_channels, message_segments

    ref = ref.strip()
    wanted = "或".join(_KIND_LABELS[k] for k in kinds)
    if (h := ctx.deps.handles.get(ref)) is not None:
        if h.kind not in kinds:
            raise ToolError(f"{ref} 是{_KIND_LABELS.get(h.kind, h.kind)}，不是{wanted}")
        return h
    m = _MESSAGE_REF.fullmatch(ref)
    if m is None:
        known = ", ".join(k for k, v in ctx.deps.handles.items.items() if v.kind in kinds) or "无"
        raise ToolError(
            f"找不到 {ref}。这次能用的{wanted}句柄：{known}；聊天记录里的内容用消息编号 #N（第 k 个用 #N/k）"
        )
    if not ctx.deps.guild_id:
        raise ToolError("私聊里没有频道记录，只能用句柄")
    msg_id, k = int(m.group(1)), int(m.group(2) or 1)
    allowed = accessible_channels(ctx.deps.guild_id, ctx.deps.channel_id, role_ids=ctx.deps.role_ids)
    segments = (await message_segments(ctx.deps.guild_id, [msg_id], allowed_channels=allowed)).get(msg_id)
    if segments is None:
        raise ToolError(f"本服务器可见聊天记录里没有消息 #{msg_id}（或者已撤回）")
    norm = normalize(segments)
    found = [(a.kind, a.data) for a in norm.attachments if a.kind in kinds]
    if "url" in kinds:
        found += [("url", {"url": url}) for url in norm.urls]
    if not found:
        raise ToolError(f"消息 #{msg_id} 里没有{wanted}")
    if not 1 <= k <= len(found):
        raise ToolError(f"消息 #{msg_id} 里只有 {len(found)} 个{wanted}")
    kind, data = found[k - 1]
    key = ctx.deps.handles.add(kind, data)
    assert key is not None
    return ctx.deps.handles.items[key]


async def vision(ctx: RunContext[Deps], images: list[ModelImage], question: str) -> str:
    resolved = chat_model("vision")
    agent = Agent(resolved.model, instructions=render("vision_system"), name="vision")
    content: list[Any] = [question or "描述这张图片，并完整转写其中的文字。"]
    for img in images:
        content += [img.note, img.content] if img.note else [img.content]
    result = await agent.run(content, model_settings=resolved.settings, event_stream_handler=drain_events)
    usage = result.usage
    ctx.deps.vision_tokens.append((resolved.label, usage.input_tokens or 0, usage.output_tokens or 0))
    return result.output


async def people_for(ctx: RunContext[Deps], user_ids: Iterable[str] = (), texts: Iterable[str] = ()) -> People:
    return await resolve(
        ctx.deps.guild_id,
        user_ids,
        bot_name=ctx.deps.settings.persona.name,
        self_id=ctx.deps.self_id or None,
        texts=texts,
    )
