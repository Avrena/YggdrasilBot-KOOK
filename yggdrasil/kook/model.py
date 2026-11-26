"""KOOK domain models: messages, incoming events, guilds, channels, members, notices and errors."""

from dataclasses import dataclass, field
from typing import Any, Literal

from yggdrasil.kook.segments import Segment

Role = Literal["owner", "admin", "member"]


@dataclass(frozen=True, slots=True)
class Sender:
    user_id: str
    name: str  # guild nickname if set, else username ("" when unknown)
    username: str = ""
    role: Role = "member"
    is_bot: bool = False
    avatar: str = ""


@dataclass(slots=True)
class Message:
    """A KOOK message in a text channel (``channel_id`` + ``guild_id``) or a direct message (``channel_id`` None)."""

    message_id: str  # KOOK UUID msg_id
    channel_id: str | None
    guild_id: str | None
    sender: Sender
    time: int  # unix seconds
    segments: list[Segment]


@dataclass(slots=True)
class Incoming(Message):
    """An incoming message event parsed for routing and pipeline execution."""

    self_id: str = ""
    text: str = ""  # plain text addressed to the bot (leading/trailing @bot stripped)
    to_me: bool = False  # @bot anywhere, reply to bot, or direct message
    reply: Message | None = None  # quoted message if present
    channel_name: str = ""
    chat_code: str | None = None  # KOOK direct-message session code

    @property
    def user_id(self) -> str:
        return self.sender.user_id


@dataclass(frozen=True, slots=True)
class Login:
    user_id: str
    username: str
    identify_num: str = ""
    avatar: str = ""

    @property
    def nickname(self) -> str:
        return self.username


@dataclass(frozen=True, slots=True)
class Guild:
    guild_id: str
    name: str
    topic: str = ""
    icon: str = ""
    owner_id: str = ""


@dataclass(frozen=True, slots=True)
class Channel:
    channel_id: str
    guild_id: str
    name: str
    topic: str = ""
    type: int = 1  # 1=text, 2=voice, 0=category
    is_category: bool = False
    parent_id: str | None = None
    level: int = 0  # KOOK position/sort order


@dataclass(frozen=True, slots=True)
class GuildRole:
    role_id: int
    name: str
    permissions: int = 0


@dataclass(frozen=True, slots=True)
class Member:
    guild_id: str
    user_id: str
    username: str
    nickname: str = ""
    role: Role = "member"
    roles: tuple[int, ...] = ()
    role_names: tuple[str, ...] = ()
    is_bot: bool = False
    avatar: str = ""
    joined_at: int | None = None  # epoch seconds
    active_time: int | None = None  # epoch seconds

    @property
    def display_name(self) -> str:
        return self.nickname or self.username


@dataclass(frozen=True, slots=True)
class GuildEmoji:
    id: str
    name: str
    emoji_type: int = 0  # 0=static, 1=animated
    guild_id: str = ""

    @property
    def url(self) -> str:
        """KOOK CDN URL for a guild custom emoji (``<guild_or_prefix>/<hash>``)."""
        if self.id.startswith(("http://", "https://")):
            return self.id
        ext = "gif" if self.emoji_type == 1 else "png"
        return f"https://img.kookapp.cn/emojis/{self.id}.{ext}"


@dataclass(slots=True)
class Observed:
    """A non-message KOOK system event worth recording in ``bot_events``."""

    kind: str
    guild_id: str | None = None
    channel_id: str | None = None
    user_id: str | None = None
    operator_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


# --- errors -----------------------------------------------------------------------------------------------------


class KookError(Exception):
    """A KOOK API or gateway operation failed."""

    def __init__(self, message: str, *, code: int | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class KookAuthError(KookError):
    """Bot token is missing, invalid, or revoked."""


class KookRateLimited(KookError):
    """KOOK returned HTTP 429; ``retry_after`` is seconds to wait."""

    def __init__(self, message: str, *, retry_after: float = 1.0) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after
