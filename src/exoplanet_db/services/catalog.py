from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from exoplanet_db.models import (
    EphemerisFit,
    PublishedSnapshot,
    Signal,
    Target,
)
from exoplanet_db.services.ephemeris import find_upcoming_transits
from exoplanet_db.services.observability import (
    calculate_observability,
    compute_priority_score,
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
        disposition: str | None = None,
        min_period_d: float | None = None,
        max_period_d: float | None = None,
        min_depth_ppm: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Signal], int]:
        query = select(Signal).options(
            joinedload(Signal.target),
        )
        count_query = select(func.count(Signal.id))

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
