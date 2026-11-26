"""Pictures in replies: render_* and show() make img#N handles, CardMessage embeds them uploaded to KOOK CDN."""

import base64
import json
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, RetryPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.usage import UsageLimits

from tests.support.agent import run_ctx
from tests.support.files import png
from tests.support.groups import fresh_channel, fresh_guild
from tests.support.kook import FakeKook, make_incoming
from tests.support.routes import Routes
from yggdrasil.agent import media
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.schema import BotReply, Section
from yggdrasil.agent.tools import images as image_tools
from yggdrasil.agent.tools import tools_for
from yggdrasil.agent.tools.attachments import view_image
from yggdrasil.agent.tools.images import check_reply_images
from yggdrasil.config import APP_CONFIG
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.llm.registry import ResolvedModel
from yggdrasil.moderation.guard import Verdict
from yggdrasil.moderation.image import ImageVerdict
from yggdrasil.pipeline import invoke, report
from yggdrasil.sandbox.job_model import Job
from yggdrasil.settings import Settings
from yggdrasil.triggers.index import Resolved

GUILD, CHANNEL, SELF, ALICE = fresh_guild(), fresh_channel(), "10001", "20001"
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
RENDER = "http://render.example.com:8080/render"
CAT, GORE = "https://example.com/cat.png", "https://example.com/gore.png"
UNMODERATED = Settings.model_validate({"moderation": {"enabled": False}})
MODERATED = Settings.model_validate({"moderation": {"enabled": True, "check_output": True}})

type Turn = Callable[[AgentInfo], tuple[str, dict[str, Any]]]


def _render(code: str, caption: str) -> Turn:
    return lambda _info: ("render_mermaid", {"code": code, "caption": caption})


def _answer(*sections: tuple[str, list[str]]) -> Turn:
    body = {"mode": "reply", "title": "图", "sections": [{"content": c, "images": i} for c, i in sections]}
    return lambda info: (info.output_tools[0].name, body)


@pytest.fixture(autouse=True)
def _world(monkeypatch: pytest.MonkeyPatch, recorded: list[IncomingMessage]) -> Routes:
    def draw(request: httpx.Request) -> httpx.Response:
        page = base64.b64encode(png(json.loads(request.content)["source"])).decode()
        return httpx.Response(200, json={"images": [{"png": page, "width": 8, "height": 8}], "warnings": []})

    pictures = {CAT: httpx.Response(200, content=png("orange")), GORE: httpx.Response(200, content=png("black"))}
    routes = Routes({RENDER: draw, **pictures})
    routes.install(monkeypatch)
    monkeypatch.setattr(APP_CONFIG, "render_url", "http://render.example.com:8080/")

    async def moderate(stage: str, *_a: Any, **_k: Any) -> Verdict:
        return Verdict(stage, "Skipped", 0.0)

    async def react(*_a: Any, **_k: Any) -> None:
        return None

    async def store_blobs(*_a: Any, **_k: Any) -> None:
        return None

    from yggdrasil.delivery import send as delivery_send

    monkeypatch.setattr(invoke, "moderate", moderate)
    monkeypatch.setattr(invoke, "react", react)
    monkeypatch.setattr(delivery_send, "store_blobs", store_blobs)
    return routes


class Run:
    def __init__(self, turns: list[Turn], settings: Settings = UNMODERATED) -> None:
        self.kook = FakeKook(bot_id=SELF)
        self.settings = settings
        self.turns = iter(turns)
        self.requests: list[list[ModelMessage]] = []

    async def _stream(self, messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        self.requests.append(messages)
        name, args = next(self.turns)(info)
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args, ensure_ascii=False))}

    def _never(self, _messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise AssertionError("runs stream")

    async def execute(self, inv: invoke.Invocator, deps: Deps) -> str:
        model = FunctionModel(self._never, stream_function=self._stream)
        resolved = ResolvedModel("chat", model, {}, UsageLimits(), "test")
        agent = inv._agent(  # pyright: ignore[reportPrivateUsage]
            resolved, tools=tools_for(["render_mermaid"]), capabilities=[], instructions=""
        )
        return await inv._execute(agent, "画图", deps, resolved, "画图")  # pyright: ignore[reportPrivateUsage]

    async def chat(self, handles: Handles | None = None) -> str:
        m = make_incoming(
            "画图", channel_id=CHANNEL, guild_id=GUILD, user_id=ALICE, self_id=SELF, message_id="m-7", to_me=True
        )
        inv = invoke.Invocator(self.kook, m, CHAT, "chat", "", self.settings, CHANNEL, GUILD)
        return await self.execute(inv, self.deps(handles))

    def deps(self, handles: Handles | None = None) -> Deps:
        return Deps(self.kook, CHANNEL, GUILD, ALICE, self.settings, handles or Handles(), "chat", self_id=SELF)

    def uploaded_colors(self) -> list[str]:
        color_of = {png(c): c for c in ("red", "green", "blue", "orange", "black")}
        return [color_of.get(data, "?") for _, data, _ in self.kook.uploaded]

    def retries(self) -> list[str]:
        return [
            str(p.content)
            for m in self.requests[-1]
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, RetryPromptPart)
        ]


async def test_only_named_renders_are_uploaded_and_embedded(recorded: list[IncomingMessage]) -> None:
    run = Run(
        [
            _render("red", "红"),
            _render("blue", "草稿"),
            _render("green", "绿"),
            _answer(("第一节", ["img#3", "img#1"]), ("第二节", [])),
        ]
    )
    assert await run.chat() == "ok"
    assert run.uploaded_colors() == ["green", "red"]
    assert len(run.kook.sent) == 1
    assert run.kook.sent[0].kind == "card"
    assert run.kook.sent[0].quote == "m-7"
    assert len(recorded) == 1


async def test_chat_pictures_can_be_embedded_and_are_moderated(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[bytes] = []

    async def check(blob: bytes) -> ImageVerdict:
        checked.append(blob)
        return ImageVerdict(blob != png("black"), "gore", "vision-model", 10, 2)

    monkeypatch.setattr(image_tools, "check_image", check)
    handles = Handles()
    handles.add("image", {"url": CAT, "summary": "[图片]"})
    handles.add("image", {"url": GORE, "summary": "[图片]"})
    run = Run([_answer(("就是这只猫", ["img#1", "img#2"]))], settings=MODERATED)
    assert await run.chat(handles) == "ok"
    assert sorted(checked) == sorted([png("orange"), png("black")])
    assert run.uploaded_colors() == ["orange"]


async def test_renders_are_not_screened_when_output_is_moderated(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[bytes] = []

    async def no_vision_route(blob: bytes) -> ImageVerdict:
        checked.append(blob)
        return ImageVerdict(False, "error")

    monkeypatch.setattr(image_tools, "check_image", no_vision_route)
    handles = Handles()
    handles.add("image", {"url": CAT, "summary": "[图片]"})
    run = Run([_render("red", "流程"), _answer(("流程如图", ["img#2", "img#1"]))], settings=MODERATED)
    assert await run.chat(handles) == "ok"
    assert checked == [png("orange")]
    assert run.uploaded_colors() == ["red"]


async def test_unknown_handles_and_too_many_pictures_are_sent_back_to_the_model() -> None:
    handles = Handles()
    for i in range(5):
        handles.add("image", {"url": f"https://example.com/{i}.png"})
    five = [f"img#{i}" for i in range(1, 6)]
    run = Run([_answer(("看图", ["img#9"])), _answer(("看图", five)), _answer(("看图", []))])
    assert await run.chat(handles) == "ok"
    first, second = run.retries()
    assert "img#9" in first
    assert "最多发 4 张图" in second


def test_only_images_each_once_may_be_named() -> None:
    ctx = run_ctx()
    ctx.deps.handles.add("image", {"url": CAT})
    ctx.deps.handles.add("file", {"url": "https://example.com/a.pdf"})
    with pytest.raises(ModelRetry, match="不是图片"):
        check_reply_images(ctx, BotReply(mode="reply", sections=[Section(content="x", images=["file#1"])]))
    twice = [Section(content="x", images=["img#1"]), Section(content="y", images=[" img#1"])]
    with pytest.raises(ModelRetry, match="不止一次"):
        check_reply_images(ctx, BotReply(mode="reply", sections=twice))
    named = check_reply_images(ctx, BotReply(mode="reply", sections=[Section(content="x", images=[" img#1 "])]))
    assert named.sections[0].images == ["img#1"]


async def test_a_picture_made_by_an_earlier_answer_is_read_back_or_reported_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept: dict[str, bytes] = {"sha-kept": png("green")}

    async def stored(sha: str) -> Any:
        return SimpleNamespace(data=kept[sha]) if sha in kept else None

    monkeypatch.setattr(media, "stored_blob", stored)
    ctx = run_ctx(settings=UNMODERATED)
    ctx.deps.handles.add("image", {"blob": "sha-kept", "caption": "旧图", "made": "render"})
    ctx.deps.handles.add("image", {"blob": "sha-gone", "caption": "", "made": "show"})
    reply = BotReply(mode="reply", sections=[Section(content="x", images=["img#1", "img#2"])])
    assert await image_tools.reply_images(ctx.deps, reply) == {"img#1": (png("green"), "旧图")}
    assert await view_image(ctx, "img#2") == "[工具错误] img#2 是之前生成的图，已经找不到了；需要的话重新生成"


async def test_a_finished_jobs_pictures_are_attached_and_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    class Workspace:
        async def read_bytes(self, path: str) -> bytes:
            return {"/workspace/shot.png": png("blue")}[path]

        async def aclose(self) -> None:
            return None

    class Manager:
        configured = True

        async def backend_for(self, _channel_id: str, _name: str) -> tuple[Workspace, str]:
            return Workspace(), "claim-1"

    monkeypatch.setattr(report, "manager", Manager)
    job = Job(
        id="j1",
        kind="agent",
        channel_id=CHANNEL,
        guild_id=GUILD,
        user_id=ALICE,
        trigger_message_id="m-orig-7",
        invocation_id=None,
        request_text="做个网页",
        title="网页",
        spec="做个网页",
        workspace="site",
        claim="claim-1",
        created_at=0.0,
        deadline=0.0,
        status="succeeded",
        images=[{"path": "/workspace/shot.png", "caption": "效果"}],
    )
    run = Run([_answer(("做好了", []))])
    inv = report.ReportInvocator(run.kook, job, GUILD, UNMODERATED)
    deps = run.deps()
    assert await inv._attach_images(deps) == []  # pyright: ignore[reportPrivateUsage]
    assert await run.execute(inv, deps) == "ok"
    assert run.uploaded_colors() == ["blue"]
    assert run.kook.sent[0].quote == "m-orig-7"
