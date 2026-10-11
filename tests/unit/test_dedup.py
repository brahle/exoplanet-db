from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.models.base import Base
from exoplanet_db.models.entities import EphemerisFit, Report, Signal, Target
from exoplanet_db.services.dedup import dedupe_signals


def _signal(sid, name, period, disposition="planet_candidate", epoch=2459000.0):
    return Signal(
        id=sid,
        target_id="t",
        display_name=name,
        primary_period_d=period,
        primary_epoch_bjd=epoch,
        primary_duration_h=2.0,
        current_disposition=disposition,
        scientific_limits=[name],
    )


def _report(rid, sid, source, key, disposition="planet_candidate", raw=None):
    return Report(
        id=rid,
        target_id="t",
        signal_id=sid,
        source_name=source,
        source_key=key,
        retrieval_utc=datetime.now(UTC),
        author_or_team="x",
        reported_disposition=disposition,
        raw_payload=raw or {},
    )


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Target(id="t", primary_name="TIC 1", tic_id="1", ra_deg=1.0, dec_deg=1.0))
        await s.commit()
    yield f
    await engine.dispose()


@pytest.mark.asyncio
async def test_promoted_ctoi_merges_into_its_toi(factory):
    async with factory() as s:
        # Periods differ by 0.4%: only the explicit identifier links them
        s.add_all(
            [
                _signal("toi", "TOI-100.01", 3.0, "false_positive"),
                _signal("ctoi", "CTOI 1.01", 3.012),
            ]
        )
        s.add_all(
            [
                _report("r1", "toi", "nasa_toi", "TOI-100.01", "false_positive"),
                _report(
                    "r2", "ctoi", "exofop_ctoi", "CTOI 1.01", raw={"Promoted to TOI": "100.01"}
                ),
            ]
        )
        s.add(
            EphemerisFit(report_id="r2", signal_id="ctoi", period_d=3.012, epoch_bjd_tdb=2459000.0)
        )
        await s.commit()
        assert (await dedupe_signals(s))["merged_by_identifier"] == 1

    async with factory() as s:
        sigs = (await s.execute(select(Signal))).scalars().all()
        assert [(x.id, x.display_name, x.current_disposition) for x in sigs] == [
            ("toi", "TOI-100.01", "false_positive")
        ]
        assert set((await s.execute(select(Report.signal_id))).scalars()) == {"toi"}
        assert set((await s.execute(select(EphemerisFit.signal_id))).scalars()) == {"toi"}
        assert "CTOI 1.01" in sigs[0].scientific_limits
        assert (await dedupe_signals(s))["merged_by_identifier"] == 0  # idempotent


@pytest.mark.asyncio
async def test_same_period_cross_source_merges_and_nasa_name_wins(factory):
    async with factory() as s:
        s.add_all(
            [
                _signal("eu", "TIC 1 (5.000 d)", 5.0),
                _signal("cp", "HOST b", 5.0004, "confirmed_planet"),
            ]
        )
        s.add_all(
            [
                _report("r1", "eu", "eumemic/exoplanets", "TIC1_P5.000"),
                _report("r2", "cp", "nasa_pscomppars", "HOST b", "confirmed_planet"),
            ]
        )
        await s.commit()
        assert (await dedupe_signals(s))["merged_by_ephemeris"] == 1
    async with factory() as s:
        sig = (await s.execute(select(Signal))).scalar_one()
        assert (sig.id, sig.display_name, sig.current_disposition) == (
            "cp",
            "HOST b",
            "confirmed_planet",
        )


@pytest.mark.asyncio
async def test_same_catalogue_entries_stay_apart(factory):
    async with factory() as s:
        s.add_all([_signal("a", "TOI-5.01", 2.0), _signal("b", "TOI-5.02", 2.0001)])
        s.add_all(
            [_report("r1", "a", "nasa_toi", "TOI-5.01"), _report("r2", "b", "nasa_toi", "TOI-5.02")]
        )
        await s.commit()
        res = await dedupe_signals(s)
        assert res["merged_by_ephemeris"] == 0 and res["kept_apart_same_catalogue"] == 1
    async with factory() as s:
        assert len((await s.execute(select(Signal))).scalars().all()) == 2
