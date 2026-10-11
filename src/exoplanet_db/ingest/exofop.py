from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exoplanet_db.ingest.common import upsert_fit, upsert_report
from exoplanet_db.models import (
    EphemerisFit,
    PublishedSnapshot,
    Report,
    Signal,
    Target,
)
from exoplanet_db.services.reconciliation import match_signal_to_existing

logger = logging.getLogger(__name__)

EXOFOP_ACKNOWLEDGEMENT = (
    "ExoFOP (Exoplanet Follow-up Observing Program) data operated by NExScI at "
    "IPAC / Caltech under contract with NASA. CTOIs are community transit observations."
)


class ExoFOPIngest:
    """
    Ingestion client for ExoFOP TOIs and Community TOIs (CTOIs).
    Maps CTOIs as first-class community candidate reports and maintains field-level provenance.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    def parse_ctoi_csv(self, csv_text: str) -> list[dict[str, Any]]:
        """Parses the ExoFOP CTOI export (download_ctoi.php?output=csv)."""

        def num(row: dict[str, str], *keys: str) -> float | None:
            for k in keys:
                v = (row.get(k) or "").strip()
                if v:
                    try:
                        return float(v)
                    except ValueError:
                        return None
            return None

        records = []
        for row in csv.DictReader(io.StringIO(csv_text.strip())):
            tic = (row.get("TIC ID") or row.get("tic_id") or row.get("TIC") or "").strip()
            ctoi = (row.get("CTOI") or row.get("ctoi") or "").strip()
            if not tic or not ctoi:
                continue
            epoch = num(row, "Transit Epoch (BJD)", "Epoch (BJD)", "epoch")
            if epoch is not None and epoch < 2400000:
                epoch += 2457000.0
            tfop = (row.get("TFOPWG Disposition") or "").strip().upper()
            user_disp = (row.get("User Disposition") or "").strip().upper()
            disp = tfop or user_disp
            records.append(
                {
                    "ctoi_id": f"CTOI {ctoi}",
                    "tic": tic,
                    "period_d": num(row, "Period (days)", "period"),
                    "period_err_d": num(row, "Period (days) Error"),
                    "epoch_bjd": epoch,
                    "epoch_err_d": num(row, "Transit Epoch (BJD) err"),
                    "depth_ppm": num(row, "Depth ppm", "Depth (ppm)", "depth"),
                    "duration_h": num(row, "Duration (hrs)", "Duration (hours)", "duration"),
                    "ra_deg": num(row, "RA", "ra"),
                    "dec_deg": num(row, "Dec", "dec"),
                    "teff_k": num(row, "Stellar Eff Temp (K)"),
                    "radius_rsun": num(row, "Stellar Radius (R_Sun)"),
                    "tess_mag": num(row, "TESS Mag"),
                    # A community user's CP claim stays on the report; it never confirms the signal.
                    "disposition": "false_positive" if disp in ("FP", "FA") else "planet_candidate",
                    "reported": disp or "PC",
                    "promoted_toi": (row.get("Promoted to TOI") or "").strip() or None,
                    "user": (row.get("User") or row.get("user") or "").strip()
                    or "ExoFOP community",
                    "notes": (row.get("Notes") or "").strip(),
                    "raw": dict(row),
                }
            )
        return records

    async def ingest_ctois(
        self,
        records: list[dict[str, Any]],
        snapshot_time: datetime | None = None,
    ) -> int:
        """Ingests CTOI records as community candidate reports."""
        now = snapshot_time or datetime.now(UTC)
        count = 0

        for r in records:
            tic = str(r["tic"])
            target_name = f"TIC {tic}"

            target_stmt = select(Target).where(
                (Target.tic_id == tic) | (Target.primary_name == target_name)
            )
            res = await self.session.execute(target_stmt)
            target = res.scalars().first()

            if not target:
                target = Target(
                    primary_name=target_name,
                    tic_id=tic,
                    ra_deg=r["ra_deg"] if r["ra_deg"] is not None else 0.0,
                    dec_deg=r["dec_deg"] if r["dec_deg"] is not None else 0.0,
                    teff_k=r["teff_k"],
                    radius_rsun=r["radius_rsun"],
                    tess_mag=r["tess_mag"],
                    aliases=[f"TIC {tic}"],
                )
                self.session.add(target)
                await self.session.flush()

            period = r["period_d"] or 0.0
            epoch = r["epoch_bjd"] or 0.0
            existing_signals = list(
                (
                    await self.session.execute(select(Signal).where(Signal.target_id == target.id))
                ).scalars()
            )
            matched_signal = next(
                (s for s in existing_signals if s.display_name == r["ctoi_id"]), None
            )
            if matched_signal is None and period > 0 and epoch > 0:
                matched_signal = match_signal_to_existing(
                    existing_signals=existing_signals,
                    cand_period=period,
                    cand_epoch=epoch,
                    period_err=r["period_err_d"],
                    epoch_err=r["epoch_err_d"],
                    duration_h=r["duration_h"],
                )

            limits = [f"ExoFOP community TOI {r['ctoi_id']} submitted by {r['user']}"]
            if r["promoted_toi"]:
                limits.append(f"Promoted to TOI {r['promoted_toi']}")
            if not period:
                limits.append("No period published (single transit or unconstrained)")

            if matched_signal:
                signal = matched_signal
                signal.scientific_limits = list(
                    dict.fromkeys((signal.scientific_limits or []) + limits)
                )
            else:
                signal = Signal(
                    target_id=target.id,
                    display_name=r["ctoi_id"],
                    primary_period_d=period,
                    primary_period_err_d=r["period_err_d"],
                    primary_epoch_bjd=epoch,
                    primary_epoch_err_d=r["epoch_err_d"],
                    primary_duration_h=r["duration_h"],
                    primary_depth_ppm=r["depth_ppm"],
                    current_disposition=r["disposition"],
                    source_localisation_status="unresolved",
                    timing_consistency_status="unresolved",
                    scientific_limits=limits,
                )
                self.session.add(signal)
                await self.session.flush()

            report = await upsert_report(
                self.session,
                Report(
                    target_id=target.id,
                    signal_id=signal.id,
                    source_name="exofop_ctoi",
                    source_key=r["ctoi_id"],
                    retrieval_utc=now,
                    author_or_team=r["user"],
                    bibcode_or_url=f"https://exofop.ipac.caltech.edu/tess/target.php?id={tic}",
                    reported_disposition=r["reported"],
                    period_d=r["period_d"],
                    period_err_d=r["period_err_d"],
                    epoch_bjd=r["epoch_bjd"],
                    epoch_err_d=r["epoch_err_d"],
                    duration_h=r["duration_h"],
                    depth_ppm=r["depth_ppm"],
                    raw_payload=r["raw"],
                    license="ExoFOP Open Access / Community Contribution",
                ),
            )
            if period > 0 and epoch > 2400000:
                await upsert_fit(
                    self.session,
                    EphemerisFit(
                        report_id=report.id,
                        signal_id=signal.id,
                        period_d=period,
                        period_err_d=r["period_err_d"],
                        epoch_bjd_tdb=epoch,
                        epoch_err_d=r["epoch_err_d"],
                        duration_h=r["duration_h"],
                        depth_ppm=r["depth_ppm"],
                        time_scale="TDB",
                        fit_model="exofop_ctoi_linear",
                        is_coherent=True,
                    ),
                )
            count += 1

        # PublishedSnapshot
        snapshot = PublishedSnapshot(
            source_name="exofop_ctoi",
            snapshot_utc=now,
            record_count=count,
            status="validated",
            validation_metrics={
                "ingested_ctois": count,
                "acknowledgement": EXOFOP_ACKNOWLEDGEMENT,
            },
            published_at=now,
        )
        self.session.add(snapshot)
        await self.session.commit()
        return count


EXOFOP_CTOI_URL = "https://exofop.ipac.caltech.edu/tess/download_ctoi.php?sort=ctoi&output=csv"


async def fetch_and_ingest_ctois(session: AsyncSession) -> int:
    """Downloads the full ExoFOP CTOI table and ingests every row."""
    import httpx

    import asyncio

    ingest = ExoFOPIngest(session)
    records: list[dict[str, Any]] = []
    body = ""
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        for attempt in range(3):
            resp = await client.get(EXOFOP_CTOI_URL)
            resp.raise_for_status()
            body = resp.text
            records = ingest.parse_ctoi_csv(body)
            if records:
                break
            await asyncio.sleep(30 * (attempt + 1))
    if not records:
        # ExoFOP occasionally answers 200 with an empty or HTML body; never treat that as "no CTOIs"
        raise ValueError(f"ExoFOP CTOI export parsed to 0 rows ({len(body)} bytes): {body[:200]!r}")
    return await ingest.ingest_ctois(records)
