"""Memory tables: channel-segmented chunks/topics + guild-shared profiles, glossary, digests, and vectors."""

from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Float,
    ForeignKey,
    Identity,
    Index,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from yggdrasil.db.base import EMBED_DIM, Base, TimestampMixin


class MessageChunk(Base, TimestampMixin):
    """A conversation window inside one channel, tagged with ``guild_id`` for guild-shared search."""

    __tablename__ = "message_chunks"
    __table_args__ = (
        Index("ix_message_chunks_channel_end", "channel_id", "end_at"),
        Index("ix_message_chunks_guild_end", "guild_id", "end_at"),
    )

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    channel_id: Mapped[str] = mapped_column(Text)
    guild_id: Mapped[str] = mapped_column(Text)
    first_msg_id: Mapped[int]
    last_msg_id: Mapped[int]
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    msg_count: Mapped[int]
    text: Mapped[str] = mapped_column(Text)


class Topic(Base, TimestampMixin):
    """A conversation topic segmented within one channel, searchable and aggregated across its guild."""

    __tablename__ = "topics"
    __table_args__ = (
        Index("ix_topics_channel_status", "channel_id", "status", "end_at"),
        Index("ix_topics_guild_status", "guild_id", "status", "end_at"),
    )

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    channel_id: Mapped[str] = mapped_column(Text)
    guild_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    participants: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    first_msg_id: Mapped[int]
    last_msg_id: Mapped[int]
    msg_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(Text, default="open")  # open | closed | archived
    dirty: Mapped[bool] = mapped_column(default=False)
    closed_at: Mapped[datetime | None]
    importance: Mapped[int | None] = mapped_column(SmallInteger)
    importance_reason: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[float | None] = mapped_column(Float)
    scored_at: Mapped[datetime | None]


class TopicMessage(Base):
    __tablename__ = "topic_messages"
    __table_args__ = (
        PrimaryKeyConstraint("topic_id", "message_id"),
        Index("ix_topic_messages_message", "message_id"),
    )

    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    message_id: Mapped[int]


class DailyDigest(Base, TimestampMixin):
    """Guild-shared daily summary aggregating all memory-sharing channels in the guild."""

    __tablename__ = "daily_digests"
    __table_args__ = (UniqueConstraint("guild_id", "day"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    guild_id: Mapped[str] = mapped_column(Text)
    day: Mapped[date]
    summary: Mapped[str] = mapped_column(Text)
    topic_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)


class GlossaryTerm(Base, TimestampMixin):
    """Guild-shared slang and in-joke dictionary."""

    __tablename__ = "glossary_terms"
    __table_args__ = (UniqueConstraint("guild_id", "term"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    guild_id: Mapped[str] = mapped_column(Text)
    term: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    definition: Mapped[str] = mapped_column(Text)
    examples: Mapped[list[Any]] = mapped_column(default=list)
    first_seen: Mapped[datetime | None]
    last_seen: Mapped[datetime | None]
    hits: Mapped[int] = mapped_column(default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    locked: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(Text, default="auto")


class GlossaryCandidate(Base, TimestampMixin):
    __tablename__ = "glossary_candidates"
    __table_args__ = (PrimaryKeyConstraint("guild_id", "term"),)

    guild_id: Mapped[str] = mapped_column(Text)
    term: Mapped[str] = mapped_column(Text)
    count: Mapped[int] = mapped_column(default=0)
    first_seen: Mapped[datetime]
    last_seen: Mapped[datetime]
    status: Mapped[str] = mapped_column(Text, default="pending")
    example_msg_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)


class UserProfile(Base, TimestampMixin):
    """Guild-shared member profile aggregated from all memory-sharing channels in the guild."""

    __tablename__ = "user_profiles"
    __table_args__ = (PrimaryKeyConstraint("guild_id", "user_id"),)

    guild_id: Mapped[str] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    interests: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    traits: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    opted_out: Mapped[bool] = mapped_column(default=False)
    last_closed_at: Mapped[datetime | None]
    last_topic_id: Mapped[int | None]


class UserProfileFact(Base, TimestampMixin):
    __tablename__ = "user_profile_facts"
    __table_args__ = (Index("ix_user_profile_facts_user", "guild_id", "user_id"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    guild_id: Mapped[str] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(Text)
    fact: Mapped[str] = mapped_column(Text)
    source_msg_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    importance: Mapped[int | None] = mapped_column(SmallInteger)
    weight: Mapped[float | None] = mapped_column(Float)
    scored_at: Mapped[datetime | None]
    archived_at: Mapped[datetime | None]
    archive_reason: Mapped[str | None] = mapped_column(Text)
    locked: Mapped[bool] = mapped_column(default=False, server_default=false())


class MemoryVector(Base):
    """One embedding per (kind, ref, space). ``kind``: chunk | topic | glossary | digest."""

    __tablename__ = "memory_vectors"
    __table_args__ = (
        PrimaryKeyConstraint("kind", "ref_id", "space_id"),
        Index("ix_memory_vectors_guild_scope", "guild_id", "space_id", "kind"),
        Index("ix_memory_vectors_channel_scope", "channel_id", "space_id", "kind"),
        Index(
            "ix_memory_vectors_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
    )

    kind: Mapped[str] = mapped_column(Text)
    ref_id: Mapped[int]
    space_id: Mapped[int] = mapped_column(ForeignKey("embed_spaces.id", ondelete="CASCADE"))
    guild_id: Mapped[str] = mapped_column(Text)
    channel_id: Mapped[str | None] = mapped_column(Text)  # set for chunk/topic, None for guild-level glossary/digest
    embedding: Mapped[Any] = mapped_column(HALFVEC(EMBED_DIM))
