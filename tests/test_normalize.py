import json
from typing import Any

from yggdrasil.ingest.normalize import normalize


def _seg(kind: str, **data: Any) -> dict[str, Any]:
    return {"type": kind, "data": data}


def _image(**data: Any) -> dict[str, Any]:
    return _seg("image", **{"ref": "r", "url": "https://example.com/i", "summary": "[图片]", "sticker": False} | data)


def test_every_segment_type_reads_as_placeholder_text():
    card = {"app": "com.tencent.miniapp", "prompt": "分享", "meta": {"x": {"title": "标题", "jumpUrl": "https://a.b"}}}
    segments = [
        _seg("reply", message_id=-42),
        _seg("mention", user_id=20001, name="阿明"),
        _seg("mention_all"),
        _seg("text", text=" 看 "),
        _seg("face", face_id="14", is_large=False),
        _seg("face", face_id="99999", is_large=True),
        _image(),
        _image(sticker=True, summary="[动画表情]"),
        _image(sticker=True, summary=""),
        _seg("record", ref="v", url="", duration=3),
        _seg("video", ref="c", url="", duration=0),
        _seg("file", file_id="f1", file_name="a.xlsx", file_size=10),
        _seg("light_app", app_name=card["app"], json_payload=json.dumps(card)),
        _seg("xml", service_id=1, xml_payload="<msg/>"),
    ]
    norm = normalize(segments)
    assert norm.text == (
        "@阿明 @全体成员 看 [表情:微笑][表情:表情99999][图片][表情包:动画表情][表情包:动画表情][语音][视频]"
        "[文件:a.xlsx][卡片:标题][卡片]"
    )
    assert (norm.reply_to, norm.mentions) == (-42, [20001])
    assert [a.kind for a in norm.attachments] == ["image", "image", "image", "record", "video", "file", "card"]
    assert norm.attachments[-1].data == {"title": "标题", "url": "https://a.b"}
    assert norm.urls == ["https://a.b"]


def test_a_mention_renders_through_the_callback_and_owns_its_space():
    segments = [_seg("mention", user_id=20002, name=""), _seg("text", text=" 你好")]
    assert normalize(segments, mention=lambda uid: f"小红({uid})").text == "@小红(20002) 你好"
    assert normalize(segments).text == "@20002  你好"


def test_a_forward_shows_a_title_only_when_it_says_something():
    titled = normalize([_seg("forward", forward_id="r1", title=" sudo-rs 不兼容 ", preview=[], summary="")])
    assert titled.text == "[合并转发:sudo-rs 不兼容]"
    assert titled.attachments[0].kind == "forward"
    generic = _seg("forward", forward_id="r2", title="群聊的聊天记录", preview=["阿明: 嗯"], summary="查看1条转发消息")
    assert normalize([generic]).text == "[合并转发]"


def test_a_link_stops_before_the_sentence_punctuation_after_it():
    text = "看 https://example.com/new-spline/; 还有 https://example.com/a;b=1?q=2. 和 https://example.com/x!"
    norm = normalize([_seg("text", text=text)])
    assert norm.urls == ["https://example.com/new-spline/", "https://example.com/a;b=1?q=2", "https://example.com/x"]


def test_markdown_from_another_bot_shows_its_text_once():
    body = "查了，没有权限。\n不是我不愿意。"
    norm = normalize([_seg("markdown", content=body), _seg("text", text=body)])
    assert norm.text == f"[markdown]{body}"


def test_market_face_shows_one_label_and_keeps_the_text_after_it():
    face = {"emoji_package_id": 200001, "emoji_id": "0123", "key": "k", "summary": "[加载中]", "url": ""}
    norm = normalize([_seg("market_face", **face), _seg("text", text="[加载中]")])
    assert norm.text == "[表情包:加载中]"
    assert [(a.kind, a.data) for a in norm.attachments] == [("mface", face)]
    assert normalize([_seg("market_face", **face), _seg("text", text="等一下")]).text == "[表情包:加载中]等一下"
