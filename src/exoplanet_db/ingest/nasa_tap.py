from __future__ import annotations

from typing import Any

import httpx

NASA_TAP_BASE_URL = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"

NASA_ACKNOWLEDGEMENT = (
    "This research has made use of the NASA Exoplanet Archive, which is operated by the "
    "California Institute of Technology, under contract with the National Aeronautics and "
    "Space Administration under the Exoplanet Exploration Program."
)


class NasaTapClient:
    """
    Client for NASA Exoplanet Archive TAP service with WAF-safe ADQL query construction.
    Avoids cone queries (CONTAINS/POINT/CIRCLE) and literal '1=1' which trigger WAF 403.
    """

    def __init__(self, base_url: str = NASA_TAP_BASE_URL, timeout_seconds: float = 60.0):
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    def build_safe_query(
        self,
        table: str,
        columns: list[str],
        where_clause: str | None = None,
        limit: int | None = None,
    ) -> str:
        col_str = ", ".join(columns)
        # Avoid literal '1=1'; use valid field bounds if no where clause provided
        where = where_clause if where_clause else "ra >= 0.0 AND ra <= 360.0"
        query = f"SELECT {col_str} FROM {table} WHERE {where}"
        if limit is not None and limit > 0:
            query = f"SELECT TOP {limit} {col_str} FROM {table} WHERE {where}"
        return query

    async def execute_query(self, query: str) -> list[dict[str, Any]]:
        params = {
            "query": query,
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                return []
            return data

    async def fetch_composite_planets(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Fetch composite planet parameters (pscomppars: 1 row per confirmed planet)."""
        cols = [
            "pl_name",
            "hostname",
            "tic_id",
            "gaia_dr3_id",
            "sy_snum",
            "sy_pnum",
            "discoverymethod",
            "disc_year",
            "pl_orbper",
            "pl_orbpererr1",
            "pl_orbpererr2",
            "pl_tranmid",
            "pl_tranmiderr1",
            "pl_tranmiderr2",
            "pl_trandur",
            "pl_trandurerr1",
            "pl_trandurerr2",
            "pl_trandep",
            "pl_trandeperr1",
            "pl_trandeperr2",
            "pl_rade",
            "pl_bmasse",
            "ra",
            "dec",
            "sy_pmra",
            "sy_pmdec",
            "sy_vmag",
            "sy_tmag",
            "st_teff",
            "st_rad",
            "st_mass",
        ]
        query = self.build_safe_query(
            table="pscomppars",
            columns=cols,
            where_clause="ra >= 0.0 AND ra <= 360.0",
            limit=limit,
        )
        return await self.execute_query(query)

    async def fetch_ps_reference_reports(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Fetch individual publication reports (ps: 1 row per planet per reference, ~38k rows)."""
        cols = [
            "pl_name",
            "hostname",
            "tic_id",
            "gaia_id",
            "disc_refname",
            "disc_pubdate",
            "pl_refname",
            "pl_orbper",
            "pl_orbpererr1",
            "pl_orbpererr2",
            "pl_tranmid",
            "pl_tranmiderr1",
            "pl_tranmiderr2",
            "pl_trandur",
            "pl_trandep",
            "ra",
            "dec",
        ]
        query = self.build_safe_query(
            table="ps",
            columns=cols,
            where_clause="default_flag = 1",
            limit=limit,
        )
        return await self.execute_query(query)

    async def fetch_toi_candidates(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Fetch TESS Object of Interest candidates (toi)."""
        cols = [
            "toi",
            "tid",
            "tfopwg_disp",
            "pl_orbper",
            "pl_orbpererr1",
            "pl_trandurh",
            "pl_trandep",
            "ra",
            "dec",
            "pmra",
            "pmdec",
        ]
        query = self.build_safe_query(
            table="toi",
            columns=cols,
            where_clause="ra >= 0.0 AND ra <= 360.0",
            limit=limit,
        )
        return await self.execute_query(query)


async def run_ingest(limit: int | None = None) -> None:
    import os
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from exoplanet_db.ingest.runner import IngestionRunner

    db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///exoplanet_db.sqlite")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    client = NasaTapClient()
    print("Fetching composite planets from NASA TAP...")
    planets = await client.fetch_composite_planets(limit=limit)
    print(f"Fetched {len(planets)} planets from NASA TAP. Ingesting into database...")
    async with session_factory() as session:
        runner = IngestionRunner(session)
        snapshot = await runner.ingest_nasa_composite(planets)
        print(f"Successfully ingested {snapshot.record_count} records. Snapshot ID: {snapshot.id}")
    await engine.dispose()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_ingest())

