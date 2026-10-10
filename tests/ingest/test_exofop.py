from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.ingest.exofop import ExoFOPIngest
from exoplanet_db.models.base import Base
from exoplanet_db.models.entities import Report, Signal, Target

# Subset of the columns in exofop.ipac.caltech.edu/tess/download_ctoi.php?output=csv
CTOI_CSV = """TIC ID,CTOI,Promoted to TOI,User Disposition,TFOPWG Disposition,TESS Mag,RA,Dec,Transit Epoch (BJD),Transit Epoch (BJD) err,Period (days),Period (days) Error,Depth ppm,Duration (hrs),Stellar Eff Temp (K),Stellar Radius (R_Sun),User,Notes
2050290974,2050290974.01,,PC,,10.2,346.183564,68.515204,2458755.0947,0.002,19.05118,0.0001,4413,0.266,5200,0.9,someone,
23456789,23456789.01,1234.01,PC,FP,9.8,10.5,-20.25,2458600.5,,3.5,,1200,2.1,,,observer2,eclipsing
23456789,23456789.02,,PC,,9.8,10.5,-20.25,,,,,,,,,observer2,single transit
"""


def test_parse_real_ctoi_columns():
    recs = ExoFOPIngest(None).parse_ctoi_csv(CTOI_CSV)
    assert [r["ctoi_id"] for r in recs] == [
        "CTOI 2050290974.01",
        "CTOI 23456789.01",
        "CTOI 23456789.02",
    ]
    a, b, c = recs
    assert a["period_d"] == 19.05118 and a["epoch_bjd"] == pytest.approx(2458755.0947)
    assert a["depth_ppm"] == 4413 and a["ra_deg"] == pytest.approx(346.183564)
    assert a["disposition"] == "planet_candidate" and a["user"] == "someone"
    assert b["disposition"] == "false_positive" and b["reported"] == "FP"  # TFOPWG wins
    assert b["promoted_toi"] == "1234.01"
    assert c["period_d"] is None and c["epoch_bjd"] is None  # kept, not invented


@pytest.mark.asyncio
async def test_ctoi_ingest_idempotent_and_keeps_unperiodic_rows():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    recs = ExoFOPIngest(None).parse_ctoi_csv(CTOI_CSV)
    for _ in range(2):
        async with factory() as session:
            assert await ExoFOPIngest(session).ingest_ctois(recs) == 3

    async with factory() as session:
        assert (await session.execute(select(func.count(Report.id)))).scalar() == 3
        assert (await session.execute(select(func.count(Signal.id)))).scalar() == 3
        target = (
            await session.execute(select(Target).where(Target.tic_id == "23456789"))
        ).scalar_one()
        assert (target.ra_deg, target.dec_deg) == (10.5, -20.25)
        single = (
            await session.execute(select(Signal).where(Signal.display_name == "CTOI 23456789.02"))
        ).scalar_one()
        assert single.primary_period_d == 0.0
        assert any("No period" in lim for lim in single.scientific_limits)
    await engine.dispose()
