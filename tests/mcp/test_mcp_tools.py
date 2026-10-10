from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import exoplanet_db.mcp.server as mcp_module
from exoplanet_db.ingest.runner import IngestionRunner
from exoplanet_db.models import Base


@pytest.fixture
async def setup_mcp_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    mcp_module.AsyncSessionLocal = session_factory

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
            }
        ]
        await runner.ingest_nasa_composite(mock_planets)

    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_lookup_target(setup_mcp_db):
    res = await mcp_module.lookup_target("WASP-12")
    assert res["found"] is True
    assert res["count"] == 1
    system = res["systems"][0]
    assert system["primary_name"] == "WASP-12"
    assert len(system["candidates"]) == 1
    cand = system["candidates"][0]
    assert cand["display_name"] == "WASP-12 b"
    assert cand["disposition"] == "confirmed_planet"
    assert "scientific_limits" in cand or "limits" in cand


@pytest.mark.asyncio
async def test_mcp_search_signals(setup_mcp_db):
    res = await mcp_module.search_signals(min_period_d=1.0, max_period_d=2.0)
    assert res["returned"] == 1
    cand = res["candidates"][0]
    assert cand["display_name"] == "WASP-12 b"


@pytest.mark.asyncio
async def test_mcp_check_novelty_evidentiary_snapshot(setup_mcp_db):
    # 1. Query near WASP-12 b coordinates with matching period
    res = await mcp_module.check_novelty(
        ra_deg=97.6397,
        dec_deg=29.6722,
        period_d=1.09142,
        cone_radius_arcsec=30.0,
    )

    # Gate: never return a boolean
    assert not isinstance(res, bool)
    assert "status" in res
    assert res["status"] == "near_matches_found"
    assert len(res["near_matches"]) == 1
    assert res["near_matches"][0]["match_reason"] == "period_match"
    assert "checked_sources" in res
    assert len(res["checked_sources"]) >= 1
    assert "known_unindexed_sources" in res

    # 2. Query in an empty patch of sky -> no match
    res_empty = await mcp_module.check_novelty(
        ra_deg=10.0,
        dec_deg=10.0,
        period_d=5.0,
    )
    assert res_empty["status"] == "no_match_in_checked_sources"
    assert len(res_empty["near_matches"]) == 0
    assert "checked_sources" in res_empty
    assert "known_unindexed_sources" in res_empty
    assert "epistemic_warning" in res_empty


@pytest.mark.asyncio
async def test_mcp_tools_http_client_mode(setup_mcp_db, monkeypatch):
    """
    Verify that MCP tools function completely over HTTP (FastAPI app)
    when AsyncSessionLocal is None (i.e. zero database credentials or internal access).
    """
    import exoplanet_db.api.app as api_module
    from httpx import ASGITransport, AsyncClient

    # Set FastAPI app session to the test DB
    api_module.AsyncSessionLocal = mcp_module.AsyncSessionLocal

    # Force MCP module to HTTP client mode (no database session)
    mcp_module.AsyncSessionLocal = None

    # Patch httpx.AsyncClient in mcp_module to target the in-memory FastAPI app
    transport = ASGITransport(app=api_module.app)

    class TestAsyncClient(AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            kwargs["base_url"] = "http://testserver"
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(mcp_module.httpx, "AsyncClient", TestAsyncClient)

    # 1. Test lookup_target via HTTP
    res_lookup = await mcp_module.lookup_target("WASP-12")
    assert res_lookup["found"] is True
    assert res_lookup["count"] == 1
    sys_res = res_lookup["systems"][0]
    assert sys_res["primary_name"] == "WASP-12"
    assert len(sys_res["candidates"]) == 1

    # 2. Test search_signals via HTTP
    res_signals = await mcp_module.search_signals(min_period_d=1.0, max_period_d=2.0)
    assert res_signals["returned"] == 1
    assert res_signals["candidates"][0]["display_name"] == "WASP-12 b"

    # 3. Test check_novelty via HTTP
    res_novelty = await mcp_module.check_novelty(
        ra_deg=97.6397,
        dec_deg=29.6722,
        period_d=1.09142,
    )
    assert res_novelty["status"] == "near_matches_found"
    assert len(res_novelty["near_matches"]) == 1

    # 4. Test get_catalog_stats via HTTP
    res_stats = await mcp_module.get_catalog_stats()
    assert res_stats["total_signals"] == 1
    assert res_stats["confirmed_planets"] == 1
    assert res_stats["total_targets"] == 1


@pytest.mark.asyncio
async def test_mcp_get_catalog_stats_db_mode(setup_mcp_db):
    res = await mcp_module.get_catalog_stats()
    assert res["total_signals"] == 1
    assert res["confirmed_planets"] == 1
    assert res["total_targets"] == 1
    assert "disposition_counts" in res
