from collections.abc import Callable
from typing import Any

import pytest

from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot


@pytest.fixture
def channel_settings(snapshot: Callable[..., Snapshot]) -> Callable[[dict[str, dict[str, Any]]], None]:
    """Install per-channel settings documents: ``channel_settings({cid: {...}})``."""

    def install(docs: dict[str, dict[str, Any]]) -> None:
        guilds = {f"guild-{cid}": GuildInfo(f"guild-{cid}", "test", "", {}, True) for cid in docs}
        channels = {cid: ChannelInfo(cid, f"guild-{cid}", None, False, 0, "ch", True, doc) for cid, doc in docs.items()}
        snapshot(guilds=guilds, channels=channels)

    return install
