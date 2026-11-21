from typing import Literal

from pydantic import BaseModel, Field


class Section(BaseModel):
    # No title: the forward card preview shows the first lines of each node, so a heading wastes it.
    content: str = Field(description="正文，纯文本，不要 markdown，不要加小标题；按意思分段，段落之间空一行")
    images: list[str] = Field(
        default_factory=list,
        description="紧跟在这一节正文后面发的图片句柄（img#N），按顺序；render_*、show() 生成的图"
        "和聊天、网页里的图都行。整条回复最多 4 张；没写进来的图不会发",
    )


class SourceRef(BaseModel):
    label: str = Field(description="来源名称，如网页标题、文件名、#消息id")
    url: str | None = None


# mode=plain: a short answer as one ordinary group message (chat only).
PLAIN_MAX_CHARS = 120
PLAIN_MAX_IMAGES = 2


class BotReply(BaseModel):
    """The structured answer of the chat / tldr agents."""

    mode: Literal["forward", "plain", "silent", "done"] = Field(
        description="forward：查证后用合并转发作答（不接或做不了的直接请求也用它简短说明）；"
        f"plain：一两句话就能答完（正文不超过 {PLAIN_MAX_CHARS} 字），或者只发 1–{PLAIN_MAX_IMAGES} 张图，"
        "直接作为普通消息发到群里，不进合并转发、不带来源和元数据（只在普通答疑里可用）；"
        "silent：不回复（不是在跟你说话、没有实际内容）；done：已经用操作类工具完成了请求（比如撤回），不再发消息"
    )
    title: str | None = Field(default=None, description="mode=forward 时这次回复的主题（≤20 字）")
    sections: list[Section] = Field(default_factory=list, description="mode=forward / plain 时的各节（plain 只写一节）")
    sources: list[SourceRef] = Field(default_factory=list, description="引用过的来源")
    silent_reason: str | None = Field(
        default=None, description="mode=silent 时一句话说明为什么不回（只记日志，不发出去）"
    )

    def full_text(self) -> str:
        return "\n\n".join(s.content for s in self.sections if s.content.strip())

    def plain_problem(self, attached: int = 0) -> str | None:
        """Why this reply can't go out as mode=plain (to send back to the model), or None. ``attached``: pictures
        the run attaches anyway."""
        text = self.full_text().strip()
        pictures = sum(len(s.images) for s in self.sections) + attached
        if not text and not pictures:
            return "mode=plain 要有一句话或一张图；不想回复就用 silent"
        if len(text) > PLAIN_MAX_CHARS:
            return f"mode=plain 的正文最多 {PLAIN_MAX_CHARS} 字，这次写了 {len(text)} 字：话多就用 forward"
        if pictures > PLAIN_MAX_IMAGES:
            return f"mode=plain 最多发 {PLAIN_MAX_IMAGES} 张图，这次有 {pictures} 张：图多就用 forward"
        return None


class OpsReply(BaseModel):
    """The structured answer of the ops agent (``yggdrasil.ops.agent``)."""

    mode: Literal["done", "reply"] = Field(
        description="done：已用工具完成操作，不发消息；reply：回一段简短纯文本（查询结果、失败原因、需要澄清时）"
    )
    text: str | None = Field(default=None, description="mode=reply 时发出的文字，纯文本，简短")
