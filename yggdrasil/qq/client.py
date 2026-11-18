"""The QQ client interface: everything the bot does on QQ, whichever protocol the account is connected through.

``client(bot)`` returns the implementation for a connected NoneBot bot (``qq.milky`` or ``qq.onebot``);
``connected()`` the one in use (the first connected bot), or None. Implementations translate segments to and from
``qq.segments``, raise ``qq.model`` errors (never an adapter's own exceptions) and go through ``bot.call_api``, so
``qq.hooks`` sees every action.

A chat is a group (``group_id``) or, with ``group_id`` None, the private chat with ``user_id``.
"""

from abc import ABC, abstractmethod
from typing import Any

from nonebot.adapters import Bot, Event

from yggdrasil.qq.model import (
    ForwardNode,
    Group,
    Incoming,
    Login,
    Member,
    Message,
    Node,
    Observed,
    Protocol,
    Sent,
)
from yggdrasil.qq.segments import Segment


class QQ(ABC):
    protocol: Protocol

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    @property
    def self_id(self) -> int:
        return int(self.bot.self_id)

    # --- events (the plugin calls these once per adapter event) ---------------------------------------------------
    @abstractmethod
    async def incoming(self, event: Event) -> Incoming | None:
        """The message event as ``Incoming`` (None for other events). Also the bot's own messages, when the
        protocol reports them (``Incoming.sender.user_id == self_id``)."""

    @abstractmethod
    def recalled(self, event: Event) -> tuple[int, int] | None:
        """(group_id, message_id) of a group message recall notice, else None."""

    @abstractmethod
    def observe(self, event: Event) -> Observed | None:
        """A request or notice to record (all of them; ``events`` decides what it keeps), else None."""

    # --- identity / info --------------------------------------------------------------------------------------
    @abstractmethod
    async def login(self) -> Login: ...

    @abstractmethod
    async def group(self, group_id: int) -> Group: ...

    @abstractmethod
    async def member(self, group_id: int, user_id: int, *, reply_timeout: float | None = None) -> Member: ...

    # --- messages ---------------------------------------------------------------------------------------------
    @abstractmethod
    async def send(
        self, *, group_id: int | None, user_id: int, segments: list[Segment], reply_timeout: float | None = None
    ) -> int:
        """Send outgoing ``segments`` (text, mention, reply, image); returns the message id.

        ``reply_timeout``: seconds to wait for QQ's answer instead of the adapter default (pictures upload slowly).
        Raises ``Dropped`` (group send taken but not posted), ``Unconfirmed`` (maybe posted) or ``QQError``."""

    @abstractmethod
    async def send_forward(
        self, *, group_id: int | None, user_id: int, nodes: list[Node], reply_timeout: float | None = None
    ) -> Sent:
        """Send a merged forward; errors as ``send``. No title/preview/summary overrides: a preview that doesn't
        match the nodes trips QQ's risk control ("risky link" warnings), so QQ derives them from the nodes."""

    @abstractmethod
    async def message(self, *, group_id: int | None, user_id: int, message_id: int) -> Message: ...

    @abstractmethod
    async def forward(self, forward_id: str) -> list[ForwardNode]: ...

    @abstractmethod
    async def history(self, group_id: int, *, before: int | None = None, limit: int = 20) -> list[Message]:
        """A page of the group's history, oldest first: message ``before`` and older (the latest page when None)."""

    @abstractmethod
    async def react(self, *, group_id: int, message_id: int, emoji: str) -> None:
        """Add the QQ face ``emoji`` (a face id) as a reaction to a group message (QQ has none in private chats)."""

    @abstractmethod
    async def recall(self, *, group_id: int | None, user_id: int, message_id: int) -> None: ...

    # --- media ------------------------------------------------------------------------------------------------
    @abstractmethod
    async def media_url(self, segment: Segment, *, group_id: int | None, user_id: int) -> str:
        """A working download link for an ``image``/``record``/``video``/``file`` segment: a fresh one for media
        (by ``ref``), the download link for a file. Raises ``QQError`` when there is none."""

    # --- requests and raw access ------------------------------------------------------------------------------
    @abstractmethod
    async def decide_request(self, observed: Observed, *, approve: bool, reason: str | None = None) -> None:
        """Answer a request this protocol recorded (``observed.data`` is its raw event). ``QQError`` when it
        can't (another protocol recorded it, QQ refused)."""

    @abstractmethod
    async def call(self, action: str, params: dict[str, Any]) -> Any:
        """A raw protocol action (the ops agent's ``qq_api``); ``QQError`` on failure."""


def client(bot: Bot) -> QQ:
    """The implementation for ``bot``'s adapter. ``TypeError`` for a bot of another adapter."""
    from nonebot.adapters.milky import Bot as MilkyBot
    from nonebot.adapters.onebot.v11 import Bot as OneBotBot

    if isinstance(bot, MilkyBot):
        from yggdrasil.qq.milky import MilkyQQ

        return MilkyQQ(bot)
    if isinstance(bot, OneBotBot):
        from yggdrasil.qq.onebot import OneBotQQ

        return OneBotQQ(bot)
    raise TypeError(f"not a QQ bot: {type(bot).__name__}")


def connected() -> QQ | None:
    """The client of a connected QQ bot (Milky first), or None."""
    import nonebot

    bots = list(nonebot.get_bots().values())
    bots.sort(key=lambda b: b.adapter.get_name() != "Milky")
    for bot in bots:
        try:
            return client(bot)
        except TypeError:
            continue
    return None
