"""The tool bridge: workspace code calling the bot's tools and fetching chat attachments over the relay's port."""

import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import fakeredis
import httpx
import pytest

from tests.support.agent import run_ctx
from tests.support.files import png
from tests.support.qq import FakeQQ
from yggdrasil import relay
from yggdrasil.agent import bridge
from yggdrasil.agent import media as media_mod
from yggdrasil.agent.deps import Handles
from yggdrasil.agent.tools import attachments
from yggdrasil.agent.tools.sandbox import sh
from yggdrasil.qq import client as qq_client
from yggdrasil.relay.grants import ToolScope
from yggdrasil.relay.server import Relay
from yggdrasil.runtime import Snapshot
from yggdrasil.sandbox.backend import ExecResult
from yggdrasil.sandbox.job_model import Job, job_key
from yggdrasil.settings import Settings
from yggdrasil.tools import web
from yggdrasil.tools.web.model import ImageLink
from yggdrasil.transcript.capture import ToolRecord, TranscriptRecorder, entries
from yggdrasil.transcript.schema import Entry

GROUP = 10001
URL = "http://relay.test"
PICTURE = "https://img.example.com/cat.png"


@pytest.fixture(autouse=True)
def runtime(snapshot: Callable[..., Snapshot], monkeypatch: pytest.MonkeyPatch) -> FakeQQ:
    snapshot()
    qq = FakeQQ()
    monkeypatch.setattr(qq_client, "connected", lambda: qq)

    async def read(url: str, **_: Any) -> Any:
        raise ImageLink(url)  # read_url registers the picture as an img#N handle

    monkeypatch.setattr(web, "read_url", read)
    return qq


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        app = Relay(http).app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL) as c:
            yield c


async def _token(job_id: str = "job1", tools: tuple[str, ...] = ("read_url", "view_image"), limit: int = 30) -> str:
    scope = ToolScope(tools, 20002, "inv-1", limit)
    return await relay.issue_token(
        group_id=GROUP, job_id=job_id, models=[], max_input_tokens=0, max_output_tokens=0, ttl_s=600, scope=scope
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_refusals(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/tools")).status_code == 401
    bad = await client.post("/v1/tools/read_url", headers=_auth("nope"), json={})
    assert bad.status_code == 401
    assert "令牌" in bad.json()["error"]
    llm_only = await relay.issue_token(
        group_id=GROUP, job_id="job0", models=["m"], max_input_tokens=1, max_output_tokens=1, ttl_s=600
    )
    assert (await client.get("/v1/tools", headers=_auth(llm_only))).status_code == 403

    token = await _token(tools=("read_url",))
    listed = (await client.get("/v1/tools", headers=_auth(token))).json()["tools"]
    assert [t["name"] for t in listed] == ["read_url"]
    assert listed[0]["parameters"]["required"] == ["url_or_handle"]
    assert listed[0]["description"].startswith("读取网页正文")
    unknown = await client.post("/v1/tools/render_markdown", headers=_auth(token), json={})
    assert unknown.status_code == 404
    assert unknown.json()["error"].endswith("代码里能调用的工具：read_url")
    assert (
        await client.post("/v1/tools/view_image", headers=_auth(token), json={"handle": "img#1"})
    ).status_code == 403
    invalid = await client.post("/v1/tools/read_url", headers=_auth(token), json={"url": PICTURE})
    assert invalid.status_code == 422
    assert "url_or_handle" in invalid.json()["error"]
    not_json = await client.post("/v1/tools/read_url", headers=_auth(token), content=b"{oops")
    assert not_json.status_code == 422


async def test_tool_calls_and_media_fetches_share_the_limit(client: httpx.AsyncClient) -> None:
    token = await _token(limit=2)
    ok = await client.post("/v1/tools/read_url", headers=_auth(token), json={"url_or_handle": PICTURE})
    assert ok.status_code == 200
    assert "img#1" in ok.json()["text"]
    missing = await client.post("/v1/media", headers=_auth(token), json={"ref": "img#9"})
    assert missing.status_code == 404
    assert "img#9" in missing.json()["error"]
    over = await client.post("/v1/tools/read_url", headers=_auth(token), json={"url_or_handle": PICTURE})
    assert over.status_code == 429
    assert "2 次" in over.json()["error"]


class _Backend:
    """A workspace whose ``ygg sh run`` executes a script that calls the bridge with the step's bridge.json."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.files: dict[str, bytes] = {}
        self.answers: list[httpx.Response] = []

    async def admit(self, timeout: float | None) -> tuple[float | None, str | None]:  # noqa: ASYNC109
        return timeout, None

    def take_notice(self) -> str | None:
        return None

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def exec(self, argv: list[str], **_: Any) -> ExecResult:
        cfg = json.loads(self.files[f"{argv[3]}/bridge.json"])
        assert cfg["url"] == URL
        self.answers.append(
            await self.client.post("/v1/tools/read_url", headers=_auth(cfg["token"]), json={"url_or_handle": PICTURE})
        )
        result = {"exit": 0, "timed_out": False, "duration": 0.3, "output": "done\n", "truncated": False}
        return ExecResult(0, (json.dumps(result) + "\n").encode(), b"")


class _Session:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend


async def test_a_live_runs_call_lands_in_the_run_and_ends_with_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(relay, "relay_url", lambda: URL)
    backend = _Backend(client)
    ctx = run_ctx(
        group_id=GROUP,
        settings=Settings(),
        sandbox=cast(Any, _Session(backend)),
        tool_call_id="call-sh",
        invocation_id="inv-9",
    )
    started = time.time()
    text = str(await sh(ctx, "python3 fetch.py"))
    [answer] = backend.answers
    assert answer.status_code == 200
    assert "img#1" in answer.json()["text"]
    # The handle the code's call registered is the run's.
    h = ctx.deps.handles.get("img#1")
    assert h is not None
    assert h.data["url"] == PICTURE
    assert text.startswith("[退出码 0")
    assert "（代码里调用了 read_url×1，共 " in text.splitlines()[-1]
    assert ctx.deps.tool_counts["ygg:read_url"] == 1
    assert ctx.deps.tool_counts["read_url"] == 0
    # The transcript keeps the call in full under the sh call that made it.
    recorder = TranscriptRecorder(tools={"call-sh": ToolRecord("sh", started, time.time(), result=text)})
    [entry] = [e for e in entries(recorder, ctx.deps.notes) if e.call_id == "call-sh"]
    assert entry.bridge is not None
    [call] = entry.bridge
    assert (call.name, call.args, call.outcome) == ("read_url", {"url_or_handle": PICTURE}, "ok")
    assert "img#1" in (call.parts[0].text or "")  # pyright: ignore[reportAttributeAccessIssue]

    await bridge.close_run(ctx.deps)
    token = json.loads(next(v for k, v in backend.files.items() if k.endswith("/bridge.json")))["token"]
    after = await client.post("/v1/tools/read_url", headers=_auth(token), json={"url_or_handle": PICTURE})
    assert after.status_code == 401


async def test_a_job_grant_resolves_the_requesting_runs_handles_as_text_and_bytes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    handles = Handles()
    for i in range(3):
        handles.add("image", {"url": f"https://img.example.com/{i}.png"})
    now = time.time()
    job = Job(
        id="job7",
        kind="command",
        group_id=GROUP,
        user_id=20002,
        trigger_message_id=777,
        invocation_id="inv-1",
        request_text="",
        title="训练",
        spec="python train.py",
        workspace="ws",
        claim="c",
        created_at=now,
        deadline=now + 600,
        handles=handles.dump(),
    )
    await fake_redis.hset(job_key(job.id), mapping=job.to_hash())  # pyright: ignore[reportArgumentType, reportGeneralTypeIssues]
    downloads: list[str] = []

    async def download(url: str, max_bytes: int, *, guarded: bool = False) -> bytes:
        downloads.append(url)
        return png("red")

    monkeypatch.setattr(media_mod, "download", download)
    recorded: list[tuple[str, list[Entry], str]] = []

    async def append(job_id: str, items: list[Entry], blobs: Any, *, full: bool, source: str) -> None:
        recorded.append((job_id, items, source))

    monkeypatch.setattr(bridge, "append_job", append)
    token = await _token(job_id=job.id)

    got = await client.post("/v1/media", headers=_auth(token), json={"ref": "img#3"})
    assert got.status_code == 200
    assert got.content == png("red")
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-ygg-file-name"] == "img-3.png"
    assert downloads == ["https://img.example.com/2.png"]
    [(job_id, [entry], source)] = recorded
    assert (job_id, source) == ("job7", "command")
    assert entry.bridge is not None
    assert (entry.bridge[0].name, entry.bridge[0].args) == ("media", {"ref": "img#3"})

    # view_image describes the picture even for a route that sees images: bridge results are text.
    monkeypatch.setattr(attachments, "sees_images", lambda _task: True)

    async def vision(_ctx: Any, images: list[Any], question: str) -> str:
        return f"{question}：一个红色方块"

    monkeypatch.setattr(attachments, "vision", vision)
    seen = await client.post(
        "/v1/tools/view_image", headers=_auth(token), json={"handle": "img#3", "question": "是什么"}
    )
    assert seen.status_code == 200
    assert seen.json()["text"] == "（以下是另一个模型写的图片描述）\n是什么：一个红色方块"
