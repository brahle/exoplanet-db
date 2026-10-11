"""Vectorised transit observation queue.

The previous implementation ran several astropy transforms per transit for every ephemeris
in the catalogue (~20k), synchronously on the event loop. This one:

- keeps one ephemeris per signal (the one with the smallest propagated timing error),
- enumerates every transit in the window with numpy,
- applies the barycentric light-travel correction in a single array call,
- evaluates Sun altitude, Moon position and sidereal time on a 5-minute grid with astropy
  and interpolates, and computes target altitude from the hour angle (coordinates precessed
  to the epoch of observation; refraction ignored, < 0.05 deg above 30 deg altitude),
- requires the whole transit (ingress, mid, egress) to be above the altitude limit and in
  darkness, not just mid-transit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import astropy.units as u
import numpy as np
from astropy.coordinates import FK5, AltAz, EarthLocation, SkyCoord, get_body, get_sun
from astropy.time import Time

from exoplanet_db.services.observability import compute_priority_score

GRID_STEP_D = 5.0 / 1440.0
MAX_TRANSITS_PER_SIGNAL = 400
DEFAULT_DURATION_H = 2.5
MAX_WINDOW_DAYS = 7.0


@dataclass(frozen=True)
class QueueSettings:
    lat_deg: float
    lon_deg: float
    start_utc: datetime
    end_utc: datetime
    height_m: float = 0.0
    min_altitude_deg: float = 30.0
    max_sun_altitude_deg: float = -18.0
    min_moon_separation_deg: float = 30.0
    # A transit predicted to ±1 day can't be scheduled; beyond this it is not "observable"
    max_timing_uncertainty_min: float = 120.0
    min_priority: float = 0.0
    observable_only: bool = True
    limit: int = 50


def _sigma_d(n: np.ndarray, s_t0: np.ndarray, s_p: np.ndarray, cov: np.ndarray) -> np.ndarray:
    var = s_t0**2 + (n**2) * s_p**2 + 2.0 * n * cov
    return np.sqrt(np.clip(var, 0.0, None))


def _best_fit_per_signal(rows: list[dict[str, Any]], mid_jd: float) -> list[dict[str, Any]]:
    """The ephemeris with the smallest propagated mid-time error at the window centre."""
    best: dict[str, tuple[float, dict[str, Any]]] = {}
    for r in rows:
        n = round((mid_jd - r["epoch_bjd"]) / r["period_d"])
        s_t0 = r["epoch_err_d"] or 0.0
        s_p = r["period_err_d"] or 0.0
        known = r["epoch_err_d"] is not None or r["period_err_d"] is not None
        sigma = math.sqrt(max(0.0, s_t0**2 + n * n * s_p**2 + 2 * n * (r["cov"] or 0.0)))
        score = sigma if known else math.inf  # unknown errors lose to any stated error
        current = best.get(r["signal_id"])
        if current is None or score < current[0]:
            best[r["signal_id"]] = (score, r)
    return [r for _, r in best.values()]


def compute_queue(rows: list[dict[str, Any]], s: QueueSettings) -> tuple[list[dict[str, Any]], int]:
    """Returns (items, total_matching_before_limit). CPU-bound; run it off the event loop."""
    if not rows:
        return [], 0
    location = EarthLocation.from_geodetic(
        lon=s.lon_deg * u.deg, lat=s.lat_deg * u.deg, height=s.height_m * u.m
    )
    t_start = Time(s.start_utc)
    t_end = Time(s.end_utc)
    start_jd, end_jd = t_start.utc.jd, t_end.utc.jd
    start_tdb, end_tdb = t_start.tdb.jd, t_end.tdb.jd

    sigs = _best_fit_per_signal(rows, 0.5 * (start_tdb + end_tdb))
    m = len(sigs)
    P = np.array([r["period_d"] for r in sigs])
    T0 = np.array([r["epoch_bjd"] for r in sigs])
    s_p = np.array([r["period_err_d"] or 0.0 for r in sigs])
    s_t0 = np.array([r["epoch_err_d"] or 0.0 for r in sigs])
    cov = np.array([r["cov"] or 0.0 for r in sigs])
    ra = np.array([r["ra_deg"] for r in sigs])
    dec = np.array([r["dec_deg"] for r in sigs])
    dur_known = np.array([bool(r["duration_h"] and r["duration_h"] > 0) for r in sigs])
    dur_h = np.where(dur_known, [r["duration_h"] or 0.0 for r in sigs], DEFAULT_DURATION_H)

    # 1. Every transit mid-time (BJD_TDB) in the window, with ±10 min margin for the
    #    barycentric correction (at most ~8.3 min)
    margin = 10.0 / 1440.0
    n_lo = np.ceil((start_tdb - margin - T0) / P).astype(np.int64)
    n_hi = np.floor((end_tdb + margin - T0) / P).astype(np.int64)
    counts = np.clip(n_hi - n_lo + 1, 0, MAX_TRANSITS_PER_SIGNAL)
    total_transits = int(counts.sum())
    if total_transits == 0:
        return [], 0
    idx = np.repeat(np.arange(m), counts)
    offsets = np.concatenate([[0], np.cumsum(counts)[:-1]])
    n = n_lo[idx] + (np.arange(total_transits) - offsets[idx])
    bjd = T0[idx] + n * P[idx]
    sigma_min = _sigma_d(n.astype(float), s_t0[idx], s_p[idx], cov[idx]) * 1440.0

    # 2. Barycentric -> topocentric (one array call)
    coords_icrs = SkyCoord(ra=ra[idx] * u.deg, dec=dec[idx] * u.deg, frame="icrs")
    t_bary = Time(bjd, format="jd", scale="tdb")
    ltt = t_bary.light_travel_time(coords_icrs, "barycentric", location=location)
    mid_utc = (t_bary - ltt).utc.jd
    in_window = (mid_utc >= start_jd) & (mid_utc <= end_jd)
    if not in_window.any():
        return [], 0
    idx, n, mid_utc, sigma_min, bjd = (
        idx[in_window],
        n[in_window],
        mid_utc[in_window],
        sigma_min[in_window],
        bjd[in_window],
    )
    half = dur_h[idx] / 48.0
    t1, t4 = mid_utc - half, mid_utc + half
    # Visibility is required over the transit padded by its 1-sigma timing uncertainty
    pad = np.minimum(sigma_min, s.max_timing_uncertainty_min) / 1440.0
    w1, w4 = t1 - pad, t4 + pad

    # 3. Sun, Moon and sidereal time on a grid covering every ingress/egress
    g0, g1 = (
        float(min(t1.min(), start_jd)) - GRID_STEP_D,
        float(max(t4.max(), end_jd)) + GRID_STEP_D,
    )
    grid = Time(np.arange(g0, g1 + GRID_STEP_D, GRID_STEP_D), format="jd", scale="utc")
    frame = AltAz(obstime=grid, location=location)
    sun_alt = get_sun(grid).transform_to(frame).alt.deg
    moon = get_body("moon", grid, location=location)
    moon_ra, moon_dec = np.radians(moon.ra.deg), np.radians(moon.dec.deg)
    moon_vec = np.stack(
        [np.cos(moon_dec) * np.cos(moon_ra), np.cos(moon_dec) * np.sin(moon_ra), np.sin(moon_dec)]
    )
    lst = np.unwrap(grid.sidereal_time("apparent", longitude=s.lon_deg * u.deg).radian)
    gx = grid.jd

    # Target coordinates of date (precession dominates: ~0.36 deg between J2000 and 2026)
    of_date = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs").transform_to(
        FK5(equinox=Time(0.5 * (start_jd + end_jd), format="jd", scale="utc"))
    )
    ra_d, dec_d = of_date.ra.radian[idx], of_date.dec.radian[idx]
    phi = math.radians(s.lat_deg)

    def altitude(t: np.ndarray) -> np.ndarray:
        ha = np.interp(t, gx, lst) - ra_d
        return np.degrees(
            np.arcsin(np.sin(phi) * np.sin(dec_d) + np.cos(phi) * np.cos(dec_d) * np.cos(ha))
        )

    alt1, alt_mid, alt4 = altitude(w1), altitude(mid_utc), altitude(w4)
    sun1, sun_mid, sun4 = (np.interp(t, gx, sun_alt) for t in (w1, mid_utc, w4))
    mv = np.stack([np.interp(mid_utc, gx, moon_vec[k]) for k in range(3)])
    mv /= np.linalg.norm(mv, axis=0)
    tv = np.stack([np.cos(dec_d) * np.cos(ra_d), np.cos(dec_d) * np.sin(ra_d), np.sin(dec_d)])
    moon_sep = np.degrees(np.arccos(np.clip((mv * tv).sum(axis=0), -1.0, 1.0)))

    min_alt = np.minimum(np.minimum(alt1, alt_mid), alt4)
    max_sun = np.maximum(np.maximum(sun1, sun_mid), sun4)
    timing_ok = sigma_min <= s.max_timing_uncertainty_min
    observable = (
        timing_ok
        & (min_alt >= s.min_altitude_deg)
        & (max_sun <= s.max_sun_altitude_deg)
        & (moon_sep >= s.min_moon_separation_deg)
    )

    def to_dt(jd: float) -> datetime:
        return Time(jd, format="jd", scale="utc").to_datetime(timezone=UTC)

    items = []
    for k in range(len(idx)):
        if s.observable_only and not observable[k]:
            continue
        r = sigs[idx[k]]
        score = compute_priority_score(
            disposition=r["disposition"],
            timing_uncertainty_min=float(sigma_min[k]),
            depth_ppm=r["depth_ppm"] or 1000.0,
            duration_h=float(dur_h[idx[k]]),
            is_observable=bool(observable[k]),
            is_coherent=True,
            credibility=r["planet_score"] / 100.0 if r.get("planet_score") is not None else None,
        )
        if score < s.min_priority:
            continue
        z = 90.0 - float(alt_mid[k])
        items.append(
            {
                "signal_id": r["signal_id"],
                "target_id": r["target_id"],
                "target_name": r["target_name"],
                "signal_name": r["signal_name"],
                "current_disposition": r["disposition"],
                "transit_mid_utc": float(mid_utc[k]),
                "transit_start_utc": float(t1[k]),
                "transit_end_utc": float(t4[k]),
                "window_start_utc": float(w1[k]),
                "window_end_utc": float(w4[k]),
                "cycle_number": int(n[k]),
                "bjd_mid_tdb": float(bjd[k]),
                "duration_h": float(dur_h[idx[k]]),
                "duration_assumed": not bool(dur_known[idx[k]]),
                "depth_ppm": r["depth_ppm"],
                "timing_uncertainty_min": round(float(sigma_min[k]), 2),
                "priority_score": score,
                "planet_score": r.get("planet_score"),
                "target_altitude_deg": round(float(alt_mid[k]), 2),
                "altitude_ingress_deg": round(float(alt1[k]), 2),
                "altitude_egress_deg": round(float(alt4[k]), 2),
                "target_airmass": round(1.0 / math.cos(math.radians(z)), 2)
                if alt_mid[k] > 5
                else 99.0,
                "sun_altitude_deg": round(float(max_sun[k]), 2),
                "moon_separation_deg": round(float(moon_sep[k]), 2),
                "is_observable": bool(observable[k]),
                "fit_model": r["fit_model"],
            }
        )
    items.sort(key=lambda it: (-it["priority_score"], it["transit_mid_utc"]))
    total = len(items)
    items = items[: s.limit]
    for it in items:
        for key in (
            "transit_mid_utc",
            "transit_start_utc",
            "transit_end_utc",
            "window_start_utc",
            "window_end_utc",
        ):
            it[key] = to_dt(it[key])
    return items, total
