"""Weighted sticker sampling for KOOK sticker drops."""

import random

from yggdrasil.stickers import drop


def test_the_sample_favours_used_stickers_but_reaches_rare_ones_and_skips_repeats() -> None:
    rng = random.Random(1)
    draws = [drop.sample(["popular", "rare"], [drop.weight(200, 0), drop.weight(0, 0)], 1, rng)[0] for _ in range(4000)]
    share = draws.count("popular") / len(draws)
    assert 0.8 < share < 0.92

    # Sent twice by the bot this week: now rarer than a sticker nobody ever used.
    draws = [drop.sample(["repeat", "fresh"], [drop.weight(200, 2), drop.weight(0, 0)], 1, rng)[0] for _ in range(4000)]
    assert draws.count("repeat") < draws.count("fresh")

    picked = drop.sample(list(range(10)), [1.0] * 10, 6, rng)
    assert len(picked) == len(set(picked)) == 6
