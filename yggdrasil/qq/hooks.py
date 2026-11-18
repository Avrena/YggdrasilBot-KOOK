"""Spans and metrics for every QQ protocol action (OneBot v11 and Milky), via NoneBot's call_api hooks."""

import time
from typing import Any

from nonebot.adapters import Bot
from nonebot.exception import ActionFailed
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from yggdrasil.core.metrics import QQ_API, QQ_API_DURATION
from yggdrasil.core.telemetry import TRACER

# NoneBot passes the same ``data`` dict to the calling and called hooks; key on its
# identity so concurrent calls from sibling tasks don't mix up their spans.
_inflight: dict[int, tuple[Span, float, str]] = {}

# Polled or trivially cheap actions that would only add noise to traces.
_NO_SPAN = {"get_status", "get_version_info"}

_PROTOCOLS = {"OneBot V11": "onebot", "Milky": "milky"}


def _protocol(bot: Bot) -> str:
    name = bot.adapter.get_name()
    return _PROTOCOLS.get(name, name)


async def on_calling_api(bot: Bot, api: str, data: dict[str, Any]) -> None:
    protocol = _protocol(bot)
    span = (
        TRACER.start_span(
            f"{protocol} {api}", kind=trace.SpanKind.CLIENT, attributes={"qq.protocol": protocol, "qq.action": api}
        )
        if api not in _NO_SPAN
        else trace.INVALID_SPAN
    )
    _inflight[id(data)] = (span, time.perf_counter(), protocol)


async def on_called_api(bot: Bot, exception: Exception | None, api: str, data: dict[str, Any], result: Any) -> None:
    entry = _inflight.pop(id(data), None)
    if entry is None:
        return
    span, started, protocol = entry
    elapsed = time.perf_counter() - started
    # "failed": the protocol answered with an error; "error": no answer (network, timeout, ...).
    outcome = "ok" if exception is None else "failed" if isinstance(exception, ActionFailed) else "error"
    attrs = {"protocol": protocol, "action": api, "outcome": outcome}
    QQ_API.add(1, attrs)
    QQ_API_DURATION.record(elapsed, attrs)
    if span.is_recording():
        if exception is not None:
            span.record_exception(exception)
            span.set_status(Status(StatusCode.ERROR, str(exception)[:200]))
            retcode = (getattr(exception, "info", None) or {}).get("retcode")
            if retcode is not None:
                span.set_attribute("qq.retcode", retcode)
        span.end()


def install() -> None:
    from nonebot.adapters.milky import Bot as MilkyBot
    from nonebot.adapters.onebot.v11 import Bot as OneBotBot

    # NoneBot keeps the hook sets on its base Bot, so this registers each hook once for both.
    for cls in (OneBotBot, MilkyBot):
        cls.on_calling_api(on_calling_api)
        cls.on_called_api(on_called_api)
