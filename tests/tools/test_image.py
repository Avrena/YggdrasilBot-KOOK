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
