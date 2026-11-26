"""The ``kook`` skill: real-time KOOK server/channel info, pinned messages, members, avatars, and file uploads."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import PurePosixPath

from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools.base import ToolResult, chat_tool, people_for
from yggdrasil.ingest.normalize import normalize
from yggdrasil.kook.model import KookError
from yggdrasil.people import clean_name
from yggdrasil.runtime import snapshot
from yggdrasil.sandbox import SandboxError
from yggdrasil.sandbox.text import fmt_size
from yggdrasil.tools.errors import ToolError

ID = "kook"
DESCRIPTION = (
    "KOOK 服务器与频道本身的信息和操作：成员资料（服务器昵称、用户名、角色头衔、入服/活跃时间）、"
    "成员或服务器头像、频道名称与主题、同服务器文字频道列表、频道置顶消息、把工作区文件上传发送到当前频道。"
    "问到或要做这些事时先加载它。"
)
INSTRUCTIONS = """\
KOOK 工具直接实时查询 KOOK 服务器；user_id 是成员 ID（「名字(user_id)」括号里的数字）。
- get_avatar 给出图片句柄 img#N：要看图用 view_image，要发给频道就放进回答某一节的 images。
- upload_file 只上传工作区里已有的文件到当前频道：传路径，不要传内容；要上传的东西先在工作区里生成。
- 只上传了文件、没有别的要说时选 mode=done。"""

UPLOAD_MAX_BYTES = 20 * 1024 * 1024
PINNED_SHOWN = 5
_PIN_CHARS = 300
_ROLES = {"owner": "服主", "admin": "管理员", "member": "成员"}


def _scope(ctx: RunContext[Deps]) -> tuple[str, str]:
    if not ctx.deps.channel_id or not ctx.deps.guild_id:
        raise ToolError("只能在服务器频道里用")
    return ctx.deps.guild_id, ctx.deps.channel_id


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


@chat_tool("member_info")
async def member_info(ctx: RunContext[Deps], user_id: str) -> str:
    """查服务器成员资料：服务器昵称、用户名、身份与角色、入服时间、活跃时间。user_id 是成员 ID。"""
    guild_id, _ = _scope(ctx)
    try:
        m = await ctx.deps.kook.member(guild_id, user_id.strip())
    except KookError as e:
        raise ToolError(f"查不到 {user_id} 的成员资料（可能不在本服务器）：{e}") from e
    nickname, username = clean_name(m.nickname), clean_name(m.username)
    lines = [
        (await people_for(ctx, [m.user_id])).who(m.user_id, nickname or username),
        f"服务器昵称：{nickname or '（未设置）'}",
        f"用户名：{username or '（未知）'}",
        f"身份：{_ROLES.get(m.role, m.role)}",
    ]
    if m.role_names:
        lines.append(f"角色：{'、'.join(m.role_names)}")
    if m.is_bot:
        lines.append("账号类型：机器人")
    if m.joined_at:
        lines.append(f"入服时间：{_when(m.joined_at)}")
    if m.active_time:
        lines.append(f"最近活跃：{_when(m.active_time)}")
    return "\n".join(lines)


@chat_tool("channel_info")
async def channel_info(ctx: RunContext[Deps]) -> str:
    """查当前频道和所在服务器的信息：服务器名、频道名与主题、同服务器文字频道列表、本频道置顶消息。"""
    guild_id, channel_id = _scope(ctx)
    kook, handles = ctx.deps.kook, ctx.deps.handles
    out: list[str] = []
    try:
        g = await kook.guild(guild_id)
        out.append(f"服务器：{g.name}（ID {guild_id}）")
        if g.topic:
            out.append(f"服务器简介：{_clip(g.topic, 300)}")
    except KookError as e:
        out.append(f"服务器信息获取失败（{e}）")

    try:
        ch = await kook.channel(channel_id)
        out.append(f"当前频道：#{ch.name}（ID {channel_id}）")
        if ch.topic:
            out.append(f"频道主题：{_clip(ch.topic, 400)}")
    except KookError:
        name = snapshot().channel_name(channel_id) or channel_id
        out.append(f"当前频道：#{name}（ID {channel_id}）")

    siblings = snapshot().guild_channels(guild_id, enabled_only=True)
    if siblings:
        out.append("\n本服务器已启用的文字频道：" + "、".join(f"#{c.name}" for c in siblings))

    try:
        pinned = (await kook.pinned_messages(channel_id, limit=PINNED_SHOWN))[:PINNED_SHOWN]
        if pinned:
            ids = [m.sender.user_id for m in pinned] + [u for m in pinned for u in normalize(m.segments).mentions]
            people = await people_for(ctx, ids)
            out.append("\n本频道置顶消息：")
            for m in pinned:
                text = _clip(normalize(m.segments, people.mention).text, _PIN_CHARS)
                who = people.who(m.sender.user_id, m.sender.name)
                out.append(f"- [{_when(m.time)}] {who}: {text}{handles.register(m.segments)}")
        else:
            out.append("\n本频道置顶消息：无")
    except KookError as e:
        out.append(f"\n本频道置顶消息：获取失败（{e}）")
    return "\n".join(out)


@chat_tool("get_avatar")
async def get_avatar(ctx: RunContext[Deps], user_id: str | None = None) -> str:
    """取头像，得到图片句柄 img#N。user_id 是成员 ID；不传则取本服务器的图标。"""
    guild_id, _ = _scope(ctx)
    if not user_id:
        try:
            g = await ctx.deps.kook.guild(guild_id)
        except KookError as e:
            raise ToolError(f"拿不到服务器图标：{e}") from e
        if not g.icon:
            raise ToolError("本服务器没有设置图标")
        key = ctx.deps.handles.add("image", {"url": g.icon, "source": "kook"})
        return f"本服务器的图标：{key}"
    uid = user_id.strip()
    try:
        m = await ctx.deps.kook.member(guild_id, uid)
    except KookError as e:
        raise ToolError(f"拿不到 {uid} 的头像：{e}") from e
    if not m.avatar:
        raise ToolError(f"{uid} 没有头像链接")
    key = ctx.deps.handles.add("image", {"url": m.avatar, "source": "kook"})
    return f"{(await people_for(ctx, [uid])).who(uid, m.display_name)} 的头像：{key}"


@chat_tool("upload_file")
async def upload_file(ctx: RunContext[Deps], path: str, name: str | None = None) -> str:
    """把当前工作区里的一个文件上传并发送到本频道（最大 20MB）。path 是工作区里的路径；name 是发送的文件名。"""
    _, channel_id = _scope(ctx)
    session = ctx.deps.sandbox
    if session is None:
        raise ToolError("这个频道没开工作区，没法上传文件")
    try:
        data = await session.get_bytes(path, max_bytes=UPLOAD_MAX_BYTES)
    except SandboxError as e:
        raise ToolError(str(e)) from e
    shown = PurePosixPath((name or "").replace("\\", "/")).name.strip() or PurePosixPath(path).name
    try:
        url = await ctx.deps.kook.upload_asset(data, shown)
        await ctx.deps.kook.send_file_url(
            channel_id=channel_id,
            user_id=ctx.deps.user_id,
            url=url,
            quote=ctx.deps.trigger_message_id,
        )
    except KookError as e:
        raise ToolError(f"上传发送失败：{e}") from e
    ctx.deps.actions.append(f"upload_file {shown}")
    return f"已上传并发送到本频道：{shown}（{fmt_size(len(data))}）"


def _tool(fn: Callable[..., Awaitable[ToolResult]]) -> Tool[Deps]:
    return Tool(fn, takes_ctx=True)


def capability() -> Capability[Deps]:
    fns = (member_info, channel_info, get_avatar, upload_file)
    return Capability[Deps](
        id=ID,
        description=DESCRIPTION,
        instructions=INSTRUCTIONS,
        tools=[_tool(fn) for fn in fns],
        defer_loading=True,
    )
