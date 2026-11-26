"""The KOOK client interface: everything the bot does on KOOK.

``LiveKook`` wraps :class:`KookRest` with lightweight guild-role and emoji caches so every caller works against a
clean, easily mockable interface (``FakeKook`` in tests).
"""

import time
from abc import ABC, abstractmethod
from typing import Any

from yggdrasil.core.log import LOGGER
from yggdrasil.kook.model import Channel, Guild, GuildEmoji, GuildRole, KookError, Login, Member, Message
from yggdrasil.kook.parser import admin_roles_from_list, parse_rest_message
from yggdrasil.kook.rest import KookRest

_CACHE_TTL_S = 300.0


class Kook(ABC):
    @property
    @abstractmethod
    def self_id(self) -> str: ...

    # --- identity / guild / channel / member info ---------------------------------------------------------------

    @abstractmethod
    async def login(self) -> Login: ...

    @abstractmethod
    async def guilds(self) -> list[Guild]: ...

    @abstractmethod
    async def guild(self, guild_id: str) -> Guild: ...

    @abstractmethod
    async def channels(self, guild_id: str) -> list[Channel]: ...

    @abstractmethod
    async def channel(self, channel_id: str) -> Channel: ...

    @abstractmethod
    async def member(self, guild_id: str, user_id: str) -> Member: ...

    @abstractmethod
    async def guild_emojis(self, guild_id: str) -> list[GuildEmoji]: ...

    async def guild_roles(self, guild_id: str) -> list[GuildRole]:
        """Guild roles (default empty for lightweight test doubles)."""
        return []

    @abstractmethod
    async def pinned_messages(self, channel_id: str, *, limit: int = 20) -> list[Message]: ...

    # --- messages & reactions -----------------------------------------------------------------------------------

    @abstractmethod
    async def send_kmarkdown(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        content: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str: ...

    @abstractmethod
    async def send_card(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        card_json: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str: ...

    @abstractmethod
    async def send_image_url(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        url: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str: ...

    @abstractmethod
    async def send_file_url(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        url: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str: ...

    @abstractmethod
    async def upload_asset(self, data: bytes, filename: str) -> str: ...

    @abstractmethod
    async def message(
        self, *, channel_id: str | None, msg_id: str, guild_id: str | None = None, chat_code: str | None = None
    ) -> Message: ...

    @abstractmethod
    async def history(
        self, channel_id: str, *, guild_id: str | None = None, before: str | None = None, limit: int = 50
    ) -> list[Message]:
        """A page of channel history, oldest first."""

    @abstractmethod
    async def react(self, *, channel_id: str | None, msg_id: str, emoji: str) -> None: ...

    @abstractmethod
    async def unreact(self, *, channel_id: str | None, msg_id: str, emoji: str, user_id: str | None = None) -> None: ...

    @abstractmethod
    async def recall(self, *, channel_id: str | None, msg_id: str) -> None: ...

    @abstractmethod
    async def call(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """Raw KOOK API v3 call (used by the ops agent's ``kook_api``)."""


class LiveKook(Kook):
    """Production KOOK client backed by :class:`KookRest`."""

    def __init__(self, rest: KookRest, login: Login) -> None:
        self.rest = rest
        self._login = login
        # guild_id -> (fetched_at, owner_id, admin_role_ids, role_map)
        self._guild_meta: dict[str, tuple[float, str, set[int], dict[int, GuildRole]]] = {}
        # guild_id -> (fetched_at, {emoji_id: GuildEmoji})
        self._emoji_cache: dict[str, tuple[float, dict[str, GuildEmoji]]] = {}

    @property
    def self_id(self) -> str:
        return self._login.user_id

    async def login(self) -> Login:
        return self._login

    async def _meta(self, guild_id: str) -> tuple[str, set[int], dict[int, GuildRole]]:
        cached = self._guild_meta.get(guild_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _CACHE_TTL_S:
            return cached[1], cached[2], cached[3]
        owner_id = ""
        role_map: dict[int, GuildRole] = {}
        try:
            g = await self.rest.guild_view(guild_id)
            owner_id = str(g.get("user_id") or "")
        except KookError as e:
            LOGGER.debug("guild_view {} failed while caching meta: {}", guild_id, e)
        try:
            raw_roles = await self.rest.guild_role_list(guild_id)
            admin_ids = admin_roles_from_list(raw_roles)
            for r in raw_roles:
                if r.get("role_id") is not None:
                    rid = int(r["role_id"])
                    role_map[rid] = GuildRole(rid, str(r.get("name") or ""), int(r.get("permissions") or 0))
        except KookError as e:
            LOGGER.debug("guild_role_list {} failed while caching meta: {}", guild_id, e)
            admin_ids = set()
        self._guild_meta[guild_id] = (now, owner_id, admin_ids, role_map)
        return owner_id, admin_ids, role_map

    async def guild_role_info(self, guild_id: str) -> tuple[str, set[int]]:
        """Cached ``(owner_id, admin_role_ids)`` for event parsing."""
        owner_id, admin_ids, _ = await self._meta(guild_id)
        return owner_id, admin_ids

    async def guild_roles(self, guild_id: str) -> list[GuildRole]:
        """Cached KOOK roles for ``guild_id`` (sorted by role_id)."""
        _, _, role_map = await self._meta(guild_id)
        return sorted(role_map.values(), key=lambda r: r.role_id)

    async def guilds(self) -> list[Guild]:
        rows = await self.rest.guild_list()
        return [
            Guild(
                guild_id=str(g["id"]),
                name=str(g.get("name") or ""),
                topic=str(g.get("topic") or ""),
                icon=str(g.get("icon") or ""),
                owner_id=str(g.get("user_id") or ""),
            )
            for g in rows
            if g.get("id")
        ]

    async def guild(self, guild_id: str) -> Guild:
        g = await self.rest.guild_view(guild_id)
        return Guild(
            guild_id=str(g.get("id") or guild_id),
            name=str(g.get("name") or ""),
            topic=str(g.get("topic") or ""),
            icon=str(g.get("icon") or ""),
            owner_id=str(g.get("user_id") or ""),
        )

    async def channels(self, guild_id: str) -> list[Channel]:
        rows = await self.rest.channel_list(guild_id)
        out: list[Channel] = []
        for c in rows:
            cid = str(c.get("id") or "")
            if not cid:
                continue
            is_cat = bool(c.get("is_category")) or int(c.get("type") or 0) == 0
            parent = str(c.get("parent_id") or "") or None
            if parent == "0":
                parent = None
            out.append(
                Channel(
                    channel_id=cid,
                    guild_id=str(c.get("guild_id") or guild_id),
                    name=str(c.get("name") or ""),
                    topic=str(c.get("topic") or ""),
                    type=int(c.get("type") or 0),
                    is_category=is_cat,
                    parent_id=None if is_cat else parent,
                    level=int(c.get("level") or 0),
                )
            )
        return out

    async def channel(self, channel_id: str) -> Channel:
        c = await self.rest.channel_view(channel_id)
        is_cat = bool(c.get("is_category")) or int(c.get("type") or 0) == 0
        parent = str(c.get("parent_id") or "") or None
        if parent == "0":
            parent = None
        return Channel(
            channel_id=str(c.get("id") or channel_id),
            guild_id=str(c.get("guild_id") or ""),
            name=str(c.get("name") or ""),
            topic=str(c.get("topic") or ""),
            type=int(c.get("type") or 0),
            is_category=is_cat,
            parent_id=None if is_cat else parent,
            level=int(c.get("level") or 0),
        )

    async def member(self, guild_id: str, user_id: str) -> Member:
        u = await self.rest.user_view(user_id, guild_id=guild_id)
        owner_id, admin_ids, role_map = await self._meta(guild_id)
        raw_roles = [int(r) for r in (u.get("roles") or []) if str(r).isdigit()]
        role = "owner" if user_id == owner_id else ("admin" if any(r in admin_ids for r in raw_roles) else "member")
        role_names = tuple(role_map[r].name for r in raw_roles if r in role_map and role_map[r].name)
        joined = int(u.get("joined_at") or 0)
        active = int(u.get("active_time") or 0)
        return Member(
            guild_id=guild_id,
            user_id=str(u.get("id") or user_id),
            username=str(u.get("username") or ""),
            nickname=str(u.get("nickname") or ""),
            role=role,
            roles=tuple(raw_roles),
            role_names=role_names,
            is_bot=bool(u.get("bot")),
            avatar=str(u.get("avatar") or ""),
            joined_at=(joined // 1000 if joined > 10_000_000_000 else joined) or None,
            active_time=(active // 1000 if active > 10_000_000_000 else active) or None,
        )

    async def guild_emojis(self, guild_id: str) -> list[GuildEmoji]:
        cached = self._emoji_cache.get(guild_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _CACHE_TTL_S:
            return list(cached[1].values())
        rows = await self.rest.guild_emoji_list(guild_id)
        by_id: dict[str, GuildEmoji] = {}
        for e in rows:
            eid = str(e.get("id") or "")
            if eid:
                by_id[eid] = GuildEmoji(
                    id=eid,
                    name=str(e.get("name") or ""),
                    emoji_type=int(e.get("type") or 0),
                    guild_id=guild_id,
                )
        self._emoji_cache[guild_id] = (now, by_id)
        return list(by_id.values())

    async def emoji_url(self, guild_id: str, emoji_id: str) -> str:
        """Resolve a guild custom emoji URL (animated GIF vs static PNG)."""
        cached = self._emoji_cache.get(guild_id)
        if cached is None or time.monotonic() - cached[0] >= _CACHE_TTL_S:
            try:
                await self.guild_emojis(guild_id)
            except KookError:
                pass
            cached = self._emoji_cache.get(guild_id)
        if cached and emoji_id in cached[1]:
            return cached[1][emoji_id].url
        return GuildEmoji(id=emoji_id, name="", emoji_type=0, guild_id=guild_id).url

    async def pinned_messages(self, channel_id: str, *, limit: int = 20) -> list[Message]:
        rows = await self.rest.message_list(channel_id, pin=1, page_size=min(limit, 50))
        return [parse_rest_message(r, channel_id=channel_id, guild_id=None) for r in rows[:limit]]

    async def send_kmarkdown(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        content: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        if channel_id is not None:
            return await self.rest.message_create(target_id=channel_id, content=content, msg_type=9, quote=quote)
        return await self.rest.direct_message_create(
            target_id=user_id, content=content, msg_type=9, quote=quote, chat_code=chat_code
        )

    async def send_card(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        card_json: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        if channel_id is not None:
            return await self.rest.message_create(target_id=channel_id, content=card_json, msg_type=10, quote=quote)
        return await self.rest.direct_message_create(
            target_id=user_id, content=card_json, msg_type=10, quote=quote, chat_code=chat_code
        )

    async def send_image_url(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        url: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        if channel_id is not None:
            return await self.rest.message_create(target_id=channel_id, content=url, msg_type=2, quote=quote)
        return await self.rest.direct_message_create(
            target_id=user_id, content=url, msg_type=2, quote=quote, chat_code=chat_code
        )

    async def send_file_url(
        self,
        *,
        channel_id: str | None,
        user_id: str,
        url: str,
        quote: str | None = None,
        chat_code: str | None = None,
    ) -> str:
        if channel_id is not None:
            return await self.rest.message_create(target_id=channel_id, content=url, msg_type=4, quote=quote)
        return await self.rest.direct_message_create(
            target_id=user_id, content=url, msg_type=4, quote=quote, chat_code=chat_code
        )

    async def upload_asset(self, data: bytes, filename: str) -> str:
        return await self.rest.asset_create(data, filename)

    async def message(
        self, *, channel_id: str | None, msg_id: str, guild_id: str | None = None, chat_code: str | None = None
    ) -> Message:
        if channel_id is not None:
            raw = await self.rest.message_view(msg_id)
        elif chat_code:
            raw = await self.rest.direct_message_view(msg_id, chat_code)
        else:
            raise KookError("direct message lookup requires chat_code")
        return parse_rest_message(raw, channel_id=channel_id, guild_id=guild_id)

    async def history(
        self, channel_id: str, *, guild_id: str | None = None, before: str | None = None, limit: int = 50
    ) -> list[Message]:
        rows = await self.rest.message_list(channel_id, msg_id=before, flag="before", page_size=limit)
        msgs = [parse_rest_message(r, channel_id=channel_id, guild_id=guild_id) for r in rows]
        msgs.sort(key=lambda m: m.time)
        return msgs

    async def react(self, *, channel_id: str | None, msg_id: str, emoji: str) -> None:
        if channel_id is not None:
            await self.rest.message_add_reaction(msg_id, emoji)
        else:
            await self.rest.direct_message_add_reaction(msg_id, emoji)

    async def unreact(self, *, channel_id: str | None, msg_id: str, emoji: str, user_id: str | None = None) -> None:
        if channel_id is not None:
            await self.rest.message_delete_reaction(msg_id, emoji, user_id=user_id)
        else:
            await self.rest.direct_message_delete_reaction(msg_id, emoji, user_id=user_id)

    async def recall(self, *, channel_id: str | None, msg_id: str) -> None:
        if channel_id is not None:
            await self.rest.message_delete(msg_id)
        else:
            await self.rest.direct_message_delete(msg_id)

    async def call(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        m = method.upper()
        if m == "GET":
            return await self.rest.request(m, path, params=params)
        return await self.rest.request(m, path, json=params)


# --- process-wide active connection -----------------------------------------------------------------------------

_active: Kook | None = None


def set_connected(kook: Kook | None) -> None:
    global _active
    _active = kook


def connected() -> Kook | None:
    return _active
