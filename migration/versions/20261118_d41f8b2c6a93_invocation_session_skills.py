"""invocation session skills

A continued answer rebuilds the skills (``agent.skills``) its agent could load, so the model keeps the tools it
loaded: ``invocation_sessions`` keeps their names. Rows kept before had none.

Revision ID: d41f8b2c6a93
Revises: e2a9c4f7b130
Create Date: 2026-11-18 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d41f8b2c6a93"
down_revision: str | Sequence[str] | None = "e2a9c4f7b130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invocation_sessions", sa.Column("skills", postgresql.JSONB(), server_default="[]", nullable=False))


def downgrade() -> None:
    op.drop_column("invocation_sessions", "skills")
