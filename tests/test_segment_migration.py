"""The data migration that moved stored OneBot segments to the ``qq.segments`` schema: its pure conversions, run on
payloads shaped like the stored rows (SnowLuma and NapCat era)."""

import base64
import hashlib
import importlib
import json
from typing import Any

migration: Any = importlib.import_module("migration.versions.20261117_e2a9c4f7b130_qq_segment_schema")

CONTENT = b"sticker bytes"
SHA1 = hashlib.sha1(CONTENT).hexdigest()
FILE_ID = base64.urlsafe_b64encode(b"\x12\x14" + hashlib.sha1(CONTENT).digest() + b"\x50\x1f").decode().rstrip("=")
URL = f"https://multimedia.nt.qq.com.cn/download?appid=1407&fileid={FILE_ID}&rkey=x"
MD5 = "0123456789ABCDEF0123456789ABCDEF"


def _seg(kind: str, **data: Any) -> dict[str, Any]:
    return {"type": kind, "data": data}


def test_each_onebot_segment_type_converts():
    card = json.dumps({"app": "com.tencent.miniapp", "prompt": "分享"})
    old = [
        _seg("reply", id="-1205"),
        _seg("at", qq="20001", name="@阿明"),
        _seg("at", qq="all"),
        _seg("text", text="看"),
        _seg("face", id="14", raw={"faceText": "/微笑"}, resultId=None, chainCount=None),
        _seg("image", file=f"{MD5}.jpg", url=URL, summary="", sub_type=0, file_size="10"),
        _seg("image", file=f"{MD5}.gif", url="https://example.com/s.gif", summary="[动画表情]", sub_type=1),
        _seg("image", file="a.gif", url="", summary="[加载中]", emoji_id="ab12", emoji_package_id=200001, key="k"),
        _seg("mface", emoji_id="cd34", emoji_package_id="230001", key="k2", summary="[开心]"),
        _seg("record", file="v.amr", url="https://example.com/v", file_size="3"),
        _seg("video", file="c.mp4", url="", file_size="9"),
        _seg("file", file_id="/f-1", file="a.xlsx", file_size="18432"),
        _seg("json", data=card),
        _seg("xml", data="<msg/>"),
        _seg("markdown", content="**hi**"),
        _seg("poke", type="1", id="1"),
        _seg("dice", result="4"),
        _seg("rps", result="2"),
        _seg("inline_keyboard", content={"rows": []}),
        _seg("contact", type="qq", id="1"),
    ]
    assert migration.convert(old) == [
        _seg("reply", message_id=-1205),
        _seg("mention", user_id=20001, name="阿明"),
        _seg("mention_all"),
        _seg("text", text="看"),
        _seg("face", face_id="14", is_large=False),
        _seg("image", ref=f"{MD5}.jpg", url=URL, summary="", sticker=False, sha1=SHA1),
        _seg("image", ref=f"{MD5}.gif", url="https://example.com/s.gif", summary="[动画表情]", sticker=True, sha1=""),
        _seg("market_face", emoji_package_id=200001, emoji_id="ab12", key="k", summary="[加载中]", url=""),
        _seg("market_face", emoji_package_id=230001, emoji_id="cd34", key="k2", summary="[开心]", url=""),
        _seg("record", ref="v.amr", url="https://example.com/v", duration=0),
        _seg("video", ref="c.mp4", url="", duration=0),
        _seg("file", file_id="/f-1", file_name="a.xlsx", file_size=18432),
        _seg("light_app", app_name="com.tencent.miniapp", json_payload=card),
        _seg("xml", service_id=0, xml_payload="<msg/>"),
        _seg("markdown", content="**hi**"),
        _seg("text", text="[戳一戳]"),
        _seg("text", text="[骰子:4]"),
        _seg("text", text="[猜拳:2]"),
        _seg("text", text="[contact]"),
    ]


def test_the_bots_own_forward_keeps_its_nodes_nested_markdown_inline():
    node = {"type": "node", "data": {"user_id": "10000", "nickname": "bot", "time": 1760000000}}
    nested = {
        "type": "node",
        "data": {"user_id": "10000", "nickname": "bot", "content": [_seg("markdown", content="# 标题")]},
    }
    content = [
        node | {"data": node["data"] | {"content": [_seg("text", text="第一段"), _seg("text", text="[图片]")]}},
        node | {"data": node["data"] | {"content": [nested]}},
    ]
    [forward] = migration.convert([_seg("forward", id="res-1", title="鹈鹕", content=content)])
    assert forward == _seg(
        "forward",
        forward_id="res-1",
        title="鹈鹕",
        preview=[],
        summary="",
        nodes=[
            {
                "user_id": 10000,
                "name": "bot",
                "time": 1760000000,
                "segments": [_seg("text", text="第一段"), _seg("text", text="[图片]")],
            },
            {"user_id": 10000, "name": "bot", "time": 1760000000, "segments": [_seg("markdown", content="# 标题")]},
        ],
    )


def test_a_napcat_forward_record_reads_its_sender():
    record = {"sender": {"user_id": 20002, "nickname": "xh", "card": "小红"}, "time": 1760000001, "message": "嗯"}
    [forward] = migration.convert([_seg("forward", id="r2", content=[record, "junk"])])
    assert forward["data"]["nodes"] == [
        {"user_id": 20002, "name": "xh", "time": 1760000001, "segments": [_seg("text", text="嗯")]}
    ]
    assert forward["data"]["title"] == ""


def test_conversion_is_total_and_idempotent():
    odd = [
        "stray",
        None,
        {"type": "text"},
        {"data": {"text": "no type"}},
        _seg("at", qq="abc"),
        _seg("reply", id="not-a-number"),
        _seg("file", file_size=float("inf")),
        _seg("json", data={"app": "x"}),
        _seg("json", data="{broken"),
        {"type": "image", "data": "not a dict"},
    ]
    once = migration.convert(odd)
    assert [s["type"] for s in once] == ["text", "text", "mention", "reply", "file", "light_app", "light_app", "image"]
    assert once[1] == _seg("text", text="[]")
    assert once[5]["data"] == {"app_name": "x", "json_payload": '{"app": "x"}'}
    assert migration.convert(once) == once
    assert migration.convert("纯文本") == [_seg("text", text="纯文本")]
    assert migration.convert({"not": "a list"}) == []


def test_transcripts_convert_every_message_and_rekey_kept_images():
    image = _seg("image", file=f"{MD5}.jpg", url=URL, summary="", sub_type=0)
    message = {"id": 7, "user_id": 20001, "segments": [image, _seg("at", qq="20002")]}
    entries = [
        {"kind": "user", "display": {"kind": "qq_context", "trigger": message, "quoted": None, "chain": [message]}},
        {
            "kind": "tool",
            "display": {"kind": "qq_forward", "nodes": [{"depth": 0, "segments": [_seg("face", id="5")]}]},
        },
        {"kind": "assistant", "parts": [{"type": "text", "text": "好"}]},
    ]
    context = {"messages": {"7": message}, "image_keys": {f"{MD5}.jpg": "b" * 64, "other": "c" * 64}}
    new_entries, new_context = migration.convert_transcript(entries, context)
    converted = [
        _seg("image", ref=f"{MD5}.jpg", url=URL, summary="", sticker=False, sha1=SHA1),
        _seg("mention", user_id=20002, name=""),
    ]
    assert new_entries[0]["display"]["trigger"]["segments"] == converted
    assert new_entries[0]["display"]["quoted"] is None
    assert new_entries[0]["display"]["chain"][0]["segments"] == converted
    assert new_entries[1]["display"]["nodes"][0]["segments"] == [_seg("face", face_id="5", is_large=False)]
    assert new_entries[2] == entries[2]
    assert new_context["messages"]["7"]["segments"] == converted
    assert new_context["image_keys"] == {SHA1: "b" * 64, "other": "c" * 64}
    assert migration.convert_transcript(new_entries, new_context) == (new_entries, new_context)


def test_md5_stickers_find_their_new_identity():
    clues = migration.Clues()
    clues.collect([_seg("image", file=f"{MD5.lower()}.gif", url=URL, sub_type=1, summary="[动画表情]")])
    face_md5 = "F" * 32
    clues.collect(
        [_seg("image", file=f"{face_md5}.gif", url="", summary="[吃瓜]", emoji_id="ee", emoji_package_id=9, key="k")]
    )
    # The saved image decides; else what the newest stored message says; else nothing.
    assert migration.sticker_target(MD5, b"saved", clues) == ("image", hashlib.sha1(b"saved").hexdigest(), None)
    assert migration.sticker_target(MD5, None, clues) == ("image", SHA1, None)
    assert migration.sticker_target("A" * 32, None, clues) is None
    kind, ref, face = migration.sticker_target(face_md5, b"saved", clues)
    assert (kind, ref, face.key, face.summary) == ("mface", "9:ee", "k", "吃瓜")
