"""What the agent of a chat or tldr run is told: instructions and user prompt with inline images."""

from collections.abc import Collection
from datetime import datetime

from pydantic_ai.messages import UserContent

from yggdrasil.agent.context import BuiltContext
from yggdrasil.agent.media import load_images, looks_like_image
from yggdrasil.config import APP_CONFIG
from yggdrasil.core.telemetry import TRACER
from yggdrasil.llm.registry import sees_images
from yggdrasil.people import self_label
from yggdrasil.prompts import render
from yggdrasil.relay import relay_url
from yggdrasil.runtime import snapshot
from yggdrasil.settings import Settings, ToolSettings
from yggdrasil.transcript.notes import TraceNotes


async def inline_images(ctx: BuiltContext, handler: str, limit: int, notes: TraceNotes) -> str | list[UserContent]:
    focus = (ctx.handles.get(key) for key in ctx.focus)
    handles = [h for h in focus if h is not None and looks_like_image(h)][:limit]
    if not handles or not sees_images(handler):
        return ctx.prompt
    with TRACER.start_as_current_span("ygg.inline_images") as span:
        loaded = await load_images(handles)
        span.set_attribute("ygg.images", len(loaded))
    if not loaded:
        return ctx.prompt
    keys = "、".join(key for key, _ in loaded)
    prompt: list[UserContent] = [ctx.prompt, f"下面直接附上了 {keys} 的原图：直接看图，不用再调用 view_image。"]
    for key, img in loaded:
        handle = ctx.handles.get(key)
        notes.image(img.data, img.content.media_type, segment=handle.data if handle else None)
        prompt += [f"{key}{img.note}：", img.content]
    return prompt


def instructions(
    handler: str,
    settings: Settings,
    channel_id: str | None,
    *,
    guild_id: str | None = None,
    tools: list[str],
    sandbox: ToolSettings | None,
    report: bool,
    nonce: str,
    role_ids: Collection[str] = (),
) -> str:
    from yggdrasil.memory.search import accessible_channels

    snap = snapshot()
    channel_name = snap.channel_name(channel_id) if channel_id else ""
    guild_id = guild_id or (snap.guild_of(channel_id) if channel_id else None)
    guild_name = snap.guild_name(guild_id) if guild_id else ""
    sibling_channels: list[str] = []
    if guild_id:
        allowed = set(accessible_channels(guild_id, channel_id, role_ids=role_ids))
        sibling_channels = [
            f"#{c.name}"
            for c in snap.guild_channels(guild_id, enabled_only=True)
            if c.channel_id != channel_id and c.name and c.channel_id in allowed
        ]
    return render(
        f"{handler}_system",
        persona=settings.persona,
        channel_name=f"#{channel_name}" if channel_name else "本频道",
        guild_name=guild_name or "本服务器",
        sibling_channels=sibling_channels,
        now=now_text(),
        tools=tools,
        sandbox=sandbox,
        sandbox_links=bool(APP_CONFIG.sandbox_link_domain),
        sandbox_bridge=sandbox is not None and sandbox.bridge_run_limit > 0 and relay_url() is not None,
        report=report,
        silent_reaction_auto=(
            settings.features.status_reactions and settings.interaction.silent_reaction_mode == "auto"
        ),
        style=render("style_rules"),
        data_rules=render(
            "data_rules",
            nonce=nonce,
            me=self_label(settings.persona.name),
            sandbox=sandbox is not None,
        ),
    )


def now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %A")
