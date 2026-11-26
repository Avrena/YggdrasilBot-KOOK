"""Defaults written on first start. Everything here is editable from the dashboard afterwards."""

from sqlalchemy import func, select

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import CommandTrigger, SettingsDoc
from yggdrasil.db.session import get_session

DEFAULT_TRIGGERS: list[dict] = [
    {
        "handler": "tldr",
        "scope": "global",
        "scope_id": None,
        "match_type": "keyword_prefix",
        "patterns": ["tldr", "太长不看", "总结一下"],
        "priority": 10,
        "note": "回复一条消息或发链接并输入 tldr",
    },
    {
        "handler": "privacy.optout",
        "scope": "global",
        "scope_id": None,
        "match_type": "keyword_exact",
        "patterns": ["不要记我"],
        "require_mention": True,
        "priority": 5,
    },
    {
        "handler": "privacy.optin",
        "scope": "global",
        "scope_id": None,
        "match_type": "keyword_exact",
        "patterns": ["记得我"],
        "require_mention": True,
        "priority": 5,
    },
    {
        "handler": "privacy.forget",
        "scope": "global",
        "scope_id": None,
        "match_type": "keyword_exact",
        "patterns": ["忘记我"],
        "require_mention": True,
        "priority": 5,
    },
    {"handler": "chat", "scope": "global", "scope_id": None, "match_type": "mention", "patterns": [], "priority": 100},
    {
        "handler": "chat",
        "scope": "global",
        "scope_id": None,
        "match_type": "reply_to_bot",
        "patterns": [],
        "priority": 100,
    },
    {
        "handler": "chat",
        "scope": "global",
        "scope_id": None,
        "match_type": "name_mention",
        "patterns": [],
        "priority": 110,
    },
    {
        "handler": "admin.review",
        "scope": "private",
        "scope_id": None,
        "match_type": "keyword_prefix",
        "patterns": ["通过"],
        "arg_template": "approve {rest}",
        "allowed_roles": ["superuser"],
        "priority": 5,
    },
    {
        "handler": "admin.review",
        "scope": "private",
        "scope_id": None,
        "match_type": "keyword_prefix",
        "patterns": ["拒绝"],
        "arg_template": "reject {rest}",
        "allowed_roles": ["superuser"],
        "priority": 5,
    },
    {
        "handler": "admin.review",
        "scope": "private",
        "scope_id": None,
        "match_type": "keyword_prefix",
        "patterns": ["改"],
        "arg_template": "edit {rest}",
        "allowed_roles": ["superuser"],
        "priority": 5,
    },
    {
        "handler": "admin.status",
        "scope": "private",
        "scope_id": None,
        "match_type": "keyword_exact",
        "patterns": ["状态", "status"],
        "allowed_roles": ["superuser"],
        "priority": 5,
    },
    {
        "handler": "admin.ops",
        "scope": "global",
        "scope_id": None,
        "match_type": "regex",
        "patterns": [r"^(?i:ops)(?![A-Za-z0-9_])"],
        "require_mention": True,
        "allowed_roles": ["superuser"],
        "priority": 5,
        "note": "超级管理员：@bot ops <指令>（私聊直接 ops <指令>），自然语言查询和修改配置",
    },
]


async def seed_defaults() -> None:
    async with get_session() as session:
        if not await session.scalar(select(func.count()).select_from(CommandTrigger)):
            for t in DEFAULT_TRIGGERS:
                session.add(CommandTrigger(**t))
            LOGGER.info("seeded {} default command triggers", len(DEFAULT_TRIGGERS))
        if await session.get(SettingsDoc, "global") is None:
            session.add(SettingsDoc(key="global", value={}))
