"""Repair NASA composite ingest data: depth units and merged TIC-less hosts

Two ingest bugs corrupted rows written from pscomppars:

1. pl_trandep is published in percent but was stored as ppm, so every composite
   depth is 10^4 too small. Reports, ephemeris fits and signals whose depth still
   equals the raw percent value are rescaled.
2. A missing tic_id was stored as the string "None", and target matching on it
   attached every TIC-less host (pulsars, microlensing, imaging) to one target.
   Those targets get tic_id NULL, and each signal whose composite row names a
   different host moves (with its reports) to that host's own target.

Data-only and idempotent; downgrade is a no-op.

Revision ID: 7b1f0c2a9e41
Revises: 6de86264d260
Create Date: 2026-10-10 23:30:00
"""

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "7b1f0c2a9e41"
down_revision: str | Sequence[str] | None = "6de86264d260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

targets = sa.table(
    "targets",
    sa.column("id", sa.String),
    sa.column("tic_id", sa.String),
    sa.column("gaia_dr3_id", sa.String),
    sa.column("primary_name", sa.String),
    sa.column("aliases", sa.JSON),
    sa.column("ra_deg", sa.Float),
    sa.column("dec_deg", sa.Float),
    sa.column("pm_ra_mas_yr", sa.Float),
    sa.column("pm_dec_mas_yr", sa.Float),
    sa.column("epoch", sa.String),
    sa.column("frame", sa.String),
    sa.column("v_mag", sa.Float),
    sa.column("tess_mag", sa.Float),
    sa.column("teff_k", sa.Float),
    sa.column("radius_rsun", sa.Float),
    sa.column("mass_msun", sa.Float),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
signals = sa.table(
    "signals",
    sa.column("id", sa.String),
    sa.column("target_id", sa.String),
    sa.column("primary_depth_ppm", sa.Float),
)
reports = sa.table(
    "reports",
    sa.column("id", sa.String),
    sa.column("target_id", sa.String),
    sa.column("signal_id", sa.String),
    sa.column("source_name", sa.String),
    sa.column("depth_ppm", sa.Float),
    sa.column("raw_payload", sa.JSON),
)
fits = sa.table(
    "ephemeris_fits",
    sa.column("report_id", sa.String),
    sa.column("depth_ppm", sa.Float),
)

COMPOSITE = "nasa_pscomppars"


def _payload(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def _float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _same(a, b):
    return a is not None and b is not None and abs(a - b) <= 1e-9 * max(1.0, abs(b))


def _repair_depths(bind) -> None:
    rows = bind.execute(
        sa.select(
            reports.c.id, reports.c.signal_id, reports.c.depth_ppm, reports.c.raw_payload
        ).where(reports.c.source_name == COMPOSITE, reports.c.depth_ppm.is_not(None))
    ).all()
    for report_id, signal_id, depth, raw in rows:
        percent = _float(_payload(raw).get("pl_trandep"))
        if percent is None or not _same(depth, percent):
            continue  # already ppm, or not traceable to the raw value
        ppm = percent * 10_000.0
        bind.execute(sa.update(reports).where(reports.c.id == report_id).values(depth_ppm=ppm))
        bind.execute(
            sa.update(fits)
            .where(fits.c.report_id == report_id, fits.c.depth_ppm == depth)
            .values(depth_ppm=ppm)
        )
        if signal_id:
            bind.execute(
                sa.update(signals)
                .where(signals.c.id == signal_id, signals.c.primary_depth_ppm == depth)
                .values(primary_depth_ppm=ppm)
            )


def _split_ticless_hosts(bind) -> None:
    bogus = bind.execute(
        sa.select(targets.c.id, targets.c.primary_name).where(targets.c.tic_id == "None")
    ).all()
    if not bogus:
        return
    bind.execute(sa.update(targets).where(targets.c.tic_id == "None").values(tic_id=None))
    now = datetime.now(UTC)

    for bogus_id, bogus_name in bogus:
        rows = bind.execute(
            sa.select(reports.c.signal_id, reports.c.raw_payload).where(
                reports.c.target_id == bogus_id,
                reports.c.source_name == COMPOSITE,
                reports.c.signal_id.is_not(None),
            )
        ).all()
        for signal_id, raw in rows:
            payload = _payload(raw)
            host = str(payload.get("hostname") or "").strip()
            if not host or host == bogus_name:
                continue
            existing = bind.execute(
                sa.select(targets.c.id).where(
                    targets.c.primary_name == host, targets.c.id != bogus_id
                )
            ).first()
            if existing:
                new_id = existing[0]
            else:
                new_id = str(uuid.uuid4())
                gaia = (
                    str(payload.get("gaia_dr3_id") or payload.get("gaia_id") or "")
                    .replace("Gaia DR3", "")
                    .replace("Gaia DR2", "")
                    .strip()
                    or None
                )
                bind.execute(
                    sa.insert(targets).values(
                        id=new_id,
                        tic_id=None,
                        gaia_dr3_id=gaia,
                        primary_name=host,
                        aliases=[host],
                        ra_deg=_float(payload.get("ra")) or 0.0,
                        dec_deg=_float(payload.get("dec")) or 0.0,
                        pm_ra_mas_yr=_float(payload.get("sy_pmra")),
                        pm_dec_mas_yr=_float(payload.get("sy_pmdec")),
                        epoch="J2000",
                        frame="ICRS",
                        v_mag=_float(payload.get("sy_vmag")),
                        tess_mag=_float(payload.get("sy_tmag")),
                        teff_k=_float(payload.get("st_teff")),
                        radius_rsun=_float(payload.get("st_rad")),
                        mass_msun=_float(payload.get("st_mass")),
                        created_at=now,
                        updated_at=now,
                    )
                )
            bind.execute(
                sa.update(signals).where(signals.c.id == signal_id).values(target_id=new_id)
            )
            bind.execute(
                sa.update(reports).where(reports.c.signal_id == signal_id).values(target_id=new_id)
            )


def upgrade() -> None:
    bind = op.get_bind()
    _repair_depths(bind)
    _split_ticless_hosts(bind)


def downgrade() -> None:
    # Data repair only; the pre-repair state was corrupt and is not restored.
    pass
