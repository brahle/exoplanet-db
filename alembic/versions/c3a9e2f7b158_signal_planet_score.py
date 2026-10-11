"""Add signals.planet_score and signals.score_factors

Revision ID: c3a9e2f7b158
Revises: b7e1f4c9d025
Create Date: 2026-10-11 12:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c3a9e2f7b158"
down_revision: str | Sequence[str] | None = "b7e1f4c9d025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    with op.batch_alter_table("signals") as batch:
        batch.add_column(sa.Column("planet_score", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("score_factors", json_type, nullable=True))
    op.create_index("ix_signals_planet_score", "signals", ["planet_score"])


def downgrade() -> None:
    op.drop_index("ix_signals_planet_score", table_name="signals")
    with op.batch_alter_table("signals") as batch:
        batch.drop_column("score_factors")
        batch.drop_column("planet_score")
