"""Operational tables: invocations, sessions, moderation, reviews, bot events, sandbox jobs, transcripts."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ARRAY, Float, ForeignKey, Identity, Index, LargeBinary, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from yggdrasil.db.base import Base


class Invocation(Base):
    """One triggered bot run, from match to delivery. Linked to its trace."""

    __tablename__ = "invocations"
    __table_args__ = (
        Index("ix_invocations_created", "created_at"),
        Index("ix_invocations_channel_created", "channel_id", "created_at"),
        Index("ix_invocations_guild_created", "guild_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    channel_id: Mapped[str | None] = mapped_column(Text)
    guild_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(Text)
    handler: Mapped[str] = mapped_column(Text)
    trigger_msg_id: Mapped[str | None] = mapped_column(Text)
    reply_msg_id: Mapped[str | None] = mapped_column(Text)
    request_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="running")
    models: Mapped[list[Any]] = mapped_column(default=list)
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    tokens_cached: Mapped[int] = mapped_column(default=0)
    tool_calls: Mapped[dict[str, Any]] = mapped_column(default=dict)
    ack_kind: Mapped[str | None] = mapped_column(Text)
    ack_latency_ms: Mapped[int | None]
    duration_ms: Mapped[int | None]
    input_verdict: Mapped[dict[str, Any] | None]
    output_verdict: Mapped[dict[str, Any] | None]
    reply: Mapped[dict[str, Any] | None]
    trace_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class InvocationSession(Base):
    """The agent run of a delivered answer, kept briefly so a reply to that answer continues it."""

    __tablename__ = "invocation_sessions"
    __table_args__ = (Index("ix_invocation_sessions_created", "created_at"),)

    invocation_id: Mapped[UUID] = mapped_column(ForeignKey("invocations.id", ondelete="CASCADE"), primary_key=True)
    handler: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)
    tools: Mapped[list[Any]]
    sandbox: Mapped[bool]
    skills: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")
    messages: Mapped[list[Any]]
    handles: Mapped[dict[str, Any]]
    nonce: Mapped[str] = mapped_column(Text)
    seen_message_id: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ModerationResult(Base):
    __tablename__ = "moderation_results"
    __table_args__ = (Index("ix_moderation_results_created", "created_at"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    invocation_id: Mapped[UUID | None] = mapped_column(ForeignKey("invocations.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float)
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    action: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(default=0)


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (Index("ix_review_items_status", "status", "expires_at"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    invocation_id: Mapped[UUID | None] = mapped_column(ForeignKey("invocations.id", ondelete="SET NULL"))
    channel_id: Mapped[str] = mapped_column(Text)
    guild_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(Text)
    trigger_msg_id: Mapped[str | None] = mapped_column(Text)
    request_text: Mapped[str] = mapped_column(Text, default="")
    reply: Mapped[dict[str, Any]]
    verdict: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(Text, default="pending")
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None]
    expires_at: Mapped[datetime]


class BotEvent(Base):
    """A non-message KOOK event worth keeping (guild/channel changes, pins, member notices)."""

    __tablename__ = "bot_events"
    __table_args__ = (
        Index("ix_bot_events_at", "at"),
        Index("ix_bot_events_status", "status", "at"),
    )

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    kind: Mapped[str] = mapped_column(Text)
    guild_id: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(Text)
    operator_id: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str | None] = mapped_column(Text)
    handled_by: Mapped[str | None] = mapped_column(Text)
    handled_at: Mapped[datetime | None]


class SandboxJob(Base):
    """A finished background job (``yggdrasil.sandbox.jobs``), archived once reported."""

    __tablename__ = "sandbox_jobs"
    __table_args__ = (
        Index("ix_sandbox_jobs_created", text("created_at DESC")),
        Index("ix_sandbox_jobs_channel_created", "channel_id", text("created_at DESC")),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel_id: Mapped[str] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    workspace: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    record: Mapped[dict[str, Any]]
    events: Mapped[list[Any]]
    archived_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    invocation_id: Mapped[UUID | None] = mapped_column(ForeignKey("invocations.id", ondelete="CASCADE"), unique=True)
    job_id: Mapped[str | None] = mapped_column(Text, unique=True)
    source: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    entries: Mapped[list[Any]] = mapped_column(default=list)
    context: Mapped[dict[str, Any]] = mapped_column(default=dict)
    size_bytes: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TranscriptBlob(Base):
    __tablename__ = "transcript_blobs"

    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    mime: Mapped[str] = mapped_column(Text)
    size: Mapped[int]
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
