"""Add variable_stars (EB / variable-star catalogues) and signals.variable_matches

Revision ID: e4b7a19c3f60
Revises: d81b6c0e4a92
Create Date: 2026-10-12 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e4b7a19c3f60"
down_revision: str | Sequence[str] | None = "d81b6c0e4a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "variable_stars",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("catalogue", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=True),
        sa.Column("ra_deg", sa.Float(), nullable=False),
        sa.Column("dec_deg", sa.Float(), nullable=False),
        sa.Column("period_d", sa.Float(), nullable=False),
        sa.Column("var_type", sa.String(32), nullable=True),
        sa.Column("mag", sa.Float(), nullable=True),
        sa.Column("mag_band", sa.String(8), nullable=True),
        sa.Column("epoch_bjd", sa.Float(), nullable=True),
    )
    op.create_index("ix_variable_stars_catalogue", "variable_stars", ["catalogue"])
    op.create_index("idx_variable_stars_coords", "variable_stars", ["dec_deg", "ra_deg"])
    with op.batch_alter_table("signals") as batch:
        batch.add_column(sa.Column("variable_matches", json_type, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("signals") as batch:
        batch.drop_column("variable_matches")
    op.drop_index("idx_variable_stars_coords", table_name="variable_stars")
    op.drop_index("ix_variable_stars_catalogue", table_name="variable_stars")
    op.drop_table("variable_stars")
