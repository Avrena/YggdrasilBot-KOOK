"""sticker drops

One row per sticker-drop decision (``stickers.drop``): outcome, the model's fit and pick, the candidates it saw, the
newest message it read, tokens and cost. The console lists them next to the invocations.

Revision ID: 7c3e5a91d2f4
Revises: d41f8b2c6a93
Create Date: 2026-11-20 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7c3e5a91d2f4"
down_revision: str | Sequence[str] | None = "d41f8b2c6a93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sticker_drops",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("fit", sa.Float(), nullable=True),
        sa.Column("sticker_id", sa.BigInteger(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("candidates", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("last_message_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["sticker_id"], ["stickers.id"], name=op.f("fk_sticker_drops_sticker_id_stickers"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sticker_drops")),
    )
    op.create_index("ix_sticker_drops_group_at", "sticker_drops", ["group_id", sa.text("at DESC")])


def downgrade() -> None:
    op.drop_index("ix_sticker_drops_group_at", table_name="sticker_drops")
    op.drop_table("sticker_drops")
