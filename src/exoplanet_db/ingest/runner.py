from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exoplanet_db.ingest.common import signal_for_existing_report, upsert_fit, upsert_report
from exoplanet_db.models import (
    EphemerisFit,
    PublishedSnapshot,
    Report,
    Signal,
    Target,
)
from exoplanet_db.services.reconciliation import check_signal_match, same_period_signal


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
            tic = str(row.get("tic_id") or "").replace("TIC", "").strip() or None
            gaia = (
                # gaia_id is a DR2 designation; only a DR3 id belongs in gaia_dr3_id
                str(row.get("gaia_dr3_id") or "").replace("Gaia DR3", "").strip() or None
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
            # pscomppars publishes pl_trandep in percent; store ppm like every other source.
            depth = (
                float(row["pl_trandep"]) * 10_000.0 if row.get("pl_trandep") is not None else None
            )

            # 1. Target matching
            # Match on TIC only when the row has one, so TIC-less hosts (pulsars, microlensing)
            # are never merged with each other.
            target_cond = Target.primary_name == hostname
            if tic:
                target_cond = target_cond | (Target.tic_id == tic)
            target_stmt = select(Target).where(target_cond)
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
            eff_period = period if (period is not None and period > 0) else 0.0
            eff_epoch = epoch if epoch is not None else 0.0

            sig_stmt = select(Signal).where(Signal.target_id == target.id)
            sig_res = await self.session.execute(sig_stmt)
            existing_signals = sig_res.scalars().all()

            matched_signal: Signal | None = None
            # A different pl_name is a different confirmed planet: never merge into it.
            candidates_to_match = [
                es
                for es in existing_signals
                if es.display_name == pl_name or es.current_disposition != "confirmed_planet"
            ]
            for es in candidates_to_match:
                if es.display_name == pl_name:
                    matched_signal = es
                    break
                if eff_period > 0 and es.primary_period_d > 0:
                    match_result = check_signal_match(
                        period1_d=es.primary_period_d,
                        period1_err=es.primary_period_err_d,
                        epoch1_bjd=es.primary_epoch_bjd,
                        epoch1_err=es.primary_epoch_err_d,
                        period2_d=eff_period,
                        period2_err=period_err,
                        epoch2_bjd=eff_epoch,
                        epoch2_err=epoch_err,
                        duration1_h=es.primary_duration_h,
                        duration2_h=duration,
                    )
                    if match_result["match_type"] == "identical_signal":
                        matched_signal = es
                        break

            if matched_signal is None:
                matched_signal = same_period_signal(candidates_to_match, eff_period)

            limits = ["NASA confirmed exoplanet composite row"]
            if eff_period == 0.0:
                limits.append("Orbital period unconstrained (e.g. direct imaging or partial arc)")

            if not matched_signal:
                matched_signal = Signal(
                    id=str(uuid.uuid4()),
                    target_id=target.id,
                    display_name=pl_name,
                    primary_period_d=eff_period,
                    primary_period_err_d=period_err,
                    primary_epoch_bjd=eff_epoch,
                    primary_epoch_err_d=epoch_err,
                    primary_duration_h=duration,
                    primary_depth_ppm=depth,
                    current_disposition="confirmed_planet",
                    source_localisation_status="supported",
                    timing_consistency_status="supported" if eff_period > 0 else "unresolved",
                    scientific_limits=limits,
                )
                self.session.add(matched_signal)
                await self.session.flush()
            elif matched_signal.current_disposition != "confirmed_planet":
                # A NASA confirmed planet outranks any candidate classification of the same signal
                matched_signal.current_disposition = "confirmed_planet"
                matched_signal.display_name = pl_name
                matched_signal.scientific_limits = list(
                    dict.fromkeys((matched_signal.scientific_limits or []) + limits)
                )

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
            report = await upsert_report(self.session, report)

            # 4. Ephemeris fit (only if epoch and period are both present from single composite)
            if eff_epoch > 2400000 and eff_period > 0:
                fit = EphemerisFit(
                    id=str(uuid.uuid4()),
                    report_id=report.id,
                    signal_id=matched_signal.id,
                    period_d=eff_period,
                    period_err_d=period_err,
                    epoch_bjd_tdb=eff_epoch,
                    epoch_err_d=epoch_err,
                    duration_h=duration,
                    depth_ppm=depth,
                    fit_model="composite_published_ephemeris",
                    is_coherent=True,
                )
                fit = await upsert_fit(self.session, fit)
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

    async def ingest_nasa_toi(
        self,
        raw_tois: list[dict[str, Any]],
        snapshot_time: datetime | None = None,
    ) -> PublishedSnapshot:
        now = snapshot_time or datetime.now(UTC)
        record_count = 0

        disp_map = {
            "PC": "planet_candidate",
            "APC": "planet_candidate",
            "CP": "confirmed_planet",
            "KP": "confirmed_planet",
            "FP": "false_positive",
            "FA": "false_positive",
        }

        for row in raw_tois:
            toi_num = str(row.get("toi", "")).strip()
            toidisplay = str(row.get("toidisplay") or (f"TOI-{toi_num}" if toi_num else "")).strip()
            tid = str(row.get("tid", "")).replace("TIC", "").strip()
            if not tid or not toidisplay:
                continue

            hostname = f"TIC {tid}"
            ra = float(row.get("ra", 0.0)) if row.get("ra") is not None else 0.0
            dec = float(row.get("dec", 0.0)) if row.get("dec") is not None else 0.0
            period = float(row.get("pl_orbper", 0.0)) if row.get("pl_orbper") is not None else 0.0
            period_err = (
                float(row.get("pl_orbpererr1", 0.0))
                if row.get("pl_orbpererr1") is not None
                else None
            )
            epoch = float(row.get("pl_tranmid", 0.0)) if row.get("pl_tranmid") is not None else 0.0
            duration = (
                float(row.get("pl_trandurh", 0.0)) if row.get("pl_trandurh") is not None else None
            )
            depth = float(row.get("pl_trandep", 0.0)) if row.get("pl_trandep") is not None else None
            tfopwg = str(row.get("tfopwg_disp", "")).strip().upper()
            disposition = disp_map.get(tfopwg, "unclassified_transit_like_signal")

            # 1. Target matching
            target_stmt = select(Target).where(
                (Target.tic_id == tid) | (Target.primary_name == hostname)
            )
            res = await self.session.execute(target_stmt)
            target = res.scalars().first()

            if not target:
                target = Target(
                    id=str(uuid.uuid4()),
                    primary_name=hostname,
                    tic_id=tid,
                    ra_deg=ra,
                    dec_deg=dec,
                    v_mag=float(row.get("st_tmag")) if row.get("st_tmag") is not None else None,
                    teff_k=float(row.get("st_teff")) if row.get("st_teff") is not None else None,
                    radius_rsun=float(row.get("st_rad")) if row.get("st_rad") is not None else None,
                    aliases=[hostname, f"TIC {tid}"],
                )
                self.session.add(target)
                await self.session.flush()

            # 2. Signal matching
            sig_stmt = select(Signal).where(Signal.target_id == target.id)
            sig_res = await self.session.execute(sig_stmt)
            existing_signals = sig_res.scalars().all()

            # Exact designation first, so an ephemeris match on another signal can't win
            matched_signal: Signal | None = await signal_for_existing_report(
                self.session, "nasa_toi", toidisplay
            ) or next((es for es in existing_signals if es.display_name == toidisplay), None)
            for es in [] if matched_signal else existing_signals:
                if period > 0 and es.primary_period_d > 0:
                    match_res = check_signal_match(
                        period1_d=es.primary_period_d,
                        period1_err=es.primary_period_err_d,
                        epoch1_bjd=es.primary_epoch_bjd,
                        epoch1_err=es.primary_epoch_err_d,
                        period2_d=period,
                        period2_err=period_err,
                        epoch2_bjd=epoch,
                        epoch2_err=None,
                        duration1_h=es.primary_duration_h,
                        duration2_h=duration,
                    )
                    if match_res["match_type"] == "identical_signal":
                        matched_signal = es
                        break

            if matched_signal is None:
                matched_signal = same_period_signal(existing_signals, period)

            limits = [
                f"NASA Exoplanet Archive TESS Object of Interest ({tfopwg or 'TOI'})",
                "TFOPWG disposition classification pipeline",
            ]

            if not matched_signal:
                matched_signal = Signal(
                    id=str(uuid.uuid4()),
                    target_id=target.id,
                    display_name=toidisplay,
                    primary_period_d=period,
                    primary_period_err_d=period_err,
                    primary_epoch_bjd=epoch,
                    primary_duration_h=duration,
                    primary_depth_ppm=depth,
                    current_disposition=disposition,
                    source_localisation_status="supported",
                    timing_consistency_status="supported" if period > 0 else "unresolved",
                    scientific_limits=limits,
                )
                self.session.add(matched_signal)
                await self.session.flush()
            else:
                matched_signal.scientific_limits = list(
                    dict.fromkeys((matched_signal.scientific_limits or []) + limits)
                )
                # TFOPWG outranks community classifications, never a NASA confirmation
                if matched_signal.current_disposition != "confirmed_planet" and tfopwg in disp_map:
                    matched_signal.current_disposition = disposition
                # Naming precedence: NASA planet name > TOI > community (CTOI, eumemic, dossier)
                has_nasa_name = (
                    await self.session.execute(
                        select(Report.id).where(
                            Report.signal_id == matched_signal.id,
                            Report.source_name == "nasa_pscomppars",
                        )
                    )
                ).first()
                if not has_nasa_name:
                    matched_signal.display_name = toidisplay

            # 3. Report
            report = Report(
                id=str(uuid.uuid4()),
                target_id=target.id,
                signal_id=matched_signal.id,
                source_name="nasa_toi",
                source_key=toidisplay,
                retrieval_utc=now,
                author_or_team="NASA Exoplanet Archive TOI",
                reported_disposition=disposition,
                raw_payload=row,
            )
            report = await upsert_report(self.session, report)

            # 4. Ephemeris fit if coherent
            if period > 0 and epoch > 2400000:
                fit = EphemerisFit(
                    id=str(uuid.uuid4()),
                    report_id=report.id,
                    signal_id=matched_signal.id,
                    period_d=period,
                    period_err_d=period_err,
                    epoch_bjd_tdb=epoch,
                    duration_h=duration,
                    is_coherent=True,
                    fit_model="transit_linear",
                )
                fit = await upsert_fit(self.session, fit)
                if not matched_signal.preferred_ephemeris_fit_id:
                    matched_signal.preferred_ephemeris_fit_id = fit.id

            record_count += 1

        snapshot = PublishedSnapshot(
            id=str(uuid.uuid4()),
            source_name="nasa_toi",
            snapshot_utc=now,
            record_count=record_count,
            status="published",
            published_at=now,
        )
        self.session.add(snapshot)
        await self.session.commit()
        return snapshot
