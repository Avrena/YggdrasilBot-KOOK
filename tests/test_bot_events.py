from collections.abc import Callable

import pytest
from sqlalchemy import delete

from tests.support.groups import fresh_group
from tests.support.qq import FakeQQ
from yggdrasil import events
from yggdrasil.db.models import BotEvent
from yggdrasil.db.session import get_session
from yggdrasil.qq import client as qq_client
from yggdrasil.qq.model import Observed
from yggdrasil.runtime import GroupInfo, Snapshot

BOT, ENABLED, OTHER = 1000, 2000, 3000


@pytest.fixture(autouse=True)
def _groups(snapshot: Callable[..., Snapshot]) -> None:
    snapshot(groups={ENABLED: GroupInfo(ENABLED, "g", True, {}), OTHER: GroupInfo(OTHER, "o", False, {})})


def _kept(kind: str, group_id: int | None, user_id: int | None) -> bool:
    return events.kept(Observed(kind, "milky", group_id, user_id), BOT)


def test_bot_self_events_are_kept_for_any_group_member_changes_only_for_enabled_ones() -> None:
    assert _kept("notice.group_decrease.kick_me", OTHER, BOT)
    assert _kept("notice.group_increase.self", OTHER, BOT)
    assert _kept("notice.group_ban.self.ban", OTHER, BOT)
    assert _kept("request.group.invite", OTHER, 42)
    assert _kept("notice.bot_offline", None, BOT)
    assert not _kept("notice.group_decrease.leave", OTHER, 42)
    assert _kept("notice.group_decrease.leave", ENABLED, 42)
    assert not _kept("notice.group_ban.all.ban", OTHER, 0)
    assert _kept("notice.group_ban.all.ban", ENABLED, 0)


@pytest.mark.db
async def test_a_request_is_answered_only_through_the_protocol_that_recorded_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_id = fresh_group()
    async with get_session() as s:
        row = BotEvent(
            kind="request.group.invite",
            group_id=group_id,
            user_id=42,
            data={"flag": "abc", "comment": "", "protocol": "onebot"},
            status="pending",
        )
        s.add(row)
        await s.flush()
        event_id = row.id
    milky = FakeQQ({"decide_request": None}, self_id=BOT, protocol="milky")
    onebot = FakeQQ({"decide_request": None}, self_id=BOT, protocol="onebot")
    try:
        monkeypatch.setattr(qq_client, "connected", lambda: milky)
        with pytest.raises(events.EventError, match="OneBot"):
            await events.decide_request(event_id, True, "qq:1")
        assert milky.called("decide_request") == []

        monkeypatch.setattr(qq_client, "connected", lambda: onebot)
        decided = await events.decide_request(event_id, True, "qq:1")  # still pending: the other protocol can
        assert decided.status == "approved"
        [call] = onebot.called("decide_request")
        observed = call["observed"]
        assert (observed.kind, observed.protocol, observed.group_id, observed.data) == (
            "request.group.invite",
            "onebot",
            group_id,
            {"flag": "abc", "comment": ""},
        )
    finally:
        async with get_session() as s:
            await s.execute(delete(BotEvent).where(BotEvent.group_id == group_id))
