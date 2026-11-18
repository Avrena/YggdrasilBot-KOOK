"""Protocol-neutral QQ types: messages, the incoming event the pipeline runs on, members, results and errors."""

from dataclasses import dataclass, field
from typing import Any, Literal

from yggdrasil.qq.segments import Segment

Role = Literal["owner", "admin", "member"]
Protocol = Literal["milky", "onebot"]


@dataclass(frozen=True, slots=True)
class Sender:
    user_id: int
    name: str  # group card, else nickname ("" when unknown)
    role: Role = "member"


@dataclass(slots=True)
class Message:
    """A QQ message in a group (``group_id``) or a private chat (``group_id`` None, the peer is the sender or, for
    the bot's own messages, the user it was sent to)."""

    message_id: int  # the protocol's id (``qq_message_id``): Milky seq, OneBot hash
    group_id: int | None
    sender: Sender
    time: int  # unix seconds
    segments: list[Segment]
    seq: int | None = None  # the group seq when the protocol gives one (Milky: == message_id)


@dataclass(slots=True)
class Incoming(Message):
    """A message event as the bot handles it: the message, plus what the protocol adapter worked out about how it
    addresses the bot."""

    self_id: int = 0
    text: str = ""  # the plain text said to the bot: without the reply, and without an @bot at its start or end
    to_me: bool = False  # @bot, a reply to the bot, or a private message
    reply: Message | None = None  # the quoted message, as fetched from QQ
    protocol: Protocol = "milky"

    @property
    def user_id(self) -> int:
        return self.sender.user_id


@dataclass(frozen=True, slots=True)
class ForwardNode:
    """One message inside a merged forward."""

    user_id: int  # 0 when the protocol doesn't say
    name: str
    time: int
    segments: list[Segment]


@dataclass(frozen=True, slots=True)
class Node:
    """One message of a forward the bot sends: outgoing segments (``qq.segments`` builders)."""

    user_id: int
    name: str
    segments: list[Segment]


@dataclass(frozen=True, slots=True)
class Sent:
    message_id: int
    forward_id: str | None = None  # a forward's id, when the protocol returns it


@dataclass(frozen=True, slots=True)
class Login:
    user_id: int
    nickname: str


@dataclass(frozen=True, slots=True)
class Group:
    group_id: int
    name: str
    member_count: int | None = None


@dataclass(frozen=True, slots=True)
class Member:
    group_id: int
    user_id: int
    nickname: str
    card: str = ""
    role: Role = "member"
    is_robot: bool | None = None  # an official QQ bot account; None when the protocol can't tell


@dataclass(slots=True)
class Observed:
    """A request or notice worth recording in ``bot_events``.

    ``kind``: ``request.friend``, ``request.group.add`` / ``request.group.invite``, ``notice.group_decrease.*``,
    ``notice.group_increase.*``, ``notice.group_ban.*``, ``notice.group_admin.*``, ``notice.bot_offline``.
    ``data`` is the protocol's raw event: only the backend that recorded it reads it back (``QQ.decide_request``).
    """

    kind: str
    protocol: Protocol
    group_id: int | None = None
    user_id: int | None = None
    operator_id: int | None = None
    comment: str = ""  # a request's message
    duration: int | None = None  # a ban's seconds
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_request(self) -> bool:
        return self.kind.startswith("request.")


# --- errors -----------------------------------------------------------------------------------------------------


class QQError(Exception):
    """A QQ action failed; ``str(e)`` is the protocol's reason."""


class Dropped(QQError):
    """QQ took a group send but never posted it (its content filter, as far as we can tell)."""


class Unconfirmed(QQError):
    """A send got no clear answer (timeout, undecodable reply): the message may be in the chat anyway."""
