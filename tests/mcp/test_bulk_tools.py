from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import exoplanet_db.api.app as api_module
import exoplanet_db.mcp.server as mcp_module
from exoplanet_db.models import Base
from exoplanet_db.models.entities import Report, Signal, Target, VariableStar

# Two ids sharing an 8-char prefix (ambiguous) but not a 12-char one
ID_A = "aaaaaaaa-0000-4000-8000-000000000001"
ID_B = "aaaaaaaa-1111-4000-8000-000000000002"
ID_C = "cccccccc-2222-4000-8000-000000000003"


def _add(session, tid, sid, *, name, tic, ra, dec, period, source, key, disp="planet_candidate"):
    session.add(
        Target(
            id=tid,
            primary_name=name,
            tic_id=tic,
            gaia_dr3_id=f"45678901234567{tic}",
            ra_deg=ra,
            dec_deg=dec,
        )
    )
    session.add(
        Signal(
            id=sid,
            target_id=tid,
            display_name=key if source == "nasa_toi" else f"{name} b",
            primary_period_d=period,
            primary_period_err_d=1.234567e-5,
            primary_epoch_bjd=2459000.123456789,
            primary_depth_ppm=1234.4,
            primary_duration_h=2.3456,
            current_disposition=disp,
            planet_score=60 if disp == "planet_candidate" else 100,
            scientific_limits=[],
        )
    )
    session.add(
        Report(
            target_id=tid,
            signal_id=sid,
            source_name=source,
            source_key=key,
            retrieval_utc=datetime.now(UTC),
            author_or_team="x",
            reported_disposition=disp,
            raw_payload={},
        )
    )


@pytest.fixture(params=["db", "http"])
async def catalogue(request, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        _add(
            s,
            "t1",
            ID_A,
            name="TOI-1001",
            tic="1001",
            ra=10.0,
            dec=20.0,
            period=3.0,
            source="nasa_toi",
            key="TOI-1001.01",
        )
        _add(
            s,
            "t2",
            ID_B,
            name="WASP-12",
            tic="2002",
            ra=97.6,
            dec=29.7,
            period=1.09142,
            source="nasa_pscomppars",
            key="WASP-12 b",
            disp="confirmed_planet",
        )
        _add(
            s,
            "t3",
            ID_C,
            name="TIC 3003",
            tic="3003",
            ra=200.0,
            dec=-30.0,
            period=7.5,
            source="eumemic/exoplanets",
            key="3003_7.5",
        )
        # A Gaia eclipsing binary 36" from an otherwise empty field, at twice 4.0 d
        s.add(
            VariableStar(
                catalogue="gaia_dr3_eb",
                source_id="999",
                name="Gaia DR3 999",
                ra_deg=300.01,
                dec_deg=0.0,
                period_d=8.0,
                var_type="EB",
                mag=15.2,
                mag_band="G",
            )
        )
        await s.commit()

    mcp_module.AsyncSessionLocal = factory
    if request.param == "http":

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


async def test_get_candidates_full_prefix_unknown_ambiguous(catalogue):
    res = await mcp_module.get_candidates(
        [ID_C, ID_B[:12], "aaaaaaaa", "ffffffff-0000", "short"], format="full"
    )
    assert [s["id"] for s in res["items"]] == [ID_C, ID_B]  # request order
    assert res["ambiguous"] == {"aaaaaaaa": [ID_A, ID_B]}
    assert res["not_found"] == ["ffffffff-0000", "short"]
    wasp = res["items"][1]
    assert wasp["target_name"] == "WASP-12" and wasp["sources"] == ["nasa_pscomppars"]
    assert wasp["planet_score"] == 100


async def test_get_candidates_compact_table(catalogue):
    res = await mcp_module.get_candidates([ID_A, ID_B, ID_C])
    cols, rows = res["signals"]["cols"], res["signals"]["rows"]
    assert all(len(r) == len(cols) for r in rows)
    row = dict(zip(cols, rows[0]))
    assert row["id"] == ID_A[:12]
    assert row["disp"] == "PC" and row["src"] == "toi" and row["loc"] == "U"
    assert row["depth_ppm"] == 1234 and row["dur_h"] == 2.35 and row["P_err"] == 1.2e-05
    assert row["T0_bjd"] == 2459000.12346
    # legend lists only codes present
    assert res["legend"]["disp"] == {"CP": "confirmed_planet", "PC": "planet_candidate"}
    assert res["legend"]["src"] == {
        "eum": "eumemic/exoplanets",
        "ps": "nasa_pscomppars",
        "toi": "nasa_toi",
    }
    assert "not_found" not in res
    # compact ids round-trip
    again = await mcp_module.get_candidates([r[0] for r in rows], format="full")
    assert [s["id"] for s in again["items"]] == [ID_A, ID_B, ID_C]


async def test_resolve_targets_identifier_kinds(catalogue):
    res = await mcp_module.resolve_targets(
        [
            "TIC 1001",
            "2002",
            "Gaia DR3 456789012345673003",
            "toi-1001.01",
            "TOI-1001",
            "wasp 12",
            "WASP-12 b",
            "3003_7.5",
            "nothing-here",
        ],
        format="full",
    )
    by = {r["query"]: (r["matched_by"], r["target_ids"]) for r in res["resolved"]}
    assert by["TIC 1001"] == ("tic", ["t1"])
    assert by["2002"] == ("tic", ["t2"])
    assert by["Gaia DR3 456789012345673003"] == ("gaia_dr3", ["t3"])
    assert by["toi-1001.01"][1] == ["t1"]
    assert by["TOI-1001"][1] == ["t1"]
    assert by["wasp 12"] == ("name", ["t2"])
    assert by["WASP-12 b"] == ("signal_name", ["t2"])
    assert by["3003_7.5"] == ("source_key", ["t3"])
    ids = {r["query"]: r["signal_ids"] for r in res["resolved"]}
    # star-level matches list the star's signals; signal-level ones only that signal
    assert ids["TIC 1001"] == [ID_A] and ids["wasp 12"] == [ID_B] and ids["TOI-1001"] == [ID_A]
    assert ids["WASP-12 b"] == [ID_B]
    assert res["not_found"] == ["nothing-here"]
    # every host once
    assert sorted(t["id"] for t in res["targets"]) == ["t1", "t2", "t3"]


async def test_resolve_targets_compact(catalogue):
    res = await mcp_module.resolve_targets(["TIC 1001", "WASP-12", "TIC 1001 "])
    assert len(res["resolved"]["rows"]) == 2  # duplicates collapse
    assert len(res["targets"]["rows"]) == 2
    sig_cols = res["signals"]["cols"]
    assert sig_cols[:3] == ["target", "id", "name"]
    assert {r[0] for r in res["signals"]["rows"]} == {"t1", "t2"}


async def test_check_novelty_bulk(catalogue):
    res = await mcp_module.check_novelty_bulk(
        [
            {"ra_deg": 10.0, "dec_deg": 20.0, "period_d": 6.0, "label": "alias"},
            {"ra_deg": 97.6, "dec_deg": 29.7, "period_d": 1.09142},
            {"ra_deg": 300.0, "dec_deg": 0.0, "period_d": 4.0, "label": "eb"},
            {"ra_deg": 0.0, "dec_deg": 0.0, "period_d": 0.0, "label": "bad"},
        ],
        format="full",
    )
    status = {r["label"]: r.get("status", r.get("error")) for r in res["results"]}
    assert status == {
        "alias": "near_matches_found",
        "1": "near_matches_found",
        "eb": "variable_star_match",
        "bad": "period_d must be positive",
    }
    assert res["results"][0]["near_matches"][0]["match_reason"] == "harmonic_alias"
    eb = res["results"][2]["variable_matches"][0]
    assert eb["catalogue"] == "gaia_dr3_eb" and eb["eclipsing"] and eb["period_ratio"] == 2.0
    assert eb["match_reason"] == "variable_harmonic" and 35 < eb["separation_arcsec"] < 37
    assert res["query"]["cone_radius_arcsec"] == 120.0
    assert {s["source_name"] for s in res["checked_sources"]} == {
        "nasa_toi",
        "nasa_pscomppars",
        "eumemic/exoplanets",
        "variables:gaia_dr3_eb",
    }
    assert "epistemic_warning" in res

    # the binary is 36" away: the old 30" default would have called this field empty
    narrow = await mcp_module.check_novelty_bulk(
        [{"ra_deg": 300.0, "dec_deg": 0.0, "period_d": 4.0}], cone_radius_arcsec=30, format="full"
    )
    assert narrow["results"][0]["status"] == "no_match_in_checked_sources"

    single = await mcp_module.check_novelty(300.0, 0.0, 4.0, format="compact")
    assert single["status"] == "variable_star_match"
    var = dict(zip(single["var"]["cols"], single["var"]["rows"][0]))
    assert var["cat"] == "geb" and var["match"] == "H" and var["eb"] == 1
    assert single["legend"]["vcat"] == {"geb": "gaia_dr3_eb"}

    compact = await mcp_module.check_novelty_bulk(
        [{"ra_deg": 10.0, "dec_deg": 20.0, "period_d": 6.0, "label": "alias"}]
    )
    assert compact["results"]["rows"] == [["alias", "near_matches_found", 1, 1, 0, 0]]
    near = dict(zip(compact["near"]["cols"], compact["near"]["rows"][0]))
    assert near["label"] == "alias" and near["match"] == "H" and near["ratio"] == 0.5
    assert compact["legend"]["match"] == {"H": "harmonic_alias"}
    assert "epistemic_warning" in compact and "known_unindexed_sources" in compact


async def test_search_signals_filters_and_compact(catalogue):
    res = await mcp_module.search_signals(min_score=70)
    assert [c["display_name"] for c in res["candidates"]] == ["WASP-12 b"]
    res = await mcp_module.search_signals(
        source=["nasa_toi", "eumemic/exoplanets"], sort="-period", format="compact"
    )
    assert res["total"] == 2
    assert [r[1] for r in res["signals"]["rows"]] == ["TIC 3003 b", "TOI-1001.01"]


async def test_dossier_accepts_prefix(catalogue):
    res = await mcp_module.get_candidate_dossier(ID_B[:12])
    assert res["found"] and res["id"] == ID_B
    assert res["assessment"]["planet_score"] == 100
    assert res["sources"][0]["source_name"] == "nasa_pscomppars"
    amb = await mcp_module.get_candidate_dossier("aaaaaaaa")
    assert amb["found"] is False and amb["ambiguous"]["aaaaaaaa"] == [ID_A, ID_B]


async def test_wire_format_is_single_minified_text(catalogue):
    tools = {t.name: t for t in await mcp_module.mcp.list_tools()}
    assert {"get_candidates", "resolve_targets", "check_novelty_bulk"} <= tools.keys()
    assert tools["get_candidates"].output_schema is None
    assert tools["get_candidates"].input_schema["properties"]["format"]["enum"] == [
        "compact",
        "full",
    ]
    result = await mcp_module.mcp.call_tool("get_candidates", {"ids": [ID_A]})
    assert len(result.content) == 1 and result.structured_content is None
    text = result.content[0].text
    assert "\n" not in text and ": " not in text
    assert json.loads(text)["signals"]["rows"][0][0] == ID_A[:12]
