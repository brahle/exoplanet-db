from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from exoplanet_db.api.app import app, get_session
from exoplanet_db.models.base import Base
from exoplanet_db.models.entities import EphemerisFit, Report, Signal, Target


@pytest.fixture
async def test_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_observation_queue_api(test_session: AsyncSession):
    # Seed a target and coherent signal
    target = Target(
        id="target-wasp12",
        primary_name="WASP-12",
        ra_deg=97.6366,
        dec_deg=29.6723,
    )
    test_session.add(target)

    signal = Signal(
        id="signal-wasp12b",
        target_id="target-wasp12",
        display_name="WASP-12 b",
        primary_period_d=1.09142,
        primary_epoch_bjd=2454508.9761,
        primary_duration_h=3.0,
        primary_depth_ppm=14000.0,
        current_disposition="confirmed_planet",
        source_localisation_status="on_target",
        timing_consistency_status="strictly_periodic",
        scientific_limits=[],
    )
    test_session.add(signal)

    report = Report(
        id="report-wasp12b-1",
        target_id="target-wasp12",
        signal_id="signal-wasp12b",
        source_name="literature",
        source_key="WASP-12b-2009",
        retrieval_utc=datetime.now(UTC),
        author_or_team="Hebb et al. 2009",
        reported_disposition="confirmed_planet",
        raw_payload={},
    )
    test_session.add(report)

    fit = EphemerisFit(
        id="fit-wasp12b-1",
        report_id="report-wasp12b-1",
        signal_id="signal-wasp12b",
        period_d=1.09142,
        period_err_d=0.0000002,
        epoch_bjd_tdb=2454508.9761,
        epoch_err_d=0.0002,
        duration_h=3.0,
        is_coherent=True,
        fit_model="transit_mcmc",
    )
    test_session.add(fit)
    await test_session.commit()

    async def override_get_session():
        yield test_session

    app.dependency_overrides[get_session] = override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # WASP-12 b has fully observable transits from Zagreb on 4-6 Dec 2026
        start_time = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
        end_time = start_time + timedelta(days=7)

        response = await client.get(
            "/v1/observations/queue",
            params={
                "lat": 45.8150,
                "lon": 15.9819,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "min_priority": 0.0,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "attribution" in data
        assert (
            data["attribution"]["community_data_license"]
            == "CC BY 4.0 (Creative Commons Attribution 4.0 International)"
        )
        assert len(data["items"]) >= 1

        top_item = data["items"][0]
        assert top_item["signal_id"] == "signal-wasp12b"
        assert top_item["target_name"] == "WASP-12"
        assert top_item["current_disposition"] == "confirmed_planet"
        assert "timing_uncertainty_min" in top_item
        assert "priority_score" in top_item
        # The whole transit, not just mid-transit, must clear the altitude limit in darkness
        for item in data["items"]:
            assert item["is_observable"] is True
            assert min(item["altitude_ingress_deg"], item["altitude_egress_deg"]) >= 30.0
            assert item["sun_altitude_deg"] <= -18.0

        # Unobservable transits are only listed on request, flagged
        everything = await client.get(
            "/v1/observations/queue",
            params={
                "lat": 45.8150,
                "lon": 15.9819,
                "start_time": datetime(2026, 12, 20, tzinfo=UTC).isoformat(),
                "end_time": datetime(2026, 12, 25, tzinfo=UTC).isoformat(),
                "observable_only": "false",
            },
        )
        rows = everything.json()["items"]
        assert len(rows) == 5 and not any(r["is_observable"] for r in rows)

        wrong_source = await client.get(
            "/v1/observations/queue",
            params={
                "lat": 45.8150,
                "lon": 15.9819,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "source": "exofop_ctoi",
            },
        )
        assert wrong_source.json()["total"] == 0
        right_source = await client.get(
            "/v1/observations/queue",
            params={
                "lat": 45.8150,
                "lon": 15.9819,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "source": "literature",
            },
        )
        assert right_source.json()["total"] >= 1
        assert right_source.json()["items"][0]["source_names"] == ["literature"]

        too_long = await client.get(
            "/v1/observations/queue",
            params={
                "lat": 45.8,
                "lon": 16.0,
                "start_time": start_time.isoformat(),
                "end_time": (start_time + timedelta(days=8)).isoformat(),
            },
        )
        assert too_long.status_code == 422

    app.dependency_overrides.clear()
