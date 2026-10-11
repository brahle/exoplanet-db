from __future__ import annotations

from datetime import UTC, datetime, timedelta

import astropy.units as u
import pytest
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
from astropy.time import Time

from exoplanet_db.services.observation_queue import QueueSettings, compute_queue

ZAGREB = (45.815, 15.9819)


def _row(ra, dec, period, sid):
    return dict(
        period_d=period,
        period_err_d=1e-6,
        epoch_bjd=2459000.0,
        epoch_err_d=1e-3,
        cov=None,
        duration_h=3.0,
        depth_ppm=5000.0,
        fit_model="x",
        signal_id=sid,
        signal_name=sid,
        disposition="planet_candidate",
        target_id=sid,
        target_name=sid,
        ra_deg=ra,
        dec_deg=dec,
    )


@pytest.mark.parametrize("ra,dec", [(97.6366, 29.6723), (310.0, 45.0), (15.0, -10.0)])
def test_altitude_sun_and_moon_match_astropy(ra, dec):
    start = datetime(2026, 12, 1, tzinfo=UTC)
    items, _ = compute_queue(
        [_row(ra, dec, 0.7311, "x")],
        QueueSettings(*ZAGREB, start, start + timedelta(days=3), observable_only=False, limit=50),
    )
    assert items
    loc = EarthLocation.from_geodetic(ZAGREB[1] * u.deg, ZAGREB[0] * u.deg)
    for it in items:
        mid = Time(it["transit_mid_utc"])
        frame = AltAz(obstime=mid, location=loc)
        target = SkyCoord(ra * u.deg, dec * u.deg).transform_to(frame)
        assert it["target_altitude_deg"] == pytest.approx(target.alt.deg, abs=0.2)
        moon = get_body("moon", mid, location=loc).transform_to(frame)
        assert it["moon_separation_deg"] == pytest.approx(moon.separation(target).deg, abs=0.5)
        # Reported Sun altitude is the highest between ingress and egress
        sun_mid = get_sun(mid).transform_to(frame).alt.deg
        assert it["sun_altitude_deg"] >= sun_mid - 0.2


def test_one_row_per_signal_using_the_most_precise_ephemeris():
    start = datetime(2026, 12, 1, tzinfo=UTC)
    loose = _row(97.6366, 29.6723, 1.09142, "s") | {"period_err_d": 1e-3, "fit_model": "loose"}
    tight = _row(97.6366, 29.6723, 1.09142, "s") | {"period_err_d": 1e-7, "fit_model": "tight"}
    items, _ = compute_queue(
        [loose, tight],
        QueueSettings(*ZAGREB, start, start + timedelta(days=2), observable_only=False),
    )
    assert items and {it["fit_model"] for it in items} == {"tight"}
    mids = [it["transit_mid_utc"] for it in items]
    assert len(mids) == len(set(mids))
