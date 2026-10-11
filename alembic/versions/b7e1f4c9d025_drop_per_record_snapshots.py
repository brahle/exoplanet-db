"""Drop per-dossier and per-file snapshot rows

Dossier ingest wrote one PublishedSnapshot per dossier (172 per run) and eumemic one per
release file, so "checked sources" listings grew every night. Ingest now records one
snapshot per source per run; this removes the per-record rows already written.

Revision ID: b7e1f4c9d025
Revises: a4d2c8e6f013
Create Date: 2026-10-11 11:30:00
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e1f4c9d025"
down_revision: str | Sequence[str] | None = "a4d2c8e6f013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

snapshots = sa.table(
    "published_snapshots",
    sa.column("id", sa.String),
    sa.column("source_name", sa.String),
    sa.column("validation_metrics", sa.JSON),
)


def upgrade() -> None:
    bind = op.get_bind()
    doomed = []
    for sid, source, metrics in bind.execute(
        sa.select(snapshots.c.id, snapshots.c.source_name, snapshots.c.validation_metrics)
    ):
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except ValueError:
                metrics = {}
        metrics = metrics or {}
        if source.startswith("git:") and "candidate" in metrics:
            doomed.append(sid)
        elif source == "eumemic/exoplanets" and "source_file" in metrics:
            doomed.append(sid)
    for i in range(0, len(doomed), 500):
        bind.execute(sa.delete(snapshots).where(snapshots.c.id.in_(doomed[i : i + 500])))


def downgrade() -> None:
    pass
