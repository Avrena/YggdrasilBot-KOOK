"""Fresh KOOK guild/channel IDs for isolated database tests."""

from itertools import count

_guild_ids = count(990_101)
_channel_ids = count(880_101)


def fresh_guild() -> str:
    """A test guild ID that nothing else in this run gets."""
    return f"g{next(_guild_ids)}"


def fresh_channel() -> str:
    """A test channel ID that nothing else in this run gets."""
    return f"ch{next(_channel_ids)}"
