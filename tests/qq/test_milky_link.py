"""The Milky link follows its console setting, and only one bot process at a time holds it."""

import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest
from nonebot.adapters.milky.config import ClientInfo
from nonebot.adapters.milky.exception import InvalidAccessToken, NetworkError

from yggdrasil.qq import milky_link
from yggdrasil.qq.milky import Adapter
from yggdrasil.qq.milky_link import Link, Target

URL = "http://yogurt.example:3000"


class FakeAdapter:
    """The adapter as the link drives it: ``ws_connect`` connects a bot (unless unreachable) until cancelled;
    ``call_http`` answers the link's probe."""

    def __init__(self, self_id: int = 10000) -> None:
        self.self_id = self_id
        self.bots: dict[str, Any] = {}
        self.dialed: list[ClientInfo] = []
        self.log: list[str] = []
        self.reachable = True
        self.login_error: Exception | None = None

    async def ws_connect(self, info: ClientInfo) -> None:
        self.dialed.append(info)
        if not self.reachable:
            await asyncio.Event().wait()
        self.bots[str(self.self_id)] = object()
        self.log.append(f"connect {info.access_token}")
        try:
            await asyncio.Event().wait()
        finally:
            self.bots.clear()
            self.log.append(f"disconnect {info.access_token}")

    async def call_http(self, info: ClientInfo, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.login_error is not None:
            raise self.login_error
        return {"uin": self.self_id, "nickname": "测试bot"}


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("WAIT_S", "RENEW_S", "FIRST_PROBE_S", "PROBE_S"):
        monkeypatch.setattr(milky_link, name, 0.01)


def _link(adapter: FakeAdapter) -> Link:
    return Link(cast(Adapter, adapter))


async def _until(check: Callable[[], bool]) -> None:
    for _ in range(300):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def test_the_link_follows_its_setting() -> None:
    yogurt = FakeAdapter()
    link = _link(yogurt)
    try:
        await link.apply(Target(URL, "a"))
        await _until(lambda: link.status().self_id == 10000)
        info = yogurt.dialed[0]
        assert (info.host, info.port, info.secure, info.access_token) == ("yogurt.example", 3000, False, "a")
        assert (link.status().url, link.status().error) == (URL, None)

        await link.apply(Target(URL, "a"))  # unchanged: the connection stays
        await link.apply(Target(URL, "b"))
        await _until(lambda: link.status().self_id is not None and len(yogurt.dialed) == 2)
        assert yogurt.log == ["connect a", "disconnect a", "connect b"]

        await link.apply(None)
        assert yogurt.log[-1] == "disconnect b"
        assert (link.status().url, link.status().self_id, yogurt.bots) == (None, None, {})
    finally:
        await link.close()


async def test_a_second_process_connects_once_the_first_lets_go() -> None:
    old, new = FakeAdapter(), FakeAdapter()
    first, second = _link(old), _link(new)
    try:
        await first.apply(Target(URL, "t"))
        await _until(lambda: first.status().self_id is not None)
        await second.apply(Target(URL, "t"))
        await _until(lambda: second.status().error is not None)
        assert "另一个 bot 进程" in (second.status().error or "")
        assert new.dialed == []

        await first.close()
        await _until(lambda: second.status().self_id is not None)
        assert old.log == ["connect t", "disconnect t"]
        assert second.status().error is None
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize(
    ("error", "needle"),
    [(InvalidAccessToken("401"), "access token"), (NetworkError("HTTP request failed: ConnectError()"), "连不上")],
)
async def test_a_link_that_is_down_says_why(error: Exception, needle: str) -> None:
    yogurt = FakeAdapter()
    yogurt.reachable, yogurt.login_error = False, error
    link = _link(yogurt)
    try:
        await link.apply(Target(URL, None))
        await _until(lambda: link.status().error is not None)
        assert needle in (link.status().error or "")
        assert link.status().self_id is None
    finally:
        await link.close()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://yogurt:3000/", ("http://yogurt:3000", "yogurt", 3000, False)),
        (" https://milky.example.com ", ("https://milky.example.com:443", "milky.example.com", 443, True)),
        ("http://[::1]:3000", ("http://[::1]:3000", "[::1]", 3000, False)),
    ],
)
def test_milky_urls_are_normalized(url: str, expected: tuple[str, str, int, bool]) -> None:
    assert milky_link.parse_url(url) == expected


@pytest.mark.parametrize(
    "url", ["yogurt:3000", "ws://yogurt:3000", "http://yogurt:3000/milky", "http://yogurt:99999", "http://u:p@yogurt"]
)
def test_milky_urls_without_a_bare_host_and_port_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="http"):
        milky_link.parse_url(url)
