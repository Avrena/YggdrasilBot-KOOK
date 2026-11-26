"""The relay's HTTP side: Anthropic ``POST /v1/messages``, OpenAI ``POST /v1/chat/completions`` (SSE or JSON),
``GET /v1/models``, plus the tool bridge (``/v1/tools``, ``/v1/media``: :mod:`yggdrasil.relay.bridge`).

A request is checked against its token's grant (expiry, model allowlist, budget) and reserves its
``max_tokens`` / ``max_completion_tokens`` of the output budget (clamped to what is left, so parallel requests can't
overrun it). It is then forwarded with the provider's real key to the agent route's upstream for that model (falling
back to the route's same-format fallback on transport errors, 408/429 and 5xx before any byte reached the client).
Responses stream through unchanged; usage is read from SSE events (Anthropic ``message_start``/``message_delta`` or
OpenAI ``usage`` chunk, with ``stream_options.include_usage`` added for OpenAI streams) or the JSON body and added to
the job's usage and the ``ygg.llm.*`` metrics. A stream that ends without reported usage is charged an output estimate
from the text it delivered. Served by its own uvicorn server on ``YGG_RELAY_PORT`` so the sandbox network policy only
has to open that port.
"""

import asyncio
import contextlib
import json
import math
import secrets
import socket
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, Literal

import anyio
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from yggdrasil.config import APP_CONFIG
from yggdrasil.core.log import LOGGER
from yggdrasil.llm.registry import record_usage
from yggdrasil.relay import bridge
from yggdrasil.relay.grants import (
    Grant,
    Target,
    add_usage,
    authenticate,
    configured_url,
    relay_models,
    release,
    request_token,
    reserve_output,
    route_targets,
    set_serving,
)

USAGE_TASK = "relay"
_ANTHROPIC_VERSION = "2023-06-01"
# The only client headers that reach the upstream; auth, host and anything else are dropped.
_PASS_HEADERS = ("anthropic-version", "anthropic-beta", "content-type", "accept")
_OPENAI_PASS_HEADERS = ("content-type", "accept")
_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
_RESPONSE_HEADERS = ("request-id", "x-request-id", "retry-after")
# Non-streamed generations can take minutes before the first byte.
_TIMEOUT = httpx.Timeout(connect=10.0, read=900.0, write=60.0, pool=10.0)
# Output estimate for text whose usage the upstream never reported (Claude tokenizers: ~3 chars/token
# for code and CJK-heavy text, more for English, so this errs on the high side).
_CHARS_PER_TOKEN = 3
_START_TIMEOUT_S = 10.0

_ApiFormat = Literal["anthropic", "openai"]


def _error(status: int, kind: str, message: str, *, api_format: _ApiFormat = "anthropic") -> JSONResponse:
    # Refusals a retry can't fix: tell SDKs not to retry them.
    headers = {"x-should-retry": "false"} if status in (400, 401, 403, 404, 429) else None
    if api_format == "openai":
        return JSONResponse({"error": {"message": message, "type": kind, "code": kind}}, status, headers=headers)
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status, headers=headers)


async def _grant(request: Request, *, api_format: _ApiFormat = "anthropic") -> Grant | Response:
    token = request_token(request.headers)
    if token is None:
        return _error(401, "authentication_error", "缺少中转令牌", api_format=api_format)
    grant = await authenticate(token)
    if grant is None:
        return _error(401, "authentication_error", "中转令牌无效或已过期", api_format=api_format)
    return grant


def _estimate_tokens(chars: int) -> int:
    return math.ceil(chars / _CHARS_PER_TOKEN)


def _int_value(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


@dataclass(slots=True)
class _Usage:
    input: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    output: int = 0

    def update(self, usage: Any) -> None:
        """Anthropic counts are cumulative over a message; some gateways repeat them as 0 later, so keep the max."""
        if not isinstance(usage, dict):
            return
        for attr, key in (
            ("input", "input_tokens"),
            ("cache_creation", "cache_creation_input_tokens"),
            ("cache_read", "cache_read_input_tokens"),
            ("output", "output_tokens"),
        ):
            value = _int_value(usage.get(key))
            if value is not None:
                setattr(self, attr, max(getattr(self, attr), value))

    def update_openai(self, usage: Any) -> bool:
        """OpenAI ``prompt_tokens`` already includes cached prompt tokens, so subtract ``cache_read`` from
        ``input`` to avoid double-counting in ``total_input``. Returns True when any token count was found."""
        if not isinstance(usage, dict):
            return False
        prompt = _int_value(usage.get("prompt_tokens"))
        completion = _int_value(usage.get("completion_tokens"))
        details = usage.get("prompt_tokens_details")
        cached = _int_value(details.get("cached_tokens")) if isinstance(details, dict) else None
        if cached is None:
            cached = _int_value(usage.get("prompt_cache_hit_tokens"))
        if cached is None:
            cached = _int_value(usage.get("cached_tokens"))
        seen = prompt is not None or completion is not None
        if cached is not None:
            self.cache_read = max(self.cache_read, cached)
        if prompt is not None:
            total = max(self.total_input, prompt)
            self.input = max(total - self.cache_read - self.cache_creation, 0)
        if completion is not None:
            self.output = max(self.output, completion)
        return seen

    @property
    def total_input(self) -> int:
        return self.input + self.cache_creation + self.cache_read


# Generated text per content delta / content block type.
_DELTA_TEXT = {"text_delta": "text", "thinking_delta": "thinking", "input_json_delta": "partial_json"}


class _SseUsage:
    """Picks usage out of an Anthropic SSE byte stream fed in arbitrary chunks."""

    def __init__(self) -> None:
        self._usage = _Usage()
        self._buf = b""
        self._final = False  # message_delta seen: its output count is authoritative
        self._chars = 0  # generated characters, for the estimate when it isn't

    @property
    def usage(self) -> _Usage:
        """Reported usage; without a ``message_delta`` (stream cut short) the output is estimated from the
        generated text, never below what ``message_start`` reported."""
        if self._final:
            return self._usage
        estimate = _estimate_tokens(self._chars)
        u = self._usage
        return _Usage(u.input, u.cache_creation, u.cache_read, max(u.output, estimate))

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and (field := _DELTA_TEXT.get(str(delta.get("type")))):
                    text = delta.get(field)
                    self._chars += len(text) if isinstance(text, str) else 0
            elif kind == "message_start":
                message = event.get("message")
                self._usage.update(message.get("usage") if isinstance(message, dict) else None)
            elif kind == "message_delta":
                self._usage.update(event.get("usage"))
                self._final = True


def _openai_msg_chars(obj: Any) -> int:
    """Character count of generated text/thinking/tool-call arguments in an OpenAI delta or message object."""
    if not isinstance(obj, dict):
        return 0
    chars = 0
    for field in ("content", "reasoning_content", "reasoning"):
        text = obj.get(field)
        if isinstance(text, str):
            chars += len(text)
    for tc in obj.get("tool_calls") or ():
        if isinstance(tc, dict):
            fn = tc.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                chars += len(fn["arguments"])
    return chars


class _OpenAISseUsage:
    """Picks usage out of an OpenAI Chat Completions SSE byte stream fed in arbitrary chunks."""

    def __init__(self) -> None:
        self._usage = _Usage()
        self._buf = b""
        self._final = False
        self._chars = 0

    @property
    def usage(self) -> _Usage:
        if self._final:
            return self._usage
        estimate = _estimate_tokens(self._chars)
        u = self._usage
        return _Usage(u.input, u.cache_creation, u.cache_read, max(u.output, estimate))

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if self._usage.update_openai(event.get("usage")):
                self._final = True
            for choice in event.get("choices") or ():
                if not isinstance(choice, dict):
                    continue
                self._chars += _openai_msg_chars(choice.get("delta"))
                # Some providers (e.g. Moonshot/Kimi) attach usage on the final choice object.
                if self._usage.update_openai(choice.get("usage")):
                    self._final = True


def _body_usage(content: bytes, *, api_format: _ApiFormat = "anthropic") -> _Usage:
    """Usage of a non-streamed message; estimated from its content when the body doesn't report it."""
    usage = _Usage()
    try:
        data = json.loads(content)
    except ValueError:
        return usage
    if not isinstance(data, dict):
        return usage
    if api_format == "openai":
        if usage.update_openai(data.get("usage")):
            return usage
        chars = sum(
            _openai_msg_chars(choice.get("message")) for choice in data.get("choices") or () if isinstance(choice, dict)
        )
        usage.output = _estimate_tokens(chars)
        return usage
    if isinstance(data.get("usage"), dict):
        usage.update(data["usage"])
        return usage
    chars = 0
    for block in data.get("content") or []:
        if isinstance(block, dict):
            for field in ("text", "thinking"):
                if isinstance(block.get(field), str):
                    chars += len(block[field])
            if "input" in block:
                chars += len(json.dumps(block["input"], ensure_ascii=False))
    usage.output = _estimate_tokens(chars)
    return usage


def _wanted_output(payload: dict[str, Any], *, api_format: _ApiFormat = "anthropic") -> int | None:
    keys = ("max_completion_tokens", "max_tokens") if api_format == "openai" else ("max_tokens",)
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _clamp(payload: dict[str, Any], max_tokens: int, *, api_format: _ApiFormat = "anthropic") -> dict[str, Any]:
    """``payload`` with output limit lowered to the reserved budget (and Anthropic thinking budget below it)."""
    if api_format == "openai":
        out = dict(payload)
        has_mct = isinstance(payload.get("max_completion_tokens"), int) and not isinstance(
            payload.get("max_completion_tokens"), bool
        )
        has_mt = isinstance(payload.get("max_tokens"), int) and not isinstance(payload.get("max_tokens"), bool)
        if has_mct:
            out["max_completion_tokens"] = min(int(payload["max_completion_tokens"]), max_tokens)
        if has_mt:
            out["max_tokens"] = min(int(payload["max_tokens"]), max_tokens)
        if not has_mct and not has_mt:
            out["max_tokens"] = max_tokens
        return out
    out = {**payload, "max_tokens": max_tokens}
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and isinstance(budget := thinking.get("budget_tokens"), int):
        if budget >= max_tokens:
            out["thinking"] = {**thinking, "budget_tokens": max_tokens - 1}
    return out


def _ensure_stream_usage(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Ask OpenAI-compatible upstreams to include final usage in SSE streams."""
    if not payload.get("stream"):
        return payload, False
    opts = payload.get("stream_options")
    if isinstance(opts, dict) and opts.get("include_usage") is True:
        return payload, False
    merged = {**(opts if isinstance(opts, dict) else {}), "include_usage": True}
    return {**payload, "stream_options": merged}, True


def _messages_url(target: Target) -> str:
    # Providers store base URLs including /v1 (see registry.anthropic_client).
    return f"{target.provider.base_url.rstrip('/').removesuffix('/v1')}/v1/messages"


def _chat_completions_url(target: Target) -> str:
    return f"{target.provider.base_url.rstrip('/')}/chat/completions"


def _upstream_headers(request: Request, target: Target, *, api_format: _ApiFormat = "anthropic") -> httpx.Headers:
    headers = httpx.Headers({"user-agent": APP_CONFIG.llm_user_agent})
    pass_headers = _OPENAI_PASS_HEADERS if api_format == "openai" else _PASS_HEADERS
    for name in pass_headers:
        if value := request.headers.get(name):
            headers[name] = value
    if api_format == "anthropic":
        headers.setdefault("anthropic-version", _ANTHROPIC_VERSION)
    headers.setdefault("content-type", "application/json")
    headers.update(target.provider.headers)
    if target.provider.api_key:
        if api_format == "openai":
            headers["authorization"] = f"Bearer {target.provider.api_key}"
        else:
            headers["x-api-key"] = target.provider.api_key
    return headers


def _passthrough_headers(upstream: httpx.Response) -> dict[str, str]:
    return {name: value for name in _RESPONSE_HEADERS if (value := upstream.headers.get(name))}


async def _account(grant: Grant, rid: str, target: Target, usage: _Usage, started: float, status: str) -> None:
    try:
        await add_usage(
            grant, rid, input_tokens=usage.total_input, cached_tokens=usage.cache_read, output_tokens=usage.output
        )
    except Exception:
        LOGGER.exception("relay usage not recorded for job {}", grant.job_id)
    record_usage(
        USAGE_TASK, target.label, input_tokens=usage.total_input, output_tokens=usage.output, cached=usage.cache_read
    )
    LOGGER.info(
        "relay {} channel={} job={} model={} in={} cached={} out={} {:.1f}s",
        status,
        grant.channel_id,
        grant.job_id,
        target.label,
        usage.total_input,
        usage.cache_read,
        usage.output,
        time.monotonic() - started,
    )


class Relay:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/messages", self.messages, methods=["POST"]),
                Route("/v1/chat/completions", self.chat_completions, methods=["POST"]),
                Route("/v1/models", self.models, methods=["GET"]),
                *bridge.routes(),
            ]
        )

    async def models(self, request: Request) -> Response:
        grant = await _grant(request)
        if isinstance(grant, Response):
            return grant
        known = {m.id: m for m in await relay_models()}
        data = [
            {
                "type": "model",
                "object": "model",
                "id": m.id,
                "display_name": m.name,
                "created": 0,
                "created_at": "1970-01-01T00:00:00Z",
            }
            for mid in grant.models
            if (m := known.get(mid))
        ]
        return JSONResponse(
            {
                "object": "list",
                "data": data,
                "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            }
        )

    async def messages(self, request: Request) -> Response:
        return await self._handle(request, api_format="anthropic")

    async def chat_completions(self, request: Request) -> Response:
        return await self._handle(request, api_format="openai")

    async def _handle(self, request: Request, *, api_format: _ApiFormat) -> Response:
        started = time.monotonic()
        grant = await _grant(request, api_format=api_format)
        if isinstance(grant, Response):
            return grant
        body = await request.body()
        try:
            payload = json.loads(body)
        except ValueError:
            return _error(400, "invalid_request_error", "请求体不是 JSON", api_format=api_format)
        model = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(model, str) or not model:
            return _error(400, "invalid_request_error", "缺少 model", api_format=api_format)
        if model not in grant.models:
            return _error(403, "permission_error", f"这个令牌不能用模型 {model}", api_format=api_format)
        targets = [t for t in route_targets().get(model, []) if t.provider.api_format == api_format]
        if not targets:
            return _error(404, "not_found_error", f"模型 {model} 现在不可用", api_format=api_format)
        wanted = _wanted_output(payload, api_format=api_format)
        rid = secrets.token_hex(8)
        budget = await reserve_output(grant, rid, wanted or grant.max_output_tokens)
        if not budget.granted:
            return _error(
                429,
                "rate_limit_error",
                f"这个任务的模型用量已到上限（输入 {budget.input_used}/{grant.max_input_tokens}，"
                f"输出 {budget.output_used}/{grant.max_output_tokens} token，"
                f"进行中的请求预留 {budget.output_pending}）",
                api_format=api_format,
            )
        rewritten = False
        if wanted is not None and budget.granted < wanted:
            payload = _clamp(payload, budget.granted, api_format=api_format)
            rewritten = True
        if api_format == "openai":
            payload, added_usage = _ensure_stream_usage(payload)
            rewritten = rewritten or added_usage
        if rewritten:
            body = json.dumps(payload).encode()
        streaming = False
        try:
            response = await self._forward(request, payload, body, targets, grant, rid, started, api_format=api_format)
            streaming = isinstance(response, StreamingResponse)
            return response
        finally:
            # A stream releases its reservation when it ends; everything else is settled here (usage
            # recorded already drops it, so this is a no-op then).
            if not streaming:
                with anyio.CancelScope(shield=True):
                    await release(grant, rid)

    async def _forward(
        self,
        request: Request,
        payload: dict[str, Any],
        body: bytes,
        targets: list[Target],
        grant: Grant,
        rid: str,
        started: float,
        *,
        api_format: _ApiFormat,
    ) -> Response:
        sent = await self._send(request, payload, body, targets, api_format=api_format)
        if isinstance(sent, Response):
            return sent
        upstream, target = sent
        if upstream.status_code >= 400:
            content = await upstream.aread()
            await upstream.aclose()
            LOGGER.warning(
                "relay upstream {} for job={} model={}: {}",
                upstream.status_code,
                grant.job_id,
                target.label,
                content[:500].decode(errors="replace"),
            )
            return Response(
                content,
                upstream.status_code,
                headers=_passthrough_headers(upstream),
                media_type=upstream.headers.get("content-type"),
            )
        media_type = upstream.headers.get("content-type", "")
        if not media_type.startswith("text/event-stream"):
            try:
                content = await upstream.aread()
            finally:
                await upstream.aclose()
            await _account(grant, rid, target, _body_usage(content, api_format=api_format), started, "ok")
            return Response(
                content, upstream.status_code, headers=_passthrough_headers(upstream), media_type=media_type
            )
        return StreamingResponse(
            self._stream(upstream, grant, rid, target, started, api_format=api_format),
            upstream.status_code,
            headers={**_passthrough_headers(upstream), "cache-control": "no-cache", "x-accel-buffering": "no"},
            media_type=media_type,
        )

    async def _send(
        self,
        request: Request,
        payload: dict[str, Any],
        body: bytes,
        targets: list[Target],
        *,
        api_format: _ApiFormat,
    ) -> tuple[httpx.Response, Target] | Response:
        """First upstream that answers with something other than a retryable failure (the last one's
        answer otherwise)."""
        failure = "上游没有响应"
        for i, target in enumerate(targets):
            last = i == len(targets) - 1
            content = body if target.model == payload["model"] else json.dumps({**payload, "model": target.model})
            url = _chat_completions_url(target) if api_format == "openai" else _messages_url(target)
            req = self.http.build_request(
                "POST", url, headers=_upstream_headers(request, target, api_format=api_format), content=content
            )
            try:
                upstream = await self.http.send(req, stream=True)
            except httpx.TransportError as e:
                LOGGER.warning("relay upstream {} unreachable: {!r}", target.label, e)
                failure = f"上游连接失败：{type(e).__name__}"
                continue
            if upstream.status_code in _RETRY_STATUS and not last:
                LOGGER.warning("relay upstream {} answered {}, trying fallback", target.label, upstream.status_code)
                await upstream.aclose()
                continue
            return upstream, target
        return _error(502, "api_error", failure, api_format=api_format)

    async def _stream(
        self,
        upstream: httpx.Response,
        grant: Grant,
        rid: str,
        target: Target,
        started: float,
        *,
        api_format: _ApiFormat,
    ) -> AsyncIterator[bytes]:
        sse: _SseUsage | _OpenAISseUsage = _OpenAISseUsage() if api_format == "openai" else _SseUsage()
        status = "aborted"
        try:
            async for chunk in upstream.aiter_bytes():
                sse.feed(chunk)
                yield chunk
            status = "ok"
        except httpx.HTTPError as e:
            status = f"upstream-error {type(e).__name__}"
            raise
        finally:
            # Runs on client disconnect too (cancellation): account for what was seen. A response whose
            # body never started streaming leaves its reservation to lapse.
            with anyio.CancelScope(shield=True):
                await upstream.aclose()
                await _account(grant, rid, target, sse.usage, started, status)


class _Server(uvicorn.Server):
    """A second server inside the bot process: the main uvicorn server owns the signal handlers."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.listening = asyncio.Event()

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)  # exits when it can't bind
        self.listening.set()


@dataclass(slots=True)
class _Running:
    server: _Server
    task: asyncio.Task[None]
    http: httpx.AsyncClient


_running: _Running | None = None


async def _serve(server: _Server) -> None:
    try:
        await server.serve()
    except SystemExit:  # uvicorn exits when it can't bind; must not reach the event loop
        pass
    except Exception:
        LOGGER.exception("llm relay server failed")


def _stopped(_task: asyncio.Task[None]) -> None:
    set_serving(False)


async def start_relay() -> None:
    """Serve the relay on ``YGG_RELAY_PORT`` when ``YGG_RELAY_URL`` is set. Returns once it listens;
    ``relay_url()`` stays None (delegation off) when it can't."""
    global _running
    url = configured_url()
    if _running is not None or url is None:
        return
    http = httpx.AsyncClient(timeout=_TIMEOUT)
    # No log_config/log_level/access_log: anything else makes uvicorn reconfigure the process-wide
    # uvicorn.* loggers that core.log routes into loguru.
    config = uvicorn.Config(
        Relay(http).app(),
        host=APP_CONFIG.host,
        port=APP_CONFIG.relay_port,
        lifespan="off",
        log_config=None,
        timeout_graceful_shutdown=5,
    )
    server = _Server(config)
    task = asyncio.create_task(_serve(server), name="llm-relay")
    listening = asyncio.create_task(server.listening.wait())
    await asyncio.wait({listening, task}, timeout=_START_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
    listening.cancel()
    if not server.listening.is_set():
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await http.aclose()
        LOGGER.error(
            "llm relay could not listen on {}:{}; delegating to workspace agents is off",
            APP_CONFIG.host,
            APP_CONFIG.relay_port,
        )
        return
    _running = _Running(server, task, http)
    set_serving(True)
    task.add_done_callback(_stopped)
    LOGGER.info("llm relay on {}:{} (sandboxes use {})", APP_CONFIG.host, APP_CONFIG.relay_port, url)


async def stop_relay() -> None:
    global _running
    if _running is None:
        return
    running, _running = _running, None
    set_serving(False)
    running.server.should_exit = True
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await running.task
    await running.http.aclose()
