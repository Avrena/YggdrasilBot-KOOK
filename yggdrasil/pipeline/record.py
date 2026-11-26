"""What a run leaves in the database: its ``Invocation`` row, moderation verdicts, and footer ``Meta``."""

import contextlib
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import update

from yggdrasil.db.models import Invocation, ModerationResult
from yggdrasil.db.session import get_session
from yggdrasil.moderation.guard import Verdict


@dataclass
class Meta:
    models: list[str] = field(default_factory=list)
    model_names: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tools: dict[str, int] = field(default_factory=dict)
    tool_seconds: dict[str, float] = field(default_factory=dict)
    slow_tools: frozenset[str] = frozenset()
    ack_s: float | None = None
    ttft_s: float | None = None
    tokens_per_s: float | None = None
    total_s: float = 0.0
    moderation: str = ""
    trace_id: str | None = None

    def render(self, fields: list[str]) -> str:
        """Compact single/multi-line footer suitable for a KOOK CardMessage ``context`` module."""
        parts: list[str] = []
        if "model" in fields and self.model_names:
            parts.append("模型 " + " · ".join(dict.fromkeys(self.model_names)))
        if "tokens" in fields:
            cached = f" / C {self.tokens_cached / 1000:.1f}k" if self.tokens_cached else ""
            parts.append(f"tokens I {self.tokens_in / 1000:.1f}k / O {self.tokens_out}{cached}")
        if "tools" in fields and self.tools:
            used = " · ".join(self._tool(k, v) for k, v in sorted(self.tools.items()))
            parts.append(f"工具 {used}")
        if "latency" in fields:
            t = [f"TTFT {self.ttft_s:.1f}s"] if self.ttft_s is not None else []
            t.append(f"{self.total_s:.1f}s")
            if self.tokens_per_s is not None:
                t.append(f"{self.tokens_per_s:.0f} tok/s")
            parts.append("耗时 " + " · ".join(t))
        if "moderation" in fields and self.moderation:
            parts.append(f"审核 {self.moderation}")
        if "trace" in fields and self.trace_id:
            parts.append(f"trace {self.trace_id[:16]}")
        return " | ".join(parts)

    def _tool(self, name: str, count: int) -> str:
        seconds = self.tool_seconds.get(name)
        took = f" {seconds:.1f}s" if name in self.slow_tools and seconds is not None else ""
        return f"{name}×{count}{took}"


async def start_row(
    *,
    channel_id: str | None,
    guild_id: str | None,
    user_id: str,
    handler: str,
    trigger_msg_id: str | None,
    request_text: str,
    trace_id: str | None,
) -> UUID:
    async with get_session() as session:
        row = Invocation(
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
            handler=handler,
            trigger_msg_id=trigger_msg_id,
            request_text=request_text[:2000],
            status="running",
            trace_id=trace_id,
        )
        session.add(row)
        await session.flush()
        return row.id


async def save_verdict(invocation_id: UUID, v: Verdict) -> None:
    async with get_session() as session:
        session.add(
            ModerationResult(
                invocation_id=invocation_id,
                stage=v.stage,
                verdict=v.verdict,
                score=v.score,
                categories=v.categories,
                action=v.action,
                model=v.model,
                latency_ms=v.latency_ms,
            )
        )


async def finish_row(
    invocation_id: UUID, status: str, error: str | None, meta: Meta, ack_kind: str | None, **extra: object
) -> None:
    with contextlib.suppress(Exception):
        async with get_session() as session:
            await session.execute(
                update(Invocation)
                .where(Invocation.id == invocation_id)
                .values(
                    status=status,
                    error=error,
                    models=list(dict.fromkeys(meta.models)),
                    tokens_in=meta.tokens_in,
                    tokens_out=meta.tokens_out,
                    tokens_cached=meta.tokens_cached,
                    tool_calls=meta.tools,
                    ack_kind=ack_kind,
                    ack_latency_ms=int(meta.ack_s * 1000) if meta.ack_s is not None else None,
                    duration_ms=int(meta.total_s * 1000),
                    **extra,
                )
            )
