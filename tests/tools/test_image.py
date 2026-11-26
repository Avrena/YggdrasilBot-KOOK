"""generate_image: OpenRouter-style /images call, KOOK CDN upload, and quote reply."""

import base64
import io
import json
import os
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from PIL import Image
from pydantic_ai import RunContext

from tests.support.agent import run_ctx
from tests.support.files import png
from tests.support.kook import FakeKook
from tests.support.routes import Routes
from yggdrasil import runtime
from yggdrasil.agent import media
from yggdrasil.agent.deps import Deps, Handle
from yggdrasil.agent.tools import images
from yggdrasil.delivery import send
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.llm import registry
from yggdrasil.moderation.image import ImageVerdict
from yggdrasil.settings import Settings

GUILD, CHANNEL, TRIGGER = "g-1", "ch-10001", "trig-555"
MODEL = "openai/gpt-image-test"
IMAGES = "https://openrouter.example.com/api/v1/images"


def _ok(picture: bytes, cost: float = 0.04) -> httpx.Response:
    item = {"b64_json": base64.b64encode(picture).decode(), "media_type": "image/png"}
    return httpx.Response(
        200, json={"data": [item], "usage": {"prompt_tokens": 12, "completion_tokens": 4175, "cost": cost}}
    )


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch, snapshot: Callable[..., runtime.Snapshot]) -> Routes:
    provider = runtime.ProviderInfo(1, "or", "https://openrouter.example.com/api/v1", "sk-test", {}, True, ())
    snapshot(
        providers={1: provider}, routes={"image": runtime.Route("image", 1, MODEL, {"quality": "high"}, None, None, {})}
    )
    routes = Routes({IMAGES: _ok(png())})
    routes.install(monkeypatch)
    return routes


def _body(upstream: Routes) -> dict[str, Any]:
    return json.loads(upstream.seen[-1].content)


@pytest.fixture(autouse=True)
def kept(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    store: dict[str, bytes] = {}

    async def put(blobs: dict[str, Any]) -> None:
        store.update({sha: b.data for sha, b in blobs.items()})

    async def get(sha: str) -> SimpleNamespace | None:
        return SimpleNamespace(data=store[sha]) if sha in store else None

    monkeypatch.setattr(send, "store_blobs", put)
    monkeypatch.setattr(media, "stored_blob", get)
    return store


def _ctx(kook: FakeKook, *, moderated: bool = False) -> RunContext[Deps]:
    settings = Settings.model_validate({"moderation": {"enabled": moderated, "check_output": moderated}})
    return run_ctx(
        kook=kook,
        channel_id=CHANNEL,
        guild_id=GUILD,
        user_id="20001",
        settings=settings,
        tool_call_id="call_1",
        trigger_message_id=TRIGGER,
        self_id="10000",
    )


async def test_the_picture_is_uploaded_and_sent_quoting_the_trigger(upstream: Routes, recorded: list[IncomingMessage]):
    kook = FakeKook(bot_id="10000")
    ctx = _ctx(kook)
    result = await images.generate_image(ctx, "一只戴墨镜的猫", aspect_ratio="16:9", text="画好了")

    assert upstream.seen[0].url.path == "/api/v1/images"
    assert upstream.seen[0].headers["authorization"] == "Bearer sk-test"
    assert _body(upstream) == {
        "quality": "high",
        "model": MODEL,
        "prompt": "一只戴墨镜的猫",
        "n": 1,
        "aspect_ratio": "16:9",
    }

    assert len(kook.uploaded) == 1
    assert Image.open(io.BytesIO(kook.uploaded[0][1])).size == (8, 8)
    assert len(kook.sent) == 2
    assert kook.sent[0].kind == "kmarkdown"
    assert kook.sent[0].content == "画好了"
    assert kook.sent[0].quote == TRIGGER
    assert kook.sent[1].kind == "image"
    assert "$0.040" in str(result)
    assert ctx.deps.actions == ["image msg-1"]

    assert len(recorded) == 2
    assert recorded[0].is_self
    assert recorded[0].kook_message_id == "msg-1"
    img_seg = next(s for s in recorded[1].segments if s["type"] == "image")
    again = await media.media_bytes(Handle("img#1", "image", img_seg["data"]), 1 << 20)
    assert again[0] == kook.uploaded[0][1]


async def test_a_large_picture_is_sent_as_jpeg(upstream: Routes, recorded: list[IncomingMessage]):
    noisy = io.BytesIO()
    Image.frombytes("RGB", (1200, 1200), os.urandom(1200 * 1200 * 3)).save(noisy, "PNG")
    upstream.answers[IMAGES] = _ok(noisy.getvalue())
    kook = FakeKook(bot_id="10000")
    await images.generate_image(_ctx(kook), "x")
    assert len(kook.uploaded) == 1
    sent = kook.uploaded[0][1]
    assert sent[:2] == b"\xff\xd8"
    assert len(sent) < len(noisy.getvalue())
    assert Image.open(io.BytesIO(sent)).size == (1200, 1200)


async def test_reference_images_go_as_data_urls(upstream: Routes):
    blue = png("blue")
    async with upstream.client() as client:
        await registry.generate_images("改成蓝色", references=[(blue, "image/png")], client=client)
    url = _body(upstream)["input_references"][0]["image_url"]["url"]
    assert url == "data:image/png;base64," + base64.b64encode(blue).decode()
    assert "aspect_ratio" not in _body(upstream)


async def test_a_refusal_reaches_the_model_and_nothing_is_sent(upstream: Routes):
    upstream.answers[IMAGES] = httpx.Response(400, json={"error": {"message": "Request rejected by the safety system"}})
    kook = FakeKook(bot_id="10000")
    ctx = _ctx(kook)
    result = await images.generate_image(ctx, "x")
    assert "画图失败：400 Request rejected by the safety system" in str(result)
    assert kook.sent == []
    assert ctx.deps.actions == []


async def test_an_unsafe_picture_is_not_sent(upstream: Routes, monkeypatch: pytest.MonkeyPatch):
    async def unsafe(_blob: bytes) -> ImageVerdict:
        return ImageVerdict(False, "gore", "vision-model", 10, 2)

    monkeypatch.setattr(images, "check_image", unsafe)
    kook = FakeKook(bot_id="10000")
    ctx = _ctx(kook, moderated=True)
    result = await images.generate_image(ctx, "x")
    assert "审核没通过（gore）" in str(result)
    assert kook.sent == []
    assert ctx.deps.actions == []


async def test_openai_gpt_image_2_generations_and_edits(
    monkeypatch: pytest.MonkeyPatch, snapshot: Callable[..., runtime.Snapshot]
) -> None:
    base = "https://api.openai.com/v1"
    provider = runtime.ProviderInfo(2, "openai", base, "sk-openai", {"cf-aig-authorization": "Bearer cfut_1"}, True, ())
    snapshot(
        providers={2: provider},
        routes={
            "image": runtime.Route(
                "image", 2, "gpt-image-2", {"quality": "medium", "output_format": "webp"}, None, None, {}
            )
        },
    )
    picture = png("green")
    ok = httpx.Response(
        200,
        json={
            "data": [{"b64_json": base64.b64encode(picture).decode()}],
            "usage": {"input_tokens": 25, "output_tokens": 1024},
        },
    )
    routes = Routes({f"{base}/images/generations": ok, f"{base}/images/edits": ok})
    routes.install(monkeypatch)

    # 1) Text-to-image hits /images/generations with native aspect_ratio for gpt-image-2
    async with routes.client() as client:
        gen = await registry.generate_images("一只小猫", aspect_ratio="16:9", client=client)
    assert routes.seen[-1].url.path == "/v1/images/generations"
    assert routes.seen[-1].headers["authorization"] == "Bearer sk-openai"
    assert routes.seen[-1].headers["cf-aig-authorization"] == "Bearer cfut_1"
    assert json.loads(routes.seen[-1].content) == {
        "quality": "medium",
        "output_format": "webp",
        "model": "gpt-image-2",
        "prompt": "一只小猫",
        "n": 1,
        "aspect_ratio": "16:9",
    }
    assert gen.images == [(picture, "image/webp")]
    assert (gen.input_tokens, gen.output_tokens, gen.cost) == (25, 1024, None)

    # 2) Editing with references hits /images/edits as multipart/form-data
    ref_a, ref_b = png("red"), png("blue")
    async with routes.client() as client:
        await registry.generate_images(
            "合并两张图", references=[(ref_a, "image/png"), (ref_b, "image/png")], client=client
        )
    edit_req = routes.seen[-1]
    assert edit_req.url.path == "/v1/images/edits"
    assert "multipart/form-data" in edit_req.headers["content-type"]
    assert b'name="image[]"' in edit_req.content
    assert b"gpt-image-2" in edit_req.content
    assert ref_a in edit_req.content
    assert ref_b in edit_req.content


async def test_openai_legacy_gpt_image_1_maps_aspect_ratio_to_size_and_explicit_api_override(
    monkeypatch: pytest.MonkeyPatch, snapshot: Callable[..., runtime.Snapshot]
) -> None:
    base = "https://gateway.example.com/v1"
    provider = runtime.ProviderInfo(3, "relay", base, "sk-relay", {}, True, ())
    snapshot(
        providers={3: provider},
        routes={"image": runtime.Route("image", 3, "gpt-image-1", {"api": "openai"}, None, None, {})},
    )
    routes = Routes({f"{base}/images/generations": _ok(png())})
    routes.install(monkeypatch)
    async with routes.client() as client:
        await registry.generate_images("风景", aspect_ratio="16:9", client=client)
    body = json.loads(routes.seen[-1].content)
    assert "api" not in body
    assert "aspect_ratio" not in body
    assert body["size"] == "1536x1024"

    # Explicit api="openrouter" works even when base_url does not contain "openrouter"
    snapshot(
        providers={3: provider},
        routes={"image": runtime.Route("image", 3, "openai/gpt-image-2", {"api": "openrouter"}, None, None, {})},
    )
    routes.answers[f"{base}/images"] = _ok(png())
    async with routes.client() as client:
        await registry.generate_images("风景", aspect_ratio="16:9", client=client)
    assert routes.seen[-1].url.path == "/v1/images"
    body = json.loads(routes.seen[-1].content)
    assert "api" not in body
    assert body["aspect_ratio"] == "16:9"
