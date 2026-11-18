"""QQ faked at the ``yggdrasil.qq`` interface: every call is recorded and answered from a script."""

import inspect
from collections.abc import Mapping
from typing import Any, cast

from nonebot.adapters import Bot, Event

from yggdrasil.qq.client import QQ
from yggdrasil.qq.model import (
    Announcement,
    Essence,
    ForwardNode,
    Group,
    GroupFiles,
    Incoming,
    Login,
    Member,
    Message,
    Node,
    Observed,
    Protocol,
    Role,
    Sender,
    Sent,
)
from yggdrasil.qq.segments import Segment
from yggdrasil.qq.segments import text as text_seg


class _Bot:
    def __init__(self, self_id: int) -> None:
        self.self_id = str(self_id)


class FakeQQ(QQ):
    """Each method answers from ``answers[<method name>]``: a value to return, an exception to raise, or a function
    of the call's keyword arguments that returns (or raises) one; a method without an answer raises
    ``NotImplementedError``. ``send``/``send_forward`` default to fresh message ids. Calls are kept in ``calls``."""

    def __init__(
        self, answers: Mapping[str, Any] | None = None, *, self_id: int = 10000, protocol: Protocol = "milky"
    ) -> None:
        super().__init__(cast(Bot, _Bot(self_id)))
        self.protocol = protocol
        self.answers: dict[str, Any] = dict(answers or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 9000

    def called(self, method: str) -> list[dict[str, Any]]:
        """The arguments of every ``method`` call, oldest first."""
        return [args for name, args in self.calls if name == method]

    async def _answer(self, method: str, **args: Any) -> Any:
        self.calls.append((method, args))
        if method not in self.answers:
            if method in ("send", "send_forward"):
                self._next_id += 1
                return self._next_id if method == "send" else Sent(self._next_id)
            raise NotImplementedError(f"no scripted answer for {method}")
        answer = self.answers[method]
        if callable(answer) and not isinstance(answer, type):
            answer = answer(**args)
            if inspect.isawaitable(answer):
                answer = await answer
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def incoming(self, event: Event) -> Incoming | None:
        return await self._answer("incoming", event=event)

    def recalled(self, event: Event) -> tuple[int, int] | None:
        return None

    def observe(self, event: Event) -> Observed | None:
        return None

    async def login(self) -> Login:
        return await self._answer("login")

    async def group(self, group_id: int) -> Group:
        return await self._answer("group", group_id=group_id)

    async def member(self, group_id: int, user_id: int, *, reply_timeout: float | None = None) -> Member:
        return await self._answer("member", group_id=group_id, user_id=user_id, reply_timeout=reply_timeout)

    async def announcements(self, group_id: int) -> list[Announcement]:
        return await self._answer("announcements", group_id=group_id)

    async def essence(self, group_id: int, *, limit: int = 20) -> list[Essence]:
        return await self._answer("essence", group_id=group_id, limit=limit)

    async def nudge(self, *, group_id: int, user_id: int) -> None:
        return await self._answer("nudge", group_id=group_id, user_id=user_id)

    async def group_files(self, group_id: int, *, folder_id: str | None = None) -> GroupFiles:
        return await self._answer("group_files", group_id=group_id, folder_id=folder_id)

    async def upload_group_file(self, group_id: int, *, data: bytes, name: str, folder_id: str | None = None) -> str:
        return await self._answer("upload_group_file", group_id=group_id, data=data, name=name, folder_id=folder_id)

    async def send(
        self, *, group_id: int | None, user_id: int, segments: list[Segment], reply_timeout: float | None = None
    ) -> int:
        return await self._answer(
            "send", group_id=group_id, user_id=user_id, segments=segments, reply_timeout=reply_timeout
        )

    async def send_forward(
        self, *, group_id: int | None, user_id: int, nodes: list[Node], reply_timeout: float | None = None
    ) -> Sent:
        return await self._answer(
            "send_forward", group_id=group_id, user_id=user_id, nodes=nodes, reply_timeout=reply_timeout
        )

    async def message(self, *, group_id: int | None, user_id: int, message_id: int) -> Message:
        return await self._answer("message", group_id=group_id, user_id=user_id, message_id=message_id)

    async def forward(self, forward_id: str) -> list[ForwardNode]:
        return await self._answer("forward", forward_id=forward_id)

    async def history(self, group_id: int, *, before: int | None = None, limit: int = 20) -> list[Message]:
        return await self._answer("history", group_id=group_id, before=before, limit=limit)

    async def react(self, *, group_id: int, message_id: int, emoji: str) -> None:
        return await self._answer("react", group_id=group_id, message_id=message_id, emoji=emoji)

    async def recall(self, *, group_id: int | None, user_id: int, message_id: int) -> None:
        return await self._answer("recall", group_id=group_id, user_id=user_id, message_id=message_id)

    async def media_url(self, segment: Segment, *, group_id: int | None, user_id: int) -> str:
        return await self._answer("media_url", segment=segment, group_id=group_id, user_id=user_id)

    async def decide_request(self, observed: Observed, *, approve: bool, reason: str | None = None) -> None:
        return await self._answer("decide_request", observed=observed, approve=approve, reason=reason)

    async def call(self, action: str, params: dict[str, Any]) -> Any:
        return await self._answer("call", action=action, params=params)


def incoming(
    segments: list[Segment] | str,
    *,
    group_id: int | None,
    user_id: int,
    self_id: int = 10000,
    message_id: int = 1,
    name: str = "",
    role: Role = "member",
    text: str | None = None,
    to_me: bool = False,
    reply: Message | None = None,
    time: int = 1760000000,
    protocol: Protocol = "milky",
) -> Incoming:
    """A message as a backend hands it over (``QQ.incoming``). A string is one text segment; ``text`` defaults to
    the text segments, stripped (an @bot at the start or end isn't part of it)."""
    segs = [text_seg(segments)] if isinstance(segments, str) else segments
    if text is None:
        text = "".join(s["data"]["text"] for s in segs if s["type"] == "text").strip()
    return Incoming(
        message_id=message_id,
        group_id=group_id,
        sender=Sender(user_id, name, role),
        time=time,
        segments=segs,
        self_id=self_id,
        text=text,
        to_me=to_me,
        reply=reply,
        protocol=protocol,
    )
