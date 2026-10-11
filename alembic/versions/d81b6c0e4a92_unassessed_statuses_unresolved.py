"""Reset localisation/timing statuses that no source assessed

TOI and eumemic ingest hard-coded "supported" without any localisation or timing
analysis. Only NASA confirmed planets (pscomppars) and evidence dossiers assess these;
every other signal goes back to "unresolved".

Revision ID: d81b6c0e4a92
Revises: c3a9e2f7b158
Create Date: 2026-10-11 12:50:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d81b6c0e4a92"
down_revision: str | Sequence[str] | None = "c3a9e2f7b158"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

signals = sa.table(
    "signals",
    sa.column("id", sa.String),
    sa.column("source_localisation_status", sa.String),
    sa.column("timing_consistency_status", sa.String),
)
reports = sa.table("reports", sa.column("signal_id", sa.String), sa.column("source_name", sa.String))


def upgrade() -> None:
    assessed = sa.select(reports.c.signal_id).where(
        (reports.c.source_name == "nasa_pscomppars") | reports.c.source_name.like("git:%")
    )
    op.get_bind().execute(
        sa.update(signals)
        .where(signals.c.id.not_in(assessed))
        .values(source_localisation_status="unresolved", timing_consistency_status="unresolved")
    )


def downgrade() -> None:
    pass
