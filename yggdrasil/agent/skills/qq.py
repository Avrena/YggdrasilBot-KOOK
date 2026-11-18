"""The ``qq`` skill: the group as QQ itself sees it (members, avatars, announcements, essence messages, group files)
and the QQ actions anyone may ask for (upload a workspace file to the group files, poke the requester)."""

import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import PurePosixPath

from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools.base import ToolResult, chat_tool, people_for
from yggdrasil.ingest.normalize import normalize
from yggdrasil.people import clean_name
from yggdrasil.qq.model import QQError
from yggdrasil.sandbox import SandboxError
from yggdrasil.sandbox.text import fmt_size
from yggdrasil.tools.errors import ToolError

ID = "qq"
DESCRIPTION = (
    "QQ 群本身的信息和操作：群成员资料（群名片、头衔、等级、入群和最后发言时间、禁言）、群或群友的头像、群名和人数、"
    "群公告、精华消息、群文件（列出、读取，把工作区里的文件上传到群文件）、戳一戳。问到或要做这些事时先加载它。"
)
INSTRUCTIONS = """\
QQ 群工具直接问 QQ，结果是实时的；user_id 是 QQ 号（「名字(QQ号)」括号里的数字）。
- get_avatar 给出图片句柄 img#N：要看图用 view_image，要发给群友就放进回答某一节的 images。
- group_files 列出的 file#N 用 read_file 读（图片用 view_image）；文件夹用它的 id 再列。
- upload_file 只上传工作区里已有的文件：传路径，不要传内容；要上传的东西先在工作区里生成。
- nudge 只能戳请求者本人，一次请求最多一次。只上传了文件或戳了一下、没有别的要说时选 mode=done。"""

USER_AVATAR = "https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
GROUP_AVATAR = "https://p.qlogo.cn/gh/{group_id}/{group_id}/640/"
UPLOAD_MAX_BYTES = 20 * 1024 * 1024
ANNOUNCEMENTS_SHOWN = 3
ESSENCE_SHOWN = 5
FILES_SHOWN = 50
_ANNOUNCEMENT_CHARS = 600
_ESSENCE_CHARS = 300
_ROLES = {"owner": "群主", "admin": "管理员", "member": "成员"}


def _group_id(ctx: RunContext[Deps]) -> int:
    if ctx.deps.group_id is None:
        raise ToolError("只能在群聊里用")
    return ctx.deps.group_id


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _keys(keys: list[str | None]) -> str:
    shown = [k for k in keys if k]
    return f" ({', '.join(dict.fromkeys(shown))})" if shown else ""


async def _fetch[T](call: Awaitable[list[T]]) -> tuple[list[T], str]:
    """One list a tool result shows, or why QQ failed to give it (the rest of the result still stands)."""
    try:
        return await call, ""
    except QQError as e:
        return [], f"获取失败（{e}）"


@chat_tool("member_info")
async def member_info(ctx: RunContext[Deps], user_id: int) -> str:
    """查群成员资料：群名片、昵称、头衔、身份、等级、入群时间、最后发言时间、是否被禁言。user_id 是 QQ 号。"""
    group_id = _group_id(ctx)
    try:
        m = await ctx.deps.qq.member(group_id, user_id)
    except QQError as e:
        raise ToolError(f"查不到 {user_id} 的群成员资料（可能不在本群）：{e}") from e
    card, nickname = clean_name(m.card), clean_name(m.nickname)
    lines = [
        (await people_for(ctx, [user_id])).who(user_id, card or nickname),
        f"群名片：{card or '（没有设置）'}",
        f"昵称：{nickname or '（未知）'}",
        f"身份：{_ROLES.get(m.role, m.role)}",
    ]
    if m.title:
        lines.append(f"头衔：{clean_name(m.title)}")
    if m.level is not None:
        lines.append(f"等级：{m.level}")
    if m.joined_at:
        lines.append(f"入群时间：{_when(m.joined_at)}")
    if m.last_spoke_at:
        lines.append(f"最后发言：{_when(m.last_spoke_at)}")
    if m.muted_until is not None and m.muted_until > time.time():
        lines.append(f"禁言中，{_when(m.muted_until)} 解除")
    else:
        lines.append("没有被禁言")
    return "\n".join(lines)


@chat_tool("group_info")
async def group_info(ctx: RunContext[Deps]) -> str:
    """查本群的群名、人数、最近的群公告和精华消息。"""
    group_id = _group_id(ctx)
    qq, handles = ctx.deps.qq, ctx.deps.handles
    group = await qq.group(group_id)
    out = [f"群名：{group.name}（群号 {group_id}）"]
    if group.member_count is not None:
        out.append(f"人数：{group.member_count}")
    # Either list may be missing on a protocol or fail on its own; the rest is still worth showing.
    notices, notices_error = await _fetch(qq.announcements(group_id))
    essence, essence_error = await _fetch(qq.essence(group_id, limit=ESSENCE_SHOWN))
    notices, essence = notices[:ANNOUNCEMENTS_SHOWN], essence[:ESSENCE_SHOWN]
    ids = [n.user_id for n in notices]
    for e in essence:
        ids += [e.user_id, e.operator_id, *normalize(e.segments).mentions]
    people = await people_for(ctx, ids)

    out.append("\n最近的群公告：" + (notices_error or ("" if notices else "无")))
    for n in notices:
        images = _keys([handles.add("image", {"url": url, "source": "qq"}) for url in n.image_urls])
        who = people.who(n.user_id)
        out.append(f"- [{_when(n.time)}] {who}: {_clip(n.text, _ANNOUNCEMENT_CHARS)}{images}")

    out.append("\n最近的精华消息：" + (essence_error or ("" if essence else "无")))
    for e in essence:
        text = _clip(normalize(e.segments, people.mention).text, _ESSENCE_CHARS)
        who, setter = people.who(e.user_id, e.sender_name), people.who(e.operator_id)
        out.append(f"- [{_when(e.time)}] {who}: {text}{handles.register(e.segments)}（{setter} 设为精华）")
    return "\n".join(out)


@chat_tool("get_avatar")
async def get_avatar(ctx: RunContext[Deps], user_id: int | None = None) -> str:
    """取头像，得到图片句柄 img#N。user_id 是群友的 QQ 号；不传就是本群的头像。"""
    group_id = _group_id(ctx)
    if user_id is None:
        key = ctx.deps.handles.add("image", {"url": GROUP_AVATAR.format(group_id=group_id), "source": "qq"})
        return f"本群的头像：{key}"
    key = ctx.deps.handles.add("image", {"url": USER_AVATAR.format(user_id=user_id), "source": "qq"})
    return f"{(await people_for(ctx, [user_id])).who(user_id)} 的头像：{key}"


@chat_tool("group_files")
async def group_files(ctx: RunContext[Deps], folder: str | None = None) -> str:
    """列出群文件：不传 folder 列根目录，传文件夹 id 列那个文件夹。文件给出 file#N 句柄，用 read_file 读。"""
    group_id = _group_id(ctx)
    listing = await ctx.deps.qq.group_files(group_id, folder_id=folder or None)
    files = sorted(listing.files, key=lambda f: f.uploaded_at, reverse=True)
    shown = files[:FILES_SHOWN]
    people = await people_for(ctx, [f.uploader_id for f in shown])
    lines = [f"[文件夹] {d.name}（id: {d.folder_id}，{d.file_count} 个文件）" for d in listing.folders]
    for f in shown:
        key = ctx.deps.handles.add("file", {"file_id": f.file_id, "file_name": f.name, "file_size": f.size})
        expires = f"，{_when(f.expires_at)} 过期" if f.expires_at else ""
        uploader = people.who(f.uploader_id, f.uploader_name)
        lines.append(f"{key} {f.name}（{fmt_size(f.size)}，{uploader} 上传于 {_when(f.uploaded_at)}{expires}）")
    if len(files) > len(shown):
        lines.append(f"（共 {len(files)} 个文件，只列出最新的 {len(shown)} 个）")
    return "\n".join(lines) or ("这个文件夹是空的" if folder else "本群群文件是空的")


@chat_tool("upload_file")
async def upload_file(ctx: RunContext[Deps], path: str, name: str | None = None, folder: str | None = None) -> str:
    """把当前工作区里的一个文件上传到本群群文件（最大 20MB）。path 是工作区里的路径（相对 /workspace 或绝对路径）；
    name 是群文件里显示的文件名，默认用原文件名；folder 是群文件夹 id（group_files 列出的），默认根目录。"""
    group_id = _group_id(ctx)
    session = ctx.deps.sandbox
    if session is None:
        raise ToolError("这个群没开工作区，没法上传文件")
    try:
        data = await session.get_bytes(path, max_bytes=UPLOAD_MAX_BYTES)
    except SandboxError as e:
        raise ToolError(str(e)) from e
    shown = PurePosixPath((name or "").replace("\\", "/")).name.strip() or PurePosixPath(path).name
    try:
        await ctx.deps.qq.upload_group_file(group_id, data=data, name=shown, folder_id=folder or None)
    except QQError as e:
        raise ToolError(f"上传失败：{e}") from e
    ctx.deps.actions.append(f"upload_file {shown}")
    where = f"群文件夹 {folder}" if folder else "群文件根目录"
    return f"已上传到{where}：{shown}（{fmt_size(len(data))}）"


@chat_tool("nudge")
async def nudge(ctx: RunContext[Deps]) -> str:
    """戳一戳请求者本人（不能戳别人），一次请求最多一次。"""
    group_id = _group_id(ctx)
    action = f"nudge {ctx.deps.user_id}"
    if action in ctx.deps.actions:
        raise ToolError("这次请求已经戳过了，只能戳一次")
    ctx.deps.actions.append(action)  # before the call: a parallel call in the same turn is refused too
    try:
        await ctx.deps.qq.nudge(group_id=group_id, user_id=ctx.deps.user_id)
    except Exception:
        ctx.deps.actions.remove(action)  # nothing was done, so mode=done must not count it
        raise
    return "已经戳了请求者一下"


def _tool(fn: Callable[..., Awaitable[ToolResult]]) -> Tool[Deps]:
    return Tool(fn, takes_ctx=True)


def capability() -> Capability[Deps]:
    """The ``qq`` skill for one run."""
    fns = (member_info, group_info, get_avatar, group_files, upload_file, nudge)
    return Capability[Deps](
        id=ID,
        description=DESCRIPTION,
        instructions=INSTRUCTIONS,
        tools=[_tool(fn) for fn in fns],
        defer_loading=True,
    )
