"""Shared ingest plumbing: idempotent report/fit upserts, GitHub revisions, TIC enrichment."""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

import httpx
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exoplanet_db.models import EphemerisFit, Report, ReproducibilityRun, Target

logger = logging.getLogger(__name__)

VIZIER_TAP_URL = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"
TIC_BATCH = 400


def _column_values(obj: Any, skip: tuple[str, ...] = ("id", "created_at")) -> dict[str, Any]:
    mapper = sa_inspect(type(obj))
    return {
        c.key: getattr(obj, c.key)
        for c in mapper.column_attrs
        if c.key not in skip and getattr(obj, c.key) is not None
    }


async def upsert_report(session: AsyncSession, report: Report) -> Report:
    """Insert `report`, or update the existing row with the same (source_name, source_key).

    Re-ingesting a source then refreshes its links and values instead of piling up copies.
    """
    existing = (
        (
            await session.execute(
                select(Report).where(
                    Report.source_name == report.source_name,
                    Report.source_key == report.source_key,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        session.add(report)
        await session.flush()
        return report
    for key, value in _column_values(report).items():
        setattr(existing, key, value)
    await session.flush()
    return existing


async def upsert_fit(session: AsyncSession, fit: EphemerisFit) -> EphemerisFit:
    """One ephemeris fit per report: update it in place when the report already has one."""
    existing = (
        (await session.execute(select(EphemerisFit).where(EphemerisFit.report_id == fit.report_id)))
        .scalars()
        .first()
    )
    if existing is None:
        session.add(fit)
        await session.flush()
        return fit
    for key, value in _column_values(fit).items():
        setattr(existing, key, value)
    await session.flush()
    return existing


async def upsert_repro_run(session: AsyncSession, run: ReproducibilityRun) -> ReproducibilityRun:
    existing = (
        (
            await session.execute(
                select(ReproducibilityRun).where(ReproducibilityRun.report_id == run.report_id)
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        session.add(run)
        await session.flush()
        return run
    for key, value in _column_values(run).items():
        setattr(existing, key, value)
    await session.flush()
    return existing


async def github_head_sha(repo: str, ref: str = "main", timeout: float = 30.0) -> str:
    """Resolve a branch to its commit SHA so stored links point at an immutable revision."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/commits/{ref}",
            headers={"Accept": "application/vnd.github.sha"},
        )
        resp.raise_for_status()
        sha = resp.text.strip()
    if len(sha) != 40:
        raise ValueError(f"Unexpected commit SHA for {repo}@{ref}: {sha!r}")
    return sha


def _float(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # drop NaN


async def fetch_tic_rows(tic_ids: list[str], timeout: float = 120.0) -> dict[str, dict[str, Any]]:
    """TIC 8.2 coordinates and stellar parameters from VizieR (IV/39/tic82), keyed by TIC id."""
    ids = sorted({t for t in tic_ids if t and t.isdigit()}, key=int)
    out: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for i in range(0, len(ids), TIC_BATCH):
            batch = ids[i : i + TIC_BATCH]
            query = (
                "SELECT TIC, RAJ2000, DEJ2000, Tmag, Vmag, Teff, Rad, Mass, GAIA, pmRA, pmDE "
                f'FROM "IV/39/tic82" WHERE TIC IN ({", ".join(batch)})'
            )
            resp = await client.post(
                VIZIER_TAP_URL,
                data={"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": query},
            )
            resp.raise_for_status()
            for row in csv.DictReader(io.StringIO(resp.text)):
                out[str(row["TIC"]).strip()] = {
                    "ra_deg": _float(row.get("RAJ2000")),
                    "dec_deg": _float(row.get("DEJ2000")),
                    "tess_mag": _float(row.get("Tmag")),
                    "v_mag": _float(row.get("Vmag")),
                    "teff_k": _float(row.get("Teff")),
                    "radius_rsun": _float(row.get("Rad")),
                    "mass_msun": _float(row.get("Mass")),
                    "gaia_dr3_id": (row.get("GAIA") or "").strip() or None,
                    "pm_ra_mas_yr": _float(row.get("pmRA")),
                    "pm_dec_mas_yr": _float(row.get("pmDE")),
                }
    return out


async def enrich_tic_targets(session: AsyncSession) -> int:
    """Fill coordinates and stellar parameters for TIC targets that lack them.

    Community catalogues (eumemic, some CTOIs) publish only a TIC id; a target stored with
    RA/Dec 0/0 would be scheduled at the wrong place in the sky.
    """
    targets = list(
        (
            await session.execute(
                select(Target).where(
                    Target.tic_id.is_not(None),
                    (
                        ((Target.ra_deg == 0.0) & (Target.dec_deg == 0.0))
                        | Target.radius_rsun.is_(None)
                        | Target.mass_msun.is_(None)
                        | Target.teff_k.is_(None)
                    ),
                )
            )
        ).scalars()
    )
    if not targets:
        return 0
    rows = await fetch_tic_rows([t.tic_id for t in targets])
    updated = 0
    for t in targets:
        row = rows.get(t.tic_id)
        if not row:
            continue
        if t.ra_deg == 0.0 and t.dec_deg == 0.0 and row["ra_deg"] is not None:
            t.ra_deg = row["ra_deg"]
            t.dec_deg = row["dec_deg"]
            t.pm_ra_mas_yr = t.pm_ra_mas_yr or row["pm_ra_mas_yr"]
            t.pm_dec_mas_yr = t.pm_dec_mas_yr or row["pm_dec_mas_yr"]
        for key in ("teff_k", "radius_rsun", "mass_msun", "tess_mag", "v_mag", "gaia_dr3_id"):
            if getattr(t, key) is None and row.get(key) is not None:
                setattr(t, key, row[key])
        updated += 1
    await session.commit()
    logger.info("Enriched %d TIC targets from VizieR TIC 8.2", updated)
    return updated
