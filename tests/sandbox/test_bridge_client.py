"""The in-sandbox bridge client (`ygg.tool` / `ygg.tools` / `ygg.media`, `ygg tool` / `ygg media`) against a local
stub of the bot's bridge; runs on the host, no sandboxd needed."""

import json
import os
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sandbox" / "lib"))

import ygg
from ygg import _bridge, _cli, _kernel

ENV_KEYS = ("YGG_BRIDGE_URL", "YGG_BRIDGE_TOKEN", "YGG_CELL_DIR")


class Stub:
    """The bridge's HTTP API: canned answers per (method, path), every request recorded."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, dict[str, str], bytes, float]] = {}
        self.requests: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "json": json.loads(body) if body else None,
                    }
                )
                status, headers, payload, delay = stub.routes.get(
                    (self.command, self.path), (404, {}, b'{"error": "unknown"}', 0.0)
                )
                time.sleep(delay)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve

            def log_message(self, *_: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def json(self, method: str, path: str, obj: Any, status: int = 200, delay: float = 0.0) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.routes[(method, path)] = (status, {"Content-Type": "application/json"}, body, delay)

    def raw(self, method: str, path: str, body: bytes, headers: dict[str, str], status: int = 200) -> None:
        self.routes[(method, path)] = (status, headers, body, 0.0)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    monkeypatch.setattr(ygg, "_cell", None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def stub() -> Iterator[Stub]:
    s = Stub()
    yield s
    s.server.shutdown()
    s.server.server_close()


@pytest.fixture
def bridge(stub: Stub) -> Stub:
    """A stub the environment points at (token ``tok``)."""
    os.environ["YGG_BRIDGE_URL"] = stub.url + "/"
    os.environ["YGG_BRIDGE_TOKEN"] = "tok"
    return stub


@pytest.fixture
def in_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "in"
    monkeypatch.setattr(_bridge, "IN_DIR", str(d))
    return d


def _write_bridge_json(directory: Path, url: str, token: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bridge.json").write_text(json.dumps({"url": url, "token": token}))


def _cli_run(capsysbinary: pytest.CaptureFixture[bytes], *args: str) -> tuple[int, str, str]:
    code = _cli.main(["/opt/ygg/ygg", *args])
    out, err = capsysbinary.readouterr()
    return code, out.decode(), err.decode()


# --- Python API ------------------------------------------------------------------------------------------------


def test_tool_posts_arguments_and_returns_the_text(bridge: Stub) -> None:
    bridge.json("POST", "/v1/tools/web_search", {"text": "第一条\n第二条"})
    text = ygg.tool("web_search", query="猫", count=3)
    assert text == "第一条\n第二条"
    assert repr(text) == "第一条\n第二条"  # shows as itself as a cell's last expression
    assert bridge.requests == [
        {"method": "POST", "path": "/v1/tools/web_search", "auth": "Bearer tok", "json": {"query": "猫", "count": 3}}
    ]


def test_a_tool_argument_may_be_called_name(bridge: Stub) -> None:
    bridge.json("POST", "/v1/tools/lookup_glossary", {"text": "ok"})
    assert ygg.tool("lookup_glossary", name="术语") == "ok"
    assert bridge.requests[0]["json"] == {"name": "术语"}


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (429, {"error": "本次对话的工具调用已达上限（30 次）"}, "本次对话的工具调用已达上限（30 次）"),
        (422, {"error": "query: 缺少必填参数"}, "query: 缺少必填参数"),
        (401, {"error": "凭证无效或已过期"}, "凭证无效或已过期"),
    ],
)
def test_refusals_raise_with_the_servers_message(bridge: Stub, status: int, body: dict[str, str], message: str) -> None:
    bridge.json("POST", "/v1/tools/web_search", body, status=status)
    with pytest.raises(ygg.BridgeError) as e:
        ygg.tool("web_search", query="x")
    assert str(e.value) == message
    assert e.value.status == status


def test_a_non_json_error_still_reports_status_and_body(bridge: Stub) -> None:
    bridge.raw("POST", "/v1/tools/web_search", b"upstream exploded", {"Content-Type": "text/plain"}, status=502)
    with pytest.raises(ygg.BridgeError, match=r"HTTP 502.*upstream exploded"):
        ygg.tool("web_search", query="x")


def test_tools_lists_the_allowed_tools(bridge: Stub) -> None:
    listed = [{"name": "web_search", "description": "搜索", "parameters": {"type": "object"}}]
    bridge.json("GET", "/v1/tools", {"tools": listed})
    assert ygg.tools() == listed
    assert bridge.requests[0]["auth"] == "Bearer tok"


def test_without_credentials_the_error_says_so() -> None:
    with pytest.raises(ygg.BridgeError, match="没有可用的工具桥"):
        ygg.tool("web_search", query="x")


def test_unreachable_bridge(monkeypatch: pytest.MonkeyPatch, stub: Stub) -> None:
    url = stub.url
    stub.server.shutdown()
    stub.server.server_close()
    monkeypatch.setenv("YGG_BRIDGE_URL", url)
    monkeypatch.setenv("YGG_BRIDGE_TOKEN", "tok")
    with pytest.raises(ygg.BridgeError, match="连不上工具桥") as e:
        ygg.tool("web_search", query="x")
    assert e.value.status is None


def test_slow_bridge_times_out(bridge: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_bridge, "TIMEOUT", 0.3)
    bridge.json("POST", "/v1/tools/read_url", {"text": "late"}, delay=1.5)
    with pytest.raises(ygg.BridgeError, match="秒内没有回应"):
        ygg.tool("read_url", url="https://example.com")


def test_credentials_from_env_win_over_the_cell_file(stub: Stub, tmp_path: Path) -> None:
    stub.json("POST", "/v1/tools/recent_chat", {"text": "ok"})
    cell = tmp_path / "cell"
    _write_bridge_json(cell, stub.url, "from-file")
    os.environ["YGG_CELL_DIR"] = str(cell)
    ygg.tool("recent_chat")
    os.environ["YGG_BRIDGE_TOKEN"] = "from-env"  # incomplete env: still the file
    ygg.tool("recent_chat")
    os.environ["YGG_BRIDGE_URL"] = stub.url
    ygg.tool("recent_chat")
    assert [r["auth"] for r in stub.requests] == ["Bearer from-file", "Bearer from-file", "Bearer from-env"]


def test_media_writes_the_servers_name_and_never_overwrites(bridge: Stub, in_dir: Path) -> None:
    name = urllib.parse.quote("报告 v2.pdf")
    bridge.raw("POST", "/v1/media", b"%PDF-1", {"Content-Type": "application/pdf", "X-Ygg-File-Name": name})
    first = ygg.media("file#2")
    second = ygg.media("file#2")
    assert first == in_dir / "报告 v2.pdf"
    assert second == in_dir / "报告 v2-2.pdf"
    assert first.read_bytes() == second.read_bytes() == b"%PDF-1"
    assert bridge.requests[0]["json"] == {"ref": "file#2"}
    assert bridge.requests[0]["auth"] == "Bearer tok"


def test_media_destinations(bridge: Stub, tmp_path: Path, in_dir: Path) -> None:
    headers = {"Content-Type": "image/png", "X-Ygg-File-Name": urllib.parse.quote("../../evil.png")}
    bridge.raw("POST", "/v1/media", b"png", headers)
    assert ygg.media("img#3") == in_dir / "evil.png"  # only the last path component of the server's name
    out = tmp_path / "out"
    out.mkdir()
    assert ygg.media("img#3", out) == out / "evil.png"
    assert ygg.media("img#3", str(out)) == out / "evil-2.png"
    exact = tmp_path / "deep" / "cat.png"
    assert ygg.media("img#3", exact) == exact
    assert ygg.media("img#3", exact) == exact  # an explicit file path is overwritten
    assert exact.read_bytes() == b"png"
    bridge.raw("POST", "/v1/media", b"x", {"Content-Type": "application/octet-stream"})
    assert ygg.media("#1234/2") == in_dir / "1234-2"  # no name from the server: one from the reference


def test_media_error(bridge: Stub, in_dir: Path) -> None:
    bridge.json("POST", "/v1/media", {"error": "img#9 不存在"}, status=404)
    with pytest.raises(ygg.BridgeError, match="img#9 不存在"):
        ygg.media("img#9")
    assert not in_dir.exists()


# --- py cells -------------------------------------------------------------------------------------------------


def test_begin_exports_the_cells_credentials_and_clears_stale_ones(tmp_path: Path) -> None:
    with_bridge, without = tmp_path / "c1", tmp_path / "c2"
    _write_bridge_json(with_bridge, "http://10.43.255.90:8090", "t1")
    without.mkdir()
    ygg._begin(str(with_bridge))
    assert os.environ["YGG_CELL_DIR"] == str(with_bridge)
    assert os.environ["YGG_BRIDGE_URL"] == "http://10.43.255.90:8090"
    assert os.environ["YGG_BRIDGE_TOKEN"] == "t1"
    ygg._begin(str(without))
    assert os.environ["YGG_CELL_DIR"] == str(without)
    assert "YGG_BRIDGE_URL" not in os.environ
    assert "YGG_BRIDGE_TOKEN" not in os.environ


def test_kernel_preloads_tool_and_media() -> None:
    ns: dict[str, Any] = {}
    for line in _kernel.PRELOAD:
        exec(line, ns)
    assert ns["tool"] is ygg.tool
    assert ns["media"] is ygg.media
    assert ns["ygg"].BridgeError is ygg.BridgeError


# --- CLI ------------------------------------------------------------------------------------------------------


def test_cli_tool_coerces_json_values(bridge: Stub, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    bridge.json("POST", "/v1/tools/search_history", {"text": "找到 2 条"})
    code, out, err = _cli_run(
        capsysbinary,
        "tool",
        "search_history",
        "query=猫 狗",
        "limit=5",
        "exact=true",
        "since=null",
        "ids=[1, 2]",
        'quoted="42"',
        "broken={x",
    )
    assert (code, out, err) == (0, "找到 2 条\n", "")
    assert bridge.requests[0]["json"] == {
        "query": "猫 狗",
        "limit": 5,
        "exact": True,
        "since": None,
        "ids": [1, 2],
        "quoted": "42",
        "broken": "{x",
    }


def test_cli_tool_json_object_and_overrides(bridge: Stub, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    bridge.json("POST", "/v1/tools/web_search", {"text": "done\n"})
    code, out, _ = _cli_run(capsysbinary, "tool", "web_search", "--json", '{"query": "a=b", "count": 1}', "count=2")
    assert (code, out) == (0, "done\n")
    assert bridge.requests[0]["json"] == {"query": "a=b", "count": 2}


def test_cli_tool_error_exits_1_with_the_message(bridge: Stub, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    bridge.json("POST", "/v1/tools/web_search", {"error": "本次任务的工具调用已达上限（120 次）"}, status=429)
    code, out, err = _cli_run(capsysbinary, "tool", "web_search", "query=x")
    assert (code, out) == (1, "")
    assert err == "ygg: 本次任务的工具调用已达上限（120 次）\n"


def test_cli_tool_list(bridge: Stub, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["query"],
    }
    bridge.json(
        "GET",
        "/v1/tools",
        {
            "tools": [
                {"name": "web_search", "description": "搜索网页。\n\n返回摘要。", "parameters": schema},
                {"name": "recent_chat", "description": "", "parameters": {"type": "object", "properties": {}}},
            ]
        },
    )
    code, out, _ = _cli_run(capsysbinary, "tool", "--list")
    assert code == 0
    assert out == "web_search(query: string, count?: integer)\n  搜索网页。\n  返回摘要。\n\nrecent_chat()\n"


@pytest.mark.parametrize(
    "args",
    [
        ["tool"],
        ["tool", "web_search", "no-equals-sign"],
        ["tool", "web_search", "=x"],
        ["tool", "web_search", "--json", "[1]"],
        ["tool", "web_search", "--json", "{bad"],
        ["tool", "--list", "web_search"],
        ["media"],
    ],
)
def test_cli_usage_errors_exit_2(args: list[str], capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    with pytest.raises(SystemExit) as e:
        _cli.main(["/opt/ygg/ygg", *args])
    assert e.value.code == 2
    assert "ygg tool <name>" in capsysbinary.readouterr().err.decode()


def test_cli_media_prints_the_path(
    bridge: Stub, in_dir: Path, tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    bridge.raw("POST", "/v1/media", b"gif", {"Content-Type": "image/gif", "X-Ygg-File-Name": "a.gif"})
    code, out, _ = _cli_run(capsysbinary, "media", "img#1")
    assert (code, out) == (0, f"{in_dir / 'a.gif'}\n")
    target = tmp_path / "x.gif"
    code, out, _ = _cli_run(capsysbinary, "media", "img#1", "-o", str(target))
    assert (code, out) == (0, f"{target}\n")
    assert target.read_bytes() == b"gif"


def test_cli_media_error(bridge: Stub, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    bridge.json("POST", "/v1/media", {"error": "fwd#1 不是图片或文件"}, status=422)
    code, out, err = _cli_run(capsysbinary, "media", "fwd#1")
    assert (code, out, err) == (1, "", "ygg: fwd#1 不是图片或文件\n")
