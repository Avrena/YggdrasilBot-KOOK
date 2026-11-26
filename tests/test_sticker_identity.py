"""KOOK sticker identification: guild custom emojis ``(emj)`` and standalone GIF / sticker images."""

from yggdrasil.stickers.collect import Sighting, sightings


def test_kook_guild_emojis_and_standalone_gif_images_are_identified():
    url = "https://img.kookapp.cn/emojis/g1/e1.png"
    emoji_segs = [{"type": "emoji", "data": {"id": "g1/e1", "name": "doge", "guild_id": "g1", "url": url}}]
    found, img_cands = sightings(emoji_segs, guild_id="g1")
    assert found == [Sighting("emoji", "g1/e1", "doge", url=url, emoji_id="g1/e1", guild_id="g1")]
    assert img_cands == []

    # Standalone GIF image is a sticker candidate
    gif_segs = [{"type": "image", "data": {"url": "https://img.kookapp.cn/assets/cat.gif", "name": "cat.gif"}}]
    found2, img_cands2 = sightings(gif_segs, guild_id="g1")
    assert found2 == []
    assert img_cands2 == [("https://img.kookapp.cn/assets/cat.gif", "cat.gif", "g1")]

    # Image accompanied by long text is treated as a regular chat photo, not a sticker
    photo_segs = [
        {"type": "text", "data": {"text": "这是我今天拍的风景照大家看看"}},
        {"type": "image", "data": {"url": "https://img.kookapp.cn/assets/photo.png", "name": "photo.png"}},
    ]
    found3, img_cands3 = sightings(photo_segs, guild_id="g1")
    assert found3 == []
    assert img_cands3 == []
