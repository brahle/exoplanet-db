from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import exoplanet_db.ingest.variables as ingest
from exoplanet_db.models import Base, PublishedSnapshot, Signal, Target, VariableStar
from exoplanet_db.services import variables
from exoplanet_db.services.planet_score import Evidence, score, score_signals


def _v(catalogue="gaia_dr3_eb", ra=10.0, dec=20.0, period=2.0, var_type="EB", name="X"):
    return VariableStar(
        catalogue=catalogue,
        source_id=name,
        name=name,
        ra_deg=ra,
        dec_deg=dec,
        period_d=period,
        var_type=var_type,
    )


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def test_match_rules():
    hits = [
        (_v(period=2.0), 40.0),  # same period
        (_v(period=4.0), 50.0),  # twice the period
        (
            _v(period=2.5),
            1.0,
        ),  # EB on the star itself, unrelated period
        (_v(catalogue="vsx", period=2.5, var_type="RRAB"), 1.0),  # pulsator, unrelated
        (_v(catalogue="vsx", period=2.0, var_type="EP"), 80.0),  # known transit
        (_v(period=2.05), 30.0),  # 2.5% off: no match at 1%
    ]
    out = variables.match(hits, 2.0, 1.0, 1.0)
    reasons = [(m["match_reason"], m["separation_arcsec"]) for m in out]
    assert reasons == [
        ("variable_period_match", 40.0),
        ("variable_harmonic", 50.0),
        ("variable_period_match", 80.0),
        ("same_star_eclipsing_binary", 1.0),
    ]
    assert out[2]["known_transit"] and not out[2]["eclipsing"]
    assert variables.is_eclipsing("vsx", "EA") and variables.is_eclipsing("vsx", "ELL")
    assert not variables.is_eclipsing("vsx", "EP") and not variables.is_eclipsing("vsx", "DSCT")


async def test_cone_wraps_ra(factory):
    async with factory() as s:
        s.add_all([_v(ra=359.99, dec=0.0, name="west"), _v(ra=0.01, dec=0.0, name="east")])
        await s.commit()
        hits = await variables.cone(s, 0.0, 0.0, 60.0)
    assert sorted(v.name for v, _ in hits) == ["east", "west"]


async def test_crossmatch_and_score(factory):
    async with factory() as s:
        s.add(Target(id="t", primary_name="TIC 1", tic_id="1", ra_deg=10.0, dec_deg=20.0))
        for sid, period in (("near", 3.0), ("far", 5.0), ("none", 7.0)):
            s.add(
                Signal(
                    id=sid,
                    target_id="t",
                    display_name=sid,
                    primary_period_d=period,
                    primary_epoch_bjd=2459000.0,
                    current_disposition="planet_candidate",
                    scientific_limits=[],
                )
            )
        s.add_all(
            [
                _v(ra=10.0, dec=20.0 + 30 / 3600, period=6.0, name="EB30"),  # 30", 2x period
                _v(ra=10.0, dec=20.0 + 90 / 3600, period=5.0, name="EB90"),  # 90", same
                _v(ra=10.0, dec=20.0 + 30 / 3600, period=7.1, name="off"),  # 1.4% off
            ]
        )
        await s.commit()
        assert await variables.crossmatch_signals(s) == {"signals": 3, "with_matches": 2}
        await score_signals(s)
        rows = {r.id: r for r in (await s.execute(select(Signal))).scalars()}
    assert [m["name"] for m in rows["near"].variable_matches] == ["EB30"]
    assert rows["none"].variable_matches == []
    near = rows["near"].score_factors[-1]
    assert (
        near["delta"] == -25
        and "30″ away" in near["label"]
        and "likely contamination" in near["label"]
    )
    assert rows["far"].score_factors[-1]["delta"] == -10


def test_score_same_star_listing_and_known_transit():
    def ev(**m):
        return Evidence(
            "planet_candidate", sources={"nasa_toi"}, toi_dispositions={"PC"}, variable_matches=[m]
        )

    base = {"catalogue": "gaia_dr3_eb", "name": "G", "period_ratio": 1.0}
    assert score(ev(**base, separation_arcsec=0.5, eclipsing=True))[0] == 50
    # a pulsator on the star itself, or the star listed as a transit host, is not adverse
    assert score(ev(**base, separation_arcsec=0.5, eclipsing=False))[0] == 60
    assert score(ev(**base, separation_arcsec=0.5, known_transit=True))[0] == 60
    assert score(ev(**base, separation_arcsec=40.0, known_transit=True))[0] == 50
    # confirmed planets are never marked down
    assert (
        score(
            Evidence(
                "confirmed_planet",
                sources={"nasa_pscomppars"},
                variable_matches=[{**base, "separation_arcsec": 10, "eclipsing": True}],
            )
        )[0]
        == 100
    )


async def test_load_catalogue_and_refresh(factory, monkeypatch):
    calls = []

    async def fake_fetch(client, query):
        for row in _fake_rows(query):
            yield row

    def _fake_rows(query):
        calls.append(query)
        if "tess-ebs" in query:
            return [
                {
                    "TIC": "5",
                    "RAJ2000": "1.0",
                    "DEJ2000": "2.0",
                    "Tmag": "9.1",
                    "BJD0": "1411.5",
                    "Per": "0.75",
                }
            ]
        if "veb" in query:
            if "DE_ICRS >= 0.0 AND" in query:
                return [
                    {
                        "Source": "77",
                        "RA_ICRS": "3.0",
                        "DE_ICRS": "1.0",
                        "Freq": "0.5",
                        "magModRef": "17.1",
                    },
                    {
                        "Source": "78",
                        "RA_ICRS": "3.0",
                        "DE_ICRS": "1.0",
                        "Freq": "",
                        "magModRef": "17.1",
                    },
                ]
            return []
        if "vsx" in query and "DEJ2000 >= 85.0" in query:
            return [
                {
                    "OID": "9",
                    "Name": "V1 Foo  ",
                    "RAJ2000": "4.0",
                    "DEJ2000": "89.9",
                    "Type": "EA  ",
                    "Period": "3.5",
                    "max": "12.0",
                    "n_max": "V",
                    "Epoch": "2450000.5",
                }
            ]
        return []

    monkeypatch.setattr(ingest, "_fetch", fake_fetch)
    async with factory() as s:
        out = await ingest.refresh_catalogues(s)
        assert out == {"tess_eb": 1, "gaia_dr3_eb": 1, "vsx": 1}
        rows = {v.catalogue: v for v in (await s.execute(select(VariableStar))).scalars()}
        assert rows["tess_eb"].epoch_bjd == 2458411.5 and rows["tess_eb"].name == "TIC 5"
        assert rows["gaia_dr3_eb"].period_d == 2.0  # 1 / frequency
        assert rows["vsx"].name == "V1 Foo" and rows["vsx"].var_type == "EA"
        assert len([q for q in calls if "B/vsx" in q]) == 36  # 5-degree bands

        # fresh catalogues are skipped; forced reloads replace rows rather than adding
        calls.clear()
        assert (await ingest.refresh_catalogues(s))["vsx"].startswith("fresh")
        assert not calls
        await ingest.refresh_catalogues(s, force=True)
        assert (await s.execute(select(func.count(VariableStar.id)))).scalar() == 3

        # a stale snapshot triggers a reload
        for snap in (await s.execute(select(PublishedSnapshot))).scalars():
            snap.snapshot_utc = datetime.now(UTC) - timedelta(days=40)
        await s.commit()
        out = await ingest.refresh_catalogues(s)
        assert out["vsx"] == 1

        status = await variables.catalogue_status(s)
        assert {x["source_name"] for x in status} == {
            "variables:tess_eb",
            "variables:gaia_dr3_eb",
            "variables:vsx",
        }


async def test_empty_catalogue_response_keeps_old_rows(factory, monkeypatch):
    async def empty(client, query):
        return
        yield

    async with factory() as s:
        s.add(_v(catalogue="tess_eb", name="old"))
        await s.commit()
        monkeypatch.setattr(ingest, "_fetch", empty)
        with pytest.raises(ValueError, match="no rows"):
            await ingest.load_catalogue(s, "tess_eb")
        assert (await s.execute(select(func.count(VariableStar.id)))).scalar() == 1
