"""Tool refs into the chat history (``#<message id>[/<k>]``), resolved the same way by every tool, and refreshing
expired QQ media URLs."""

from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from pydantic_ai import BinaryContent, ToolReturn

from tests.support.agent import run_ctx
from tests.support.db import insert_message, purge_groups
from tests.support.files import png
from tests.support.groups import fresh_group
from tests.support.qq import FakeQQ
from yggdrasil.agent import media
from yggdrasil.agent.deps import Handle
from yggdrasil.agent.tools import attachments
from yggdrasil.agent.tools.base import resolve_ref
from yggdrasil.qq import segments as seg
from yggdrasil.qq.model import ForwardNode, QQError
from yggdrasil.tools.errors import ToolError

GROUP, OTHER = fresh_group(), fresh_group()


def _img(ref: str) -> dict[str, Any]:
    url = f"https://img.example.com/{ref}?rkey=old"
    return {"type": "image", "data": {"ref": ref, "url": url, "summary": "[图片]", "sticker": False, "sha1": ""}}


@pytest.fixture(scope="module")
async def ids(_db_engine: None) -> AsyncIterator[dict[str, int]]:
    """The history rows, stored once for the module: its tests only read them."""
    await purge_groups(GROUP, OTHER)
    forward = {"forward_id": "res-1", "title": "群聊的聊天记录", "preview": [], "summary": "查看1条转发消息"}
    rows = {
        "two": (GROUP, [_img("a"), seg.text("看这个"), _img("b")], False),
        "text": (GROUP, [seg.text("没有图")], False),
        "recalled": (GROUP, [_img("c")], True),
        "other": (OTHER, [_img("d")], False),
        # A forward as QQ hands it over: only its id, the nodes are fetched when it is opened.
        "forward": (GROUP, [{"type": "forward", "data": forward}], False),
        "link": (GROUP, [seg.text("看看 https://example.com/post 这个")], False),
        # A photo sent as a group file (as QQ delivers it: a file segment, not an image).
        "photo_file": (GROUP, [_group_file("photo.jpg")], False),
    }
    yield {
        key: await insert_message(gid, user_id=3, segments=segs, recalled=recalled)
        for key, (gid, segs, recalled) in rows.items()
    }
    await purge_groups(GROUP, OTHER)


@pytest.mark.db
async def test_a_message_ref_picks_the_kth_attachment_of_the_wanted_kind(ids: dict[str, int]) -> None:
    ctx = run_ctx(group_id=GROUP)
    first = await resolve_ref(ctx, f"#{ids['two']}", "image")
    second = await resolve_ref(ctx, f"#{ids['two']}/2", "image")
    assert (first.kind, first.data["ref"]) == ("image", "a")
    assert second.data["ref"] == "b"
    # Registered like any attachment, so the model can keep using the img#N key.
    assert ctx.deps.handles.get(second.key) is second
    assert (await resolve_ref(ctx, f"#{ids['forward']}", "forward")).data["forward_id"] == "res-1"
    # Links in the text count for the link kinds (read_url / read_x).
    assert (await resolve_ref(ctx, f"#{ids['link']}", "url", "card")).data == {"url": "https://example.com/post"}


@pytest.mark.db
async def test_a_handle_of_another_kind_says_what_it_is(ids: dict[str, int]) -> None:
    ctx = run_ctx(group_id=GROUP)
    image = await resolve_ref(ctx, f"#{ids['two']}", "image")
    with pytest.raises(ToolError, match="是图片，不是合并转发"):
        await resolve_ref(ctx, image.key, "forward")


@pytest.mark.db
@pytest.mark.parametrize(
    ("ref", "why"),
    [
        ("other", "没有消息"),
        ("recalled", "没有消息"),
        ("text", "没有图片"),
        ("two/3", "只有 2 个图片"),
        ("img#9", "找不到 img#9"),
        # A bare number is not a message ref (read_x takes bare post ids).
        ("12345", "找不到 12345"),
    ],
)
async def test_refusals_explain_what_is_missing(ids: dict[str, int], ref: str, why: str) -> None:
    key, _, n = ref.partition("/")
    target = f"#{ids[key]}" + (f"/{n}" if n else "") if key in ids else ref
    with pytest.raises(ToolError, match=why):
        await resolve_ref(run_ctx(group_id=GROUP), target, "image")


@pytest.mark.db
async def test_a_forward_from_the_history_opens_by_message_number(ids: dict[str, int]) -> None:
    # The prod failure: a remark replied to a forward that was only in the history; expand_forward("#<id>") said
    # "找不到句柄" because only fwd#N handles were accepted.
    inner = {"type": "forward", "data": {"forward_id": "res-2", "title": "", "preview": [], "summary": ""}}
    forwards = {
        "res-1": [ForwardNode(10001, "转发里的人", 1760000000, [seg.text("节点内容")]), ForwardNode(0, "", 0, [inner])],
        "res-2": [ForwardNode(10002, "里层的人", 1760000000, [seg.text("里层内容")])],
    }
    qq = FakeQQ({"forward": lambda forward_id: forwards[forward_id]})
    result = await attachments.expand_forward(run_ctx(group_id=GROUP, qq=qq), f"#{ids['forward']}")
    # Nested forwards open too, indented under their node.
    assert [c["forward_id"] for c in qq.called("forward")] == ["res-1", "res-2"]
    assert "节点内容" in str(result)
    assert "\n  [" in str(result)
    assert "里层内容" in str(result)


def _group_file(name: str) -> dict[str, Any]:
    return {"type": "file", "data": {"file_id": f"/{name}", "file_name": name, "file_size": 1024}}


def _files() -> FakeQQ:
    """QQ's group files: a download link for each."""
    return FakeQQ({"media_url": lambda segment, **_: f"https://files.example.com{segment['data']['file_id']}?fresh"})


@pytest.fixture
def group_files(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(url: str, max_bytes: int, *, guarded: bool = False) -> bytes:
        return png() if "photo.jpg" in url else b"just some notes\n"

    monkeypatch.setattr(media, "download", fake)
    monkeypatch.setattr(attachments, "sees_images", lambda *_: True)


def _shown_image(result: Any) -> BinaryContent:
    assert isinstance(result, ToolReturn)
    return next(c for c in cast(list[Any], result.content) if isinstance(c, BinaryContent))


@pytest.mark.db
async def test_a_picture_sent_as_a_group_file_can_be_viewed(ids: dict[str, int], group_files: None) -> None:
    # The prod failure: view_image refused file#1 ("是文件，不是图片") and read_file sent the model back to view_image.
    ctx = run_ctx(group_id=GROUP, qq=_files())
    by_number = _shown_image(await attachments.view_image(ctx, f"#{ids['photo_file']}"))
    assert by_number.media_type == "image/png"
    by_handle = _shown_image(await attachments.read_file(ctx, "file#1"))
    assert by_handle.data == by_number.data


async def test_view_image_on_a_file_that_is_not_a_picture_points_to_read_file(group_files: None) -> None:
    ctx = run_ctx(group_id=GROUP, qq=_files())
    key = ctx.deps.handles.add("file", _group_file("notes.txt")["data"])
    assert key is not None
    # Tools hand errors to the model as text.
    assert await attachments.view_image(ctx, key) == "[工具错误] notes.txt 不是图片，用 read_file 读"


def _images(fresh: str | None) -> FakeQQ:
    """QQ's media links: a fresh one for a picture, or none any more."""
    return FakeQQ({"media_url": fresh or QQError("image not found in cache")})


def _expired(url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError("400", request=request, response=httpx.Response(400, request=request))


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def fake(url: str, max_bytes: int, *, guarded: bool = False) -> bytes:
        seen.append(url)
        if "rkey=old" in url:
            raise _expired(url)
        return b"png"

    monkeypatch.setattr(media, "download", fake)
    return seen


async def test_expired_image_url_is_refreshed(downloads: list[str]) -> None:
    qq = _images("https://img.example.com/a?rkey=new")
    h = Handle("img#1", "image", _img("a")["data"])
    assert await media.media_bytes(qq, GROUP, 20001, h, 1024) == (b"png", "file")
    [call] = qq.called("media_url")
    assert (call["segment"]["type"], call["segment"]["data"]["ref"], call["group_id"]) == ("image", "a", GROUP)
    assert downloads == ["https://img.example.com/a?rkey=old", "https://img.example.com/a?rkey=new"]


async def test_expired_image_without_fresh_url_explains(downloads: list[str]) -> None:
    h = Handle("img#1", "image", _img("a")["data"])
    with pytest.raises(ToolError, match="过期"):
        await media.media_bytes(_images(None), GROUP, 20001, h, 1024)


async def test_an_image_link_pillow_cannot_read_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review finding: an SVG from read_url's img#N reached the vision model labelled as JPEG and failed the run.
    async def svg(url: str, max_bytes: int, *, guarded: bool = False) -> bytes:
        return b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'

    monkeypatch.setattr(media, "download", svg)
    h = Handle("img#1", "image", {"url": "https://example.com/badge.svg", "source": "web"})
    with pytest.raises(ToolError, match="不是能识别的图片格式"):
        await media.load_image(_images(None), GROUP, 20001, h)
