from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import astropy.units as u
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time

from exoplanet_db.models.entities import EphemerisFit


class CoherentEphemerisRequiredError(ValueError):
    """Raised when an ephemeris solution is not coherent or mixes disparate reference values."""

    pass


def propagate_ephemeris_cycle(
    period_d: float,
    period_err_d: float | None,
    epoch_bjd_tdb: float,
    epoch_err_d: float | None,
    n_cycle: int,
    cov_epoch_period: float | None = None,
) -> tuple[float, float]:
    """
    Propagates ephemeris to cycle N with full covariance matrix uncertainty:
    T(N) = T_0 + N * P
    sigma_T(N)^2 = sigma_T0^2 + N^2 * sigma_P^2 + 2 * N * cov(T0, P)
    """
    tn_bjd = epoch_bjd_tdb + n_cycle * period_d

    sig_t0 = epoch_err_d or 0.0
    sig_p = period_err_d or 0.0
    cov = cov_epoch_period or 0.0

    var_tn = (sig_t0**2) + (n_cycle**2) * (sig_p**2) + (2.0 * n_cycle * cov)
    sigma_tn = math.sqrt(max(0.0, var_tn))
    return tn_bjd, sigma_tn


def bjd_to_utc(
    bjd_tdb: float,
    ra_deg: float,
    dec_deg: float,
    lat_deg: float | None = None,
    lon_deg: float | None = None,
    height_m: float = 0.0,
) -> datetime:
    """
    Converts Barycentric Julian Date in TDB (BJD_TDB) to topocentric UTC datetime.
    Accounts for topocentric barycentric light-travel time difference (up to +-8.316 min).
    """
    coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")

    if lat_deg is not None and lon_deg is not None:
        location = EarthLocation.from_geodetic(
            lon=lon_deg * u.deg, lat=lat_deg * u.deg, height=height_m * u.m
        )
    else:
        # Geocentric default if observer coordinates not specified
        location = EarthLocation.from_geocentric(0.0 * u.m, 0.0 * u.m, 0.0 * u.m)

    # Fixed-point inversion: TDB at Earth topocenter vs SSB
    t_guess = Time(bjd_tdb, format="jd", scale="tdb")
    for _ in range(3):
        ltt = t_guess.light_travel_time(coord, "barycentric", location=location)
        bjd_modeled = t_guess.tdb.jd + ltt.to_value("day")
        diff_d = bjd_tdb - bjd_modeled
        t_guess = t_guess + diff_d * u.day

    utc_dt = t_guess.utc.to_datetime(timezone=UTC)
    return utc_dt


def find_upcoming_transits(
    fit: EphemerisFit,
    ra_deg: float,
    dec_deg: float,
    start_utc: datetime,
    end_utc: datetime,
    observer_lat_deg: float | None = None,
    observer_lon_deg: float | None = None,
) -> list[dict[str, Any]]:
    """
    Computes upcoming transit mid-times within [start_utc, end_utc] using a coherent EphemerisFit.
    Strictly enforces is_coherent=True.
    """
    if not fit.is_coherent:
        raise CoherentEphemerisRequiredError(
            f"EphemerisFit '{fit.id}' is marked incoherent; combining parameters from disparate references is prohibited."
        )

    p = fit.period_d
    t0 = fit.epoch_bjd_tdb

    # Extract covariance if present
    cov = None
    if fit.covariance_matrix:
        mat = fit.covariance_matrix
        if len(mat) >= 2 and len(mat[0]) >= 2:
            cov = mat[0][1]

    # Convert start and end to approximate BJD
    start_bjd = Time(start_utc).tdb.jd
    end_bjd = Time(end_utc).tdb.jd

    # Cycle range
    min_n = math.floor((start_bjd - t0) / p) - 1
    max_n = math.ceil((end_bjd - t0) / p) + 1

    transits = []
    for n in range(min_n, max_n + 1):
        bjd_mid, sigma_d = propagate_ephemeris_cycle(
            period_d=p,
            period_err_d=fit.period_err_d,
            epoch_bjd_tdb=t0,
            epoch_err_d=fit.epoch_err_d,
            n_cycle=n,
            cov_epoch_period=cov,
        )

        # BJD is within ~0.005 days of UTC mid. Pre-filter cycles outside window
        if bjd_mid < (start_bjd - 0.05) or bjd_mid > (end_bjd + 0.05):
            continue

        utc_mid = bjd_to_utc(
            bjd_tdb=bjd_mid,
            ra_deg=ra_deg,
            dec_deg=dec_deg,
            lat_deg=observer_lat_deg,
            lon_deg=observer_lon_deg,
        )

        if start_utc <= utc_mid <= end_utc:
            duration_h = fit.duration_h or 2.5
            half_dur = timedelta(hours=duration_h / 2.0)
            transits.append(
                {
                    "cycle_number": n,
                    "bjd_mid_tdb": bjd_mid,
                    "utc_mid": utc_mid,
                    "timing_uncertainty_min": sigma_d * 1440.0,
                    "duration_h": duration_h,
                    "transit_start_utc": utc_mid - half_dur,
                    "transit_end_utc": utc_mid + half_dur,
                    "is_coherent": True,
                    "solution_name": fit.fit_model,
                }
            )

    return transits
