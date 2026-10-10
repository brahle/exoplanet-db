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
