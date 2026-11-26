"""Tests for KOOK event/REST parsing: KMarkdown tags, ``(chn)``, ``extra.quote``, CardMessage, bot flag, recalls."""

import json

from yggdrasil.kook.parser import parse_incoming, parse_observed, parse_recalled, parse_rest_message


def test_parse_kmarkdown_mentions_channels_emojis_and_quote():
    event = {
        "channel_type": "GROUP",
        "type": 9,
        "target_id": "ch-1",
        "author_id": "u-alice",
        "content": "(met)bot-1(met) 去 (chn)ch-2(chn) 看看 (emj)doge(emj)[g-1/emo1]",
        "msg_id": "msg-new",
        "msg_timestamp": 1_700_000_000_000,
        "extra": {
            "type": 9,
            "guild_id": "g-1",
            "channel_name": "闲聊",
            "mention": ["bot-1"],
            "author": {
                "id": "u-alice",
                "username": "alice",
                "nickname": "爱丽丝",
                "roles": [10],
                "bot": False,
            },
            "kmarkdown": {
                "mention_part": [{"id": "bot-1", "username": "Ygg", "nickname": "小树"}],
                "mention_channel_part": [{"id": "ch-2", "name": "技术讨论"}],
            },
            "quote": {
                "id": "msg-orig-uuid",
                "rong_id": "legacy-rong-id",
                "type": 9,
                "content": "原消息内容",
                "create_at": 1_699_999_000_000,
                "author": {"id": "bot-1", "username": "Ygg", "bot": True},
            },
        },
    }
    inc = parse_incoming(event, self_id="bot-1", owner_id="u-owner", admin_role_ids={10})
    assert inc is not None
    assert inc.to_me is True
    assert inc.sender.role == "admin"
    assert inc.sender.is_bot is False
    # Quote prefers `id` over `rong_id`
    assert inc.reply is not None
    assert inc.reply.message_id == "msg-orig-uuid"
    assert inc.reply.sender.is_bot is True
    assert any(s["type"] == "reply" and s["data"]["message_id"] == "msg-orig-uuid" for s in inc.segments)
    # Channel tag resolved to #技术讨论 inside text segment
    assert any(s["type"] == "text" and "#技术讨论" in s["data"]["text"] for s in inc.segments)
    # Emoji parsed with id
    emj_segs = [s for s in inc.segments if s["type"] == "emoji"]
    assert len(emj_segs) == 1
    assert emj_segs[0]["data"]["id"] == "g-1/emo1"
    # Addressed text strips leading @bot
    assert "#技术讨论" in inc.text


def test_parse_bot_sender_from_flag_and_reply_to_bot_sets_to_me():
    event = {
        "channel_type": "GROUP",
        "type": 9,
        "target_id": "ch-1",
        "author_id": "other-bot",
        "content": "我接着你的话问一句",
        "msg_id": "msg-bot-1",
        "msg_timestamp": 1_700_000_100_000,
        "extra": {
            "type": 9,
            "guild_id": "g-1",
            "author": {"id": "other-bot", "username": "Helper", "bot": True},
            "quote": {
                "id": "bot-prev",
                "type": 9,
                "content": "之前的回答",
                "author": {"id": "bot-1", "username": "Ygg", "bot": True},
            },
        },
    }
    inc = parse_incoming(event, self_id="bot-1")
    assert inc is not None
    assert inc.sender.is_bot is True
    assert inc.to_me is True


def test_parse_card_message_extracts_text_and_images():
    cards = [
        {
            "type": "card",
            "theme": "secondary",
            "modules": [
                {"type": "header", "text": {"type": "plain-text", "content": "今日快报"}},
                {"type": "section", "text": {"type": "kmarkdown", "content": "一切正常"}},
                {"type": "container", "elements": [{"type": "image", "src": "https://img.kookapp.cn/a.png"}]},
            ],
        }
    ]
    raw = {
        "id": "msg-card-1",
        "type": 10,
        "content": json.dumps(cards),
        "create_at": 1_700_000_200_000,
        "author": {"id": "u-1", "username": "bob"},
    }
    msg = parse_rest_message(raw, channel_id="ch-1", guild_id="g-1")
    assert msg.message_id == "msg-card-1"
    assert any(s["type"] == "card" and "今日快报" in s["data"]["plain_text"] for s in msg.segments)
    assert any(s["type"] == "image" and s["data"]["url"] == "https://img.kookapp.cn/a.png" for s in msg.segments)


def test_parse_recalled_and_observed_system_events():
    deleted = {
        "type": 255,
        "target_id": "ch-1",
        "extra": {"type": "deleted_message", "body": {"msg_id": "dead-uuid", "channel_id": "ch-1"}},
    }
    assert parse_recalled(deleted) == ("ch-1", "dead-uuid")

    joined = {
        "type": 255,
        "target_id": "g-9",
        "extra": {"type": "self_joined_guild", "body": {"guild_id": "g-9"}},
    }
    obs = parse_observed(joined)
    assert obs is not None
    assert obs.kind == "kook.self_joined_guild"
    assert obs.guild_id == "g-9"
