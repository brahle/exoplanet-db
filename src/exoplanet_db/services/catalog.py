from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from exoplanet_db.models import (
    EphemerisFit,
    PublishedSnapshot,
    Report,
    Signal,
    Target,
)
from exoplanet_db.services.system_physics import (
    planet_radius_earth,
    semi_major_axis_au,
    source_url,
    star_mass_from_payloads,
    star_radius_from_payloads,
    star_teff_from_payloads,
)


class CatalogService:
    """
    Shared Service Layer for Exoplanet DB.
    Single source of truth used by both the REST API and the MCP Server.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_systems(
        self,
        search: str | None = None,
        ra_min: float | None = None,
        ra_max: float | None = None,
        dec_min: float | None = None,
        dec_max: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Target], int]:
        query = select(Target).options(selectinload(Target.signals))
        count_query = select(func.count(Target.id))

        if search:
            search_pattern = f"%{search.strip()}%"
            filter_clause = (
                Target.primary_name.ilike(search_pattern)
                | Target.tic_id.ilike(search_pattern)
                | Target.gaia_dr3_id.ilike(search_pattern)
            )
            query = query.where(filter_clause)
            count_query = count_query.where(filter_clause)

        if ra_min is not None:
            query = query.where(Target.ra_deg >= ra_min)
            count_query = count_query.where(Target.ra_deg >= ra_min)
        if ra_max is not None:
            query = query.where(Target.ra_deg <= ra_max)
            count_query = count_query.where(Target.ra_deg <= ra_max)
        if dec_min is not None:
            query = query.where(Target.dec_deg >= dec_min)
            count_query = count_query.where(Target.dec_deg >= dec_min)
        if dec_max is not None:
            query = query.where(Target.dec_deg <= dec_max)
            count_query = count_query.where(Target.dec_deg <= dec_max)

        total_res = await self.session.execute(count_query)
        total = total_res.scalar() or 0

        query = query.order_by(Target.primary_name).limit(limit).offset(offset)
        res = await self.session.execute(query)
        items = list(res.scalars().all())
        return items, total

    async def get_system_by_id(self, target_id: str) -> Target | None:
        query = (
            select(Target)
            .where(Target.id == target_id)
            .options(selectinload(Target.signals), selectinload(Target.reports))
        )
        res = await self.session.execute(query)
        return res.scalars().first()

    async def list_candidates(
        self,
        search: str | None = None,
        source: str | None = None,
        disposition: str | None = None,
        min_period_d: float | None = None,
        max_period_d: float | None = None,
        min_depth_ppm: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Signal], int]:
        query = (
            select(Signal)
            .join(Signal.target)
            .options(
                joinedload(Signal.target),
            )
        )
        count_query = select(func.count(Signal.id)).join(Signal.target)

        if search:
            s_pat = f"%{search.strip()}%"
            search_cond = (
                Signal.display_name.ilike(s_pat)
                | Target.primary_name.ilike(s_pat)
                | Target.tic_id.ilike(s_pat)
                | Target.gaia_dr3_id.ilike(s_pat)
                # Identifiers from every source (TOI-1109.01, CTOI ..., eumemic keys), so a
                # signal merged under its confirmed-planet name is still found by its TOI name
                | Signal.id.in_(select(Report.signal_id).where(Report.source_key.ilike(s_pat)))
            )
            query = query.where(search_cond)
            count_query = count_query.where(search_cond)

        if source:
            # Filter on the report rows that actually came from the source
            cond = Signal.id.in_(select(Report.signal_id).where(Report.source_name == source))
            query = query.where(cond)
            count_query = count_query.where(cond)

        if disposition:
            query = query.where(Signal.current_disposition == disposition)
            count_query = count_query.where(Signal.current_disposition == disposition)
        if min_period_d is not None:
            query = query.where(Signal.primary_period_d >= min_period_d)
            count_query = count_query.where(Signal.primary_period_d >= min_period_d)
        if max_period_d is not None:
            query = query.where(Signal.primary_period_d <= max_period_d)
            count_query = count_query.where(Signal.primary_period_d <= max_period_d)
        if min_depth_ppm is not None:
            query = query.where(Signal.primary_depth_ppm >= min_depth_ppm)
            count_query = count_query.where(Signal.primary_depth_ppm >= min_depth_ppm)

        total_res = await self.session.execute(count_query)
        total = total_res.scalar() or 0

        query = query.order_by(Signal.primary_period_d).limit(limit).offset(offset)
        res = await self.session.execute(query)
        items = list(res.scalars().all())
        return items, total

    async def get_candidate_by_id(self, signal_id: str) -> Signal | None:
        query = (
            select(Signal)
            .where(Signal.id == signal_id)
            .options(
                selectinload(Signal.target),
                selectinload(Signal.reports),
                selectinload(Signal.ephemeris_fits),
                selectinload(Signal.harmonics),
            )
        )
        res = await self.session.execute(query)
        return res.scalars().first()

    async def get_candidate_detail(self, signal_id: str) -> dict[str, Any] | None:
        """Candidate plus its provenance, host-star physics and sibling signals (for system views)."""
        query = (
            select(Signal)
            .where(Signal.id == signal_id)
            .options(
                selectinload(Signal.target)
                .selectinload(Target.signals)
                .selectinload(Signal.reports),
                selectinload(Signal.target)
                .selectinload(Target.signals)
                .selectinload(Signal.ephemeris_fits),
            )
        )
        res = await self.session.execute(query)
        signal = res.scalars().first()
        if signal is None:
            return None
        target = signal.target
        siblings = sorted(target.signals, key=lambda s: (s.primary_period_d or 0.0, s.display_name))
        payloads = [r.raw_payload for s in siblings for r in s.reports if r.raw_payload]

        star_radius = target.radius_rsun or star_radius_from_payloads(payloads)
        star_mass = target.mass_msun or star_mass_from_payloads(payloads)
        star_teff = target.teff_k or star_teff_from_payloads(payloads)

        def physical(s: Signal) -> dict[str, Any]:
            raw = next((r.raw_payload for r in s.reports if r.raw_payload), None)
            radius, radius_basis = planet_radius_earth(raw, s.primary_depth_ppm, star_radius)
            a_au = semi_major_axis_au(s.primary_period_d, star_mass)
            a_basis = "kepler3_catalogue_mass" if a_au is not None else None
            if a_au is None and (s.primary_period_d or 0) > 0:
                a_au = semi_major_axis_au(s.primary_period_d, 1.0)
                a_basis = "kepler3_assumed_solar_mass"
            fit = self._preferred_fit(s)
            return {
                "id": s.id,
                "display_name": s.display_name,
                "current_disposition": s.current_disposition,
                "period_d": s.primary_period_d if (s.primary_period_d or 0) > 0 else None,
                "period_err_d": s.primary_period_err_d,
                "epoch_bjd": s.primary_epoch_bjd if (s.primary_epoch_bjd or 0) > 2400000 else None,
                "epoch_err_d": s.primary_epoch_err_d
                if s.primary_epoch_err_d is not None
                else (fit.epoch_err_d if fit else None),
                "duration_h": s.primary_duration_h,
                "duration_err_h": fit.duration_err_h if fit else None,
                "depth_ppm": s.primary_depth_ppm,
                "depth_err_ppm": fit.depth_err_ppm if fit else None,
                "radius_earth": radius,
                "radius_basis": radius_basis,
                "semi_major_axis_au": a_au,
                "semi_major_axis_basis": a_basis,
                "fit_model": fit.fit_model if fit else None,
            }

        reports = sorted(signal.reports, key=lambda r: r.retrieval_utc, reverse=True)
        sources = [
            {
                "source_name": r.source_name,
                "source_key": r.source_key,
                "url": source_url(
                    r.source_name, r.source_key, r.bibcode_or_url, r.raw_payload, target.tic_id
                ),
                "author_or_team": r.author_or_team,
                "license": r.license,
                "commit_sha": r.commit_sha,
                "retrieval_utc": r.retrieval_utc,
                "reported_disposition": r.reported_disposition,
            }
            for r in reports
        ]
        fit = self._preferred_fit(signal)
        primary = next(
            (src for r, src in zip(reports, sources) if fit is not None and r.id == fit.report_id),
            sources[0] if sources else None,
        )

        return {
            "signal": signal,
            "target": target,
            "planet": physical(signal),
            "sources": sources,
            "primary_source": primary,
            "star": {
                "radius_rsun": star_radius,
                "mass_msun": star_mass,
                "teff_k": star_teff,
                "tess_mag": target.tess_mag,
            },
            "system": [physical(s) for s in siblings],
        }

    @staticmethod
    def _preferred_fit(signal: Signal) -> EphemerisFit | None:
        fits = list(signal.ephemeris_fits or [])
        if not fits:
            return None
        for f in fits:
            if f.id == signal.preferred_ephemeris_fit_id:
                return f
        return fits[0]

    async def get_population(self) -> dict[str, list[Any]]:
        """Columnar period/depth/radius for every signal with a period (population context plots)."""
        sig_rows = (
            await self.session.execute(
                select(
                    Signal.id,
                    Signal.display_name,
                    Signal.current_disposition,
                    Signal.primary_period_d,
                    Signal.primary_depth_ppm,
                    Signal.target_id,
                    Target.radius_rsun,
                ).join(Signal.target)
            )
        ).all()
        catalogue_radius: dict[str, float] = {}
        rep_rows = (
            await self.session.execute(
                select(Report.signal_id, Report.target_id, Report.raw_payload).where(
                    Report.source_name.in_(["nasa_pscomppars", "nasa_toi"])
                )
            )
        ).all()
        star_radius_by_target: dict[str, float] = {}
        for sid, tid, raw in rep_rows:
            if not raw:
                continue
            r, basis = planet_radius_earth(raw, None, None)
            if sid and r is not None and basis == "catalogue":
                catalogue_radius.setdefault(sid, r)
            st = star_radius_from_payloads([raw])
            if st is not None:
                star_radius_by_target.setdefault(tid, st)

        cols: dict[str, list[Any]] = {
            "id": [],
            "name": [],
            "disposition": [],
            "period_d": [],
            "depth_ppm": [],
            "radius_earth": [],
        }
        for sid, name, disp, period, depth, tid, star_r in sig_rows:
            if not period or period <= 0:
                continue
            radius = catalogue_radius.get(sid)
            if radius is None:
                radius, _ = planet_radius_earth(
                    None, depth, star_r or star_radius_by_target.get(tid)
                )
            cols["id"].append(sid)
            cols["name"].append(name)
            cols["disposition"].append(disp)
            cols["period_d"].append(round(period, 6))
            cols["depth_ppm"].append(round(depth, 1) if depth else None)
            cols["radius_earth"].append(round(radius, 3) if radius else None)
        return cols

    async def list_published_snapshots(self, limit: int | None = None) -> list[PublishedSnapshot]:
        query = select(PublishedSnapshot).order_by(PublishedSnapshot.published_at.desc())
        if limit:
            query = query.limit(limit)
        res = await self.session.execute(query)
        return list(res.scalars().all())

    async def get_observation_queue(
        self,
        lat_deg: float,
        lon_deg: float,
        start_time: datetime,
        end_time: datetime,
        min_priority: float = 0.0,
        limit: int = 50,
        *,
        min_altitude_deg: float = 30.0,
        max_sun_altitude_deg: float = -18.0,
        min_moon_separation_deg: float = 30.0,
        max_timing_uncertainty_min: float = 120.0,
        observable_only: bool = True,
        height_m: float = 0.0,
    ) -> dict[str, Any]:
        """Observable transits in [start_time, end_time] for one site: {"items", "total"}."""
        import asyncio

        from exoplanet_db.services.observation_queue import QueueSettings, compute_queue

        # Stars that never rise above the limit at this latitude are skipped in SQL
        max_alt_cond = (90.0 - func.abs(lat_deg - Target.dec_deg)) >= min_altitude_deg
        rows = (
            await self.session.execute(
                select(
                    EphemerisFit.period_d,
                    EphemerisFit.period_err_d,
                    EphemerisFit.epoch_bjd_tdb,
                    EphemerisFit.epoch_err_d,
                    EphemerisFit.covariance_matrix,
                    EphemerisFit.duration_h,
                    EphemerisFit.depth_ppm,
                    EphemerisFit.fit_model,
                    Signal.id,
                    Signal.display_name,
                    Signal.current_disposition,
                    Signal.primary_duration_h,
                    Signal.primary_depth_ppm,
                    Target.id,
                    Target.primary_name,
                    Target.ra_deg,
                    Target.dec_deg,
                )
                .join(Signal, Signal.id == EphemerisFit.signal_id)
                .join(Target, Target.id == Signal.target_id)
                .where(
                    EphemerisFit.is_coherent.is_(True),
                    EphemerisFit.period_d > 0,
                    EphemerisFit.epoch_bjd_tdb > 2400000,
                    Signal.current_disposition != "false_positive",
                    ~((Target.ra_deg == 0.0) & (Target.dec_deg == 0.0)),
                    max_alt_cond,
                )
            )
        ).all()

        def covariance(mat: Any) -> float | None:
            if mat and len(mat) >= 2 and len(mat[0]) >= 2:
                return mat[0][1]
            return None

        records = [
            {
                "period_d": p,
                "period_err_d": pe,
                "epoch_bjd": e,
                "epoch_err_d": ee,
                "cov": covariance(cm),
                "duration_h": dur or sdur,
                "depth_ppm": dep or sdep,
                "fit_model": fm,
                "signal_id": sid,
                "signal_name": sname,
                "disposition": disp,
                "target_id": tid,
                "target_name": tname,
                "ra_deg": ra,
                "dec_deg": dec,
            }
            for (
                p,
                pe,
                e,
                ee,
                cm,
                dur,
                dep,
                fm,
                sid,
                sname,
                disp,
                sdur,
                sdep,
                tid,
                tname,
                ra,
                dec,
            ) in rows
        ]
        settings = QueueSettings(
            lat_deg=lat_deg,
            lon_deg=lon_deg,
            start_utc=start_time,
            end_utc=end_time,
            height_m=height_m,
            min_altitude_deg=min_altitude_deg,
            max_sun_altitude_deg=max_sun_altitude_deg,
            min_moon_separation_deg=min_moon_separation_deg,
            max_timing_uncertainty_min=max_timing_uncertainty_min,
            min_priority=min_priority,
            observable_only=observable_only,
            limit=limit,
        )
        # CPU-bound astropy/numpy work: keep the event loop (and /healthz) responsive
        items, total = await asyncio.to_thread(compute_queue, records, settings)
        return {"items": items, "total": total}

    async def get_source_status(self) -> list[dict[str, Any]]:
        """Current coverage of each source: one entry per source, not per ingest run.

        reports/signals count what is indexed now; commit_sha is the revision of the
        most recent snapshot that recorded one (git-pinned sources).
        """
        source_rows = (
            await self.session.execute(
                select(
                    Report.source_name,
                    func.count(func.distinct(Report.signal_id)),
                    func.count(Report.id),
                ).group_by(Report.source_name)
            )
        ).all()
        snapshots = (
            await self.session.execute(
                select(
                    PublishedSnapshot.source_name,
                    PublishedSnapshot.snapshot_utc,
                    PublishedSnapshot.commit_sha,
                ).order_by(PublishedSnapshot.snapshot_utc.desc())
            )
        ).all()
        last_ingested: dict[str, Any] = {}
        commit: dict[str, str] = {}
        for name, ts, sha in snapshots:
            last_ingested.setdefault(name, ts)
            if sha and sha != "HEAD":
                commit.setdefault(name, sha)
        return sorted(
            (
                {
                    "source_name": name,
                    "signals": n_signals,
                    "reports": n_reports,
                    "last_ingested_utc": last_ingested.get(name),
                    "commit_sha": commit.get(name),
                }
                for name, n_signals, n_reports in source_rows
            ),
            key=lambda r: -r["signals"],
        )

    async def cone_search(
        self, ra_deg: float, dec_deg: float, radius_arcsec: float
    ) -> list[dict[str, Any]]:
        """Every target within radius_arcsec, nearest first, with its signals and their sources.

        Unbounded (no page limit) and correct across RA 0/360 and near the poles.
        """
        from exoplanet_db.services.reconciliation import angular_distance_arcsec

        r_deg = radius_arcsec / 3600.0
        dec_lo, dec_hi = max(-90.0, dec_deg - r_deg), min(90.0, dec_deg + r_deg)
        cond = (Target.dec_deg >= dec_lo) & (Target.dec_deg <= dec_hi)
        if dec_hi < 89.9 and dec_lo > -89.9:
            cos_edge = min(math.cos(math.radians(dec_lo)), math.cos(math.radians(dec_hi)))
            ra_half = min(180.0, r_deg / max(cos_edge, 1e-6))
            ra_lo, ra_hi = ra_deg - ra_half, ra_deg + ra_half
            if ra_half < 180.0:
                if ra_lo < 0:
                    ra_cond = (Target.ra_deg >= ra_lo + 360.0) | (Target.ra_deg <= ra_hi)
                elif ra_hi >= 360.0:
                    ra_cond = (Target.ra_deg >= ra_lo) | (Target.ra_deg <= ra_hi - 360.0)
                else:
                    ra_cond = (Target.ra_deg >= ra_lo) & (Target.ra_deg <= ra_hi)
                cond = cond & ra_cond
        targets = (
            await self.session.execute(
                select(Target)
                .where(cond)
                .options(selectinload(Target.signals).selectinload(Signal.reports))
            )
        ).scalars()
        hits = []
        for t in targets:
            sep = angular_distance_arcsec(ra_deg, dec_deg, t.ra_deg, t.dec_deg)
            if sep <= radius_arcsec:
                hits.append({"target": t, "separation_arcsec": sep})
        hits.sort(key=lambda h: h["separation_arcsec"])
        return hits

    async def get_stats(self) -> dict[str, Any]:
        disp_query = select(Signal.current_disposition, func.count(Signal.id)).group_by(
            Signal.current_disposition
        )
        disp_res = await self.session.execute(disp_query)
        disp_counts = dict(disp_res.all())

        targets_query = select(func.count(Target.id))
        targets_res = await self.session.execute(targets_query)
        total_targets = targets_res.scalar() or 0

        total_signals = sum(disp_counts.values())
        confirmed_planets = disp_counts.get("confirmed_planet", 0) + disp_counts.get(
            "validated_planet", 0
        )
        planet_candidates = disp_counts.get("planet_candidate", 0)
        unclassified_signals = disp_counts.get("unclassified_transit_like_signal", 0)
        candidates_total = planet_candidates + unclassified_signals
        false_positives = disp_counts.get("false_positive", 0)

        sources = await self.get_source_status()

        in_nasa_table = (
            await self.session.execute(
                select(func.count(func.distinct(Report.signal_id))).where(
                    Report.source_name == "nasa_pscomppars"
                )
            )
        ).scalar() or 0
        tess_discoveries = (
            await self.session.execute(
                select(func.count(func.distinct(Report.signal_id))).where(
                    Report.source_name == "nasa_pscomppars",
                    Report.raw_payload["disc_facility"].as_string()
                    == "Transiting Exoplanet Survey Satellite (TESS)",
                )
            )
        ).scalar() or 0

        return {
            "sources": sources,
            "confirmed_in_nasa_archive": in_nasa_table,
            "confirmed_tess_discoveries": tess_discoveries,
            "total_signals": total_signals,
            "confirmed_planets": confirmed_planets,
            "planet_candidates": planet_candidates,
            "unclassified_signals": unclassified_signals,
            "candidates_total": candidates_total,
            "false_positives": false_positives,
            "total_targets": total_targets,
            "disposition_counts": disp_counts,
        }
