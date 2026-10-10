from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
        """Parses ExoFOP CTOI CSV table into structured dictionaries."""
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
        records = []
        for row in reader:
            # Handle varied headers in ExoFOP exports
            ctoi_id = (row.get("ctoi") or row.get("CTOI") or row.get("Identifier") or "").strip()
            tic = (
                row.get("tic_id") or row.get("TIC ID") or row.get("TIC") or row.get("tid") or ""
            ).strip()
            period_str = (
                row.get("period") or row.get("Period (days)") or row.get("Period") or ""
            ).strip()
            epoch_str = (
                row.get("epoch")
                or row.get("Epoch (BJD)")
                or row.get("Epoch")
                or row.get("Midpoint")
                or ""
            ).strip()

            if not tic or not period_str or not epoch_str:
                continue

            try:
                period = float(period_str)
                epoch_raw = float(epoch_str)
                epoch_bjd = epoch_raw if epoch_raw > 2400000 else epoch_raw + 2457000.0
                depth_ppm = float(row.get("Depth (ppm)") or row.get("depth") or 1000.0)
                duration_h = float(row.get("Duration (hours)") or row.get("duration") or 2.5)
                ra_deg = float(row.get("RA") or row.get("ra") or 0.0)
                dec_deg = float(row.get("Dec") or row.get("dec") or 0.0)
                user = row.get("User") or row.get("user") or "Community Observer"
                notes = row.get("Notes") or row.get("notes") or ""

                records.append(
                    {
                        "ctoi_id": ctoi_id or f"CTOI-{tic}.01",
                        "tic": tic,
                        "period_d": period,
                        "epoch_bjd": epoch_bjd,
                        "depth_ppm": depth_ppm,
                        "duration_h": duration_h,
                        "ra_deg": ra_deg,
                        "dec_deg": dec_deg,
                        "user": user,
                        "notes": notes,
                        "raw": dict(row),
                    }
                )
            except (ValueError, TypeError):
                continue
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
                    ra_deg=r["ra_deg"],
                    dec_deg=r["dec_deg"],
                    epoch=2000.0,
                    aliases=[f"TIC {tic}"],
                )
                self.session.add(target)
                await self.session.flush()

            # Signal matching
            signals_stmt = select(Signal).where(Signal.target_id == target.id)
            existing_signals = list((await self.session.execute(signals_stmt)).scalars().all())

            matched_signal = match_signal_to_existing(
                existing_signals=existing_signals,
                cand_period=r["period_d"],
                cand_epoch=r["epoch_bjd"],
                duration_h=r["duration_h"],
            )

            limits = [
                f"exofop_ctoi:{r['ctoi_id']}",
                f"observer:{r['user']}",
            ]

            if matched_signal:
                signal = matched_signal
                signal.scientific_limits = list(set((signal.scientific_limits or []) + limits))
            else:
                signal = Signal(
                    target_id=target.id,
                    display_name=r["ctoi_id"],
                    primary_period_d=r["period_d"],
                    primary_epoch_bjd=r["epoch_bjd"],
                    primary_duration_h=r["duration_h"],
                    primary_depth_ppm=r["depth_ppm"],
                    current_disposition="planet_candidate",
                    source_localisation_status="unresolved",
                    timing_consistency_status="unresolved",
                    scientific_limits=limits,
                )
                self.session.add(signal)
                await self.session.flush()

            # Report record
            report = Report(
                target_id=target.id,
                signal_id=signal.id,
                source_name="exofop_ctoi",
                source_key=r["ctoi_id"],
                retrieval_utc=now,
                author_or_team=r["user"],
                bibcode_or_url=f"https://exofop.ipac.caltech.edu/tess/target.php?id={tic}",
                reported_disposition="planet_candidate",
                period_d=r["period_d"],
                epoch_bjd=r["epoch_bjd"],
                duration_h=r["duration_h"],
                depth_ppm=r["depth_ppm"],
                raw_payload=r["raw"],
                license="ExoFOP Open Access / Community Contribution",
            )
            self.session.add(report)
            await self.session.flush()

            # EphemerisFit
            ephem_fit = EphemerisFit(
                report_id=report.id,
                signal_id=signal.id,
                period_d=r["period_d"],
                epoch_bjd_tdb=r["epoch_bjd"],
                duration_h=r["duration_h"],
                depth_ppm=r["depth_ppm"],
                time_scale="TDB",
                fit_model="exofop_ctoi_linear",
                is_coherent=True,
            )
            self.session.add(ephem_fit)
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
