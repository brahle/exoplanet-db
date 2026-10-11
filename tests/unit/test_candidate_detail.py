from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from exoplanet_db.api import app as app_module
from exoplanet_db.api.app import app, get_session
from exoplanet_db.models.base import Base
from exoplanet_db.models.entities import EphemerisFit, Report, Signal, Target
from exoplanet_db.services.system_physics import (
    radius_from_depth_earth,
    semi_major_axis_au,
    source_url,
)


def test_semi_major_axis_earth_orbit():
    assert semi_major_axis_au(365.25, 1.0) == pytest.approx(1.0)
    assert semi_major_axis_au(0.0, 1.0) is None
    assert semi_major_axis_au(10.0, None) is None


def test_radius_from_depth_jupiter_like():
    # 1% depth on a Sun-like star is ~10.9 Earth radii
    assert radius_from_depth_earth(10_000, 1.0) == pytest.approx(10.9076, rel=1e-4)
    assert radius_from_depth_earth(None, 1.0) is None
    assert radius_from_depth_earth(500, None) is None


def test_source_url_prefers_stored_url_and_derives_nasa_links():
    assert (
        source_url("git:brahle/x", "k", "https://github.com/brahle/x/tree/abc/c", {}, None)
        == "https://github.com/brahle/x/tree/abc/c"
    )
    assert (
        source_url("nasa_pscomppars", "WASP-12 b", None, {"hostname": "WASP-12"}, None)
        == "https://exoplanetarchive.ipac.caltech.edu/overview/WASP-12"
    )
    assert (
        source_url("nasa_toi", "TOI-1.01", None, {}, "123")
        == "https://exofop.ipac.caltech.edu/tess/target.php?id=123"
    )
    assert source_url("nasa_pscomppars", "x", None, {}, None) is None


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        now = datetime.now(UTC)
        s.add(Target(id="t1", primary_name="HOST-1", tic_id="111", ra_deg=10.0, dec_deg=-20.0))
        for i, (period, depth) in enumerate([(3.0, 10_000.0), (12.0, 400.0)], start=1):
            s.add(
                Signal(
                    id=f"s{i}",
                    target_id="t1",
                    display_name=f"HOST-1 {'bc'[i - 1]}",
                    primary_period_d=period,
                    primary_epoch_bjd=2459000.0,
                    primary_epoch_err_d=0.001,
                    primary_duration_h=2.0,
                    primary_depth_ppm=depth,
                    current_disposition="confirmed_planet",
                    scientific_limits=[],
                )
            )
        s.add(
            Report(
                id="r1",
                target_id="t1",
                signal_id="s1",
                source_name="nasa_pscomppars",
                source_key="HOST-1 b",
                retrieval_utc=now - timedelta(days=1),
                author_or_team="NASA Exoplanet Archive",
                reported_disposition="confirmed_planet",
                raw_payload={"hostname": "HOST-1", "st_rad": 1.0, "st_mass": 1.0, "pl_rade": 11.2},
            )
        )
        s.add(
            Report(
                id="r2",
                target_id="t1",
                signal_id="s1",
                source_name="git:brahle/exoplanet-candidates",
                source_key="dossier-1",
                retrieval_utc=now,
                author_or_team="brahle/exoplanet-candidates",
                bibcode_or_url="https://github.com/brahle/exoplanet-candidates/tree/abc/candidates/x",
                reported_disposition="planet_candidate",
                raw_payload={},
            )
        )
        s.add(
            EphemerisFit(
                id="f1",
                report_id="r1",
                signal_id="s1",
                period_d=3.0,
                epoch_bjd_tdb=2459000.0,
                depth_err_ppm=50.0,
                duration_err_h=0.1,
            )
        )
        await s.commit()
        yield s
    await engine.dispose()


@pytest.fixture
async def client(session: AsyncSession):
    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    app_module._POPULATION_CACHE.update(at=0.0, body=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_candidate_detail_has_sources_physics_and_system(client: AsyncClient):
    res = await client.get("/v1/candidates/s1")
    assert res.status_code == 200
    body = res.json()

    # Provenance comes from report rows, newest first, each with a public link
    assert [s["source_name"] for s in body["sources"]] == [
        "git:brahle/exoplanet-candidates",
        "nasa_pscomppars",
    ]
    assert body["sources"][1]["url"] == "https://exoplanetarchive.ipac.caltech.edu/overview/HOST-1"
    # The preferred fit came from the NASA report, so that is the primary source
    assert body["primary_source"]["source_name"] == "nasa_pscomppars"
    assert body["source_name"] == "nasa_pscomppars"

    star = body["host_star"]
    assert star["radius_rsun"] == 1.0 and star["mass_msun"] == 1.0

    planet = body["planet"]
    assert planet["radius_earth"] == 11.2 and planet["radius_basis"] == "catalogue"
    assert planet["semi_major_axis_basis"] == "kepler3_catalogue_mass"
    assert planet["semi_major_axis_au"] == pytest.approx((3.0 / 365.25) ** (2 / 3))
    assert planet["depth_err_ppm"] == 50.0 and planet["duration_err_h"] == 0.1
    assert body["epoch_err_d"] == 0.001

    names = [p["display_name"] for p in body["system"]]
    assert names == ["HOST-1 b", "HOST-1 c"]
    sibling = body["system"][1]
    assert sibling["radius_basis"] == "transit_depth"
    assert sibling["radius_earth"] == pytest.approx(math.sqrt(400 / 1e6) * 109.076)


@pytest.mark.asyncio
async def test_candidate_detail_404(client: AsyncClient):
    assert (await client.get("/v1/candidates/nope")).status_code == 404


@pytest.mark.asyncio
async def test_population_is_columnar_and_gzipped(client: AsyncClient):
    res = await client.get("/v1/population", headers={"Accept-Encoding": "gzip"})
    assert res.status_code == 200
    assert res.headers["content-encoding"] == "gzip"
    body = res.json()  # httpx transparently decodes gzip
    assert body["count"] == 2
    i = body["id"].index("s1")
    assert body["radius_earth"][i] == 11.2
    assert body["period_d"][i] == 3.0

    plain = await client.get("/v1/population", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert json.loads(plain.content)["count"] == 2
    j = body["id"].index("s2")
    assert body["radius_earth"][j] == pytest.approx(math.sqrt(400 / 1e6) * 109.076, abs=1e-3)


@pytest.mark.asyncio
async def test_detail_assets_and_import_map_resolve(client: AsyncClient):
    index = (await client.get("/")).text
    assert '"three": "/static/vendor/three/three.module.min.js"' in index
    assert '"three/addons/": "/static/vendor/three/"' in index
    assert "/static/charts.js" in index
    for path in [
        "/static/charts.js",
        "/static/system3d.js",
        "/static/vendor/three/three.module.min.js",
        # system3d.js imports these through the "three/addons/" prefix
        "/static/vendor/three/controls/OrbitControls.js",
        "/static/vendor/three/renderers/CSS2DRenderer.js",
    ]:
        assert (await client.get(path)).status_code == 200, path


@pytest.mark.asyncio
async def test_healthz_reports_package_version(client: AsyncClient):
    assert (await client.get("/healthz")).json()["version"] == app_module.APP_VERSION


@pytest.mark.asyncio
async def test_search_matches_source_identifiers(client: AsyncClient):
    # "dossier-1" is only a report source_key, not a signal or target name
    res = await client.get("/v1/candidates", params={"search": "dossier-1"})
    assert [i["id"] for i in res.json()["items"]] == ["s1"]


@pytest.mark.asyncio
async def test_candidate_filters_and_sorting(client: AsyncClient, session):
    from exoplanet_db.services.planet_score import score_signals

    await score_signals(session)
    res = await client.get("/v1/candidates", params={"source": "nasa_pscomppars"})
    assert [i["id"] for i in res.json()["items"]] == ["s1"]
    res = await client.get(
        "/v1/candidates",
        params=[("source", "nasa_pscomppars"), ("source", "git:brahle/exoplanet-candidates")],
    )
    assert res.json()["total"] == 1
    res = await client.get("/v1/candidates", params={"min_depth_ppm": 1000, "max_period_d": 5})
    assert [i["id"] for i in res.json()["items"]] == ["s1"]
    res = await client.get("/v1/candidates", params={"max_period_d": 100})
    assert res.json()["total"] == 2
    res = await client.get("/v1/candidates", params={"min_duration_h": 3})
    assert res.json()["total"] == 0
    items = (await client.get("/v1/candidates", params={"sort": "-score"})).json()["items"]
    assert items[0]["id"] == "s1" and items[0]["planet_score"] == 100
    assert items[0]["source_names"] == ["git:brahle/exoplanet-candidates", "nasa_pscomppars"]
    # Both fixture signals are confirmed planets
    res = await client.get("/v1/candidates", params={"min_score": 90})
    assert [i["id"] for i in res.json()["items"]] == ["s1", "s2"]
    res = await client.get("/v1/candidates", params={"max_score": 50})
    assert res.json()["total"] == 0
    res = await client.get("/v1/candidates", params={"localisation": "contradicted"})
    assert res.json()["total"] == 0
    detail = (await client.get("/v1/candidates/s1")).json()
    assert detail["score_factors"][0]["delta"] == 100
