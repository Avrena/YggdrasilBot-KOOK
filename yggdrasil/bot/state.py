"""Process-wide bot state: identity cache, connection and heartbeat tracking (keyed by the bot's ``self_id``)."""

import time
from dataclasses import dataclass

from yggdrasil.qq.client import QQ
from yggdrasil.qq.model import QQError


@dataclass(slots=True)
class Identity:
    self_id: int
    nickname: str


_identities: dict[str, Identity] = {}
connected: dict[str, float] = {}
last_heartbeat: dict[str, float] = {}
# Accounts the protocol reported logged out (``notice.bot_offline``) while the connection stays up; cleared on
# reconnect.
offline: set[str] = set()
# Latest message id from someone other than the bot, per group (for quote-when-busy).
last_group_message: dict[int, int] = {}


def should_quote(group_id: int | None, trigger_message_id: int, mode: str) -> bool:
    if group_id is None or mode == "never":
        return False
    if mode == "always":
        return True
    return last_group_message.get(group_id, trigger_message_id) != trigger_message_id


async def identity(qq: QQ) -> Identity:
    key = str(qq.self_id)
    ident = _identities.get(key)
    if ident is None:
        try:
            login = await qq.login()
            ident = Identity(login.user_id, login.nickname)
        except QQError:
            ident = Identity(qq.self_id, "Bot")
        _identities[key] = ident
    return ident


def mark_connected(self_id: str) -> None:
    connected[self_id] = time.time()
    last_heartbeat[self_id] = time.time()
    offline.discard(self_id)


def mark_disconnected(self_id: str) -> None:
    connected.pop(self_id, None)
    _identities.pop(self_id, None)


def heartbeat(self_id: str) -> None:
    last_heartbeat[self_id] = time.time()
