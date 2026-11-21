"""Pictures in replies go by reference: render_* and show() only make img#N handles, and the forward carries the
handles the reply's sections name, moderated, each right after its section. A finished job's pictures are still
attached whatever the reply names."""

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
from tests.support.groups import fresh_group
from tests.support.qq import FakeQQ, incoming
from tests.support.routes import Routes
from yggdrasil.agent import media
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.schema import BotReply, Section
from yggdrasil.agent.tools import images as image_tools
from yggdrasil.agent.tools import tools_for
from yggdrasil.agent.tools.attachments import view_image
from yggdrasil.agent.tools.images import check_reply_images
from yggdrasil.config import APP_CONFIG
from yggdrasil.delivery import send
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.llm.registry import ResolvedModel
from yggdrasil.moderation.guard import Verdict
from yggdrasil.moderation.image import ImageVerdict
from yggdrasil.pipeline import invoke, report
from yggdrasil.qq.model import Login, Node, Sent
from yggdrasil.sandbox.job_model import Job
from yggdrasil.settings import Settings
from yggdrasil.triggers.index import Resolved

GROUP, SELF, ALICE = fresh_group(), 10001, 20001
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
RENDER = "http://render.example.com:8080/render"
CAT, GORE = "https://example.com/cat.png", "https://example.com/gore.png"
UNMODERATED = Settings.model_validate({"moderation": {"enabled": False}})
MODERATED = Settings.model_validate({"moderation": {"enabled": True, "check_output": True}})

type Turn = Callable[[AgentInfo], tuple[str, dict[str, Any]]]  # the tool the model calls and its arguments


def _render(code: str, caption: str) -> Turn:
    return lambda _info: ("render_mermaid", {"code": code, "caption": caption})


def _answer(*sections: tuple[str, list[str]]) -> Turn:
    body = {"mode": "forward", "title": "图", "sections": [{"content": c, "images": i} for c, i in sections]}
    return lambda info: (info.output_tools[0].name, body)


@pytest.fixture(autouse=True)
def _world(monkeypatch: pytest.MonkeyPatch, recorded: list[IncomingMessage]) -> Routes:
    """The render service draws a page in the color its source names; the web has a cat and a gore picture; text
    moderation passes everything and reactions go nowhere."""

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

    monkeypatch.setattr(invoke, "moderate", moderate)
    monkeypatch.setattr(invoke, "react", react)
    return routes


class Run:
    """A chat run in a group whose model plays ``turns`` in order and sees every request."""

    def __init__(self, turns: list[Turn], settings: Settings = UNMODERATED) -> None:
        self.qq = FakeQQ({"login": Login(SELF, "Yggdrasil"), "send_forward": Sent(41, "res")}, self_id=SELF)
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
        m = incoming("画图", group_id=GROUP, user_id=ALICE, self_id=SELF, message_id=7, to_me=True)
        inv = invoke.Invocator(self.qq, m, CHAT, "chat", "", self.settings, GROUP)
        return await self.execute(inv, self.deps(handles))

    def deps(self, handles: Handles | None = None) -> Deps:
        return Deps(self.qq, GROUP, ALICE, self.settings, handles or Handles(), "chat", self_id=SELF)

    def nodes(self) -> list[str]:
        """The sent forward's nodes: text as is, a picture as "<caption>:<color>" (pictures are one-color PNGs)."""
        [call] = self.qq.called("send_forward")
        return [_node(n) for n in call["nodes"]]

    def retries(self) -> list[str]:
        return [
            str(p.content)
            for m in self.requests[-1]
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, RetryPromptPart)
        ]


_COLORS = {png(c): c for c in ("red", "green", "blue", "orange", "black")}


def _node(n: Node) -> str:
    text = "".join(str(s["data"].get("text", "")) for s in n.segments if s["type"] == "text")
    image = next((s for s in n.segments if s["type"] == "image"), None)
    if image is None:
        return text
    data = base64.b64decode(str(image["data"]["uri"]).removeprefix("base64://"))
    return f"{text.strip()}:{_COLORS.get(data, '?')}"


async def test_only_named_renders_go_out_each_right_after_its_section(recorded: list[IncomingMessage]) -> None:
    run = Run(
        [
            _render("red", "红"),
            _render("blue", "草稿"),  # rendered by mistake: never named, never sent
            _render("green", "绿"),
            _answer(("第一节", ["img#3", "img#1"]), ("第二节", [])),
        ]
    )
    assert await run.chat() == "ok"
    nodes = run.nodes()
    assert nodes[:4] == ["第一节", "绿:green", "红:red", "第二节"]
    assert len(nodes) == 5  # and the metadata
    assert not any(n.endswith(":blue") for n in nodes)
    # Our record of the forward keeps the pictures as placeholders.
    [record] = recorded
    stored = [" ".join(s["data"]["text"].strip() for s in n["segments"]) for n in record.segments[0]["data"]["nodes"]]
    assert stored == ["第一节", "绿 [图片]", "红 [图片]", "第二节"]


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
    assert await run.chat(handles) == "ok"  # the refused picture is left out, the reply still goes
    assert sorted(checked) == sorted([png("orange"), png("black")])
    assert run.nodes()[:2] == ["就是这只猫", ":orange"]
    assert len(run.nodes()) == 3


async def test_renders_are_not_screened_when_the_group_moderates_output(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[bytes] = []

    async def no_vision_route(blob: bytes) -> ImageVerdict:  # moderation fails closed
        checked.append(blob)
        return ImageVerdict(False, "error")

    monkeypatch.setattr(image_tools, "check_image", no_vision_route)
    handles = Handles()
    handles.add("image", {"url": CAT, "summary": "[图片]"})
    run = Run([_render("red", "流程"), _answer(("流程如图", ["img#2", "img#1"]))], settings=MODERATED)
    assert await run.chat(handles) == "ok"
    assert checked == [png("orange")]  # only the chat picture was screened (and refused)
    assert run.nodes()[:2] == ["流程如图", "流程:red"]
    assert len(run.nodes()) == 3


async def test_unknown_handles_and_too_many_pictures_are_sent_back_to_the_model() -> None:
    handles = Handles()
    for i in range(5):
        handles.add("image", {"url": f"https://example.com/{i}.png"})
    five = [f"img#{i}" for i in range(1, 6)]
    run = Run([_answer(("看图", ["img#9"])), _answer(("看图", five)), _answer(("看图", []))])
    assert await run.chat(handles) == "ok"
    first, second = run.retries()
    assert "img#9" in first
    assert "只能写工具结果或聊天记录里出现过的 img#N" in first
    assert "最多发 4 张图" in second
    assert run.nodes()[0] == "看图"


def _plain(text: str, images: list[str] | None = None) -> Turn:
    body = {"mode": "plain", "sections": [{"content": text, "images": images or []}]}
    return lambda info: (info.output_tools[0].name, body)


async def test_a_plain_answer_is_one_ordinary_message_with_its_pictures(
    monkeypatch: pytest.MonkeyPatch, recorded: list[IncomingMessage]
) -> None:
    kept: list[dict[str, Any]] = []

    async def store_blobs(blobs: dict[str, Any]) -> None:
        kept.append(blobs)

    monkeypatch.setattr(send, "store_blobs", store_blobs)
    handles = Handles()
    handles.add("image", {"url": CAT, "summary": "[图片]"})
    run = Run([_plain("**就是**这只猫", ["img#1"])])
    run.qq.answers["send"] = 42
    run.settings = Settings.model_validate({"moderation": {"enabled": False}, "interaction": {"quote_reply": "always"}})
    assert await run.chat(handles) == "ok"
    assert run.qq.called("send_forward") == []
    [sent] = run.qq.called("send")
    segments = sent["segments"]
    assert [s["type"] for s in segments] == ["reply", "text", "image"]  # quotes the request, no metadata
    assert segments[0]["data"]["message_id"] == 7
    assert segments[1]["data"]["text"] == "就是这只猫"
    assert base64.b64decode(segments[2]["data"]["uri"].removeprefix("base64://")) == png("orange")
    assert [r.qq_message_id for r in recorded] == [42]
    assert len(kept) == 1  # the picture is kept for a later reply to this message


async def test_a_long_plain_answer_goes_back_to_the_model() -> None:
    run = Run([_plain("太长了。" * 40), _answer(("还是写成一篇", []))])
    assert await run.chat() == "ok"
    [retry] = run.retries()
    assert "最多 120 字" in retry
    assert run.nodes()[0] == "还是写成一篇"


async def test_tldr_cannot_answer_plain() -> None:
    run = Run([_plain("一句话"), _answer(("总结", []))])
    m = incoming("tldr", group_id=GROUP, user_id=ALICE, self_id=SELF, message_id=7, to_me=True)
    inv = invoke.Invocator(run.qq, m, CHAT, "tldr", "", run.settings, GROUP)
    assert await run.execute(inv, run.deps()) == "ok"
    assert run.retries() == ["这里不能用 mode=plain，用 forward"]
    assert run.nodes()[0] == "总结"


def test_only_images_each_once_may_be_named() -> None:
    ctx = run_ctx()
    ctx.deps.handles.add("image", {"url": CAT})
    ctx.deps.handles.add("forward", {"forward_id": "f1"})
    with pytest.raises(ModelRetry, match="不是图片"):
        check_reply_images(ctx, BotReply(mode="forward", sections=[Section(content="x", images=["fwd#1"])]))
    twice = [Section(content="x", images=["img#1"]), Section(content="y", images=[" img#1"])]
    with pytest.raises(ModelRetry, match="不止一次"):
        check_reply_images(ctx, BotReply(mode="forward", sections=twice))
    named = check_reply_images(ctx, BotReply(mode="forward", sections=[Section(content="x", images=[" img#1 "])]))
    assert named.sections[0].images == ["img#1"]


async def test_a_picture_made_by_an_earlier_answer_is_read_back_or_reported_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that continues an answer gets its handles back, but not the bytes of the images it made."""
    kept: dict[str, bytes] = {"sha-kept": png("green")}

    async def stored(sha: str) -> Any:
        return SimpleNamespace(data=kept[sha]) if sha in kept else None

    monkeypatch.setattr(media, "stored_blob", stored)
    ctx = run_ctx(settings=UNMODERATED)
    ctx.deps.handles.add("image", {"blob": "sha-kept", "caption": "旧图", "made": "render"})
    ctx.deps.handles.add("image", {"blob": "sha-gone", "caption": "", "made": "show"})
    reply = BotReply(mode="forward", sections=[Section(content="x", images=["img#1", "img#2"])])
    assert await image_tools.reply_images(ctx.deps, reply) == {"img#1": (png("green"), "旧图")}
    assert await view_image(ctx, "img#2") == "[工具错误] img#2 是之前生成的图，已经找不到了；需要的话重新生成"


async def test_a_finished_jobs_pictures_are_attached_after_the_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    class Workspace:
        async def read_bytes(self, path: str) -> bytes:
            return {"/workspace/shot.png": png("blue")}[path]

        async def aclose(self) -> None:
            return None

    class Manager:
        configured = True

        async def backend_for(self, _group_id: int, _name: str) -> tuple[Workspace, str]:
            return Workspace(), "claim-1"

    monkeypatch.setattr(report, "manager", Manager)
    job = Job(
        id="j1",
        kind="agent",
        group_id=GROUP,
        user_id=ALICE,
        trigger_message_id=7,
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
    inv = report.ReportInvocator(run.qq, job, UNMODERATED)
    deps = run.deps()
    assert await inv._attach_images(deps) == []  # pyright: ignore[reportPrivateUsage]
    assert await run.execute(inv, deps) == "ok"
    assert run.nodes()[:2] == ["做好了", "效果:blue"]
