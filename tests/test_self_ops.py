"""Tests for KOOK ops agent guardrails: ``kook_gate`` allowlist and sparse settings ``_merge_patch``."""

import pytest

from yggdrasil.ops.tools import kook_gate, merge_patch
from yggdrasil.tools.errors import ToolError


def test_kook_gate_reads_freely_writes_by_allowlist_and_blocks_destructive():
    allowed = ["/message/pin", "/message/unpin", "/channel/update"]
    assert kook_gate("GET", "/guild/list", allowed) is False
    assert kook_gate("get", "channel/list", allowed) is False
    assert kook_gate("POST", "/message/pin", allowed) is True

    with pytest.raises(ToolError, match="不允许"):
        kook_gate("POST", "/guild/leave", ["/guild/leave"])
    with pytest.raises(ToolError, match="不允许"):
        kook_gate("POST", "/user/offline", ["/user/offline"])
    with pytest.raises(ToolError, match="不在允许"):
        kook_gate("POST", "/message/delete", allowed)
    with pytest.raises(ToolError, match="只支持 GET 或 POST"):
        kook_gate("DELETE", "/channel/1", allowed)


def test_merge_patch_merges_nested_and_null_removes_back_to_inherited():
    base = {"persona": {"name": "A", "style": "short"}, "features": {"tldr": False}}
    merged = merge_patch(base, {"persona": {"name": None, "aliases": ["树"]}, "features": {"tldr": True}})
    assert merged == {"persona": {"style": "short", "aliases": ["树"]}, "features": {"tldr": True}}
    # Removing the last key of a section drops the empty dict cleanly
    cleared = merge_patch({"features": {"tldr": False}}, {"features": {"tldr": None}})
    assert cleared == {}
