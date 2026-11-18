"""The render_* tools as the model calls them: the render service turns Mermaid, Markdown or HTML into pages, each an
img#N handle the reply may embed; rendering alone sends nothing."""

import base64
import json
from typing import Any

import httpx
import pytest
from pydantic_ai import RunContext

from tests.support.agent import run_ctx
from tests.support.files import png
from tests.support.routes import Routes
from yggdrasil.agent.deps import Deps
from yggdrasil.agent.schema import BotReply, Section
from yggdrasil.agent.tools.images import MAX_IMAGES, render_html, render_markdown, render_mermaid, reply_images
from yggdrasil.config import APP_CONFIG
from yggdrasil.settings import Settings
from yggdrasil.tools import render as renderer

BASE = "http://render.example.com:8080"
RENDER = f"{BASE}/render"
COLORS = ("red", "green", "blue")
SEND = "要发就在回复某一节的 images 里写"


def _ok(pages: int, warnings: list[str] | None = None) -> httpx.Response:
    images = [{"png": base64.b64encode(png(c)).decode(), "width": 8, "height": 8} for c in COLORS[:pages]]
    return httpx.Response(200, json={"images": images, "warnings": warnings or []})


def _ctx() -> RunContext[Deps]:
    """A chat run in a group that doesn't moderate output, whose context showed one chat picture (img#1)."""
    ctx = run_ctx(settings=Settings.model_validate({"moderation": {"enabled": False}}))
    ctx.deps.handles.add("image", {"url": "https://example.com/chat.png"})
    return ctx


def _reply(*images: str) -> BotReply:
    return BotReply(mode="forward", sections=[Section(content="正文", images=list(images))])


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Routes:
    """The configured render service; ``answers[RENDER]`` is its answer (one page unless a test says otherwise)."""
    monkeypatch.setattr(APP_CONFIG, "render_url", BASE + "/")
    routes = Routes({RENDER: _ok(1)})
    routes.install(monkeypatch)
    return routes


def _asked(service: Routes) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in service.seen]


async def test_pages_become_handles_in_order_with_captions(service: Routes) -> None:
    service.answers[RENDER] = _ok(3)
    ctx = _ctx()
    result = await render_markdown(ctx, "# long", " 对比 ")
    assert _asked(service) == [{"kind": "markdown", "source": "# long", "max_images": MAX_IMAGES}]
    assert result == f"已生成 img#2、img#3、img#4（共 3 页，对比）。{SEND} img#2、img#3、img#4；不引用就不会发。"
    pictures = await reply_images(ctx.deps, _reply("img#4", "img#2"))
    assert pictures == {"img#4": (png("blue"), "对比（3/3）"), "img#2": (png("red"), "对比")}


async def test_a_single_page_without_caption(service: Routes) -> None:
    ctx = _ctx()
    result = await render_mermaid(ctx, "graph TD\nA-->B")
    assert _asked(service)[0]["max_images"] == 1
    assert result == f"已生成 img#2（无标题）。{SEND} img#2；不引用就不会发。"
    assert await reply_images(ctx.deps, _reply("img#2")) == {"img#2": (png("red"), "")}


async def test_service_warnings_reach_the_model(service: Routes) -> None:
    service.answers[RENDER] = _ok(1, ["第 3 个 mermaid 代码块语法错误，已按代码显示"])
    result = str(await render_markdown(_ctx(), "# x"))
    assert result.endswith("只引用新的那张）：\n- 第 3 个 mermaid 代码块语法错误，已按代码显示")


async def test_an_unreachable_service_is_unavailable(service: Routes) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    service.answers[RENDER] = refuse
    assert "渲染服务暂时不可用" in str(await render_html(_ctx(), "<b>x</b>"))


@pytest.mark.parametrize("source", ["  \n", "x" * (renderer.MAX_SOURCE_CHARS + 1)])
async def test_sources_are_checked_before_the_service(service: Routes, source: str) -> None:
    assert str(await render_markdown(_ctx(), source)).startswith("[工具错误]")
    assert service.seen == []


async def test_without_a_render_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(APP_CONFIG, "render_url", None)
    assert await render_mermaid(_ctx(), "graph TD\nA-->B") == "[工具错误] 没有配置渲染服务"
