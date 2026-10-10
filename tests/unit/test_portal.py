from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from exoplanet_db.api.app import app, get_session
from exoplanet_db.models.base import Base
from exoplanet_db.models.entities import Signal, Target


@pytest.fixture
async def portal_test_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        # Seed test target and signals
        target = Target(
            id="target-wasp12",
            primary_name="WASP-12",
            ra_deg=97.6366,
            dec_deg=29.6723,
        )
        session.add(target)
        signal = Signal(
            id="signal-wasp12b",
            target_id="target-wasp12",
            display_name="WASP-12 b",
            primary_period_d=1.09142,
            primary_epoch_bjd=2454508.9761,
            current_disposition="confirmed_planet",
            source_localisation_status="on_target",
            timing_consistency_status="strictly_periodic",
            scientific_limits=[],
        )
        session.add(signal)
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_human_portal_endpoints(portal_test_session: AsyncSession):
    async def override_get_session():
        yield portal_test_session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Test root HTML portal
        res = await client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers.get("content-type", "")
        assert "Exoplanet DB" in res.text
        assert 'href="#catalog"' in res.text
        assert 'href="#queue"' in res.text
        assert 'id="source-filter"' in res.text
        assert 'id="stats-grid"' in res.text
        assert 'id="stat-total-signals"' in res.text
        assert 'id="stat-confirmed-planets"' in res.text
        assert 'id="stat-candidates-total"' in res.text
        assert 'id="stat-total-targets"' in res.text
        assert "Creative Commons Attribution 4.0 International" in res.text

        # 2. Test static CSS
        res_css = await client.get("/static/portal.css")
        assert res_css.status_code == 200
        assert "text/css" in res_css.headers.get("content-type", "")
        assert "--accent-cyan" in res_css.text
        assert ".source-badge" in res_css.text
        assert ".stats-grid" in res_css.text
        assert ".stat-card" in res_css.text

        # 3. Test static JS
        res_js = await client.get("/static/portal.js")
        assert res_js.status_code == 200
        assert "application/javascript" in res_js.headers.get(
            "content-type", ""
        ) or "text/javascript" in res_js.headers.get("content-type", "")
        assert "handleRoute" in res_js.text
        assert "openDossierModal" in res_js.text
        assert "loadCatalogStats" in res_js.text

        # 4. Test /v1/stats endpoint
        res_stats = await client.get("/v1/stats")
        assert res_stats.status_code == 200
        stats_data = res_stats.json()
        assert "total_signals" in stats_data
        assert "confirmed_planets" in stats_data
        assert "candidates_total" in stats_data
        assert "total_targets" in stats_data
        assert "attribution" in stats_data

        # 5. Test /openapi.json has valid servers list
        res_openapi = await client.get("/openapi.json")
        assert res_openapi.status_code == 200
        openapi_data = res_openapi.json()
        assert "servers" in openapi_data
        assert len(openapi_data["servers"]) > 0
