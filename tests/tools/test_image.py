"""generate_image: the image route's OpenRouter-style /images call, and the picture going to the group as its own
message (moderated like every image the bot sends)."""

import base64
import io
import json
import os
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import nonebot
import pytest
from PIL import Image
from pydantic_ai import RunContext

from tests.support.agent import run_ctx
from tests.support.files import png
from tests.support.qq import FakeQQ
from tests.support.routes import Routes
from yggdrasil import runtime
from yggdrasil.agent import media
from yggdrasil.agent.deps import Deps, Handle
from yggdrasil.agent.tools import images
from yggdrasil.delivery import send
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.llm import registry
from yggdrasil.moderation.image import ImageVerdict
from yggdrasil.qq.model import Message, QQError, Sender, Unconfirmed
from yggdrasil.settings import Settings

GROUP, TRIGGER, SENT = 10001, 555, 77
MODEL = "openai/gpt-image-test"
IMAGES = "https://openrouter.example.com/api/v1/images"


def _ok(picture: bytes, cost: float = 0.04) -> httpx.Response:
    item = {"b64_json": base64.b64encode(picture).decode(), "media_type": "image/png"}
    return httpx.Response(
        200, json={"data": [item], "usage": {"prompt_tokens": 12, "completion_tokens": 4175, "cost": cost}}
    )


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch, snapshot: Callable[..., runtime.Snapshot]) -> Routes:
    """The image route's provider; ``answers[IMAGES]`` is what it draws."""
    provider = runtime.ProviderInfo(1, "or", "https://openrouter.example.com/api/v1", "sk-test", {}, True, ())
    snapshot(
        providers={1: provider}, routes={"image": runtime.Route("image", 1, MODEL, {"quality": "high"}, None, None, {})}
    )
    routes = Routes({IMAGES: _ok(png())})
    routes.install(monkeypatch)
    return routes


def _body(upstream: Routes) -> dict[str, Any]:
    return json.loads(upstream.seen[-1].content)


def _qq(*, timeout: bool = False, fetched: bool = True) -> FakeQQ:
    """QQ confirms the picture as message ``SENT`` (or never does) and, unless ``fetched`` is off, hands it back as
    it may hand our own pictures back: with a ref but an empty url."""
    stored = {"type": "image", "data": {"ref": "res-abc", "url": "", "summary": "[图片]", "sticker": False, "sha1": ""}}
    return FakeQQ(
        {
            "send": Unconfirmed("no answer within 120s") if timeout else SENT,
            "message": (
                (lambda message_id, **_: Message(message_id, GROUP, Sender(10000, ""), 0, [stored]))
                if fetched
                else QQError("message not found")
            ),
        }
    )


@pytest.fixture(autouse=True)
def kept(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """The blob store the bot keeps the pictures it sent in."""
    store: dict[str, bytes] = {}

    async def put(blobs: dict[str, Any]) -> None:
        store.update({sha: b.data for sha, b in blobs.items()})

    async def get(sha: str) -> SimpleNamespace | None:
        return SimpleNamespace(data=store[sha]) if sha in store else None

    monkeypatch.setattr(send, "store_blobs", put)
    monkeypatch.setattr(media, "stored_blob", get)
    return store


def _sent(qq: FakeQQ) -> list[list[dict[str, Any]]]:
    return [call["segments"] for call in qq.called("send")]


def _ctx(qq: FakeQQ, *, moderated: bool = False) -> RunContext[Deps]:
    settings = Settings.model_validate({"moderation": {"enabled": moderated, "check_output": moderated}})
    return run_ctx(
        qq=qq,
        group_id=GROUP,
        user_id=20001,
        settings=settings,
        tool_call_id="call_1",
        trigger_message_id=TRIGGER,
        self_id=10000,
    )


async def test_the_picture_is_sent_replying_to_the_trigger(upstream: Routes, recorded: list[IncomingMessage]):
    qq = _qq()
    ctx = _ctx(qq)
    result = await images.generate_image(ctx, "一只戴墨镜的猫", aspect_ratio="16:9", text="画好了")

    assert upstream.seen[0].url.path == "/api/v1/images"
    assert upstream.seen[0].headers["authorization"] == "Bearer sk-test"
    # The route's params go along; "auto" would leave aspect_ratio out.
    assert _body(upstream) == {
        "quality": "high",
        "model": MODEL,
        "prompt": "一只戴墨镜的猫",
        "n": 1,
        "aspect_ratio": "16:9",
    }

    [message] = _sent(qq)
    assert message[0] == {"type": "reply", "data": {"message_id": TRIGGER}}
    assert message[1] == {"type": "text", "data": {"text": "画好了"}}
    assert message[2]["type"] == "image"
    picture = base64.b64decode(message[2]["data"]["uri"].removeprefix("base64://"))
    assert Image.open(io.BytesIO(picture)).size == (8, 8)

    assert "$0.040" in str(result)
    assert ctx.deps.actions == [f"image {SENT}"]  # lets the run end with mode=done
    # Our own record keeps the picture, so a later "改一下这张图" can load it again although QQ gave no url for it.
    [own] = recorded
    assert own.is_self
    assert own.qq_message_id == SENT
    [segment] = own.segments
    assert (segment["type"], segment["data"]["ref"]) == ("image", "res-abc")
    again = await media.media_bytes(qq, GROUP, 20001, Handle("img#1", "image", segment["data"]), 1 << 20)
    assert again == (picture, "file")


async def test_a_picture_qq_wont_hand_back_is_still_kept(upstream: Routes, recorded: list[IncomingMessage]):
    qq = _qq(fetched=False)
    await images.generate_image(_ctx(qq), "x")
    [own] = recorded
    reply, segment = own.segments
    assert reply == {"type": "reply", "data": {"message_id": TRIGGER}}
    assert (segment["type"], segment["data"]["ref"], segment["data"]["url"]) == ("image", "", "")
    sent = base64.b64decode(_sent(qq)[0][1]["data"]["uri"].removeprefix("base64://"))
    again = await media.media_bytes(qq, GROUP, 20001, Handle("img#1", "image", segment["data"]), 1 << 20)
    assert again == (sent, "file")


async def test_an_unconfirmed_send_counts_as_sent(upstream: Routes, recorded: list[IncomingMessage]):
    # QQ can take longer than the call's timeout to confirm a picture it did post (the first prod drawing): the run
    # must end as done, and the model must not post it again.
    qq = _qq(timeout=True)
    ctx = _ctx(qq)
    result = await images.generate_image(ctx, "x")
    [call] = qq.called("send")
    assert call["reply_timeout"] > nonebot.get_driver().config.api_timeout
    assert "不要重发" in str(result)
    assert ctx.deps.actions == ["image (unconfirmed)"]
    assert recorded == []  # no message id to record it under


async def test_a_large_picture_is_sent_as_jpeg(upstream: Routes, recorded: list[IncomingMessage]):
    noisy = io.BytesIO()
    Image.frombytes("RGB", (512, 512), os.urandom(512 * 512 * 3)).save(noisy, "PNG")  # over the 600 KB send limit
    upstream.answers[IMAGES] = _ok(noisy.getvalue())
    qq = _qq()
    await images.generate_image(_ctx(qq), "x")
    reply, image = _sent(qq)[0]  # no text: the reply to the trigger, then the picture
    assert reply["type"] == "reply"
    sent = base64.b64decode(image["data"]["uri"].removeprefix("base64://"))
    assert sent[:2] == b"\xff\xd8"
    assert len(sent) < len(noisy.getvalue())
    assert Image.open(io.BytesIO(sent)).size == (512, 512)


async def test_reference_images_go_as_data_urls(upstream: Routes):
    blue = png("blue")
    async with upstream.client() as client:
        await registry.generate_images("改成蓝色", references=[(blue, "image/png")], client=client)
    url = _body(upstream)["input_references"][0]["image_url"]["url"]
    assert url == "data:image/png;base64," + base64.b64encode(blue).decode()
    assert "aspect_ratio" not in _body(upstream)


async def test_a_refusal_reaches_the_model_and_nothing_is_sent(upstream: Routes):
    upstream.answers[IMAGES] = httpx.Response(400, json={"error": {"message": "Request rejected by the safety system"}})
    qq = _qq()
    ctx = _ctx(qq)
    result = await images.generate_image(ctx, "x")
    assert "画图失败：400 Request rejected by the safety system" in str(result)
    assert _sent(qq) == []
    assert ctx.deps.actions == []


async def test_an_unsafe_picture_is_not_sent(upstream: Routes, monkeypatch: pytest.MonkeyPatch):
    async def unsafe(_blob: bytes) -> ImageVerdict:
        return ImageVerdict(False, "gore", "vision-model", 10, 2)

    monkeypatch.setattr(images, "check_image", unsafe)
    qq = _qq()
    ctx = _ctx(qq, moderated=True)
    result = await images.generate_image(ctx, "x")
    assert "审核没通过（gore）" in str(result)
    assert _sent(qq) == []
    assert ctx.deps.actions == []
