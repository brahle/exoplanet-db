from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.ingest.eumemic import PUBLISHED_TIERS, EumemicIngest, _earlier_tier
from exoplanet_db.models.base import Base
from exoplanet_db.models.entities import Report, Signal, Target

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


@pytest.mark.asyncio
async def test_discarded_rows_are_ingested_with_their_verdict():
    from exoplanet_db.ingest.eumemic import attach_vetting_rows, parse_vetting_rows

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    csv_text = ALLSKY_CSV.replace(",tier\n", ",tier,c_reason\n", 1).replace(",C\n", ",C,grazing\n")
    rows = EumemicIngest(None).parse_candidates_csv(csv_text)

    async with factory() as session:
        assert await EumemicIngest(session).ingest_candidates(rows, commit_sha="abc") == 5
    async with factory() as session:
        by_name = {
            s.display_name: s for s in (await session.execute(select(Signal))).scalars().all()
        }
        eb = by_name["TIC 200000003 (1.250 d)"]
        assert eb.current_disposition == "false_positive"
        assert any("likely eclipsing binary" in x for x in eb.scientific_limits)
        c = by_name["TIC 200000004 (9.000 d)"]
        assert c.current_disposition == "unclassified_transit_like_signal"
        assert any("tier C" in x and "(grazing)" in x for x in c.scientific_limits)

    # Re-tiered in a later release: the old verdict is replaced, not appended
    retiered = [dict(r, tier="B") if r["tic"] == "200000004" else r for r in rows]
    async with factory() as session:
        await EumemicIngest(session).ingest_candidates(retiered, commit_sha="def")
    async with factory() as session:
        c = (
            await session.execute(
                select(Signal).where(Signal.display_name == "TIC 200000004 (9.000 d)")
            )
        ).scalar_one()
        assert c.current_disposition == "planet_candidate"
        assert [x for x in c.scientific_limits if x.startswith("eumemic")] == [
            "eumemic/exoplanets tier B candidate (results_public/candidates.csv)"
        ]

    # Vetting evidence attaches to existing signals only, never creates them
    vet_csv = (
        "tic,rank,period,t0,duration_h,depth_ppm,fails,MES,snr,known_status\n"
        "200000003,1,1.25,2002.5,1.5,900,significant secondary; odd-even transit differences,9,10,new\n"
        "999999999,1,4.0,2000.0,2.0,500,,9,10,new\n"
    )
    vet = parse_vetting_rows(vet_csv, "vetted_signals_tce")
    assert vet[0]["epoch_bjd"] == 2459002.5
    async with factory() as session:
        before = (await session.execute(select(func.count(Signal.id)))).scalar()
        res = await attach_vetting_rows(session, vet, "abc", ["vetted_signals_tce.csv.gz"])
        assert res == {"rows": 2, "attached": 1, "with_failed_tests": 1}
        assert (await session.execute(select(func.count(Signal.id)))).scalar() == before
        rep = (
            await session.execute(
                select(Report).where(Report.source_name == "eumemic/exoplanets:vetting")
            )
        ).scalar_one()
        assert rep.raw_payload["fails"] == ["significant secondary", "odd-even transit differences"]
        assert rep.source_key == "vetted_signals_tce:TIC200000003_P1.2500"
    await engine.dispose()


@pytest.mark.asyncio
async def test_discard_does_not_override_a_toi_disposition():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        sig = Signal(
            target_id="t",
            display_name="TOI-9.01",
            primary_period_d=1.25,
            primary_epoch_bjd=2459002.5,
            current_disposition="planet_candidate",
            scientific_limits=[],
        )
        session.add(
            Target(id="t", primary_name="TIC 200000003", tic_id="200000003", ra_deg=1, dec_deg=1)
        )
        session.add(sig)
        await session.flush()
        session.add(
            Report(
                target_id="t",
                signal_id=sig.id,
                source_name="nasa_toi",
                source_key="TOI-9.01",
                retrieval_utc=datetime.now(UTC),
                author_or_team="x",
                reported_disposition="planet_candidate",
                raw_payload={},
            )
        )
        await session.commit()
    rows = [
        r for r in EumemicIngest(None).parse_candidates_csv(ALLSKY_CSV) if r["tic"] == "200000003"
    ]
    async with factory() as session:
        await EumemicIngest(session).ingest_candidates(rows, commit_sha="abc")
    async with factory() as session:
        s = (await session.execute(select(Signal))).scalar_one()
        assert s.current_disposition == "planet_candidate"
        assert any("likely eclipsing binary" in x for x in s.scientific_limits)
    await engine.dispose()
