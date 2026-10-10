from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.ingest.runner import IngestionRunner
from exoplanet_db.models.base import Base
from exoplanet_db.models.entities import Signal, Target


def _row(pl_name: str, hostname: str, period: float) -> dict:
    return {
        "pl_name": pl_name,
        "hostname": hostname,
        "tic_id": None,
        "ra": 10.0,
        "dec": 5.0,
        "pl_orbper": period,
    }


@pytest.mark.asyncio
async def test_hosts_without_tic_do_not_merge():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        await IngestionRunner(session).ingest_nasa_composite(
            [
                _row("PSR B1257+12 b", "PSR B1257+12", 25.262),
                _row("OGLE-2005-BLG-071L b", "OGLE-2005-BLG-071L", 3500.0),
                _row("OGLE-2005-BLG-071L c", "OGLE-2005-BLG-071L", 7000.0),
            ]
        )

    async with factory() as session:
        assert (await session.execute(select(func.count(Target.id)))).scalar() == 2
        counts = dict(
            (
                await session.execute(
                    select(Target.primary_name, func.count(Signal.id))
                    .join(Signal, Signal.target_id == Target.id)
                    .group_by(Target.primary_name)
                )
            ).all()
        )
        assert counts == {"PSR B1257+12": 1, "OGLE-2005-BLG-071L": 2}
    await engine.dispose()


@pytest.mark.asyncio
async def test_prune_orphan_signals_removes_only_unreported():
    from exoplanet_db.ingest.common import prune_orphan_signals

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await IngestionRunner(session).ingest_nasa_composite([_row("A b", "A", 3.0)])
    async with factory() as session:
        target_id = (await session.execute(select(Target.id))).scalar_one()
        session.add(
            Signal(
                target_id=target_id,
                display_name="stale",
                primary_period_d=9.0,
                primary_epoch_bjd=0.0,
                current_disposition="confirmed_planet",
                scientific_limits=[],
            )
        )
        await session.commit()
        assert await prune_orphan_signals(session) == ["stale"]
        names = (await session.execute(select(Signal.display_name))).scalars().all()
        assert names == ["A b"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_toi_name_wins_over_community_name_but_not_nasa_name():
    from exoplanet_db.ingest.exofop import ExoFOPIngest

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ctoi = {
        "ctoi_id": "CTOI 50309953.01",
        "tic": "50309953",
        "period_d": 4.0,
        "period_err_d": None,
        "epoch_bjd": 2459000.0,
        "epoch_err_d": None,
        "depth_ppm": 900.0,
        "duration_h": 2.0,
        "ra_deg": 10.0,
        "dec_deg": 5.0,
        "teff_k": None,
        "radius_rsun": None,
        "tess_mag": None,
        "disposition": "planet_candidate",
        "reported": "PC",
        "promoted_toi": "1109.01",
        "user": "someone",
        "notes": "",
        "raw": {},
    }
    toi = {
        "toi": "1109.01",
        "toidisplay": "TOI-1109.01",
        "tid": 50309953,
        "tfopwg_disp": "FP",
        "pl_orbper": 4.0,
        "pl_tranmid": 2459000.0,
        "pl_trandurh": 2.0,
        "pl_trandep": 900.0,
        "ra": 10.0,
        "dec": 5.0,
    }
    async with factory() as session:
        await ExoFOPIngest(session).ingest_ctois([ctoi])
    for _ in range(2):
        async with factory() as session:
            await IngestionRunner(session).ingest_nasa_toi([toi])
    async with factory() as session:
        sigs = (
            await session.execute(select(Signal.display_name, Signal.current_disposition))
        ).all()
        assert sigs == [("TOI-1109.01", "false_positive")]
    await engine.dispose()
