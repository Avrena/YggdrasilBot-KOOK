import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent, BinaryContent, RunContext, ToolReturn
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.workspaces.local import LocalWorkspaceBackend

from tests.support.agent import run_ctx
from tests.support.files import png
from yggdrasil.agent.deps import Deps
from yggdrasil.agent.media import safe_name
from yggdrasil.agent.schema import BotReply, Section
from yggdrasil.agent.tools import sandbox as sandbox_tools
from yggdrasil.agent.tools import suite_enabled, tool_names, tools_for
from yggdrasil.agent.tools.images import reply_images
from yggdrasil.agent.tools.sandbox import format_py, format_sh, py, sandbox_put, sh
from yggdrasil.moderation.image import parse_answer
from yggdrasil.sandbox import SandboxError
from yggdrasil.sandbox.backend import ExecResult
from yggdrasil.sandbox.state import Link
from yggdrasil.settings import Settings, ToolSettings


def _settings(**moderation: Any) -> Settings:
    return Settings.model_validate({"moderation": moderation})


def test_idle_must_outlast_invocation_budget():
    ToolSettings(sandbox_idle_min=11, sandbox_invocation_s=600)
    with pytest.raises(ValidationError, match="sandbox_idle_min"):
        ToolSettings(sandbox_idle_min=10, sandbox_invocation_s=600)
    with pytest.raises(ValidationError, match="sandbox_command_max_s"):
        ToolSettings(sandbox_command_timeout_s=200, sandbox_command_max_s=100)


def test_suite_only_for_group_chat_with_every_switch_on():
    on, off = Settings(), Settings.model_validate({"tools": {"sandbox_enabled": False}})
    assert suite_enabled(on, "chat", 1, global_settings=on, configured=True)
    assert not suite_enabled(on, "tldr", 1, global_settings=on, configured=True)
    assert not suite_enabled(on, "chat", None, global_settings=on, configured=True)
    assert not suite_enabled(on, "chat", 1, global_settings=on, configured=False)
    # The kill switch is read from the global settings, a group override cannot turn it back on.
    assert not suite_enabled(on, "chat", 1, global_settings=off, configured=True)
    no_sandbox = Settings.model_validate({"tools": {"enabled": ["web_search"]}})
    assert not suite_enabled(no_sandbox, "chat", 1, global_settings=on, configured=True)


def test_plain_tool_lists_never_carry_the_pseudo_name():
    enabled = Settings().tools.enabled
    assert "sandbox" in enabled
    assert "sandbox" not in tool_names(enabled, "chat")
    assert "sandbox" not in tool_names(enabled, "tldr")


def test_safe_names():
    assert safe_name("../../报表 2024.xlsx", "file1") == "报表_2024.xlsx"
    assert safe_name("C:\\tmp\\a b.txt", "file1") == "a_b.txt"
    assert safe_name("...", "file1") == "file1"


def test_image_moderation_answer_fails_closed():
    assert parse_answer("SAFE") == (True, "")
    assert parse_answer("UNSAFE: gore") == (False, "gore")
    assert parse_answer("unsafe") == (False, "unspecified")
    assert parse_answer("这张图没问题") == (False, "error")


# --- result formatting ------------------------------------------------------------


def test_sh_result_points_at_the_full_log_when_truncated():
    text = format_sh(
        {
            "exit": 1,
            "timed_out": False,
            "duration": 0.42,
            "output": "head\n…\ntail\n",
            "output_bytes": 123456,
            "truncated": True,
            "out_path": "/workspace/.ygg/sh/x/out",
        },
        60,
    )
    assert text.startswith("[退出码 1，用时 0.4 秒]\nhead\n…\ntail")
    assert "123456 字节" in text
    assert "/workspace/.ygg/sh/x/out" in text
    assert "ws_read_file" in text


def test_sh_timeout_and_signal_and_empty_output():
    timed_out = format_sh({"exit": None, "timed_out": True, "duration": 5.0, "output": ""}, 5)
    assert timed_out == "[超时：运行超过 5 秒，已终止]\n（没有输出）"
    killed = format_sh({"exit": None, "timed_out": False, "duration": 1.0, "output": "x"}, 5)
    assert killed.startswith("[被信号终止")


def _py_result(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "ok",
        "kernel": {"pid": 77, "started": False, "previous": "alive"},
        "output": "",
        "output_truncated": False,
        "result": None,
        "error": None,
        "images": [],
        "images_dropped": 0,
        "effects": [],
        "duration": 0.05,
    }
    return {**base, **kw}


def test_py_result_shows_output_value_and_error():
    ok = format_py(_py_result(output="hi\n", result="42"), 60, restarted=False)
    assert ok == "[完成，用时 0.1 秒]\nhi\n结果：42"
    err = format_py(
        _py_result(
            status="error",
            error={
                "ename": "ZeroDivisionError",
                "evalue": "division by zero",
                "traceback": "Traceback…\nZeroDivisionError: division by zero",
            },
        ),
        60,
        restarted=False,
    )
    assert err.startswith("[出错")
    assert err.endswith("ZeroDivisionError: division by zero")
    assert err.count("division by zero") == 1
    assert format_py(_py_result(), 60, restarted=False).endswith("（没有输出）")


def test_py_result_explains_kernel_loss():
    fresh = format_py(_py_result(kernel={"pid": 5, "started": True, "previous": "dead"}), 60, restarted=False)
    assert "变量和导入都没了" in fresh.splitlines()[0]
    first = format_py(_py_result(kernel={"pid": 5, "started": True, "previous": "none"}), 60, restarted=False)
    assert "变量" not in first
    assert "已按要求重启" in format_py(
        _py_result(kernel={"pid": 5, "started": True, "previous": "none"}), 60, restarted=True
    )
    assert "已中断" in format_py(_py_result(status="timeout", duration=60.0), 60, restarted=False)
    died = format_py(
        _py_result(
            status="died",
            kernel={"pid": None, "started": False, "previous": "alive"},
            error={"ename": "KernelDied", "evalue": "超时后中断 5 秒内没有响应，kernel 已被杀掉", "traceback": ""},
        ),
        60,
        restarted=False,
    )
    assert died == (
        "[kernel 已终止，变量和导入都没了，下次 py 会启动新的 kernel；原因见下面的报错]\n"
        "KernelDied: 超时后中断 5 秒内没有响应，kernel 已被杀掉"
    )


# --- the tools against a fake workspace ----------------------------------------------


class FakeBackend:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.files: dict[str, bytes] = {}
        self.calls: list[list[str]] = []
        self.audits: list[str] = []
        self.notice: str | None = "[工作区] 这是新建的空工作区。"

    async def admit(self, timeout: float | None) -> tuple[float | None, str | None]:  # noqa: ASYNC109
        assert timeout is not None
        return min(timeout, 100.0), "[工作区] 截到 100 秒" if timeout > 100 else None

    def take_notice(self) -> str | None:
        notice, self.notice = self.notice, None
        return notice

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def exists(self, path: str) -> bool:
        return path in self.files

    async def exec(self, argv: list[str], *, seconds: float = 60, audit: str | None = None, **_: Any) -> ExecResult:
        self.calls.append(list(argv))
        if audit is not None:
            self.audits.append(audit)
        if argv[1:3] == ["py", "restart"]:
            return ExecResult(0, b'{"killed": true}\n', b"")
        return ExecResult(0, (json.dumps(self.result) + "\n").encode(), b"")


class FakeSession:
    def __init__(self, result: dict[str, Any], files: dict[str, bytes] | None = None) -> None:
        self.backend = FakeBackend(result)
        self.files = files or {}
        self.kernel: list[int | None] = []
        self.published: list[int] = []

    async def get_bytes(self, path: str, *, max_bytes: int) -> bytes:
        if path not in self.files:
            raise SandboxError(f"读取失败：文件不存在 {path}")
        return self.files[path]

    async def require(self) -> str:
        return "ws"

    async def put_bytes(self, rel_path: str, data: bytes) -> str:
        path = f"/workspace/{rel_path}"
        await self.backend.write_bytes(path, data)
        return path

    async def publish(self, port: int) -> Link:
        if port == 9999:
            raise SandboxError("端口 9999 上没有程序在监听。")
        self.published.append(port)
        return Link(
            port=port, url="https://sbx-abc.example.com", command="python3 -m http.server", created_at=0.0, pid=9
        )

    async def unpublish(self, port: int) -> bool:
        return False

    async def keep_kernel(self, pid: int | None) -> None:
        self.kernel.append(pid)


def _ctx(session: FakeSession) -> RunContext[Deps]:
    return run_ctx(settings=_settings(enabled=False), sandbox=cast(Any, session))


async def test_sh_writes_the_script_and_runs_the_helper():
    session = FakeSession(
        {"exit": 0, "timed_out": False, "duration": 0.2, "output": "ok\n", "output_bytes": 3, "truncated": False}
    )
    ctx = _ctx(session)
    text = str(await sh(ctx, "echo ok", timeout=500))
    [(path, data)] = session.backend.files.items()
    assert path.startswith("/workspace/.ygg/sh/")
    assert path.endswith("/script.sh")
    assert data == b"echo ok"
    step = path.removesuffix("/script.sh")
    assert session.backend.calls == [["/opt/ygg/ygg", "sh", "run", step, "--timeout", "100"]]
    assert session.backend.audits[0].startswith(f"sh {step}: echo ok")
    assert text == "[工作区] 这是新建的空工作区。\n[工作区] 截到 100 秒\n[退出码 0，用时 0.2 秒]\nok"
    assert ctx.deps.tool_counts["sh"] == 1


async def test_sandbox_put_keeps_names_and_never_overwrites(monkeypatch: pytest.MonkeyPatch):
    async def fake_media(_qq: Any, _gid: Any, _uid: int, h: Any, _max: int, **_: Any) -> tuple[bytes, str]:
        return h.data["blob"], str(h.data.get("file_name") or "file")

    monkeypatch.setattr(sandbox_tools, "media_bytes", fake_media)
    session = FakeSession({})
    session.backend.files["/workspace/in/report.csv"] = b"old"
    ctx = _ctx(session)
    img1 = ctx.deps.handles.add("image", {"blob": png("red")})
    img2 = ctx.deps.handles.add("image", {"blob": png("blue")})
    doc = ctx.deps.handles.add("file", {"blob": b"a,b\n", "file_name": "report.csv"})
    named = ctx.deps.handles.add("file", {"blob": b"x", "file_name": "notes.txt"})
    keys = [k for k in (img1, img2, doc, named) if k]
    assert len(keys) == 4
    parallel = await asyncio.gather(sandbox_put(ctx, keys[0]), sandbox_put(ctx, keys[1]))
    texts = [str(t) for t in parallel] + [str(await sandbox_put(ctx, k)) for k in keys[2:]]
    files = session.backend.files
    assert files["/workspace/in/img-1.png"] == png("red")
    assert files["/workspace/in/img-2.png"] == png("blue")
    assert files["/workspace/in/report.csv"] == b"old"
    assert files["/workspace/in/report-2.csv"] == b"a,b\n"
    assert files["/workspace/in/notes.txt"] == b"x"
    assert "/workspace/in/img-1.png" in texts[0]
    assert "/workspace/in/img-2.png" in texts[1]
    assert "/workspace/in/report-2.csv" in texts[2]


async def test_helper_failure_is_a_tool_error_text():
    session = FakeSession({})
    session.backend.exec = _broken  # pyright: ignore[reportAttributeAccessIssue]
    text = str(await sh(_ctx(session), "true"))
    assert text.startswith("[工具错误]")
    assert "no such file" in text


async def _broken(argv: list[str], **_: Any) -> ExecResult:
    return ExecResult(127, b"", b"no such file")


async def test_py_applies_effects_keeps_the_kernel_and_returns_images(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sandbox_tools, "sees_images", lambda _task: True)
    cell_images = [f"/workspace/.ygg/cells/c/out-{i}.png" for i in range(5)]
    show = "/workspace/.ygg/cells/c/show-0.png"
    files = {p: png() for p in cell_images} | {show: png("blue"), "/workspace/notes.txt": b"not an image"}
    session = FakeSession(
        _py_result(
            output="started\n",
            images=cell_images,
            images_dropped=1,
            effects=[
                {"type": "show", "path": show, "caption": "蓝图"},
                {"type": "publish", "port": 8000},
                {"type": "publish", "port": 9999},
                {"type": "unpublish", "port": 8001},
                {"type": "show", "path": "/workspace/missing.png", "caption": ""},
                {"type": "show", "path": "/workspace/notes.txt", "caption": ""},
            ],
        ),
        files,
    )
    ctx = _ctx(session)
    result = await py(ctx, "print('started')", restart=True)
    assert isinstance(result, ToolReturn)
    text = str(result.return_value)
    assert session.backend.calls[0] == ["/opt/ygg/ygg", "py", "restart"]
    assert session.backend.calls[1][:3] == ["/opt/ygg/ygg", "py", "run"]
    assert "--max-images" in session.backend.calls[1]
    assert session.kernel == [77]
    assert "已按要求重启 kernel" in text
    assert "show：已生成 img#1（蓝图）。要发就在回复某一节的 images 里写 img#1；不引用就不会发。" in text
    assert "https://sbx-abc.example.com" in text
    assert "publish 失败：端口 9999 上没有程序在监听。" in text
    assert "unpublish：端口 8001 没有公开的链接" in text
    assert "show 失败：读取失败" in text
    assert "show 失败：这个文件不是能发的图片" in text
    assert "另有 2 张图没有返回" in text
    assert ctx.deps.images == []  # shown, not attached: the reply sends it only by naming img#1
    reply = BotReply(mode="forward", sections=[Section(content="效果如图", images=["img#1"])])
    assert await reply_images(ctx.deps, reply) == {"img#1": (png("blue"), "蓝图")}
    content = list(cast(Any, result.content))
    assert sum(isinstance(c, BinaryContent) for c in content) == sandbox_tools.PY_MAX_IMAGES
    assert session.published == [8000]


async def test_py_without_vision_describes_images_and_forgets_a_dead_kernel(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sandbox_tools, "sees_images", lambda _task: False)
    seen: list[int] = []

    async def vision(_ctx: Any, images: list[Any], _question: str) -> str:
        seen.append(len(images))
        return "一张红色方块"

    monkeypatch.setattr(sandbox_tools, "vision", vision)
    session = FakeSession(
        _py_result(status="died", kernel={"pid": None, "started": False, "previous": "alive"}, images=["/w/a.png"]),
        {"/w/a.png": png()},
    )
    result = await py(_ctx(session), "while True: pass", timeout=1)
    assert isinstance(result, str)
    assert "一张红色方块" in result
    assert seen == [1]
    assert session.kernel == [None]


# --- harness file tools -------------------------------------------------------------


async def test_ws_file_tools_read_and_edit_next_to_the_bots_own_read_file(tmp_path: Path):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    results: list[str] = []

    def script(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        results.extend(str(p.content) for p in last.parts if isinstance(p, ToolReturnPart))
        steps = [
            ToolCallPart("ws_read_file", {"path": "a.txt"}),
            ToolCallPart("ws_edit_file", {"path": "a.txt", "old_text": "two", "new_text": "2"}),
            TextPart("done"),
        ]
        return ModelResponse(parts=[steps[len(results)]])

    agent = Agent(
        FunctionModel(script),
        deps_type=Deps,
        tools=tools_for(["read_file"]),  # the QQ attachment reader the harness tool shares its bare name with
        capabilities=sandbox_tools.capabilities(),
    )
    deps = run_ctx().deps
    await agent.run("看一下", deps=deps, workspace=LocalWorkspaceBackend(tmp_path))

    assert "1\tone" in results[0]
    assert results[1].startswith("Edited a.txt")
    assert (tmp_path / "a.txt").read_text() == "one\n2\n"
    assert deps.tool_counts == {"ws_read_file": 1, "ws_edit_file": 1}
