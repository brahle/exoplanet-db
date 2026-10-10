from __future__ import annotations

import uuid
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
from exoplanet_db.services.reconciliation import check_signal_match


class IngestionRunner:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def ingest_nasa_composite(
        self,
        raw_planets: list[dict[str, Any]],
        snapshot_time: datetime | None = None,
    ) -> PublishedSnapshot:
        now = snapshot_time or datetime.now(UTC)
        record_count = 0

        for row in raw_planets:
            pl_name = str(row.get("pl_name", "")).strip()
            hostname = str(row.get("hostname", "")).strip()
            if not pl_name or not hostname:
                continue

            ra = float(row.get("ra", 0.0))
            dec = float(row.get("dec", 0.0))
            tic = str(row.get("tic_id", "")).replace("TIC", "").strip() or None
            gaia = (
                str(row.get("gaia_id", "")).replace("Gaia DR3", "").replace("Gaia DR2", "").strip()
                or None
            )
            period = float(row.get("pl_orbper", 0.0)) if row.get("pl_orbper") is not None else None
            period_err = (
                float(row.get("pl_orbpererr1", 0.0))
                if row.get("pl_orbpererr1") is not None
                else None
            )
            epoch = float(row.get("pl_tranmid", 0.0)) if row.get("pl_tranmid") is not None else None
            epoch_err = (
                float(row.get("pl_tranmiderr1", 0.0))
                if row.get("pl_tranmiderr1") is not None
                else None
            )
            duration = (
                float(row.get("pl_trandur", 0.0)) if row.get("pl_trandur") is not None else None
            )
            depth = float(row.get("pl_trandep", 0.0)) if row.get("pl_trandep") is not None else None

            # 1. Target matching
            target_stmt = select(Target).where(
                (Target.primary_name == hostname) | (Target.tic_id == tic)
            )
            res = await self.session.execute(target_stmt)
            target = res.scalars().first()

            if not target:
                target = Target(
                    id=str(uuid.uuid4()),
                    primary_name=hostname,
                    tic_id=tic,
                    gaia_dr3_id=gaia,
                    ra_deg=ra,
                    dec_deg=dec,
                    v_mag=float(row.get("sy_vmag")) if row.get("sy_vmag") is not None else None,
                    teff_k=float(row.get("st_teff")) if row.get("st_teff") is not None else None,
                    radius_rsun=float(row.get("st_rad")) if row.get("st_rad") is not None else None,
                    mass_msun=float(row.get("st_mass")) if row.get("st_mass") is not None else None,
                    aliases=[hostname] + ([f"TIC {tic}"] if tic else []),
                )
                self.session.add(target)
                await self.session.flush()

            # 2. Signal creation / matching
            if period and period > 0:
                sig_stmt = select(Signal).where(Signal.target_id == target.id)
                sig_res = await self.session.execute(sig_stmt)
                existing_signals = sig_res.scalars().all()

                matched_signal: Signal | None = None
                for es in existing_signals:
                    match_result = check_signal_match(
                        period1_d=es.primary_period_d,
                        period1_err=es.primary_period_err_d,
                        epoch1_bjd=es.primary_epoch_bjd,
                        epoch1_err=es.primary_epoch_err_d,
                        period2_d=period,
                        period2_err=period_err,
                        epoch2_bjd=epoch or 0.0,
                        epoch2_err=epoch_err,
                        duration1_h=es.primary_duration_h,
                        duration2_h=duration,
                    )
                    if match_result["match_type"] == "identical_signal":
                        matched_signal = es
                        break

                if not matched_signal:
                    matched_signal = Signal(
                        id=str(uuid.uuid4()),
                        target_id=target.id,
                        display_name=pl_name,
                        primary_period_d=period,
                        primary_period_err_d=period_err,
                        primary_epoch_bjd=epoch or 0.0,
                        primary_epoch_err_d=epoch_err,
                        primary_duration_h=duration,
                        primary_depth_ppm=depth,
                        current_disposition="confirmed_planet",
                        source_localisation_status="supported",
                        timing_consistency_status="supported",
                        scientific_limits=["NASA confirmed exoplanet composite row"],
                    )
                    self.session.add(matched_signal)
                    await self.session.flush()

                # 3. Report record
                report = Report(
                    id=str(uuid.uuid4()),
                    target_id=target.id,
                    signal_id=matched_signal.id,
                    source_name="nasa_pscomppars",
                    source_key=pl_name,
                    retrieval_utc=now,
                    author_or_team="NASA Exoplanet Archive",
                    license="Public Domain / US Government",
                    reported_disposition="confirmed_planet",
                    period_d=period,
                    period_err_d=period_err,
                    epoch_bjd=epoch,
                    epoch_err_d=epoch_err,
                    duration_h=duration,
                    depth_ppm=depth,
                    raw_payload=row,
                )
                self.session.add(report)
                await self.session.flush()

                # 4. Ephemeris fit (only if epoch and period are both present from single composite)
                if epoch and epoch > 0:
                    fit = EphemerisFit(
                        id=str(uuid.uuid4()),
                        report_id=report.id,
                        signal_id=matched_signal.id,
                        period_d=period,
                        period_err_d=period_err,
                        epoch_bjd_tdb=epoch,
                        epoch_err_d=epoch_err,
                        duration_h=duration,
                        depth_ppm=depth,
                        fit_model="composite_published_ephemeris",
                        is_coherent=True,
                    )
                    self.session.add(fit)
                    await self.session.flush()
                    if not matched_signal.preferred_ephemeris_fit_id:
                        matched_signal.preferred_ephemeris_fit_id = fit.id

                record_count += 1

        # 5. Create published snapshot
        snapshot = PublishedSnapshot(
            id=str(uuid.uuid4()),
            source_name="nasa_pscomppars",
            snapshot_utc=now,
            record_count=record_count,
            status="validated",
            validation_metrics={
                "processed_rows": len(raw_planets),
                "imported_signals": record_count,
            },
            published_at=now,
        )
        self.session.add(snapshot)
        await self.session.commit()
        return snapshot
