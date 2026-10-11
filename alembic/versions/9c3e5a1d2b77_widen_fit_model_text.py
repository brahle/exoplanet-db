"""Widen ephemeris_fits.fit_model and covariance_method to TEXT

Dossiers describe their fit model in prose; run-5 descriptions exceed 128 chars.

Revision ID: 9c3e5a1d2b77
Revises: 7b1f0c2a9e41
Create Date: 2026-10-11 10:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9c3e5a1d2b77"
down_revision: str | Sequence[str] | None = "7b1f0c2a9e41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ephemeris_fits") as batch:
        batch.alter_column("fit_model", type_=sa.Text(), existing_nullable=False)
        batch.alter_column("covariance_method", type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("ephemeris_fits") as batch:
        batch.alter_column("fit_model", type_=sa.String(length=128), existing_nullable=False)
        batch.alter_column("covariance_method", type_=sa.String(length=256), existing_nullable=True)
