"""How KOOK users are shown to models: always ``名字(user_id)``; the bot itself is ``{bot_name}(你)``.

Nicknames change at any time, so ``user_id`` is the stable identity. Stored ``plain_text`` keeps raw ``@<user_id>``
mentions; they are rewritten here at render time with guild-level aliases resolved in one batched query.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message, UserAlias
from yggdrasil.db.session import get_session

UNKNOWN_NAME = "成员"
_STRIP = str.maketrans("", "", '()（）<>"\r\n')
# ``@<user_id>`` written by normalize() for mention segments; skips email addresses.
MENTION = re.compile(r"(?<![A-Za-z0-9._%+-])@(\d{5,20})(?![\d(（])")
_SELF_LOOKBACK = timedelta(days=30)


def clean_name(name: str | None) -> str:
    return " ".join((name or "").translate(_STRIP).split())


def label(name: str | None, user_id: str) -> str:
    return f"{clean_name(name) or UNKNOWN_NAME}({user_id})"


def self_label(bot_name: str) -> str:
    return f"{clean_name(bot_name) or 'bot'}(你)"


def mentioned_ids(texts: Iterable[str]) -> set[str]:
    return {m for text in texts for m in MENTION.findall(text)}


@dataclass(slots=True)
class People:
    """Resolved names for one render; build with :func:`resolve`."""

    bot_name: str
    names: dict[str, str] = field(default_factory=dict)
    self_ids: frozenset[str] = frozenset()

    def who(self, user_id: str, fallback: str | None = None, *, is_self: bool = False) -> str:
        if is_self or user_id in self.self_ids:
            return self_label(self.bot_name)
        return label(self.names.get(user_id) or fallback, user_id)

    def mention(self, user_id: str) -> str:
        return self.who(user_id)

    def mentions(self, text: str) -> str:
        return MENTION.sub(lambda m: "@" + self.who(m.group(1)), text)


async def resolve(
    guild_id: str | None,
    user_ids: Iterable[str] = (),
    *,
    bot_name: str,
    self_id: str | None = None,
    texts: Iterable[str] = (),
) -> People:
    """Latest guild-level alias of every user in ``user_ids`` and every ``@<uid>`` in ``texts``, in one query."""
    ids = {str(u).strip() for u in user_ids if u} | mentioned_ids(texts)
    self_ids = {self_id} if self_id else set()
    if not guild_id or (not ids and self_ids):
        return People(bot_name, {}, frozenset(self_ids))
    names: dict[str, str] = {}
    async with get_session() as session:
        if ids:
            rows = await session.execute(
                select(UserAlias.user_id, UserAlias.name)
                .where(UserAlias.guild_id == guild_id, UserAlias.user_id.in_(ids))
                .ext(distinct_on(UserAlias.user_id))
                .order_by(UserAlias.user_id, UserAlias.last_seen.desc())
            )
            names = {u: n for u, n in rows.all() if n}
        if not self_ids:
            found = await session.scalar(
                select(Message.user_id)
                .where(
                    Message.guild_id == guild_id,
                    Message.is_self,
                    Message.sent_at > datetime.now(UTC) - _SELF_LOOKBACK,
                )
                .order_by(Message.sent_at.desc())
                .limit(1)
            )
            if found:
                self_ids.add(found)
    return People(bot_name, names, frozenset(self_ids))


async def names_in_guilds(pairs: Iterable[tuple[str | None, str | None]]) -> dict[tuple[str, str], str]:
    """Latest alias per ``(guild_id, user_id)`` across several guilds, in one query."""
    keys = {(g, u) for g, u in pairs if g and u}
    if not keys:
        return {}
    async with get_session() as session:
        rows = await session.execute(
            select(UserAlias.guild_id, UserAlias.user_id, UserAlias.name)
            .where(tuple_(UserAlias.guild_id, UserAlias.user_id).in_(keys))
            .ext(distinct_on(UserAlias.guild_id, UserAlias.user_id))
            .order_by(UserAlias.guild_id, UserAlias.user_id, UserAlias.last_seen.desc())
        )
        return {(g, u): n for g, u, n in rows.all() if n}


async def member_label(guild_id: str | None, user_id: str | None) -> str:
    """Human-facing ``名字(user_id)`` when known in the guild, else bare ``user_id``."""
    if not guild_id or not user_id:
        return str(user_id or "")
    try:
        name = (await names_in_guilds([(guild_id, user_id)])).get((guild_id, user_id))
    except Exception:
        LOGGER.exception("alias lookup failed")
        name = None
    return label(name, user_id) if name else user_id
