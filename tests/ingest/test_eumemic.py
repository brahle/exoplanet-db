from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.ingest.eumemic import PUBLISHED_TIERS, EumemicIngest, _earlier_tier
from exoplanet_db.models.base import Base
from exoplanet_db.models.entities import Report, Signal

# Header and rows in the shape of results_public/candidates_allsky.csv
ALLSKY_CSV = """TIC,P_d,P_err,T0_BJD,Rp_Re,b,T14_h,depth_ppm,MES,n_transits,Tmag,Teff,Rstar,dist_pc,ruwe,set,tier
137499615,0.991911,4e-06,2458842.66166,1.56,0.53,1.2,493,12.7,177,11.63,4052,0.65,90.1,1.15,SNR>=9,A
200000001,3.5,1e-05,2459000.5,2.0,0.2,2.0,800,10,30,11.0,5000,0.9,100,1.0,SNR>=9,N
200000002,7.25,2e-05,2459001.5,1.1,0.4,1.8,150,8,20,12.0,4500,0.7,80,1.0,SNR 7.5-9,L1
200000003,1.25,1e-05,2459002.5,3.0,0.1,1.5,900,9,40,10.0,5200,0.9,60,1.0,neighbour<15",likely EB
200000004,9.0,1e-05,2459003.5,1.9,0.3,2.2,400,9,12,11.0,4800,0.8,70,1.0,SNR>=9,C
"""


def test_parse_keeps_real_values_and_stable_keys():
    rows = EumemicIngest(None).parse_candidates_csv(ALLSKY_CSV)
    assert [r["tier"] for r in rows] == ["A", "N", "L1", "LIKELY EB", "C"]
    first = rows[0]
    assert first["source_key"] == "TIC137499615_P0.992"
    assert first["depth_ppm"] == 493.0 and first["duration_h"] == 1.2
    assert first["t0_bjd_tdb"] == pytest.approx(2458842.66166)
    published = [r for r in rows if r["tier"] in PUBLISHED_TIERS]
    assert [r["tic"] for r in published] == ["137499615", "200000001", "200000002"]


def test_parse_does_not_invent_missing_depth_or_duration():
    rows = EumemicIngest(None).parse_candidates_csv("TIC,P_d,T0_BJD,tier\n1,2.0,2459000.0,A\n")
    assert rows[0]["depth_ppm"] is None and rows[0]["duration_h"] is None


def test_earlier_tier_lookup_matches_on_tic_and_period():
    tiers = [
        {"TIC": "354944123", "P_d": "18.908204", "tier": "A"},
        {"TIC": "1", "P_d": "2.0", "tier": "C"},
    ]
    assert _earlier_tier(tiers, "354944123", 18.90820) == "A"
    assert _earlier_tier(tiers, "354944123", 9.45) is None
    assert _earlier_tier(tiers, "1", 2.0) == "C"


@pytest.mark.asyncio
async def test_ingest_published_candidates_is_idempotent():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sha = "f3dd74abec5879661d76bcfcae2ce6744ba18aad"

    rows = [
        r
        for r in EumemicIngest(None).parse_candidates_csv(ALLSKY_CSV)
        if r["tier"] in PUBLISHED_TIERS
    ]
    for _ in range(2):
        async with factory() as session:
            count = await EumemicIngest(session).ingest_candidates(
                rows, commit_sha=sha, source_file="results_public/candidates_allsky.csv"
            )
            assert count == 3

    async with factory() as session:
        assert (await session.execute(select(func.count(Report.id)))).scalar() == 3
        dispositions = (await session.execute(select(Signal.current_disposition))).scalars().all()
        assert set(dispositions) == {"planet_candidate"}  # tier N and L1 are candidates too
        rep = (
            await session.execute(select(Report).where(Report.source_key == "TIC137499615_P0.992"))
        ).scalar_one()
        assert rep.bibcode_or_url == (
            f"https://github.com/eumemic/exoplanets/blob/{sha}/results_public/candidates_allsky.csv"
        )
        assert rep.license == "CC BY 4.0"
    await engine.dispose()
