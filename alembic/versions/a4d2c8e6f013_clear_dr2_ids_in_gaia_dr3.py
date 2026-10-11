"""Clear Gaia DR2 ids written into targets.gaia_dr3_id by TIC enrichment

TIC 8.2's GAIA column is a Gaia DR2 source_id; v0.1.13-v0.1.20 enrichment copied it
into gaia_dr3_id for TIC-only targets. Only NASA pscomppars rows and dossiers that
state a DR3 id are trusted; every other target's gaia_dr3_id is cleared.

Revision ID: a4d2c8e6f013
Revises: 9c3e5a1d2b77
Create Date: 2026-10-11 10:40:00
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4d2c8e6f013"
down_revision: str | Sequence[str] | None = "9c3e5a1d2b77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

targets = sa.table("targets", sa.column("id", sa.String), sa.column("gaia_dr3_id", sa.String))
reports = sa.table(
    "reports",
    sa.column("target_id", sa.String),
    sa.column("source_name", sa.String),
    sa.column("raw_payload", sa.JSON),
)


def _payload(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def upgrade() -> None:
    bind = op.get_bind()
    trusted: set[str] = set()
    for target_id, source, raw in bind.execute(
        sa.select(reports.c.target_id, reports.c.source_name, reports.c.raw_payload)
    ):
        payload = _payload(raw)
        if source == "nasa_pscomppars" and payload.get("gaia_dr3_id"):
            trusted.add(target_id)
        elif source.startswith("git:") and (payload.get("target") or {}).get("gaia_dr3"):
            trusted.add(target_id)
    ids = [
        tid
        for (tid,) in bind.execute(sa.select(targets.c.id).where(targets.c.gaia_dr3_id.is_not(None)))
        if tid not in trusted
    ]
    for i in range(0, len(ids), 500):
        bind.execute(sa.update(targets).where(targets.c.id.in_(ids[i : i + 500])).values(gaia_dr3_id=None))


def downgrade() -> None:
    pass
