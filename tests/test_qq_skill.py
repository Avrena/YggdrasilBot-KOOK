"""The ``qq`` skill (``agent.skills``): offered on demand in the group chat runs that enable it, usable once the model
loads it, and still loaded in a reply that continues an answer which loaded it."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.workspaces import FileEntry

from tests.support.agent import run_ctx
from tests.support.groups import fresh_group
from tests.support.qq import FakeQQ, incoming
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.schema import BotReply
from yggdrasil.agent.skills import qq as qq_skill
from yggdrasil.agent.skills import skill_names, skills_for
from yggdrasil.agent.skills.qq import upload_file
from yggdrasil.pipeline import invoke
from yggdrasil.pipeline import session as sessions
from yggdrasil.sandbox import Session
from yggdrasil.settings import Settings, ToolSettings
from yggdrasil.triggers.index import Resolved

GROUP, SELF, ALICE = fresh_group(), 10000, 20001
QQ_TOOLS = {"member_info", "group_info", "get_avatar", "group_files", "upload_file", "nudge"}
SKILLED = Settings.model_validate({"tools": {"skills": ["qq"]}})
LOAD = [ToolCallPart("load_capability", {"id": "qq"})]
DONE = [ToolCallPart("final_result", {"mode": "done"})]
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)


def _call(name: str, **args: Any) -> ToolCallPart:
    return ToolCallPart(name, args)


class Script:
    """A model answering each request with the next of ``turns``; keeps what each request sent and offered."""

    def __init__(self, *turns: list[ToolCallPart]) -> None:
        self.turns = list(turns)
        self.tools: list[dict[str, ToolDefinition]] = []
        self.instructions: list[str] = []
        self.seen: list[list[ModelMessage]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.tools.append({t.name: t for t in info.function_tools})
        self.instructions.append(info.instructions or "")
        self.seen.append(messages)
        return ModelResponse(parts=self.turns.pop(0))

    def returned(self, request: int, tool: str) -> list[Any]:
        """What ``tool`` returned in the turn that request ``request`` (0-based) sent."""
        return [
            p.content for p in self.seen[request][-1].parts if isinstance(p, ToolReturnPart) and p.tool_name == tool
        ]


def _agent(script: Script, skills: list[str]) -> Agent[Deps, BotReply]:
    """The chat agent as ``pipeline.invoke`` builds it, with these skills."""
    return Agent(FunctionModel(script), output_type=BotReply, deps_type=Deps, capabilities=skills_for(skills))


def _deps(qq: FakeQQ, **fields: Any) -> Deps:
    return Deps(qq, GROUP, ALICE, SKILLED, Handles(), "chat", self_id=SELF, **fields)


def test_only_group_chat_runs_that_enable_it_get_the_skill() -> None:
    assert skill_names(SKILLED, "chat", GROUP) == ["qq"]  # job reports run as group chat too
    assert skill_names(SKILLED, "chat", None) == []  # private chat
    assert skill_names(SKILLED, "tldr", GROUP) == []
    assert skill_names(Settings(), "chat", GROUP) == []  # off unless a group turns it on
    assert skill_names(Settings.model_validate({"tools": {"skills": ["qq", "nope", "qq"]}}), "chat", GROUP) == ["qq"]


async def test_until_loaded_the_model_sees_only_the_catalog_and_load_capability() -> None:
    script = Script(DONE)
    await _agent(script, ["qq"]).run("问个问题", deps=_deps(FakeQQ()))
    assert "load_capability" in script.tools[0]
    assert not script.tools[0].keys() & QQ_TOOLS
    assert qq_skill.DESCRIPTION in script.instructions[0]
    assert qq_skill.INSTRUCTIONS not in script.instructions[0]

    plain = Script(DONE)
    await _agent(plain, []).run("问个问题", deps=_deps(FakeQQ()))
    assert "load_capability" not in plain.tools[0]


async def test_once_loaded_the_model_can_call_its_tools() -> None:
    script = Script(LOAD, [_call("get_avatar")], DONE)
    deps = _deps(FakeQQ())
    await _agent(script, ["qq"]).run("把群头像发我看看", deps=deps)

    assert script.returned(1, "load_capability") == [{"instructions": qq_skill.INSTRUCTIONS}]
    assert script.tools[1].keys() >= QQ_TOOLS
    assert script.returned(2, "get_avatar") == ["本群的头像：img#1"]
    avatar = deps.handles.get("img#1")
    assert avatar is not None
    # A handle with a source: view_image and the reply fetch it through the SSRF guard.
    assert avatar.data == {"url": f"https://p.qlogo.cn/gh/{GROUP}/{GROUP}/640/", "source": "qq"}
    assert deps.tool_counts["get_avatar"] == 1


async def test_nudge_pokes_the_requester_once_per_run() -> None:
    qq = FakeQQ({"nudge": None})
    # Two pokes in parallel, then another one.
    script = Script(LOAD, [_call("nudge"), _call("nudge")], [_call("nudge")], DONE)
    deps = _deps(qq)
    result = await _agent(script, ["qq"]).run("戳我一下", deps=deps)

    assert script.tools[1]["nudge"].parameters_json_schema.get("properties", {}) == {}  # no one else to poke
    assert qq.called("nudge") == [{"group_id": GROUP, "user_id": ALICE}]
    refused = "[工具错误] 这次请求已经戳过了，只能戳一次"
    assert sorted(script.returned(2, "nudge")) == sorted(["已经戳了请求者一下", refused])
    assert script.returned(3, "nudge") == [refused]
    assert result.output.mode == "done"
    assert deps.actions == [f"nudge {ALICE}"]


async def test_a_failed_nudge_is_no_action() -> None:
    qq = FakeQQ({"nudge": RuntimeError("戳不了")})
    script = Script(LOAD, [_call("nudge")], DONE)
    deps = _deps(qq)
    await _agent(script, ["qq"]).run("戳我一下", deps=deps)
    assert script.returned(2, "nudge") == ["[工具错误] RuntimeError: 戳不了"]
    assert deps.actions == []  # mode=done would claim nothing was done


class Workspace:
    """The selected workspace's files as the sandbox backend serves them to ``Session``."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def stat(self, path: str) -> FileEntry:
        if path not in self.files:
            raise FileNotFoundError(2, "No such file or directory", path)
        return FileEntry(name=path.rsplit("/", 1)[-1], path=path, is_dir=False, size=len(self.files[path]))

    async def read_bytes(self, path: str) -> bytes:
        return self.files[path]


def _session(files: dict[str, bytes]) -> Session:
    session = Session(cast(Any, SimpleNamespace(token="t")), GROUP, invocation_id=None, cfg=ToolSettings())
    session.backend = cast(Any, Workspace(files))
    return session


async def test_upload_file_sends_a_workspace_file_to_the_group_files() -> None:
    qq = FakeQQ({"upload_group_file": "file-1"})
    session = _session(
        {
            "/workspace/out/report.pdf": b"%PDF-1.7 report",
            "/workspace/out/big.bin": b"\0" * (20 * 1024 * 1024 + 1),
        }
    )
    ctx = run_ctx(qq=qq, group_id=GROUP, user_id=ALICE, self_id=SELF, sandbox=session)

    assert (
        await upload_file(ctx, "out/report.pdf", folder="/reports") == "已上传到群文件夹 /reports：report.pdf（15 B）"
    )
    named = await upload_file(ctx, "/workspace/out/report.pdf", name="../周报.pdf")
    assert named == "已上传到群文件根目录：周报.pdf（15 B）"
    assert qq.called("upload_group_file") == [
        {"group_id": GROUP, "data": b"%PDF-1.7 report", "name": "report.pdf", "folder_id": "/reports"},
        {"group_id": GROUP, "data": b"%PDF-1.7 report", "name": "周报.pdf", "folder_id": None},
    ]
    assert ctx.deps.actions == ["upload_file report.pdf", "upload_file 周报.pdf"]

    too_big = await upload_file(ctx, "out/big.bin")
    assert isinstance(too_big, str)
    assert too_big.startswith("[工具错误] 文件太大")
    missing = await upload_file(ctx, "out/nothing.txt")
    assert isinstance(missing, str)
    assert missing.startswith("[工具错误] 读取失败：文件不存在")
    no_workspace = run_ctx(qq=qq, group_id=GROUP, user_id=ALICE, self_id=SELF)
    assert await upload_file(no_workspace, "out/report.pdf") == "[工具错误] 这个群没开工作区，没法上传文件"
    assert len(qq.called("upload_group_file")) == 2
    assert len(ctx.deps.actions) == 2


async def test_a_reply_to_an_answer_that_loaded_the_skill_uses_it_without_loading_again() -> None:
    qq = FakeQQ({"nudge": None})
    answer = await _agent(Script(LOAD, [_call("get_avatar")], DONE), ["qq"]).run("群头像", deps=_deps(qq))
    # What is kept goes through JSON, as it does through the database.
    kept = ModelMessagesTypeAdapter.validate_python(
        ModelMessagesTypeAdapter.dump_python(answer.all_messages(), mode="json")
    )

    reply = Script([_call("nudge")], DONE)
    await _agent(reply, ["qq"]).run("再戳我一下", deps=_deps(qq), message_history=kept)
    assert reply.tools[0].keys() >= QQ_TOOLS
    assert reply.returned(1, "nudge") == ["已经戳了请求者一下"]


@pytest.mark.parametrize(("settings", "continued"), [(SKILLED, True), (Settings(), False)])
async def test_a_reply_starts_over_when_the_answers_skill_is_off_now(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, continued: bool
) -> None:
    setup = sessions.Setup("chat", "chat 的规则", ["read_url"], sandbox=False, skills=["qq"])
    kept = sessions.Session(uuid4(), setup, [], Handles(), "ab12cd", None, datetime.now(UTC))

    async def load(*_: Any) -> sessions.Session:
        return kept

    monkeypatch.setattr(sessions, "load", load)
    m = incoming("再戳我一下", group_id=GROUP, user_id=ALICE, self_id=SELF, to_me=True)
    inv = invoke.Invocator(FakeQQ(self_id=SELF), m, CHAT, "chat", "", settings, GROUP)
    found = await inv._session(4242)  # pyright: ignore[reportPrivateUsage]
    assert (found is kept) == continued
