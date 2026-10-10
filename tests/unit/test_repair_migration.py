from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from exoplanet_db.models.base import Base
from exoplanet_db.models.entities import EphemerisFit, Report, Signal, Target

MIGRATION = next((Path(__file__).parents[2] / "alembic" / "versions").glob("7b1f0c2a9e41_*.py"))


def _load_migration():
    spec = importlib.util.spec_from_file_location("repair_migration", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _report(rid, sid, tid, host, depth_pct):
    return Report(
        id=rid,
        target_id=tid,
        signal_id=sid,
        source_name="nasa_pscomppars",
        source_key=sid,
        retrieval_utc=datetime.now(UTC),
        author_or_team="NASA Exoplanet Archive",
        reported_disposition="confirmed_planet",
        depth_ppm=depth_pct,
        raw_payload={
            "hostname": host,
            "pl_trandep": depth_pct,
            "ra": 1.0,
            "dec": 2.0,
            "st_rad": 0.5,
        },
    )


def test_repair_rescales_depth_and_splits_ticless_hosts():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Target(id="bogus", primary_name="PSR B1257+12", tic_id="None", ra_deg=0, dec_deg=0))
        s.add(Target(id="real", primary_name="WASP-1", tic_id="42", ra_deg=0, dec_deg=0))
        for sid, tid, host, pct in [
            ("psr", "bogus", "PSR B1257+12", None),
            ("ogle", "bogus", "OGLE-2005-BLG-071L", None),
            ("wasp", "real", "WASP-1", 1.25),
        ]:
            s.add(
                Signal(
                    id=sid,
                    target_id=tid,
                    display_name=f"{host} b",
                    primary_period_d=3.0,
                    primary_epoch_bjd=2459000.0,
                    primary_depth_ppm=pct,
                    current_disposition="confirmed_planet",
                    scientific_limits=[],
                )
            )
            s.add(_report(f"r-{sid}", sid, tid, host, pct))
        s.add(
            EphemerisFit(
                report_id="r-wasp",
                signal_id="wasp",
                period_d=3.0,
                epoch_bjd_tdb=2459000.0,
                depth_ppm=1.25,
            )
        )
        s.commit()

    mod = _load_migration()
    for _ in range(2):  # idempotent
        with engine.begin() as conn:
            mod._repair_depths(conn)
            mod._split_ticless_hosts(conn)

    with engine.connect() as conn:
        q = lambda sql: conn.execute(text(sql)).all()  # noqa: E731
        assert q("select primary_depth_ppm from signals where id='wasp'") == [(12500.0,)]
        assert q("select depth_ppm from reports where id='r-wasp'") == [(12500.0,)]
        assert q("select depth_ppm from ephemeris_fits where report_id='r-wasp'") == [(12500.0,)]
        assert q("select tic_id from targets where id='bogus'") == [(None,)]
        moved = q(
            "select t.primary_name, t.radius_rsun from signals s join targets t on t.id = s.target_id where s.id='ogle'"
        )
        assert moved == [("OGLE-2005-BLG-071L", 0.5)]
        assert q("select target_id from reports where id='r-ogle'") != [("bogus",)]
        assert q("select target_id from signals where id='psr'") == [("bogus",)]
        assert q("select count(*) from targets") == [(3,)]
