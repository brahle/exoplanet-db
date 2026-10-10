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

EUMEMIC_DATA_ATTRIBUTION = (
    "Results, tables, and figures from eumemic/exoplanets licensed under CC BY 4.0 "
    "(Creative Commons Attribution 4.0 International). Source: https://github.com/eumemic/exoplanets"
)


class EumemicIngest:
    """
    Community adapter for eumemic/exoplanets catalog.
    Ingests candidate tables, ephemerides, predictions, and vetting tiers (A, B, N, L1, C).
    Enforces CC BY 4.0 attribution and normalises BJD_TDB to BTJD (BJD - 2457000).
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    def parse_candidates_csv(self, csv_content: str) -> list[dict[str, Any]]:
        """Parses eumemic candidates.csv into typed dictionaries."""
        reader = csv.DictReader(io.StringIO(csv_content.strip()))
        rows = []
        for r in reader:
            # Handle possible key naming variations across candidates.csv and candidates_allsky.csv
            tic = (r.get("tic") or r.get("TIC") or r.get("tic_id") or r.get("tid") or "").strip()
            period_str = r.get("period") or r.get("P_d") or r.get("pl_orbper") or r.get("period_d")
            t0_str = r.get("t0") or r.get("T0_BJD") or r.get("t0_bjd") or r.get("pl_tranmid") or r.get("epoch")
            tier = (r.get("tier") or r.get("Tier") or "A").strip()
            status = r.get("status") or r.get("disposition") or "planet_candidate"
            depth_str = r.get("depth") or r.get("depth_ppm") or r.get("pl_trandep")
            dur_str = r.get("duration") or r.get("T14_h") or r.get("duration_h") or r.get("pl_trandur")
            ra_str = r.get("ra") or r.get("ra_deg")
            dec_str = r.get("dec") or r.get("dec_deg")
            p_err_val = r.get("P_err") or r.get("period_err")
            t0_err_val = r.get("t0_err") or r.get("T0_err")
            candidate_label = (r.get("label") or r.get("candidate_id") or f"TIC {tic}.01").strip()

            if not tic or not period_str or not t0_str:
                continue

            try:
                period = float(period_str)
                t0_raw = float(t0_str)
                # Normalise to BTJD and BJD_TDB
                if t0_raw > 2400000:
                    bjd_tdb = t0_raw
                    btjd = t0_raw - 2457000.0
                else:
                    btjd = t0_raw
                    bjd_tdb = t0_raw + 2457000.0

                rows.append(
                    {
                        "tic": tic,
                        "candidate_id": candidate_label,
                        "period_d": period,
                        "period_err_d": float(p_err_val) if p_err_val else None,
                        "t0_btjd": btjd,
                        "t0_bjd_tdb": bjd_tdb,
                        "t0_err_d": float(t0_err_val) if t0_err_val else None,
                        "depth_ppm": float(depth_str) if depth_str else 1000.0,
                        "duration_h": float(dur_str) if dur_str else 2.5,
                        "tier": tier.upper(),
                        "status": status,
                        "ra_deg": float(ra_str) if ra_str else None,
                        "dec_deg": float(dec_str) if dec_str else None,
                        "raw": dict(r),
                    }
                )
            except (ValueError, TypeError):
                continue
        return rows

    async def ingest_candidates(
        self,
        records: list[dict[str, Any]],
        commit_sha: str = "HEAD",
        source_file: str = "results_public/candidates.csv",
    ) -> int:
        """Ingests candidates parsed from eumemic/exoplanets."""
        now = datetime.now(UTC)
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
                    ra_deg=r["ra_deg"] or 0.0,
                    dec_deg=r["dec_deg"] or 0.0,
                    epoch=2000.0,
                    aliases=[f"TIC {tic}"],
                )
                self.session.add(target)
                await self.session.flush()

            # Signal reconciliation
            signals_stmt = select(Signal).where(Signal.target_id == target.id)
            existing_signals = list((await self.session.execute(signals_stmt)).scalars().all())

            matched_signal = match_signal_to_existing(
                existing_signals=existing_signals,
                cand_period=r["period_d"],
                cand_epoch=r["t0_bjd_tdb"],
                period_err=r["period_err_d"],
                epoch_err=r["t0_err_d"],
                duration_h=r["duration_h"],
            )

            # Map tier to disposition
            tier_disposition = "planet_candidate"
            if r["tier"] in ("A", "B"):
                tier_disposition = "planet_candidate"
            elif r["tier"] in ("L1", "C"):
                tier_disposition = "unclassified_transit_like_signal"
            elif r["tier"] == "N":
                tier_disposition = "false_positive"

            limits = [
                f"eumemic_tier:{r['tier']}",
                f"source_file:{source_file}",
            ]

            if matched_signal:
                signal = matched_signal
                # Update limits
                signal.scientific_limits = list(set((signal.scientific_limits or []) + limits))
            else:
                signal = Signal(
                    target_id=target.id,
                    display_name=r["candidate_id"],
                    primary_period_d=r["period_d"],
                    primary_period_err_d=r["period_err_d"],
                    primary_epoch_bjd=r["t0_bjd_tdb"],
                    primary_duration_h=r["duration_h"],
                    primary_depth_ppm=r["depth_ppm"],
                    current_disposition=tier_disposition,
                    source_localisation_status="unresolved",
                    timing_consistency_status="supported",
                    scientific_limits=limits,
                )
                self.session.add(signal)
                await self.session.flush()

            # Report
            report = Report(
                target_id=target.id,
                signal_id=signal.id,
                source_name="eumemic/exoplanets",
                source_key=f"{r['candidate_id']}@{commit_sha[:8]}",
                retrieval_utc=now,
                commit_sha=commit_sha,
                author_or_team="eumemic",
                bibcode_or_url=f"https://github.com/eumemic/exoplanets/blob/{commit_sha}/{source_file}",
                reported_disposition=tier_disposition,
                period_d=r["period_d"],
                period_err_d=r["period_err_d"],
                epoch_bjd=r["t0_bjd_tdb"],
                epoch_err_d=r["t0_err_d"],
                duration_h=r["duration_h"],
                depth_ppm=r["depth_ppm"],
                raw_payload=r["raw"],
                license="CC BY 4.0",
            )
            self.session.add(report)
            await self.session.flush()

            # EphemerisFit
            ephem_fit = EphemerisFit(
                report_id=report.id,
                signal_id=signal.id,
                period_d=r["period_d"],
                period_err_d=r["period_err_d"],
                epoch_bjd_tdb=r["t0_bjd_tdb"],
                epoch_err_d=r["t0_err_d"],
                duration_h=r["duration_h"],
                depth_ppm=r["depth_ppm"],
                time_scale="TDB",
                fit_model=f"eumemic_tier_{r['tier']}",
                is_coherent=True,
            )
            self.session.add(ephem_fit)
            count += 1

        # Snapshot
        snapshot = PublishedSnapshot(
            source_name="eumemic/exoplanets",
            snapshot_utc=now,
            commit_sha=commit_sha,
            record_count=count,
            status="validated",
            validation_metrics={
                "source_file": source_file,
                "ingested_candidates": count,
                "license": "CC BY 4.0",
            },
            published_at=now,
        )
        self.session.add(snapshot)
        await self.session.commit()
        return count


async def fetch_and_ingest_eumemic(session: AsyncSession) -> int:
    """Fetches candidate catalogs directly from eumemic/exoplanets on GitHub and ingests them."""
    import httpx

    ingest = EumemicIngest(session)
    total_ingested = 0
    urls = [
        (
            "https://raw.githubusercontent.com/eumemic/exoplanets/main/results_public/candidates.csv",
            "results_public/candidates.csv",
        ),
        (
            "https://raw.githubusercontent.com/eumemic/exoplanets/main/results_public/candidates_allsky.csv",
            "results_public/candidates_allsky.csv",
        ),
    ]

    async with httpx.AsyncClient(timeout=45.0) as client:
        for url, source_file in urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    records = ingest.parse_candidates_csv(resp.text)
                    count = await ingest.ingest_candidates(
                        records, commit_sha="eumemic-main", source_file=source_file
                    )
                    total_ingested += count
                else:
                    logger.warning(f"HTTP {resp.status_code} fetching {url}")
            except Exception as e:
                logger.warning(f"Failed to fetch and ingest {url}: {e}")

    return total_ingested

