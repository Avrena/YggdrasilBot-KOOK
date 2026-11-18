import random

import pytest
from sqlalchemy import delete

from tests.support.groups import fresh_group
from yggdrasil.agent.permissions import may_recall
from yggdrasil.db.models import Invocation
from yggdrasil.db.session import get_session
from yggdrasil.ops.tools import merge_patch, qq_gate
from yggdrasil.tools.errors import ToolError


def test_qq_gate_reads_freely_writes_by_allowlist_and_never_leaks_or_leaves() -> None:
    allowed = ["set_group_ban", "set_group_leave", "set_group_member_mute", "quit_group"]
    assert qq_gate("onebot", "get_group_list", allowed) is False
    assert qq_gate("onebot", "_get_group_notice", allowed) is False
    assert qq_gate("onebot", "set_group_ban", allowed) is True
    for action in ("get_cookies", "get_csrf_token", "nc_get_rkey", "set_group_kick", "set_group_leave"):
        with pytest.raises(ToolError):
            qq_gate("onebot", action, allowed)
    # Each protocol's own names: Milky's never-list holds even when the allow-list names it.
    assert qq_gate("milky", "get_group_member_list", allowed) is False
    assert qq_gate("milky", "set_group_member_mute", allowed) is True
    for action in ("quit_group", "accept_group_invitation", "get_cookies", "kick_group_member"):
        with pytest.raises(ToolError):
            qq_gate("milky", action, allowed)


def test_merge_patch_merges_nested_and_null_removes_back_to_inherited() -> None:
    doc = {"moderation": {"default_rule": {"threshold": 0.5}, "enabled": True}, "persona": {"name": "x"}}
    out = merge_patch(doc, {"moderation": {"default_rule": {"threshold": None}, "check_input": False}, "persona": None})
    # Emptied sections disappear entirely so the inherited value applies again.
    assert out == {"moderation": {"enabled": True, "check_input": False}}
    assert doc["persona"] == {"name": "x"}  # input untouched


@pytest.mark.db
async def test_only_the_asker_or_group_staff_may_recall_a_bot_answer() -> None:
    group_id, asker, other = fresh_group(), 111, 222
    bot_msg = random.randint(1, 2**31)
    async with get_session() as s:
        s.add(Invocation(group_id=group_id, user_id=asker, handler="chat", reply_qq_message_id=bot_msg, status="ok"))
    try:
        assert await may_recall("member", asker, group_id, bot_msg)
        assert not await may_recall("member", other, group_id, bot_msg)
        assert not await may_recall("member", asker, group_id, bot_msg + 1)  # not an answer we know of
        assert await may_recall("admin", other, group_id, bot_msg)
        assert await may_recall("superuser", other, group_id, bot_msg + 1)
    finally:
        async with get_session() as s:
            await s.execute(delete(Invocation).where(Invocation.group_id == group_id))
