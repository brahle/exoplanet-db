from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.ingest.nasa_tap import NasaTapClient
from exoplanet_db.ingest.runner import IngestionRunner
from exoplanet_db.models import Base


def test_nasa_tap_query_builder():
    client = NasaTapClient()
    query = client.build_safe_query(
        table="pscomppars",
        columns=["pl_name", "ra", "dec"],
        where_clause="ra >= 10.0 AND ra <= 20.0",
        limit=50,
    )
    assert "1=1" not in query
    assert "POINT" not in query
    assert "CONTAINS" not in query
    assert "SELECT TOP 50 pl_name, ra, dec FROM pscomppars WHERE ra >= 10.0 AND ra <= 20.0" == query


@pytest.mark.asyncio
async def test_nasa_composite_ingestion():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        runner = IngestionRunner(session)
        mock_planets = [
            {
                "pl_name": "WASP-12 b",
                "hostname": "WASP-12",
                "tic_id": "38846515",
                "gaia_id": "Gaia DR3 341251",
                "ra": 97.6397,
                "dec": 29.6722,
                "pl_orbper": 1.09142,
                "pl_orbpererr1": 0.00001,
                "pl_tranmid": 2454508.976,
                "pl_tranmiderr1": 0.001,
                "pl_trandur": 3.0,
                "pl_trandep": 14000.0,
                "sy_vmag": 11.57,
                "st_teff": 6300.0,
            },
            {
                "pl_name": "WASP-12 c",  # Second planet around same star
                "hostname": "WASP-12",
                "tic_id": "38846515",
                "gaia_id": "Gaia DR3 341251",
                "ra": 97.6397,
                "dec": 29.6722,
                "pl_orbper": 8.5201,
                "pl_orbpererr1": 0.0001,
                "pl_tranmid": 2454515.120,
                "pl_tranmiderr1": 0.005,
                "pl_trandur": 4.5,
                "pl_trandep": 2100.0,
                "sy_vmag": 11.57,
                "st_teff": 6300.0,
            },
        ]

        snapshot = await runner.ingest_nasa_composite(mock_planets)

        assert snapshot.status == "validated"
        assert snapshot.record_count == 2
        assert snapshot.source_name == "nasa_pscomppars"
