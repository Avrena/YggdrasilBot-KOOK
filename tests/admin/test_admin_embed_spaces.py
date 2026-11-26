"""Admin API tests for deleting embedding spaces."""

from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from yggdrasil.admin import common
from yggdrasil.db.models import ConfigAudit, EmbedSpace


class _FakeSession:
    def __init__(self, spaces: dict[int, EmbedSpace]) -> None:
        self.spaces = spaces
        self.executed: list[tuple[str, Any]] = []
        self.deleted: list[object] = []
        self.added: list[object] = []
        self.flushed = False

    async def execute(self, stmt: Any, params: Any = None) -> None:
        self.executed.append((str(stmt), params))

    async def get(self, model: type[Any], ident: Any) -> Any:
        if model is EmbedSpace:
            return self.spaces.get(ident)
        return None

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)
        if isinstance(obj, EmbedSpace):
            self.spaces.pop(obj.id, None)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed = True


async def test_delete_embed_space_removes_active_space_and_publishes_reload(
    client: httpx.AsyncClient, published: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    space = EmbedSpace(id=1, provider_id=7, model="grok-4.7", dim=1024, active=True, status="ready")
    fake = _FakeSession({1: space})

    @asynccontextmanager
    async def _fake_session():
        yield fake

    monkeypatch.setattr(common, "get_session", _fake_session)

    resp = await client.delete("/api/embed-spaces/1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert fake.deleted == [space]
    assert fake.flushed is True
    assert any("pg_advisory_xact_lock" in sql for sql, _ in fake.executed)
    assert published == ["embed_space.delete 1"]
    audits = [obj for obj in fake.added if isinstance(obj, ConfigAudit)]
    assert len(audits) == 1
    assert audits[0].entity == "embed_space"
    assert audits[0].action == "delete"
    assert audits[0].entity_id == "1"
    assert audits[0].detail == {"provider_id": 7, "model": "grok-4.7", "active": True, "status": "ready"}


async def test_delete_embed_space_missing_returns_404(
    client: httpx.AsyncClient, published: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSession({})

    @asynccontextmanager
    async def _fake_session():
        yield fake

    monkeypatch.setattr(common, "get_session", _fake_session)

    resp = await client.delete("/api/embed-spaces/999")
    assert resp.status_code == 404, resp.text
    assert published == []
