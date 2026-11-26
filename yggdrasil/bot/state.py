"""Process-wide bot state: identity cache, connection tracking and per-channel latest message (for quote-when-busy)."""

import time
from dataclasses import dataclass

from yggdrasil.kook.client import Kook
from yggdrasil.kook.model import KookError


@dataclass(slots=True)
class Identity:
    self_id: str
    nickname: str


_identities: dict[str, Identity] = {}
connected: dict[str, float] = {}
last_heartbeat: dict[str, float] = {}
# Latest non-bot message id per channel (for quote-when-busy).
last_channel_message: dict[str, str] = {}


def should_quote(channel_id: str | None, trigger_message_id: str, mode: str) -> bool:
    if not trigger_message_id or mode == "never":
        return False
    if mode == "always":
        return True
    if channel_id is None:
        return False
    return last_channel_message.get(channel_id, trigger_message_id) != trigger_message_id


async def identity(kook: Kook) -> Identity:
    key = kook.self_id
    ident = _identities.get(key)
    if ident is None:
        try:
            login = await kook.login()
            ident = Identity(login.user_id, login.username)
        except KookError:
            ident = Identity(kook.self_id, "Bot")
        _identities[key] = ident
    return ident


def mark_connected(self_id: str) -> None:
    connected[self_id] = time.time()
    last_heartbeat[self_id] = time.time()


def mark_disconnected(self_id: str) -> None:
    connected.pop(self_id, None)
    _identities.pop(self_id, None)


def heartbeat(self_id: str) -> None:
    last_heartbeat[self_id] = time.time()
