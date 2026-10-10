from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.ingest.exofop import ExoFOPIngest
from exoplanet_db.models import Base, EphemerisFit, Report, Signal, Target


@pytest.mark.asyncio
async def test_exofop_ctoi_ingest():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    sample_ctoi_csv = """CTOI,TIC ID,Period (days),Epoch (BJD),Depth (ppm),Duration (hours),RA,Dec,User,Notes
CTOI-1234.01,23456789,4.5678,2458600.5,1200,3.2,120.5,30.2,Observer Alice,High SNR transit
CTOI-5678.01,98765432,1.2345,1600.0,2500,1.8,60.0,-45.0,Observer Bob,V-shaped candidate
"""

    async with session_factory() as session:
        adapter = ExoFOPIngest(session)
        records = adapter.parse_ctoi_csv(sample_ctoi_csv)
        assert len(records) == 2
        assert records[0]["ctoi_id"] == "CTOI-1234.01"
        assert records[0]["tic"] == "23456789"
        assert abs(records[0]["epoch_bjd"] - 2458600.5) < 1e-4

        # Check BTJD conversion in row 2: 1600.0 -> 2458600.0
        assert abs(records[1]["epoch_bjd"] - 2458600.0) < 1e-4

        count = await adapter.ingest_ctois(records)
        assert count == 2

    async with session_factory() as session:
        # Check Target
        t_res = await session.execute(select(Target).where(Target.tic_id == "23456789"))
        target = t_res.scalars().first()
        assert target is not None
        assert target.primary_name == "TIC 23456789"

        # Check Signal
        sig_res = await session.execute(select(Signal).where(Signal.target_id == target.id))
        sig = sig_res.scalars().first()
        assert sig is not None
        assert sig.display_name == "CTOI-1234.01"
        assert sig.current_disposition == "planet_candidate"
        assert any("exofop_ctoi:CTOI-1234.01" in lim for lim in sig.scientific_limits)

        # Check Report
        rep_res = await session.execute(select(Report).where(Report.signal_id == sig.id))
        rep = rep_res.scalars().first()
        assert rep is not None
        assert rep.source_name == "exofop_ctoi"
        assert rep.author_or_team == "Observer Alice"

        # Check EphemerisFit
        fit_res = await session.execute(
            select(EphemerisFit).where(EphemerisFit.signal_id == sig.id)
        )
        fit = fit_res.scalars().first()
        assert fit is not None
        assert fit.fit_model == "exofop_ctoi_linear"

    await engine.dispose()
