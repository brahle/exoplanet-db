from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from exoplanet_db.models import (
    PublishedSnapshot,
    Signal,
    Target,
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
            selectinload(Signal.target),
            selectinload(Signal.ephemeris_fits),
            selectinload(Signal.harmonics),
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
