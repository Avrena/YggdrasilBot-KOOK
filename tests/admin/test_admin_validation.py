"""Validation tests for the KOOK Admin API triggers endpoint."""

import httpx
import pytest

VALID_TRIGGER = {"handler": "tldr", "match_type": "regex", "patterns": [r"总结最近(\d+)条"], "arg_template": "-n {0}"}


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"patterns": ["总结(最近"]}, "无法编译"),
        ({"allowed_roles": ["member", "moderator"]}, "未知角色"),
        ({"match_type": "fuzzy"}, "未知匹配方式"),
        ({"target": "everyone"}, "未知目标"),
        ({"handler": "rm_rf"}, "未知 handler"),
        ({"scope": "channel"}, "scope_id"),
        ({"scope": "guild"}, "scope_id"),
        ({"scope": "category"}, "scope_id"),
        ({"scope": "global", "scope_id": "123"}, "scope_id"),
        ({"patterns": ["  "]}, "至少需要一个 pattern"),
        ({"arg_template": "-n {user}"}, "参数模板"),
    ],
)
async def test_trigger_validation_rejects(
    client: httpx.AsyncClient, published: list[str], patch: dict, needle: str
) -> None:
    resp = await client.post("/api/triggers", json={**VALID_TRIGGER, **patch})
    assert resp.status_code == 422, resp.text
    assert needle in resp.text
    assert published == []
