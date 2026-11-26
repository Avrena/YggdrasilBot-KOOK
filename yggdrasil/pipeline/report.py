"""Report of a finished background job: synthetic chat invocation replying directly with a quoted KOOK CardMessage."""

import secrets

from pydantic_ai import Tool
from pydantic_ai.usage import RunUsage

from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.schema import BotReply
from yggdrasil.agent.skills import skill_names, skills_for
from yggdrasil.agent.tools import sandbox as sandbox_tools
from yggdrasil.agent.tools import suite_enabled, tool_names, tools_for
from yggdrasil.agent.tools.images import MAX_IMAGES, attach_image
from yggdrasil.agent.tools.jobs import JOB_CONTROL_TOOLS, jobs
from yggdrasil.bot.state import identity
from yggdrasil.core.log import LOGGER
from yggdrasil.delivery.send import send_kmarkdown
from yggdrasil.kook import client as kook_client
from yggdrasil.kook.client import Kook
from yggdrasil.llm.registry import TaskNotConfigured, chat_model
from yggdrasil.llm.streaming import RunStats
from yggdrasil.people import member_label
from yggdrasil.pipeline import ratelimit
from yggdrasil.pipeline.invoke import Invocator
from yggdrasil.pipeline.prompt import instructions
from yggdrasil.runtime import snapshot
from yggdrasil.sandbox import SandboxError, manager
from yggdrasil.sandbox.job_model import KIND_TEXT, STATUS_TEXT, Job, ReportDeferred, fmt_duration
from yggdrasil.settings import Settings
from yggdrasil.tools.errors import ToolError

_RESULT_CHARS = 12000


async def run_report(job: Job) -> str | None:
    kook = kook_client.connected()
    if kook is None:
        raise ReportDeferred("no bot connection")
    snap = snapshot()
    if not snap.channel_enabled(job.channel_id):
        LOGGER.info("sandbox job {}: channel {} is disabled, no report", job.id, job.channel_id)
        return None
    guild_id = snap.guild_of(job.channel_id)
    settings = snap.settings(channel_id=job.channel_id, guild_id=guild_id)
    if await ratelimit.tokens_exhausted(job.channel_id, settings.ratelimit):
        await _notice(kook, job, guild_id)
        await ratelimit.add_tokens(job.channel_id, _relay_tokens(job))
        return None
    async with ratelimit.slot(job.channel_id, job.user_id, settings.ratelimit) as granted:
        if not granted:
            raise ReportDeferred("requester's queue is full")
        inv = ReportInvocator(kook, job, guild_id, settings)
        await inv.run()
        return str(inv.invocation_id) if inv.invocation_id is not None else None


def _relay_tokens(job: Job) -> int:
    return job.usage.get("input_tokens", 0) + job.usage.get("output_tokens", 0)


def _headline(job: Job) -> str:
    return f"后台任务「{job.title}」{STATUS_TEXT[job.status]}"


async def _notice(kook: Kook, job: Job, guild_id: str | None) -> None:
    ident = await identity(kook)
    text = f"{_headline(job)}（今天的用量已到上限，不再生成详细说明）。"
    for link in job.links:
        text += f"\n{link.get('url')}"
    await send_kmarkdown(
        kook,
        channel_id=job.channel_id,
        guild_id=guild_id,
        user_id=job.user_id,
        reply_to=job.trigger_message_id,
        content=text,
        self_id=ident.self_id,
        nickname=ident.nickname,
    )


class ReportInvocator(Invocator):
    """A synthetic chat run reporting a finished sandbox job, quoting the original trigger message directly."""

    def __init__(self, kook: Kook, job: Job, guild_id: str | None, settings: Settings) -> None:
        self._setup(
            kook,
            "chat",
            settings,
            job.channel_id,
            guild_id,
            user_id=job.user_id,
            trigger_message_id=job.trigger_message_id,
            role="member",
            record_handler="report",
        )
        self.job = job
        self._relay_counted = False

    async def _run(self) -> str:
        job = self.job
        ident = await identity(self.kook)
        self.notes.bot = (ident.self_id, self.settings.persona.name)
        sandbox_on = suite_enabled(
            self.settings,
            "chat",
            self.channel_id,
            global_settings=snapshot().settings(),
            configured=manager().configured,
        )
        await self._start_row(f"[后台任务 {job.id}] {job.title}：{STATUS_TEXT[job.status]}")
        try:
            resolved_model = chat_model("chat")
        except TaskNotConfigured:
            LOGGER.warning("chat has no model route; job {} is not reported", job.id)
            return "failed"
        deps = Deps(
            self.kook,
            self.channel_id,
            self.guild_id,
            self.user_id,
            self.settings,
            Handles(),
            "chat",
            self_id=ident.self_id,
            bot_nickname=ident.nickname,
            trigger_message_id=job.trigger_message_id,
            invocation_id=str(self.invocation_id) if self.invocation_id is not None else None,
            request_text=job.request_text,
            notes=self.notes,
        )
        tools_cfg = self.settings.tools
        tool_list = tool_names(tools_cfg.enabled, "chat")
        if sandbox_on and self.channel_id is not None:
            self.sandbox = deps.sandbox = manager().session(
                self.channel_id, invocation_id=deps.invocation_id, cfg=tools_cfg, workspace=job.workspace
            )
            suite = sandbox_tools.suite(exclude=JOB_CONTROL_TOOLS)
        else:
            jobs_tool = Tool(jobs, takes_ctx=True)
            suite = sandbox_tools.Suite(tools=[jobs_tool], names=[jobs_tool.name])
        image_notes = await self._attach_images(deps)
        nonce = secrets.token_hex(3)
        requester = await member_label(self.guild_id, self.user_id)
        prompt = build_prompt(job, nonce=nonce, requester=requester, image_notes=image_notes)
        agent = self._agent(
            resolved_model,
            tools=tools_for(tool_list) + suite.tools,
            capabilities=suite.capabilities + skills_for(skill_names(self.settings, "chat", self.channel_id)),
            instructions=instructions(
                "chat",
                self.settings,
                self.channel_id,
                guild_id=self.guild_id,
                tools=tool_list + suite.names,
                sandbox=tools_cfg if self.sandbox is not None else None,
                report=True,
                nonce=nonce,
            ),
        )
        mod_text = f"{job.request_text}\n{job.result[:4000]}"
        return await self._execute(agent, prompt, deps, resolved_model, mod_text)

    async def _attach_images(self, deps: Deps) -> list[str]:
        if not self.job.images:
            return []
        notes: list[str] = []
        found = await manager().backend_for(self.job.channel_id, self.job.workspace) if manager().configured else None
        if found is None or found[1] != self.job.claim:
            if found is not None:
                await found[0].aclose()
            return ["任务给出的图片随工作区一起没了，发不出来。"]
        backend = found[0]
        try:
            for item in self.job.images[:MAX_IMAGES]:
                path, caption = item.get("path", ""), item.get("caption", "")
                try:
                    blob = await backend.read_bytes(path)
                    await attach_image(deps, blob, caption)
                except ToolError as e:
                    notes.append(f"{path}：{e}")
                except (OSError, SandboxError) as e:
                    notes.append(f"{path} 读不出来（{e}）")
        finally:
            await backend.aclose()
        return notes

    def _account(self, model_label: str, usage: RunUsage, deps: Deps, stats: RunStats) -> None:
        super()._account(model_label, usage, deps, stats)
        if self.job.usage and not self._relay_counted:
            self._relay_counted = True
            self.meta.models.append(f"relay/{self.job.model}")
            self.meta.tokens_in += self.job.usage.get("input_tokens", 0)
            self.meta.tokens_out += self.job.usage.get("output_tokens", 0)
            self.meta.tokens_cached += self.job.usage.get("cached_tokens", 0)

    def _billable_tokens(self) -> int:
        return super()._billable_tokens() + (0 if self._relay_counted else _relay_tokens(self.job))

    async def _status(self, emoji: str) -> bool:
        return False

    async def deliver(self, reply: BotReply) -> None:
        if not reply.title:
            reply = reply.model_copy(update={"title": _headline(self.job)})
        # Background job reports finish minutes later: always quote the original trigger unless quote_reply=never.
        if self.settings.interaction.quote_reply == "when_busy":
            self.settings = self.settings.model_copy(
                update={"interaction": self.settings.interaction.model_copy(update={"quote_reply": "always"})}
            )
        await super().deliver(reply)


def build_prompt(job: Job, *, nonce: str, requester: str, image_notes: list[str]) -> str:
    def block(tag: str, body: str) -> str:
        name = f"{tag}-{nonce}"
        return f"<{name}>\n{body.replace(nonce, '*' * len(nonce))}\n</{name}>"

    took = fmt_duration((job.ended_at or job.created_at) - job.created_at)
    facts = [
        f"编号：{job.id}",
        f"类型：{KIND_TEXT[job.kind]}",
        f"标题：{job.title}",
        f"状态：{STATUS_TEXT[job.status]}（用时 {took}）",
        f"请求者：{requester}",
        f"工作区：{job.workspace}（这次回复已经选中它，工作区工具直接作用在它上面）",
    ]
    if job.error:
        facts.append(f"说明：{job.error}")
    if job.usage:
        facts.append(
            f"模型用量：{job.usage.get('requests', 0)} 次请求，输入 {job.usage.get('input_tokens', 0)} token，"
            f"输出 {job.usage.get('output_tokens', 0)} token"
        )
    parts = [
        block("原始请求", job.request_text or "（无）"),
        block("后台任务", "\n".join(facts)),
        block("任务内容", job.spec[:4000]),
    ]
    result = job.result or "（没有输出）"
    if len(result) > _RESULT_CHARS:
        result = result[:_RESULT_CHARS] + "\n…（后面省略）"
    parts.append(block("任务结果" if job.kind == "command" else "任务的最终回答", result))
    if job.links:
        parts.append(block("公开链接", "\n".join(f"{x.get('url')}（端口 {x.get('port')}）" for x in job.links)))
    attached = len(job.images) - len(image_notes) if job.images else 0
    notes = []
    if attached > 0:
        notes.append(f"任务给出的 {attached} 张图已经附在这次回复的卡片里，正文不用再描述图里的内容。")
    notes += image_notes
    notes.append(
        "这是之前接下的后台任务的回报，不是新的请求：用 mode=reply 把结果清晰地告诉请求者，"
        "说清做成了什么、在哪里（路径、链接）、没完成什么；失败、超时或取消时说明原因和下一步建议。"
    )
    if job.kind == "command":
        notes.append(f"完整输出在工作区「{job.workspace}」的 {job.dir}/out，需要细看时可以读它。")
    return "\n\n".join([*parts, *notes])
