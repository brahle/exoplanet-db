from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from exoplanet_db.models import (
    EphemerisFit,
    PublishedSnapshot,
    Report,
    Signal,
    Target,
)
from exoplanet_db.services.ephemeris import find_upcoming_transits
from exoplanet_db.services.observability import (
    calculate_observability,
    compute_priority_score,
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
            )
            query = query.where(search_cond)
            count_query = count_query.where(search_cond)

        if source:
            if source == "nasa_pscomppars":
                cond = (
                    Signal.current_disposition == "confirmed_planet"
                ) & ~Signal.scientific_limits.cast(String).ilike("%TOI%")
                query = query.where(cond)
                count_query = count_query.where(cond)
            elif source == "nasa_toi":
                cond = Signal.scientific_limits.cast(String).ilike("%TOI%")
                query = query.where(cond)
                count_query = count_query.where(cond)
            elif "eumemic" in source:
                cond = Signal.scientific_limits.cast(String).ilike("%eumemic%")
                query = query.where(cond)
                count_query = count_query.where(cond)
            elif "git" in source:
                cond = Signal.scientific_limits.cast(String).ilike(
                    "%checked catalogues%"
                ) | Signal.scientific_limits.cast(String).ilike("%Conditional diagnostic%")
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

    async def list_published_snapshots(self) -> list[PublishedSnapshot]:
        query = select(PublishedSnapshot).order_by(PublishedSnapshot.published_at.desc())
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
    ) -> list[dict[str, Any]]:
        query = (
            select(EphemerisFit)
            .where(EphemerisFit.is_coherent.is_(True))
            .options(selectinload(EphemerisFit.signal).selectinload(Signal.target))
        )
        res = await self.session.execute(query)
        fits = list(res.scalars().all())

        queue_entries: list[dict[str, Any]] = []

        for fit in fits:
            signal = fit.signal
            if not signal or not signal.target:
                continue
            target = signal.target
            if target.ra_deg is None or target.dec_deg is None:
                continue

            # Fast geometric pre-filter: maximum meridian altitude is 90 - |lat - dec|
            if (90.0 - abs(lat_deg - target.dec_deg)) < 15.0:
                continue

            try:
                transits = find_upcoming_transits(
                    fit=fit,
                    ra_deg=target.ra_deg,
                    dec_deg=target.dec_deg,
                    start_utc=start_time,
                    end_utc=end_time,
                    observer_lat_deg=lat_deg,
                    observer_lon_deg=lon_deg,
                )
            except Exception:
                continue

            for t in transits:
                obs = calculate_observability(
                    ra_deg=target.ra_deg,
                    dec_deg=target.dec_deg,
                    time_utc=t["utc_mid"],
                    lat_deg=lat_deg,
                    lon_deg=lon_deg,
                )

                score = compute_priority_score(
                    disposition=signal.current_disposition,
                    timing_uncertainty_min=t["timing_uncertainty_min"],
                    depth_ppm=signal.primary_depth_ppm or 1000.0,
                    duration_h=t["duration_h"],
                    is_observable=obs["is_observable"],
                    is_coherent=True,
                )

                if score >= min_priority:
                    queue_entries.append(
                        {
                            "signal_id": signal.id,
                            "target_id": target.id,
                            "target_name": target.primary_name,
                            "signal_name": signal.display_name,
                            "current_disposition": signal.current_disposition,
                            "transit_mid_utc": t["utc_mid"],
                            "transit_start_utc": t["transit_start_utc"],
                            "transit_end_utc": t["transit_end_utc"],
                            "duration_h": t["duration_h"],
                            "timing_uncertainty_min": round(t["timing_uncertainty_min"], 2),
                            "priority_score": score,
                            "target_altitude_deg": obs["target_altitude_deg"],
                            "target_airmass": obs["target_airmass"],
                            "sun_altitude_deg": obs["sun_altitude_deg"],
                            "moon_separation_deg": obs["moon_separation_deg"],
                            "is_observable": obs["is_observable"],
                            "fit_model": t.get("solution_name"),
                        }
                    )

        queue_entries.sort(key=lambda x: (-x["priority_score"], x["transit_mid_utc"]))
        return queue_entries[:limit]

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

        return {
            "total_signals": total_signals,
            "confirmed_planets": confirmed_planets,
            "planet_candidates": planet_candidates,
            "unclassified_signals": unclassified_signals,
            "candidates_total": candidates_total,
            "false_positives": false_positives,
            "total_targets": total_targets,
            "disposition_counts": disp_counts,
        }
