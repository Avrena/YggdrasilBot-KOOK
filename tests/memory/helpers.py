from datetime import UTC, datetime, timedelta
from itertools import count

from yggdrasil.memory.search import MessageView

T0 = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)  # 12:00 Asia/Shanghai
_ids = count(1)


def mv(
    minute: float,
    text: str = "消息",
    *,
    user: str = "10001",
    name: str = "甲",
    mid: int | None = None,
    channel_id: str = "ch-1",
) -> MessageView:
    i = mid if mid is not None else next(_ids)
    return MessageView(
        i,
        channel_id,
        f"guild-{channel_id}",
        f"k-{i}",
        T0 + timedelta(minutes=minute),
        str(user),
        name,
        text,
        None,
        False,
    )
