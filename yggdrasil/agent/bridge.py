"""The tool bridge, bot side: code in a workspace (``sh`` / ``py`` of a chat run, background jobs) calls the bot's
read-only tools and fetches chat attachments by reference over HTTP on the relay's port (:mod:`yggdrasil.relay.bridge`).

Credentials: a chat run gets one token at its first ``sh`` / ``py`` call (``bridge_file``: written as ``bridge.json``
into the step or cell directory, which the in-sandbox helper turns into ``YGG_BRIDGE_URL`` / ``YGG_BRIDGE_TOKEN``)
and loses it when the run ends (``close_run``); a background job's token gets its tool scope when the job starts
(``sandbox.jobs``). The scope (``relay.grants.ToolScope``) names the tools the group allows (``allowed_tools``), the
requester and the call limit, counted in Redis.

Context: a call from a run live in this process runs on that run's ``Deps``: handles it registers land in the run's
``Handles``, its vision sub-calls and ``ygg:<name>`` counts in the run's metadata, and its arguments and result in the
transcript under the ``sh`` / ``py`` call running the code (the latest one started). Without a live run (a job, or a
run on another replica) the ``Deps`` are rebuilt from the grant like a job report's: the group's settings, the
connected QQ client and the handles the job saved from the run that started it (else none), kept per token in this
process; the job's transcript records the call. Results are text only (``Deps.text_only``).
"""

import asyncio
import json
import mimetypes
import secrets
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cache
from typing import Any

import httpx
from pydantic import ValidationError
from pydantic_ai import RunContext, Tool, ToolReturn
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage

from yggdrasil import relay
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.media import MediaTooLarge, file_name, is_image, media_bytes, media_type
from yggdrasil.agent.skills import qq as qq_skill
from yggdrasil.agent.skills import skill_names
from yggdrasil.agent.tools import ALL_TOOLS
from yggdrasil.agent.tools.base import resolve_ref
from yggdrasil.core.log import LOGGER
from yggdrasil.qq import client as qq_client
from yggdrasil.relay.grants import Grant, ToolScope, count_call
from yggdrasil.runtime import snapshot
from yggdrasil.sandbox.job_model import Job
from yggdrasil.sandbox.jobs import runner
from yggdrasil.settings import Settings
from yggdrasil.tools.errors import ToolError
from yggdrasil.transcript.capture import text_part
from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.transcript.schema import BridgeCall, Entry
from yggdrasil.transcript.store import append_job

TOOLS = (
    "web_search",
    "read_url",
    "read_x",
    "search_x",
    "search_history",
    "get_messages_around",
    "recent_chat",
    "lookup_glossary",
    "get_profile",
    "expand_forward",
    "read_file",
    "view_image",
)
"""Chat tools workspace code may call (when the group has them enabled for chat)."""
QQ_TOOLS = ("member_info", "group_info", "group_files")
"""``qq`` skill tools workspace code may call (when the group has the skill)."""
MEDIA = "media"
"""The name a media fetch is counted and recorded under."""
RUN_TTL_S = 3600
"""Lifetime of a chat run's token: longer than any run; it is revoked when the run ends."""


class Refused(Exception):
    """A bridge request the bot turns down: the HTTP status and the message the code gets."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@cache
def _registry() -> dict[str, Tool[Deps]]:
    fns = {name: ALL_TOOLS[name] for name in TOOLS} | {
        "member_info": qq_skill.member_info,
        "group_info": qq_skill.group_info,
        "group_files": qq_skill.group_files,
    }
    return {name: Tool(fn, takes_ctx=True, name=name) for name, fn in fns.items()}


def allowed_tools(settings: Settings, group_id: int | None) -> tuple[str, ...]:
    """The bridge tools of a chat run in this group: its enabled tools among ``TOOLS``, plus ``QQ_TOOLS`` when it has
    the ``qq`` skill. Handler exclusions don't apply: chat leaves ``recent_chat`` out only because its prompt already
    carries the recent chat, which workspace code doesn't see."""
    names = [name for name in TOOLS if name in settings.tools.enabled]
    if qq_skill.ID in skill_names(settings, "chat", group_id):
        names += QQ_TOOLS
    return tuple(names)


def _scope(grant: Grant) -> ToolScope:
    if grant.scope is None:
        raise Refused(403, "这个令牌不能调用 bot 的工具")
    return grant.scope


def tool_specs(grant: Grant) -> list[dict[str, Any]]:
    """The tools ``grant`` may call: name, description, JSON schema of the arguments."""
    registry = _registry()
    return [
        {"name": t.name, "description": t.description or "", "parameters": t.function_schema.json_schema}
        for name in _scope(grant).tools
        if (t := registry.get(name)) is not None
    ]


# --- chat runs ------------------------------------------------------------------------------------------------------


@dataclass(eq=False)
class RunBridge:
    """A chat run's tool bridge: its token (as ``bridge.json``) and the ``sh`` / ``py`` call running code now."""

    subject: str  # the grant's job id
    file: bytes | None = None  # bridge.json, once the token is issued
    ctx: RunContext[Deps] | None = None  # the sh / py call started last; bridge calls are recorded under it
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_LIVE: dict[str, Deps] = {}
"""Grant subject → the Deps of the chat run it belongs to, while that run is live in this process."""


async def bridge_file(ctx: RunContext[Deps]) -> bytes | None:
    """``bridge.json`` for the ``sh`` / ``py`` call ``ctx`` is about to run: the run's token, issued on first use.
    None when the run has no bridge (the relay isn't served, ``tools.bridge_run_limit`` is 0, issuing failed)."""
    deps = ctx.deps
    url = relay.relay_url()
    limit = deps.settings.tools.bridge_run_limit
    if url is None or limit <= 0 or deps.group_id is None:
        return None
    run = deps.bridge
    if run is None:
        run = deps.bridge = RunBridge(f"run-{deps.invocation_id or secrets.token_hex(8)}")
    async with run.lock:  # parallel first calls share one token
        if run.file is None:
            scope = ToolScope(allowed_tools(deps.settings, deps.group_id), deps.user_id, deps.invocation_id, limit)
            try:
                token = await relay.issue_token(
                    group_id=deps.group_id,
                    job_id=run.subject,
                    models=[],
                    max_input_tokens=0,
                    max_output_tokens=0,
                    ttl_s=RUN_TTL_S,
                    scope=scope,
                )
            except Exception:
                LOGGER.exception("issuing the tool bridge token of {} failed", run.subject)
                return None
            run.file = json.dumps({"url": url, "token": token}).encode()
            _LIVE[run.subject] = deps
    run.ctx = ctx
    return run.file


async def close_run(deps: Deps) -> None:
    """The run is over: its bridge token stops working. Never raises."""
    run, deps.bridge = deps.bridge, None
    if run is None or run.file is None:
        return
    _LIVE.pop(run.subject, None)
    try:
        await relay.revoke_token(run.subject)
    except Exception:
        LOGGER.exception("revoking the tool bridge token of {} failed", run.subject)


def summary(ctx: RunContext[Deps]) -> str | None:
    """The line an ``sh`` / ``py`` result ends with when its code called the bot: which tools, how often, how long."""
    calls = ctx.deps.notes.bridge.get(ctx.tool_call_id or "")
    if not calls:
        return None
    used = "、".join(f"{name}×{n}" for name, n in Counter(c.name for c in calls).items())
    seconds = sum(c.duration_ms or 0 for c in calls) / 1000
    errors = sum(c.outcome != "ok" for c in calls)
    failed = f"，其中 {errors} 次出错" if errors else ""
    return f"（代码里调用了 {used}，共 {seconds:.1f} 秒{failed}）"


# --- the context a call runs in ---------------------------------------------------------------------------------------


class _NoModel(Model):
    """The model of a call's context outside any agent run; bridge tools never use ``ctx.model``."""

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        raise RuntimeError("tool bridge calls run outside any agent run")

    @property
    def model_name(self) -> str:
        return "tool-bridge"

    @property
    def system(self) -> str:
        return "ygg"


@dataclass(slots=True)
class _Detached:
    """What calls of one token share without a live run: the handles, and the job they came from."""

    handles: Handles
    job: Job | None
    expires_at: float


_DETACHED: dict[str, _Detached] = {}


@dataclass(slots=True)
class _Target:
    """Where one call runs and is recorded."""

    grant: Grant
    deps: Deps  # the live run's, or rebuilt for the grant
    parent: RunContext[Deps] | None  # the live run's sh / py call running the code
    job: Job | None  # the job whose transcript records the call (no live run)


async def _target(grant: Grant, scope: ToolScope) -> _Target:
    live = _LIVE.get(grant.job_id)
    if live is not None and live.bridge is not None and live.bridge.ctx is not None:
        return _Target(grant, live, live.bridge.ctx, None)
    qq = qq_client.connected()
    if qq is None:
        raise Refused(503, "bot 现在没有连上 QQ，稍后再试")
    now = time.time()
    for key in [k for k, d in _DETACHED.items() if d.expires_at <= now]:
        del _DETACHED[key]
    state = _DETACHED.get(grant.job_id)
    if state is None:
        job = await runner().get(grant.job_id)
        handles = Handles.load(job.handles) if job is not None else Handles()
        state = _DETACHED[grant.job_id] = _Detached(handles, job, grant.expires_at)
    job = state.job
    settings = snapshot().settings(grant.group_id)
    deps = Deps(
        qq,
        grant.group_id,
        scope.user_id,
        settings,
        state.handles,
        "chat",
        self_id=qq.self_id,
        trigger_message_id=job.trigger_message_id if job is not None else None,
        invocation_id=scope.invocation_id,
        request_text=job.request_text if job is not None else "",
        notes=TraceNotes(full=settings.privacy.capture_llm_content),
    )
    return _Target(grant, deps, None, job)


async def _admit(grant: Grant, scope: ToolScope) -> _Target:
    target = await _target(grant, scope)
    if not await count_call(grant):
        raise Refused(429, f"调用次数用完了：这个令牌最多调用 {scope.limit} 次（工具和取文件合计）")
    return target


async def _run[T](
    target: _Target,
    name: str,
    args: dict[str, Any],
    body: Callable[[RunContext[Deps]], Awaitable[tuple[T, str]]],
) -> T:
    """Run one call on its own copy of the target's ``Deps`` (own counters, text only; handles, notes and images
    shared) and record it: what ``body`` returned with its text, or its refusal."""
    call_id = f"ygg-{secrets.token_hex(6)}"
    deps = replace(target.deps, tool_counts=Counter(), tool_seconds=defaultdict(float), text_only=True)
    if target.parent is not None:
        ctx = replace(target.parent, deps=deps, tool_call_id=call_id, tool_name=name)
    else:
        ctx = RunContext(deps=deps, model=_NoModel(), usage=RunUsage(), tool_call_id=call_id, tool_name=name)
    at, started = datetime.now(UTC), time.perf_counter()
    try:
        value, text = await body(ctx)
    except Refused as e:
        await _record(target, call_id, name, args, e.message, True, at, time.perf_counter() - started)
        raise
    await _record(target, call_id, name, args, text, False, at, time.perf_counter() - started)
    return value


async def _record(
    target: _Target,
    call_id: str,
    name: str,
    args: dict[str, Any],
    text: str,
    failed: bool,
    at: datetime,
    elapsed: float,
) -> None:
    deps, grant = target.deps, target.grant
    notes = deps.notes
    failed = failed or call_id in notes.errors
    notes.errors.discard(call_id)
    call = BridgeCall(
        name=name,
        at=at,
        args=args if notes.full else None,
        chars=len(json.dumps(args, ensure_ascii=False, default=str)),
        parts=[text_part(text, notes.full)],
        outcome="error" if failed else "ok",
        duration_ms=int(elapsed * 1000),
        display=notes.displays.pop(call_id, None),
    )
    LOGGER.bind(audit="sandbox", action="bridge_call", group=grant.group_id, job=grant.job_id).info(
        "tool bridge {}: {} {} in {:.1f}s", grant.job_id, name, call.outcome, elapsed
    )
    if target.parent is not None:
        deps.tool_counts[f"ygg:{name}"] += 1
        deps.tool_seconds[f"ygg:{name}"] += elapsed
        notes.bridged(target.parent.tool_call_id or "", call)
    elif target.job is not None:
        entry = Entry(kind="event", at=at, level="info", text=f"代码调用了 {name}", bridge=[call])
        source = "omp" if target.job.kind == "agent" else "command"
        await append_job(target.job.id, [entry], notes.blobs, full=notes.full, source=source)


# --- calls ------------------------------------------------------------------------------------------------------------


def _text(result: object) -> str:
    """A tool result as code gets it: text only (a ``ToolReturn``'s text part)."""
    if isinstance(result, ToolReturn):
        result = result.return_value
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


async def call_tool(grant: Grant, name: str, args: object) -> str:
    """Call tool ``name`` with ``args`` (the JSON object the code sent) for ``grant``; the result text. Tool failures
    are text, as the model sees them; refusals raise ``Refused``."""
    scope = _scope(grant)
    tool = _registry().get(name)
    if tool is None:
        raise Refused(404, f"没有这个工具：{name}")
    if name not in scope.tools:
        raise Refused(403, f"这个令牌不能调用 {name}：本群没有开放它")
    if not isinstance(args, dict):
        raise Refused(422, "参数要是一个 JSON 对象")
    try:
        valid = tool.function_schema.validator.validate_python(args)
    except ValidationError as e:
        raise Refused(422, RetryPromptPart.from_error(e, tool_name=name).model_response()) from e
    target = await _admit(grant, scope)

    async def body(ctx: RunContext[Deps]) -> tuple[str, str]:
        text = _text(await tool.function_schema.call(valid, ctx))
        return text, text

    return await _run(target, name, args, body)


@dataclass(frozen=True, slots=True)
class Media:
    data: bytes
    name: str  # a plain file name
    content_type: str


async def fetch_media(grant: Grant, ref: object) -> Media:
    """The bytes of an image or file ``ref`` (``img#N``, ``file#N``, ``#<message id>[/k]``) for ``grant``."""
    scope = _scope(grant)
    if not isinstance(ref, str) or not ref.strip():
        raise Refused(422, "ref 要是 img#N、file#N 或 #消息编号 这样的引用")
    ref = ref.strip()
    target = await _admit(grant, scope)

    async def body(ctx: RunContext[Deps]) -> tuple[Media, str]:
        media = await _media(ctx, ref)
        return media, f"{media.name}（{media.content_type}，{len(media.data)} 字节）"

    return await _run(target, MEDIA, {"ref": ref}, body)


async def _media(ctx: RunContext[Deps], ref: str) -> Media:
    deps = ctx.deps
    known = deps.handles.get(ref)
    if known is not None and known.kind not in ("image", "file"):
        raise Refused(400, f"{ref} 不是图片或文件，没有可以下载的内容")
    try:
        h = await resolve_ref(ctx, ref, "image", "file")
    except ToolError as e:
        raise Refused(404, str(e)) from e
    max_mb = deps.settings.tools.file_max_mb
    try:
        blob, _ = await media_bytes(
            deps.qq, deps.group_id, deps.user_id, h, max_mb * 1024 * 1024, made=deps.made_images
        )
    except MediaTooLarge as e:
        raise Refused(413, str(e)) from e
    except ToolError as e:
        raise Refused(404, str(e)) from e
    except httpx.HTTPError as e:
        raise Refused(502, f"下载失败：{type(e).__name__}") from e
    if len(blob) > max_mb * 1024 * 1024:
        raise Refused(413, f"文件太大（超过 {max_mb}MB）")
    name = file_name(ref, h, blob)
    kind = media_type(blob) if is_image(blob) else mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Media(blob, name, kind)
