from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import exoplanet_db.api.app as api_module
import exoplanet_db.mcp.server as mcp_module
from exoplanet_db.models import Base
from exoplanet_db.models.entities import PublishedSnapshot, Report, Signal, Target


def _add_signal(session, i, ra, dec, period, source="nasa_toi"):
    tid, sid = f"t{i}", f"s{i}"
    session.add(Target(id=tid, primary_name=f"TIC {i}", tic_id=str(i), ra_deg=ra, dec_deg=dec))
    session.add(
        Signal(
            id=sid,
            target_id=tid,
            display_name=f"TOI-{i}.01",
            primary_period_d=period,
            primary_epoch_bjd=2459000.0,
            current_disposition="planet_candidate",
            scientific_limits=[],
        )
    )
    session.add(
        Report(
            target_id=tid,
            signal_id=sid,
            source_name=source,
            source_key=f"TOI-{i}.01",
            retrieval_utc=datetime.now(UTC),
            author_or_team="x",
            reported_disposition="planet_candidate",
            raw_payload={},
        )
    )


@pytest.fixture(params=["db", "http"])
async def novelty(request, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        # Crowded field: 60 decoy stars at 10.00-10.01 deg, the real match last (farthest)
        for i in range(60):
            _add_signal(s, 1000 + i, 10.0 + i * 1e-5, 20.0, 7.0 + i)
        _add_signal(s, 2000, 10.0 + 70 / 3600 / 0.94, 20.0, 3.0)  # ~70" away, P = 3.0
        # Across RA 0/360
        _add_signal(s, 3000, 359.9995, -5.0, 5.0, source="exofop_ctoi")
        for run in range(3):  # three nightly runs must still yield one entry per source
            s.add(
                PublishedSnapshot(
                    source_name="nasa_toi",
                    snapshot_utc=datetime(2026, 10, 1 + run, tzinfo=UTC),
                    record_count=8148,
                    commit_sha=None,
                    status="validated",
                    validation_metrics={},
                    published_at=datetime(2026, 10, 1 + run, tzinfo=UTC),
                )
            )
        await s.commit()

    mcp_module.AsyncSessionLocal = factory
    if request.param == "http":
        api_module.AsyncSessionLocal = factory

        async def override():
            async with factory() as session:
                yield session

        api_module.app.dependency_overrides[api_module.get_session] = override
        transport = ASGITransport(app=api_module.app)

        class TestClient(AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                kwargs["base_url"] = "http://testserver"
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(mcp_module.httpx, "AsyncClient", TestClient)
        mcp_module.AsyncSessionLocal = None
    yield
    api_module.app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_one_checked_source_entry_per_catalogue(novelty):
    res = await mcp_module.check_novelty(ra_deg=50.0, dec_deg=50.0, period_d=1.0)
    names = [s["source_name"] for s in res["checked_sources"]]
    assert sorted(names) == ["exofop_ctoi", "nasa_toi"]
    toi = next(s for s in res["checked_sources"] if s["source_name"] == "nasa_toi")
    assert toi["reports"] == 61 and toi["last_ingested_utc"].startswith("2026-10-03")


@pytest.mark.asyncio
async def test_crowded_cone_is_not_truncated(novelty):
    res = await mcp_module.check_novelty(
        ra_deg=10.0, dec_deg=20.0, period_d=6.0, cone_radius_arcsec=120
    )
    assert res["cone_targets_searched"] == 61
    # P = 3.0 at ~70" is the 1/2 alias of 6.0 d, beyond the first 50 targets
    alias = [m for m in res["near_matches"] if m["display_name"] == "TOI-2000.01"]
    assert alias and alias[0]["match_reason"] == "harmonic_alias"
    assert alias[0]["sources"] == ["nasa_toi"]


@pytest.mark.asyncio
async def test_cone_across_ra_zero(novelty):
    res = await mcp_module.check_novelty(
        ra_deg=0.0003, dec_deg=-5.0, period_d=5.0, cone_radius_arcsec=10
    )
    assert [m["display_name"] for m in res["near_matches"]] == ["TOI-3000.01"]
    assert res["near_matches"][0]["sources"] == ["exofop_ctoi"]
