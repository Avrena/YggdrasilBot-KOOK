"""Runs whose trigger message is deleted while they work: they stop and send nothing."""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any


class Watch:
    def __init__(self) -> None:
        self.recalled = False
        self._tasks: set[asyncio.Task[Any]] = set()

    def guard(self, task: asyncio.Task[Any]) -> None:
        if self.recalled:
            task.cancel()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def mark(self) -> None:
        self.recalled = True
        for task in list(self._tasks):
            task.cancel()


_watches: dict[tuple[str, str], list[Watch]] = {}


@contextmanager
def watch(channel_id: str | None, message_id: str) -> Generator[Watch]:
    w = Watch()
    if channel_id is None or not message_id:
        yield w
        return
    key = (channel_id, message_id)
    _watches.setdefault(key, []).append(w)
    try:
        yield w
    finally:
        watches = _watches[key]
        watches.remove(w)
        if not watches:
            del _watches[key]


def recall(channel_id: str, message_id: str) -> bool:
    watches = _watches.get((channel_id, message_id), [])
    for w in watches:
        w.mark()
    return bool(watches)
