"""Tests for KOOK WebSocket frame decoding and ``EventSequencer``."""

import json
import zlib

import pytest

from yggdrasil.kook.gateway import EventSequencer, build_resume_url, decode_frame


def test_decode_frame_handles_plain_and_zlib_bytes():
    payload = {"s": 0, "sn": 7, "d": {"type": 9, "content": "你好 KOOK"}}
    raw_json = json.dumps(payload, ensure_ascii=False)
    assert decode_frame(raw_json) == payload
    assert decode_frame(zlib.compress(raw_json.encode("utf-8"))) == payload
    with pytest.raises((zlib.error, ValueError)):
        decode_frame(b"not-valid-zlib-or-json")
    with pytest.raises(ValueError, match="not a JSON object"):
        decode_frame("[]")


def test_build_resume_url_appends_resume_params():
    url = build_resume_url("wss://ws.kookapp.cn/gateway?compress=1", sn=42, session_id="sess-9")
    assert "compress=1" in url
    assert "resume=1" in url
    assert "sn=42" in url
    assert "session_id=sess-9" in url


def test_event_sequencer_orders_dedups_and_flushes_overdue():
    seq = EventSequencer()

    # In-order delivery
    assert [e["n"] for e in seq.push(1, {"n": 1})] == [1]
    assert seq.last_sn == 1

    # Duplicate sn ignored
    assert seq.push(1, {"n": 1}) == []

    # Out-of-order: sn=3 arrives before sn=2
    assert seq.push(3, {"n": 3}) == []
    assert seq.last_sn == 1

    # sn=2 arrives -> delivers 2 and 3 in order
    got = seq.push(2, {"n": 2})
    assert [e["n"] for e in got] == [2, 3]
    assert seq.last_sn == 3

    # Gap (sn=4 missing): sn=5 buffers, then flush_overdue drains it
    assert seq.push(5, {"n": 5}) == []
    flushed = seq.flush_overdue()
    assert [e["n"] for e in flushed] == [5]
    assert seq.last_sn == 5

    # Reset clears state
    seq.reset()
    assert seq.last_sn == 0
    assert [e["n"] for e in seq.push(1, {"n": 99})] == [99]
