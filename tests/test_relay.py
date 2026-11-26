import asyncio
import json
import logging
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import fakeredis
import httpx
import pytest

from tests.support.routes import Routes
from yggdrasil.config import APP_CONFIG
from yggdrasil.llm.catalog import ModelCaps
from yggdrasil.relay import grants, issue_token, relay_models, relay_url, relay_usage, revoke_token
from yggdrasil.relay.grants import authenticate, pending_key, token_key
from yggdrasil.relay.server import Relay, start_relay, stop_relay
from yggdrasil.runtime import ProviderInfo, Route, Snapshot

REAL_KEY = "sk-real-provider-key"
MODEL = "claude-opus-5-5"
FALLBACK = "claude-sonnet-5-5"
CAPS = ModelCaps(
    catalog_id="anthropic/claude-opus-5-5", vision=True, pdf=True, tools=True, reasoning=True, context=500_000
)

SSE = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"'
    + MODEL.encode()
    + b'","content":[],"usage":{"input_tokens":100,"cache_creation_input_tokens":20,'
    b'"cache_read_input_tokens":300,"output_tokens":1}}}\n\n'
    b"event: content_block_start\n"
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"\xe4\xbd\xa0\xe5\xa5\xbd"}}\n\n'
    b"event: content_block_stop\n"
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"input_tokens":0,"output_tokens":42}}\n\n'
    b"event: message_stop\n"
    b'data: {"type":"message_stop"}\n\n'
)


def _config(*, fallback_format: str = "anthropic") -> dict[str, Any]:
    primary = ProviderInfo(
        1,
        "gw",
        "https://gw.example.com/v1",
        REAL_KEY,
        {"x-provider": "p1"},
        True,
        (MODEL,),
        api_format="anthropic",
        caps={MODEL: CAPS},
    )
    backup = ProviderInfo(
        2, "backup", "https://backup.example.com", "sk-backup", {}, True, (FALLBACK,), fallback_format
    )
    route = Route("agent", 1, MODEL, {}, 2, FALLBACK, {})
    return {"providers": {1: primary, 2: backup}, "routes": {"agent": route}}


@pytest.fixture(autouse=True)
def providers(snapshot: Callable[..., Snapshot]) -> None:
    """The agent route the relay serves: the primary provider and the fallback."""
    snapshot(**_config())


@pytest.fixture
def upstream() -> Routes:
    """The providers behind the relay, answering by host."""
    return Routes()


@pytest.fixture
async def client(upstream: Routes) -> AsyncIterator[httpx.AsyncClient]:
    async with upstream.client() as http:
        app = Relay(http).app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay.test") as c:
            yield c


async def _token(job_id: str = "job1", **kw: int) -> str:
    args: dict[str, Any] = {"max_input_tokens": 1_000_000, "max_output_tokens": 100_000, "ttl_s": 600, **kw}
    return await issue_token(channel_id="ch-10001", job_id=job_id, models=[MODEL], **args)


def _sse() -> Callable[[httpx.Request], httpx.Response]:
    """The upstream streaming ``SSE`` in 37-byte pieces (splitting it mid-line and mid-character)."""

    def respond(_request: httpx.Request) -> httpx.Response:
        async def chunks() -> AsyncIterator[bytes]:
            for i in range(0, len(SSE), 37):
                yield SSE[i : i + 37]

        headers = {"content-type": "text/event-stream", "request-id": "req_1"}
        return httpx.Response(200, headers=headers, content=chunks())

    return respond


async def test_token_lifecycle(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    token = await _token(ttl_s=600)
    assert len(token) >= 40
    grant = await authenticate(token)
    assert grant is not None
    assert (grant.channel_id, grant.job_id, grant.models) == ("ch-10001", "job1", (MODEL,))
    # Only the hash is stored, and it expires with the grant.
    keys = await fake_redis.keys("ygg:relay:*")
    assert all(token not in str(k) for k in keys)
    values = [await fake_redis.get(k) for k in keys]
    assert all(token not in str(v or "") for v in values)
    assert 0 < await fake_redis.ttl(token_key(grants.digest(token))) <= 600

    reissued = await _token()
    assert await authenticate(token) is None  # the job's previous token stops working
    assert await authenticate(reissued) is not None

    await revoke_token("job1")
    assert await authenticate(reissued) is None
    assert await fake_redis.keys("ygg:relay:*") == []
    await revoke_token("job1")  # idempotent


async def test_expired_grant_is_rejected(fake_redis: fakeredis.FakeAsyncRedis) -> None:
    token = await _token()
    key = token_key(grants.digest(token))
    data = json.loads(await fake_redis.get(key) or "{}")
    await fake_redis.set(key, json.dumps({**data, "expires_at": data["expires_at"] - 3600}))
    assert await authenticate(token) is None


async def test_relay_models_come_from_the_chat_route() -> None:
    models = await relay_models()
    assert [m.id for m in models] == [MODEL, FALLBACK]
    opus = models[0]
    assert (opus.name, opus.context_window, opus.reasoning, opus.vision) == (
        "Anthropic/claude-opus-5-5",
        500_000,
        True,
        True,
    )
    assert opus.max_tokens > 0  # unknown output limit → default
    assert not models[1].vision


async def test_relay_models_include_openai_and_isolate_cross_format_fallback(
    snapshot: Callable[..., Snapshot],
) -> None:
    snapshot(**_config(fallback_format="openai"))
    models = await relay_models()
    assert [(m.id, m.api_format) for m in models] == [(MODEL, "anthropic"), (FALLBACK, "openai")]
    targets = grants.route_targets()
    # Primary (anthropic) must not fall through to an openai-format backup.
    assert [t.model for t in targets[MODEL]] == [MODEL]
    assert [t.model for t in targets[FALLBACK]] == [FALLBACK]


async def test_requests_without_valid_token_are_rejected(client: httpx.AsyncClient) -> None:
    body = {"model": MODEL, "max_tokens": 10, "messages": []}
    r = await client.post("/v1/messages", json=body)
    assert r.status_code == 401
    assert r.json()["type"] == "error"
    assert r.json()["error"]["type"] == "authentication_error"
    r = await client.post("/v1/messages", json=body, headers={"authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_model_outside_the_grant_is_forbidden(client: httpx.AsyncClient, upstream: Routes) -> None:
    token = await _token()
    r = await client.post("/v1/messages", json={"model": FALLBACK, "messages": []}, headers={"x-api-key": token})
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "permission_error"
    assert upstream.seen == []


async def test_exhausted_budget_is_rejected(client: httpx.AsyncClient, upstream: Routes) -> None:
    upstream.answers["gw.example.com"] = _sse()
    token = await _token(max_output_tokens=40)
    headers = {"authorization": f"Bearer {token}"}
    body = {"model": MODEL, "stream": True, "messages": []}
    first = await client.post("/v1/messages", json=body, headers=headers)
    assert first.status_code == 200  # under budget when it started; the stream is not cut
    second = await client.post("/v1/messages", json=body, headers=headers)
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limit_error"
    assert len(upstream.seen) == 1


async def test_stream_passes_through_and_usage_is_recorded(client: httpx.AsyncClient, upstream: Routes) -> None:
    upstream.answers["gw.example.com"] = _sse()
    token = await _token()
    body = json.dumps({"model": MODEL, "stream": True, "max_tokens": 100, "messages": []})
    async with client.stream(
        "POST",
        "/v1/messages",
        content=body,
        headers={
            "authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "interleaved-thinking-2025-05-14",
            "content-type": "application/json",
            "x-forwarded-for": "10.0.0.1",
            "cookie": "a=b",
        },
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["request-id"] == "req_1"
        received = b"".join([chunk async for chunk in r.aiter_bytes()])
    assert received == SSE

    assert await relay_usage("job1") == grants.RelayUsage(
        requests=1, input_tokens=420, cached_tokens=300, output_tokens=42
    )

    (sent,) = upstream.seen
    assert str(sent.url) == "https://gw.example.com/v1/messages"
    assert sent.content == body.encode()  # forwarded unchanged
    assert sent.headers["x-api-key"] == REAL_KEY
    assert sent.headers["host"] == "gw.example.com"
    assert sent.headers["anthropic-beta"] == "interleaved-thinking-2025-05-14"
    assert sent.headers["x-provider"] == "p1"
    assert "authorization" not in sent.headers
    assert "cookie" not in sent.headers
    assert "x-forwarded-for" not in sent.headers
    assert all(token not in v for v in sent.headers.values())


async def test_non_streaming_usage_is_recorded(client: httpx.AsyncClient, upstream: Routes) -> None:
    message = {
        "id": "msg_2",
        "type": "message",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 7, "cache_read_input_tokens": 3, "output_tokens": 5},
    }
    upstream.answers["gw.example.com"] = lambda _r: httpx.Response(200, json=message)
    token = await _token()
    r = await client.post("/v1/messages", json={"model": MODEL, "messages": []}, headers={"x-api-key": token})
    assert r.status_code == 200
    assert r.json() == message
    assert upstream.seen[0].headers["anthropic-version"] == "2023-06-01"  # default when the client sent none
    assert await relay_usage("job1") == grants.RelayUsage(requests=1, input_tokens=10, cached_tokens=3, output_tokens=5)


async def test_primary_failure_falls_back_with_the_fallback_model(client: httpx.AsyncClient, upstream: Routes) -> None:
    upstream.answers["gw.example.com"] = lambda _r: httpx.Response(529, json={"type": "error"})
    upstream.answers["backup.example.com"] = _sse()
    token = await _token()
    r = await client.post(
        "/v1/messages", json={"model": MODEL, "stream": True, "messages": []}, headers={"x-api-key": token}
    )
    assert r.status_code == 200
    assert r.content == SSE
    primary, backup = upstream.seen
    assert json.loads(backup.content)["model"] == FALLBACK
    assert str(backup.url) == "https://backup.example.com/v1/messages"
    assert backup.headers["x-api-key"] == "sk-backup"
    assert primary.headers["x-api-key"] == REAL_KEY


async def test_upstream_client_errors_pass_through_unaccounted(client: httpx.AsyncClient, upstream: Routes) -> None:
    err = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    upstream.answers["gw.example.com"] = lambda _r: httpx.Response(400, json=err)
    token = await _token()
    r = await client.post("/v1/messages", json={"model": MODEL, "messages": []}, headers={"x-api-key": token})
    assert (r.status_code, r.json()) == (400, err)
    assert len(upstream.seen) == 1  # a 400 is not retried on the fallback
    assert (await relay_usage("job1")).requests == 0


async def test_unreachable_upstreams_give_an_api_error(client: httpx.AsyncClient, upstream: Routes) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    upstream.answers["gw.example.com"] = refuse
    upstream.answers["backup.example.com"] = refuse
    token = await _token()
    r = await client.post("/v1/messages", json={"model": MODEL, "messages": []}, headers={"x-api-key": token})
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"


async def test_models_lists_the_grant(client: httpx.AsyncClient) -> None:
    token = await _token()
    r = await client.get("/v1/models", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == [MODEL]
    assert (await client.get("/v1/models")).status_code == 401


async def test_parallel_requests_cannot_overrun_the_output_budget(
    client: httpx.AsyncClient, upstream: Routes, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    both_upstream = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        if len(upstream.seen) == 2:
            both_upstream.set()
        await both_upstream.wait()  # both are in flight before either records usage
        max_tokens = json.loads(request.content)["max_tokens"]
        return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": max_tokens}})

    upstream.answers["gw.example.com"] = slow
    token = await _token(max_output_tokens=100)
    body = {"model": MODEL, "max_tokens": 80, "messages": []}
    responses = await asyncio.gather(
        *(client.post("/v1/messages", json=body, headers={"x-api-key": token}) for _ in range(3))
    )
    assert sorted(r.status_code for r in responses) == [200, 200, 429]
    # The second request was clamped to what the first one's reservation left.
    assert sorted(json.loads(r.content)["max_tokens"] for r in upstream.seen) == [20, 80]
    assert (await relay_usage("job1")).output_tokens == 100
    assert await fake_redis.hgetall(pending_key("job1")) == {}  # pyright: ignore[reportGeneralTypeIssues]


async def test_failed_requests_release_their_reservation(
    client: httpx.AsyncClient, upstream: Routes, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    upstream.answers["gw.example.com"] = lambda _r: httpx.Response(400, json={"type": "error"})
    token = await _token(max_output_tokens=100)
    for _ in range(3):
        r = await client.post(
            "/v1/messages", json={"model": MODEL, "max_tokens": 80, "messages": []}, headers={"x-api-key": token}
        )
        assert r.status_code == 400
    assert await fake_redis.hgetall(pending_key("job1")) == {}  # pyright: ignore[reportGeneralTypeIssues]


async def test_lapsed_reservations_stop_counting(
    client: httpx.AsyncClient, upstream: Routes, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    upstream.answers["gw.example.com"] = lambda r: httpx.Response(200, json={"usage": {"output_tokens": 1}})
    token = await _token(max_output_tokens=100)
    await fake_redis.hset(pending_key("job1"), mapping={"dead": "100:1", "garbage": "x"})
    r = await client.post(
        "/v1/messages", json={"model": MODEL, "max_tokens": 50, "messages": []}, headers={"x-api-key": token}
    )
    assert r.status_code == 200
    assert json.loads(upstream.seen[0].content)["max_tokens"] == 50
    assert await fake_redis.hgetall(pending_key("job1")) == {}


@pytest.mark.parametrize(("left", "thinking"), [(20000, 16000), (8000, 7999)])
async def test_a_clamped_request_keeps_its_thinking_budget_below_max_tokens(
    client: httpx.AsyncClient, upstream: Routes, left: int, thinking: int
) -> None:
    upstream.answers["gw.example.com"] = lambda _r: httpx.Response(200, json={"usage": {"output_tokens": 1}})
    token = await _token(max_output_tokens=left)
    body = {"model": MODEL, "max_tokens": 32000, "thinking": {"type": "enabled", "budget_tokens": 16000}}
    r = await client.post("/v1/messages", json={**body, "messages": []}, headers={"x-api-key": token})
    assert r.status_code == 200
    sent = json.loads(upstream.seen[0].content)
    assert (sent["max_tokens"], sent["thinking"]["budget_tokens"]) == (left, thinking)


async def test_stream_cut_before_message_delta_charges_an_estimate(client: httpx.AsyncClient, upstream: Routes) -> None:
    cut = SSE[: SSE.index(b"event: message_delta")] + (
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"'
        + b"x"
        * 600
        + b'"}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"'
        + b"y" * 298
        + b'"}}\n\n'
    )
    upstream.answers["gw.example.com"] = lambda _r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=cut
    )
    token = await _token()
    r = await client.post(
        "/v1/messages", json={"model": MODEL, "stream": True, "messages": []}, headers={"x-api-key": token}
    )
    assert r.content == cut
    # 2 + 600 + 298 generated characters → 300 tokens, though message_start only reported 1.
    assert (await relay_usage("job1")).output_tokens == 300


async def test_a_stream_cut_after_message_start_charges_what_it_reported(
    client: httpx.AsyncClient, upstream: Routes
) -> None:
    cut = SSE[: SSE.index(b"event: content_block_start")]
    upstream.answers["gw.example.com"] = lambda _r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=cut
    )
    token = await _token()
    r = await client.post(
        "/v1/messages", json={"model": MODEL, "stream": True, "messages": []}, headers={"x-api-key": token}
    )
    assert r.content == cut
    assert (await relay_usage("job1")).output_tokens == 1  # nothing generated yet: message_start's own count


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def relay_config(monkeypatch: pytest.MonkeyPatch) -> int:
    port = _free_port()
    monkeypatch.setattr(APP_CONFIG, "relay_url", "http://10.0.0.1:8090/")
    monkeypatch.setattr(APP_CONFIG, "relay_port", port)
    monkeypatch.setattr(APP_CONFIG, "host", "127.0.0.1")
    return port


async def test_relay_server_serves_without_touching_shared_logging(relay_config: int) -> None:
    sentinel = logging.NullHandler()
    access = logging.getLogger("uvicorn.access")
    access.addHandler(sentinel)
    loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.access", "uvicorn.error", "uvicorn.asgi")]
    before = [(lg.handlers[:], lg.propagate, lg.level, lg.filters[:]) for lg in loggers]
    try:
        assert relay_url() is None  # configured but not serving yet
        await start_relay()
        assert relay_url() == "http://10.0.0.1:8090"
        async with httpx.AsyncClient() as http:
            r = await http.get(f"http://127.0.0.1:{relay_config}/v1/models")
        assert r.status_code == 401
        assert [(lg.handlers[:], lg.propagate, lg.level, lg.filters[:]) for lg in loggers] == before
    finally:
        await stop_relay()
        access.removeHandler(sentinel)
    assert relay_url() is None


async def test_relay_that_cannot_bind_stays_disabled(relay_config: int) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", relay_config))
        taken.listen()
        await start_relay()  # logs, doesn't raise or exit
        assert relay_url() is None
        await stop_relay()


OPENAI_MODEL = "kimi-k2.5"
OPENAI_FALLBACK = "deepseek-reasoner"

OPENAI_SSE = (
    b'data: {"id":"cmpl_1","object":"chat.completion.chunk","choices":[{"index":0,'
    b'"delta":{"role":"assistant","reasoning_content":"\xe6\x80\x9d\xe8\x80\x83"}}]}\n\n'
    b'data: {"id":"cmpl_1","object":"chat.completion.chunk","choices":[{"index":0,'
    b'"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"}}]}\n\n'
    b'data: {"id":"cmpl_1","object":"chat.completion.chunk","choices":[],'
    b'"usage":{"prompt_tokens":1000,"completion_tokens":45,'
    b'"prompt_tokens_details":{"cached_tokens":300}}}\n\n'
    b"data: [DONE]\n\n"
)


def _openai_config() -> dict[str, Any]:
    primary = ProviderInfo(
        10,
        "kimi",
        "https://api.moonshot.cn/v1",
        "sk-kimi-real",
        {"x-vendor": "moonshot"},
        True,
        (OPENAI_MODEL,),
        api_format="openai",
        caps={
            OPENAI_MODEL: ModelCaps(
                catalog_id="moonshotai/kimi-k2.5", vision=True, pdf=False, tools=True, reasoning=True, context=262_144
            )
        },
    )
    backup = ProviderInfo(
        11,
        "deepseek",
        "https://api.deepseek.com/v1",
        "sk-ds-real",
        {},
        True,
        (OPENAI_FALLBACK,),
        api_format="openai",
    )
    route = Route("agent", 10, OPENAI_MODEL, {}, 11, OPENAI_FALLBACK, {})
    return {"providers": {10: primary, 11: backup}, "routes": {"agent": route}}


async def _openai_token(job_id: str = "job-oai", **kw: int) -> str:
    args: dict[str, Any] = {"max_input_tokens": 1_000_000, "max_output_tokens": 100_000, "ttl_s": 600, **kw}
    return await issue_token(channel_id="ch-10001", job_id=job_id, models=[OPENAI_MODEL], **args)


async def test_openai_stream_passes_through_injects_usage_and_records_usage(
    snapshot: Callable[..., Snapshot], client: httpx.AsyncClient, upstream: Routes
) -> None:
    snapshot(**_openai_config())
    models = await relay_models()
    assert [(m.id, m.api_format) for m in models] == [(OPENAI_MODEL, "openai"), (OPENAI_FALLBACK, "openai")]

    def respond(_request: httpx.Request) -> httpx.Response:
        async def chunks() -> AsyncIterator[bytes]:
            for i in range(0, len(OPENAI_SSE), 29):
                yield OPENAI_SSE[i : i + 29]

        return httpx.Response(
            200, headers={"content-type": "text/event-stream", "x-request-id": "oai_1"}, content=chunks()
        )

    upstream.answers["api.moonshot.cn"] = respond
    token = await _openai_token()
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": OPENAI_MODEL,
            "stream": True,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"authorization": f"Bearer {token}", "cookie": "secret=1"},
    )
    assert r.status_code == 200
    assert r.content == OPENAI_SSE
    assert r.headers["x-request-id"] == "oai_1"
    # prompt_tokens=1000 already includes cached_tokens=300; total input must stay 1000 (not 1300).
    assert await relay_usage("job-oai") == grants.RelayUsage(
        requests=1, input_tokens=1000, cached_tokens=300, output_tokens=45
    )

    (sent,) = upstream.seen
    assert str(sent.url) == "https://api.moonshot.cn/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer sk-kimi-real"
    assert sent.headers["x-vendor"] == "moonshot"
    assert "x-api-key" not in sent.headers
    assert "cookie" not in sent.headers
    assert json.loads(sent.content)["stream_options"] == {"include_usage": True}


async def test_openai_moonshot_choice_usage_and_non_stream_fallback(
    snapshot: Callable[..., Snapshot], client: httpx.AsyncClient, upstream: Routes
) -> None:
    snapshot(**_openai_config())
    # Moonshot choice-level usage in SSE
    moonshot_sse = (
        b'data: {"id":"cmpl_m","choices":[{"index":0,"delta":{"content":"ok"},'
        b'"usage":{"prompt_tokens":500,"completion_tokens":12,"cached_tokens":200}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    upstream.answers["api.moonshot.cn"] = lambda _r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=moonshot_sse
    )
    token = await _openai_token(job_id="job-ms")
    r = await client.post(
        "/v1/chat/completions",
        json={"model": OPENAI_MODEL, "stream": True, "messages": []},
        headers={"authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert await relay_usage("job-ms") == grants.RelayUsage(
        requests=1, input_tokens=500, cached_tokens=200, output_tokens=12
    )

    # Fallback on 503 to DeepSeek backup (with prompt_cache_hit_tokens)
    upstream.seen.clear()
    upstream.answers["api.moonshot.cn"] = lambda _r: httpx.Response(503, json={"error": {"message": "busy"}})
    upstream.answers["api.deepseek.com"] = lambda _r: httpx.Response(
        200,
        json={
            "id": "cmpl_ds",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 20, "prompt_cache_hit_tokens": 150},
        },
    )
    token2 = await _openai_token(job_id="job-ds")
    r2 = await client.post(
        "/v1/chat/completions",
        json={"model": OPENAI_MODEL, "messages": []},
        headers={"authorization": f"Bearer {token2}"},
    )
    assert r2.status_code == 200
    assert len(upstream.seen) == 2
    assert str(upstream.seen[1].url) == "https://api.deepseek.com/v1/chat/completions"
    assert json.loads(upstream.seen[1].content)["model"] == OPENAI_FALLBACK
    assert upstream.seen[1].headers["authorization"] == "Bearer sk-ds-real"
    assert await relay_usage("job-ds") == grants.RelayUsage(
        requests=1, input_tokens=400, cached_tokens=150, output_tokens=20
    )


async def test_openai_clamps_max_completion_tokens_and_estimates_cut_stream(
    snapshot: Callable[..., Snapshot], client: httpx.AsyncClient, upstream: Routes
) -> None:
    snapshot(**_openai_config())
    cut = (
        b'data: {"choices":[{"index":0,"delta":{"reasoning_content":"'
        + b"a" * 300
        + b'","content":"'
        + b"b" * 150
        + b'","tool_calls":[{"index":0,"function":{"arguments":"'
        + b"c" * 150
        + b'"}}]}}]}\n\n'
    )
    upstream.answers["api.moonshot.cn"] = lambda _r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=cut
    )
    token = await _openai_token(job_id="job-clamp", max_output_tokens=500)
    r = await client.post(
        "/v1/chat/completions",
        json={"model": OPENAI_MODEL, "stream": True, "max_completion_tokens": 4000, "messages": []},
        headers={"authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert json.loads(upstream.seen[0].content)["max_completion_tokens"] == 500
    # 300 + 150 + 150 = 600 chars → 200 estimated output tokens
    assert (await relay_usage("job-clamp")).output_tokens == 200


async def test_relay_protocol_mismatch_returns_404(
    snapshot: Callable[..., Snapshot], client: httpx.AsyncClient, upstream: Routes
) -> None:
    # Default fixture has Anthropic MODEL; hitting /v1/chat/completions must fail cleanly in OpenAI error shape.
    anth_token = await _token()
    r = await client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": []},
        headers={"authorization": f"Bearer {anth_token}"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "not_found_error"
    assert upstream.seen == []

    # OpenAI model hitting /v1/messages must fail cleanly in Anthropic error shape.
    snapshot(**_openai_config())
    oai_token = await _openai_token(job_id="job-mismatch")
    r2 = await client.post(
        "/v1/messages",
        json={"model": OPENAI_MODEL, "messages": []},
        headers={"x-api-key": oai_token},
    )
    assert r2.status_code == 404
    assert r2.json()["type"] == "error"
    assert upstream.seen == []
