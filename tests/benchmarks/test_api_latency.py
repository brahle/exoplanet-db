from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.ingest.runner import IngestionRunner
from exoplanet_db.models import Base
from exoplanet_db.services.catalog import CatalogService


@pytest.mark.asyncio
async def test_api_latency_p95_under_50ms():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Populate 50 synthetic targets
    async with session_factory() as session:
        runner = IngestionRunner(session)
        mock_planets = [
            {
                "pl_name": f"Target-{i} b",
                "hostname": f"Target-{i}",
                "tic_id": str(100000 + i),
                "ra": (i * 7.2) % 360.0,
                "dec": (i * 3.5) % 180.0 - 90.0,
                "pl_orbper": 1.0 + (i * 0.2),
                "pl_tranmid": 2458000.0 + i,
                "pl_trandur": 2.5,
                "pl_trandep": 500.0 + (i * 10),
            }
            for i in range(50)
        ]
        await runner.ingest_nasa_composite(mock_planets)

    # Warmup query compilation and engine
    async with session_factory() as s:
        svc = CatalogService(s)
        await svc.list_candidates(min_period_d=1.0, max_period_d=10.0, limit=20)

    # Concurrently execute 50 requests
    latencies: list[float] = []
    sem = asyncio.Semaphore(10)

    async def make_query(idx: int):
        async with sem:
            async with session_factory() as s:
                svc = CatalogService(s)
                t0 = time.perf_counter()
                _ = await svc.list_candidates(min_period_d=1.0, max_period_d=10.0, limit=20)
                t1 = time.perf_counter()
                latencies.append((t1 - t0) * 1000.0)  # in ms

    tasks = [make_query(i) for i in range(50)]
    await asyncio.gather(*tasks)

    p95 = float(np.percentile(latencies, 95))
    assert p95 < 50.0, f"Expected p95 < 50ms, got {p95:.2f}ms"
