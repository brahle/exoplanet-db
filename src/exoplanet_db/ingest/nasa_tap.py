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
            "disc_facility",
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
        """Fetch TESS Object of Interest candidates (toi, ~8148 candidate entries)."""
        cols = [
            "toi",
            "toidisplay",
            "tid",
            "tfopwg_disp",
            "pl_orbper",
            "pl_orbpererr1",
            "pl_tranmid",
            "pl_trandurh",
            "pl_trandep",
            "ra",
            "dec",
            "st_pmra",
            "st_pmdec",
            "st_teff",
            "st_rad",
            "st_tmag",
        ]
        query = self.build_safe_query(
            table="toi",
            columns=cols,
            where_clause="ra >= 0.0 AND ra <= 360.0",
            limit=limit,
        )
        return await self.execute_query(query)


SOURCES = ("composite", "toi", "eumemic", "ctoi", "dossiers")
SOURCE_GROUPS = {"all": SOURCES, "nasa": ("composite", "toi")}


async def run_ingest(source: str = "all", limit: int | None = None) -> list[str]:
    """Ingests the selected sources, then fills TIC coordinates/stellar data.

    Every source runs even if an earlier one fails; returns the names of failed sources.
    Order matters: NASA tables first, so community candidates reconcile against them.
    """
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from exoplanet_db.ingest.common import enrich_tic_targets
    from exoplanet_db.ingest.eumemic import fetch_and_ingest_eumemic
    from exoplanet_db.ingest.exofop import fetch_and_ingest_ctois
    from exoplanet_db.ingest.git_dossier import fetch_and_ingest_github_dossiers
    from exoplanet_db.ingest.runner import IngestionRunner

    db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///exoplanet_db.sqlite")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    client = NasaTapClient()
    selected = SOURCE_GROUPS.get(source, (source,))

    async def composite(session):
        rows = await client.fetch_composite_planets(limit=limit)
        snap = await IngestionRunner(session).ingest_nasa_composite(rows)
        return f"{len(rows)} rows fetched, {snap.record_count} ingested"

    async def toi(session):
        rows = await client.fetch_toi_candidates(limit=limit)
        snap = await IngestionRunner(session).ingest_nasa_toi(rows)
        return f"{len(rows)} rows fetched, {snap.record_count} ingested"

    async def eumemic(session):
        return f"{await fetch_and_ingest_eumemic(session)} published candidates"

    async def ctoi(session):
        return f"{await fetch_and_ingest_ctois(session)} CTOIs"

    async def dossiers(session):
        return f"{await fetch_and_ingest_github_dossiers(session)} dossiers"

    steps = {
        "composite": composite,
        "toi": toi,
        "eumemic": eumemic,
        "ctoi": ctoi,
        "dossiers": dossiers,
    }
    failed: list[str] = []
    for name in selected:
        print(f"[{name}] ingesting...", flush=True)
        async with session_factory() as session:
            try:
                print(f"[{name}] {await steps[name](session)}", flush=True)
            except Exception as exc:  # keep going; report at the end
                await session.rollback()
                failed.append(name)
                print(f"[{name}] FAILED: {exc!r}", flush=True)

    async with session_factory() as session:
        try:
            print(f"[tic] enriched {await enrich_tic_targets(session)} targets", flush=True)
        except Exception as exc:
            failed.append("tic")
            print(f"[tic] FAILED: {exc!r}", flush=True)

    await engine.dispose()
    return failed


def main() -> None:
    import argparse
    import asyncio
    import sys

    parser = argparse.ArgumentParser(description="Exoplanet DB Ingestion Runner")
    parser.add_argument("--source", default="all", choices=["all", "nasa", *SOURCES])
    parser.add_argument("--limit", type=int, default=None, help="Row limit for testing")
    args = parser.parse_args()

    failures = asyncio.run(run_ingest(source=args.source, limit=args.limit))
    if failures:
        print(f"Ingest finished with failures: {', '.join(failures)}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
