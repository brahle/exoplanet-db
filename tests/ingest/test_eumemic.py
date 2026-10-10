from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.ingest.eumemic import EumemicIngest
from exoplanet_db.models import Base, EphemerisFit, Report, Signal


@pytest.mark.asyncio
async def test_eumemic_ingest_and_tier_mapping():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    sample_csv = """tic,candidate_id,period,t0,tier,status,depth,duration,ra,dec
12345678,TIC-12345678.01,3.14159,2458500.123,A,planet_candidate,1500,2.1,180.5,25.2
87654321,TIC-87654321.01,0.98765,1500.456,C,candidate,800,1.5,45.0,-12.0
99999999,TIC-99999999.01,10.5,2459000.0,N,false_positive,5000,4.0,200.0,0.0
"""

    async with session_factory() as session:
        adapter = EumemicIngest(session)
        parsed = adapter.parse_candidates_csv(sample_csv)
        assert len(parsed) == 3

        # Test T0 normalization
        # First row: raw BJD 2458500.123 -> btjd = 1500.123
        assert abs(parsed[0]["t0_btjd"] - 1500.123) < 1e-4
        assert abs(parsed[0]["t0_bjd_tdb"] - 2458500.123) < 1e-4

        # Second row: raw BTJD 1500.456 -> bjd = 2458500.456
        assert abs(parsed[1]["t0_btjd"] - 1500.456) < 1e-4
        assert abs(parsed[1]["t0_bjd_tdb"] - 2458500.456) < 1e-4

        ingested_count = await adapter.ingest_candidates(
            parsed,
            commit_sha="a1b2c3d4e5f6",
            source_file="results_public/candidates.csv",
        )
        assert ingested_count == 3

    async with session_factory() as session:
        # Check Tier A mapped to planet_candidate
        sig_a_res = await session.execute(
            select(Signal).where(Signal.display_name == "TIC-12345678.01")
        )
        sig_a = sig_a_res.scalars().first()
        assert sig_a is not None
        assert sig_a.current_disposition == "planet_candidate"
        assert "eumemic_tier:A" in sig_a.scientific_limits

        # Check Tier C mapped to unclassified_transit_like_signal
        sig_c_res = await session.execute(
            select(Signal).where(Signal.display_name == "TIC-87654321.01")
        )
        sig_c = sig_c_res.scalars().first()
        assert sig_c is not None
        assert sig_c.current_disposition == "unclassified_transit_like_signal"
        assert "eumemic_tier:C" in sig_c.scientific_limits

        # Check Tier N mapped to false_positive
        sig_n_res = await session.execute(
            select(Signal).where(Signal.display_name == "TIC-99999999.01")
        )
        sig_n = sig_n_res.scalars().first()
        assert sig_n is not None
        assert sig_n.current_disposition == "false_positive"
        assert "eumemic_tier:N" in sig_n.scientific_limits

        # Check CC BY 4.0 license attribution in Report
        rep_res = await session.execute(select(Report).where(Report.signal_id == sig_a.id))
        rep = rep_res.scalars().first()
        assert rep is not None
        assert rep.license == "CC BY 4.0"
        assert rep.source_name == "eumemic/exoplanets"

        # Check EphemerisFit
        fit_res = await session.execute(
            select(EphemerisFit).where(EphemerisFit.signal_id == sig_a.id)
        )
        fit = fit_res.scalars().first()
        assert fit is not None
        assert fit.fit_model == "eumemic_tier_A"

    await engine.dispose()
