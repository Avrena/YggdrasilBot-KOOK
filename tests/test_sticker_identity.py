"""Which segments are stickers and under what identity, and refreshing an expired sticker link through QQ."""

import httpx
import pytest

from tests.support.qq import FakeQQ
from yggdrasil.stickers import collect
from yggdrasil.stickers.collect import PendingFetch, Sighting, sighting

SHA1 = "AB" * 20


def test_image_stickers_are_known_by_content_sha1_and_market_faces_by_ids():
    image = {"ref": "res-1", "url": "https://example.com/s", "summary": "[动画表情]", "sticker": True, "sha1": SHA1}
    assert sighting({"type": "image", "data": image}) == Sighting(
        "image", SHA1.lower(), "动画表情", url="https://example.com/s", media_ref="res-1"
    )
    assert sighting({"type": "image", "data": image | {"sha1": ""}}) is None  # no stable identity
    assert sighting({"type": "image", "data": image | {"sticker": False}}) is None  # a photo
    face = {"emoji_package_id": 230001, "emoji_id": "5f3c", "key": "k", "summary": "[开心]", "url": ""}
    assert sighting({"type": "market_face", "data": face}) == Sighting(
        "mface", "230001:5f3c", "开心", emoji_id="5f3c", package_id="230001", key="k"
    )


async def test_an_expired_link_is_refreshed_from_the_segment_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = "https://example.com/fresh"
    downloads: list[str] = []

    async def download(url: str, max_bytes: int) -> bytes:
        downloads.append(url)
        if url != fresh:
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("gone", request=request, response=httpx.Response(403, request=request))
        return b"GIF89a"

    qq = FakeQQ({"media_url": fresh})
    monkeypatch.setattr(collect, "download", download)
    monkeypatch.setattr(collect.client, "connected", lambda: qq)
    assert await collect.fetch_image(PendingFetch(1, "https://example.com/old", "res-1")) == b"GIF89a"
    assert downloads == ["https://example.com/old", fresh]
    [call] = qq.called("media_url")
    assert call["segment"]["data"]["ref"] == "res-1"
