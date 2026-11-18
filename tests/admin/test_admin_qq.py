"""The Milky connection settings: the token is write-only, every change is audited and reloads the bot."""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete, select

from tests.admin.helpers import ADMIN
from yggdrasil.crypto import decrypt
from yggdrasil.db.models import ConfigAudit, SettingsDoc
from yggdrasil.db.session import get_session
from yggdrasil.qq.milky_link import KEY

TOKEN = "milky-secret-token"


@pytest.fixture
async def milky_row() -> AsyncIterator[None]:
    """Run against no stored Milky setting; put back whatever was there, drop this test's audit rows."""
    async with get_session() as s:
        before = await s.get(SettingsDoc, KEY)
        saved = dict(before.value) if before else None
        if before is not None:
            await s.delete(before)
    yield
    async with get_session() as s:
        await s.execute(delete(ConfigAudit).where(ConfigAudit.actor == ADMIN, ConfigAudit.entity == "qq"))
        await s.execute(delete(SettingsDoc).where(SettingsDoc.key == KEY))
        if saved is not None:
            s.add(SettingsDoc(key=KEY, value=saved))


@pytest.mark.db
async def test_the_token_is_stored_encrypted_and_never_shown(
    client: httpx.AsyncClient, published: list[str], milky_row: None
) -> None:
    resp = await client.put("/api/qq/milky", json={"url": "http://yogurt:3000/", "token": f" {TOKEN} "})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["url"], body["token_set"]) == ("http://yogurt:3000", True)
    # The test process runs no bot, so there is no link to show.
    assert body["link"]["managed"] is False
    got = await client.get("/api/qq/milky")
    assert TOKEN not in resp.text
    assert TOKEN not in got.text
    assert got.json()["token_set"] is True

    async with get_session() as s:
        row = await s.get(SettingsDoc, KEY)
        assert row is not None
        assert decrypt(row.value["token_enc"].encode()) == TOKEN
        audit = (
            await s.scalars(select(ConfigAudit).where(ConfigAudit.actor == ADMIN, ConfigAudit.entity == "qq"))
        ).all()
    assert [(a.entity, a.entity_id, a.action) for a in audit] == [("qq", "milky", "update")]
    assert TOKEN not in str(audit[0].detail)
    assert published == ["qq.update milky"]

    # A new URL alone keeps the token.
    kept = (await client.put("/api/qq/milky", json={"url": "https://milky.example.com"})).json()
    assert (kept["url"], kept["token_set"]) == ("https://milky.example.com:443", True)


@pytest.mark.db
async def test_clearing_the_token_then_the_setting(
    client: httpx.AsyncClient, published: list[str], milky_row: None
) -> None:
    assert (await client.put("/api/qq/milky", json={"url": "http://yogurt:3000", "token": TOKEN})).status_code == 200

    cleared = await client.delete("/api/qq/milky/token")
    assert cleared.status_code == 200, cleared.text
    assert (cleared.json()["url"], cleared.json()["token_set"]) == ("http://yogurt:3000", False)
    async with get_session() as s:
        row = await s.get(SettingsDoc, KEY)
        assert row is not None
        assert "token_enc" not in row.value

    gone = await client.delete("/api/qq/milky")
    assert gone.status_code == 200, gone.text
    assert (gone.json()["url"], gone.json()["token_set"]) == (None, False)
    assert (await client.get("/api/qq/milky")).json()["url"] is None
    assert published == ["qq.update milky", "qq.clear_token milky", "qq.delete milky"]


async def test_a_url_with_a_path_is_refused(client: httpx.AsyncClient, published: list[str]) -> None:
    resp = await client.put("/api/qq/milky", json={"url": "http://yogurt:3000/api", "token": TOKEN})
    assert resp.status_code == 422
    assert "不带路径" in resp.text
    assert published == []
