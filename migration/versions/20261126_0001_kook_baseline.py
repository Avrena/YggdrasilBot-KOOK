"""KOOK baseline schema (fresh KOOK-native deployment, no QQ legacy tables).

Revision ID: 0001_kook_baseline
Revises:
Create Date: 2026-11-26 00:00:00
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from yggdrasil.db.partitions import months_to_ensure, partition_ddl

revision: str = "0001_kook_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_search")

    op.create_table(
        "admins",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("kind", "value", name=op.f("pk_admins")),
    )
    op.create_table(
        "guilds",
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("icon", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_guilds")),
    )
    op.create_table(
        "channels",
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("is_category", sa.Boolean(), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), server_default="", nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("backfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", name=op.f("pk_channels")),
    )
    op.create_index("ix_channels_guild_parent", "channels", ["guild_id", "parent_id"], unique=False)
    op.create_table(
        "command_triggers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("handler", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=True),
        sa.Column("match_type", sa.Text(), nullable=False),
        sa.Column("patterns", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("arg_template", sa.Text(), nullable=True),
        sa.Column("require_mention", sa.Boolean(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("priority", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("cooldown_s", sa.BigInteger(), nullable=False),
        sa.Column("allowed_roles", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_command_triggers")),
    )
    op.create_index("ix_command_triggers_scope", "command_triggers", ["scope", "scope_id"], unique=False)
    op.create_table(
        "config_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tags", sa.ARRAY(sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_audit")),
    )
    op.create_table(
        "providers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("api_format", sa.Text(), server_default="openai", nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key_enc", sa.LargeBinary(), nullable=True),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("models", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("models_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model_aliases", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("model_caps", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("upstream_caps", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("balance_key_enc", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_providers")),
        sa.UniqueConstraint("name", name=op.f("uq_providers_name")),
    )
    op.create_table(
        "search_providers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key_enc", sa.LargeBinary(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.BigInteger(), nullable=False),
        sa.Column("monthly_limit", sa.BigInteger(), nullable=True),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_providers")),
        sa.UniqueConstraint("name", name=op.f("uq_search_providers_name")),
    )
    op.create_table(
        "task_routes",
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fallback_provider_id", sa.BigInteger(), nullable=True),
        sa.Column("fallback_model", sa.Text(), nullable=True),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["fallback_provider_id"],
            ["providers.id"],
            name=op.f("fk_task_routes_fallback_provider_id_providers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["providers.id"], name=op.f("fk_task_routes_provider_id_providers"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("task", name=op.f("pk_task_routes")),
    )
    op.create_table(
        "embed_spaces",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dim", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["providers.id"], name=op.f("fk_embed_spaces_provider_id_providers"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embed_spaces")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "prompts",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("active_version_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_prompts")),
    )
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_versions")),
    )
    op.create_index("ix_prompt_versions_key", "prompt_versions", ["key", "id"], unique=False)
    op.create_table(
        "pipeline_state",
        sa.Column("pipeline", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("last_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("pipeline", "scope_id", name=op.f("pk_pipeline_state")),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("kook_message_id", sa.Text(), nullable=False),
        sa.Column("reply_to_msg_id", sa.Text(), nullable=True),
        sa.Column("sender_name", sa.Text(), nullable=False),
        sa.Column("plain_text", sa.Text(), nullable=False),
        sa.Column("segments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_self", sa.Boolean(), nullable=False),
        sa.Column("recalled", sa.Boolean(), nullable=False),
        sa.Column("recalled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", "sent_at", name=op.f("pk_messages")),
        postgresql_partition_by="RANGE (sent_at)",
    )
    op.create_index("ix_messages_channel_sent", "messages", ["channel_id", sa.literal_column("sent_at DESC")])
    op.create_index("ix_messages_guild_sent", "messages", ["guild_id", sa.literal_column("sent_at DESC")])
    op.create_index("ix_messages_channel_kook", "messages", ["channel_id", "kook_message_id"])
    op.create_index(
        "ix_messages_guild_user_sent", "messages", ["guild_id", "user_id", sa.literal_column("sent_at DESC")]
    )
    op.create_table(
        "user_aliases",
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "user_id", "name", name=op.f("pk_user_aliases")),
    )
    op.create_table(
        "stickers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=True),
        sa.Column("emoji_id", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("width", sa.BigInteger(), nullable=True),
        sa.Column("height", sa.BigInteger(), nullable=True),
        sa.Column("frames", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("usage", sa.Text(), nullable=False),
        sa.Column("ocr_text", sa.Text(), nullable=False),
        sa.Column("emotions", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("unsafe_category", sa.Text(), nullable=False),
        sa.Column("tag_attempts", sa.BigInteger(), nullable=False),
        sa.Column("tagged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_count", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stickers")),
        sa.UniqueConstraint("kind", "ref", name=op.f("uq_stickers_kind_ref")),
    )
    op.create_index("ix_stickers_status", "stickers", ["status"], unique=False)
    op.create_table(
        "sticker_blobs",
        sa.Column("sticker_id", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["sticker_id"], ["stickers.id"], name=op.f("fk_sticker_blobs_sticker_id_stickers"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("sticker_id", name=op.f("pk_sticker_blobs")),
    )
    op.create_table(
        "sticker_usage",
        sa.Column("sticker_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["sticker_id"], ["stickers.id"], name=op.f("fk_sticker_usage_sticker_id_stickers"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("sticker_id", "guild_id", name=op.f("pk_sticker_usage")),
    )
    op.create_table(
        "sticker_drops",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("fit", sa.Float(), nullable=True),
        sa.Column("sticker_id", sa.BigInteger(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("candidates", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("last_message_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["sticker_id"], ["stickers.id"], name=op.f("fk_sticker_drops_sticker_id_stickers"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sticker_drops")),
    )
    op.create_index("ix_sticker_drops_channel_at", "sticker_drops", ["channel_id", sa.literal_column("at DESC")])
    op.create_table(
        "message_chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("first_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("last_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("msg_count", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_chunks")),
    )
    op.create_index("ix_message_chunks_channel_end", "message_chunks", ["channel_id", "end_at"], unique=False)
    op.create_index("ix_message_chunks_guild_end", "message_chunks", ["guild_id", "end_at"], unique=False)
    op.create_table(
        "topics",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("keywords", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("participants", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("last_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("msg_count", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("dirty", sa.Boolean(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("importance", sa.SmallInteger(), nullable=True),
        sa.Column("importance_reason", sa.Text(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topics")),
    )
    op.create_index("ix_topics_channel_status", "topics", ["channel_id", "status", "end_at"], unique=False)
    op.create_index("ix_topics_guild_status", "topics", ["guild_id", "status", "end_at"], unique=False)
    op.create_table(
        "topic_messages",
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["topic_id"], ["topics.id"], name=op.f("fk_topic_messages_topic_id_topics"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("topic_id", "message_id", name=op.f("pk_topic_messages")),
    )
    op.create_index("ix_topic_messages_message", "topic_messages", ["message_id"], unique=False)
    op.create_table(
        "daily_digests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("topic_ids", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_digests")),
        sa.UniqueConstraint("guild_id", "day", name=op.f("uq_daily_digests_guild_id_day")),
    )
    op.create_table(
        "glossary_terms",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("aliases", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("examples", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hits", sa.BigInteger(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_glossary_terms")),
        sa.UniqueConstraint("guild_id", "term", name=op.f("uq_glossary_terms_guild_id_term")),
    )
    op.create_table(
        "glossary_candidates",
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("example_msg_ids", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "term", name=op.f("pk_glossary_candidates")),
    )
    op.create_table(
        "user_profiles",
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("interests", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("traits", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("opted_out", sa.Boolean(), nullable=False),
        sa.Column("last_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_topic_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "user_id", name=op.f("pk_user_profiles")),
    )
    op.create_table(
        "user_profile_facts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("fact", sa.Text(), nullable=False),
        sa.Column("source_msg_ids", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("importance", sa.SmallInteger(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_reason", sa.Text(), nullable=True),
        sa.Column("locked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_profile_facts")),
    )
    op.create_index("ix_user_profile_facts_user", "user_profile_facts", ["guild_id", "user_id"], unique=False)
    op.create_table(
        "memory_vectors",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ref_id", sa.BigInteger(), nullable=False),
        sa.Column("space_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=True),
        sa.Column("embedding", pgvector.sqlalchemy.halfvec.HALFVEC(dim=1024), nullable=False),
        sa.ForeignKeyConstraint(
            ["space_id"], ["embed_spaces.id"], name=op.f("fk_memory_vectors_space_id_embed_spaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("kind", "ref_id", "space_id", name=op.f("pk_memory_vectors")),
    )
    op.create_index(
        "ix_memory_vectors_hnsw",
        "memory_vectors",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
    )
    op.create_index("ix_memory_vectors_guild_scope", "memory_vectors", ["guild_id", "space_id", "kind"], unique=False)
    op.create_index(
        "ix_memory_vectors_channel_scope", "memory_vectors", ["channel_id", "space_id", "kind"], unique=False
    )
    op.create_table(
        "invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=True),
        sa.Column("guild_id", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("handler", sa.Text(), nullable=False),
        sa.Column("trigger_msg_id", sa.Text(), nullable=True),
        sa.Column("reply_msg_id", sa.Text(), nullable=True),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("models", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tokens_in", sa.BigInteger(), nullable=False),
        sa.Column("tokens_out", sa.BigInteger(), nullable=False),
        sa.Column("tokens_cached", sa.BigInteger(), nullable=False),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ack_kind", sa.Text(), nullable=True),
        sa.Column("ack_latency_ms", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("input_verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output_verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reply", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invocations")),
    )
    op.create_index("ix_invocations_created", "invocations", ["created_at"], unique=False)
    op.create_index("ix_invocations_channel_created", "invocations", ["channel_id", "created_at"], unique=False)
    op.create_index("ix_invocations_guild_created", "invocations", ["guild_id", "created_at"], unique=False)
    op.create_table(
        "invocation_sessions",
        sa.Column("invocation_id", sa.Uuid(), nullable=False),
        sa.Column("handler", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("tools", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sandbox", sa.Boolean(), nullable=False),
        sa.Column("skills", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("messages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("handles", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("seen_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["invocations.id"],
            name=op.f("fk_invocation_sessions_invocation_id_invocations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("invocation_id", name=op.f("pk_invocation_sessions")),
    )
    op.create_index("ix_invocation_sessions_created", "invocation_sessions", ["created_at"], unique=False)
    op.create_table(
        "moderation_results",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("invocation_id", sa.Uuid(), nullable=True),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("categories", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["invocations.id"],
            name=op.f("fk_moderation_results_invocation_id_invocations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_results")),
    )
    op.create_index("ix_moderation_results_created", "moderation_results", ["created_at"], unique=False)
    op.create_table(
        "review_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("invocation_id", sa.Uuid(), nullable=True),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("trigger_msg_id", sa.Text(), nullable=True),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("reply", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["invocations.id"],
            name=op.f("fk_review_items_invocation_id_invocations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_items")),
    )
    op.create_index("ix_review_items_status", "review_items", ["status", "expires_at"], unique=False)
    op.create_table(
        "bot_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("operator_id", sa.Text(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("handled_by", sa.Text(), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_events")),
    )
    op.create_index("ix_bot_events_at", "bot_events", ["at"], unique=False)
    op.create_index("ix_bot_events_status", "bot_events", ["status", "at"], unique=False)
    op.create_table(
        "sandbox_jobs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("record", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("events", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sandbox_jobs")),
    )
    op.create_index("ix_sandbox_jobs_created", "sandbox_jobs", [sa.literal_column("created_at DESC")])
    op.create_index(
        "ix_sandbox_jobs_channel_created", "sandbox_jobs", ["channel_id", sa.literal_column("created_at DESC")]
    )
    op.create_table(
        "transcripts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("invocation_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("entries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["invocations.id"],
            name=op.f("fk_transcripts_invocation_id_invocations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcripts")),
        sa.UniqueConstraint("invocation_id", name=op.f("uq_transcripts_invocation_id")),
        sa.UniqueConstraint("job_id", name=op.f("uq_transcripts_job_id")),
    )
    op.create_table(
        "transcript_blobs",
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_transcript_blobs")),
    )

    for month in months_to_ensure(datetime.now(UTC).date(), back=1, ahead=3):
        op.execute(partition_ddl(month))

    # BM25 over message text (jieba tokenizer) with guild_id, channel_id, user_id, sent_at, recalled as filter fields.
    op.execute(
        "CREATE INDEX messages_bm25 ON messages USING bm25 "
        "(id, (plain_text::pdb.jieba), guild_id, channel_id, user_id, sent_at, recalled, is_self)"
    )
    op.execute(
        "CREATE INDEX topics_bm25 ON topics USING bm25 "
        "(id, (title::pdb.jieba), (summary::pdb.jieba), guild_id, channel_id, end_at)"
    )


def downgrade() -> None:
    for tbl in (
        "transcript_blobs",
        "transcripts",
        "sandbox_jobs",
        "bot_events",
        "review_items",
        "moderation_results",
        "invocation_sessions",
        "invocations",
        "memory_vectors",
        "user_profile_facts",
        "user_profiles",
        "glossary_candidates",
        "glossary_terms",
        "daily_digests",
        "topic_messages",
        "topics",
        "message_chunks",
        "sticker_drops",
        "sticker_usage",
        "sticker_blobs",
        "stickers",
        "user_aliases",
        "messages",
        "pipeline_state",
        "prompt_versions",
        "prompts",
        "settings",
        "embed_spaces",
        "task_routes",
        "search_providers",
        "providers",
        "config_audit",
        "command_triggers",
        "channels",
        "guilds",
        "admins",
    ):
        op.drop_table(tbl)
