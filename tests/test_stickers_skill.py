"""Tests for the on-demand ``stickers`` skill (inline KOOK emojis & animated/image stickers)."""

import pytest

from tests.support.agent import run_ctx
from tests.support.files import png
from tests.support.kook import FakeKook
from yggdrasil.agent.schema import BotReply, Section
from yggdrasil.agent.skills import SKILLS, skill_names, stickers
from yggdrasil.agent.tools.images import reply_images
from yggdrasil.db.models import Sticker
from yggdrasil.settings import Settings

GUILD, CHANNEL, SELF, ALICE = "g-1", "ch-1", "10001", "20001"
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def test_stickers_skill_is_registered_and_enabled_by_default() -> None:
    assert "stickers" in SKILLS
    assert "stickers" in skill_names(Settings(), "chat", CHANNEL)
    assert skill_names(Settings(), "tldr", CHANNEL) == []


async def test_search_stickers_filters_and_ranks_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = [
        stickers._StickerRow(  # pyright: ignore[reportPrivateUsage]
            id=1,
            kind="emoji",
            guild_id=GUILD,
            emoji_id="g-1/emj-doge",
            summary="doge",
            description="滑稽狗头",
            usage="调侃和吐槽",
            emotions=["滑稽", "吐槽"],
            ocr_text="",
            frames=1,
            uses=20,
            seen_count=20,
        ),
        stickers._StickerRow(  # pyright: ignore[reportPrivateUsage]
            id=2,
            kind="image",
            guild_id=None,
            emoji_id=None,
            summary="cat.gif",
            description="猫咪疯狂敲键盘",
            usage="写代码崩溃或赶工",
            emotions=["崩溃", "忙碌"],
            ocr_text="改不完了",
            frames=12,
            uses=5,
            seen_count=10,
        ),
    ]

    async def _fake_pool(_guild_id: str | None) -> list[stickers._StickerRow]:  # pyright: ignore[reportPrivateUsage]
        return pool

    monkeypatch.setattr(stickers, "_load_pool", _fake_pool)
    ctx = run_ctx(kook=FakeKook(bot_id=SELF), channel_id=CHANNEL, guild_id=GUILD, user_id=ALICE, settings=Settings())

    hit_emoji = str(await stickers.search_stickers(ctx, "doge"))
    assert "#1 [本服内联表情] doge：滑稽狗头" in hit_emoji
    assert "调侃和吐槽" in hit_emoji
    assert "#2" not in hit_emoji

    hit_gif = str(await stickers.search_stickers(ctx, "崩溃"))
    assert "#2 [动图表情包]" in hit_gif
    assert "#1" not in hit_gif

    miss = str(await stickers.search_stickers(ctx, "完全不存在的关键词"))
    assert "没有找到匹配" in miss


async def test_pick_sticker_inline_emoji_and_animated_image_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    same_emoji = Sticker(
        id=1,
        kind="emoji",
        ref="g-1/emj-doge",
        guild_id=GUILD,
        emoji_id="g-1/emj-doge",
        summary="doge",
        status="ready",
        enabled=True,
    )
    gif_sticker = Sticker(
        id=2,
        kind="image",
        ref="sha-gif",
        guild_id=None,
        emoji_id=None,
        summary="cat.gif",
        status="ready",
        enabled=True,
    )
    cross_emoji = Sticker(
        id=3,
        kind="emoji",
        ref="g-other/emj-hi",
        guild_id="g-other",
        emoji_id="g-other/emj-hi",
        summary="hi",
        status="ready",
        enabled=True,
    )
    png_bytes = png("blue")
    store: dict[int, tuple[Sticker, bytes | None]] = {
        1: (same_emoji, None),
        2: (gif_sticker, TINY_GIF),
        3: (cross_emoji, png_bytes),
    }

    async def _fake_load(sid: int) -> tuple[Sticker | None, bytes | None]:
        return store.get(sid, (None, None))

    monkeypatch.setattr(stickers, "_load_sticker", _fake_load)
    ctx = run_ctx(kook=FakeKook(bot_id=SELF), channel_id=CHANNEL, guild_id=GUILD, user_id=ALICE, settings=Settings())

    # 1) Same-guild KOOK custom emoji returns native inline KMarkdown token
    res_emoji = str(await stickers.pick_sticker(ctx, 1))
    assert "(emj)doge(emj)[g-1/emj-doge]" in res_emoji

    # 2) Image/GIF sticker registers img#1 and preserves original GIF bytes in reply_images
    res_gif = str(await stickers.pick_sticker(ctx, 2))
    assert "img#1" in res_gif
    reply = BotReply(mode="reply", style="text", sections=[Section(content="", images=["img#1"])])
    pics = await reply_images(ctx.deps, reply)
    assert pics["img#1"][0] == TINY_GIF

    # 3) Cross-guild custom emoji falls back to image handle instead of unusable (emj) token
    res_cross = str(await stickers.pick_sticker(ctx, 3))
    assert "img#2" in res_cross
    assert "(emj)" not in res_cross
