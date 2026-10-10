from __future__ import annotations

import math
from typing import Any


def propagate_coordinates(
    ra_deg: float,
    dec_deg: float,
    pm_ra_mas_yr: float | None,
    pm_dec_mas_yr: float | None,
    from_epoch_year: float,
    to_epoch_year: float,
) -> tuple[float, float]:
    """
    Propagates coordinates from from_epoch_year to to_epoch_year using proper motion.
    pm_ra_mas_yr is mu_alpha * cos(dec) in milliarcseconds per year.
    pm_dec_mas_yr is mu_delta in milliarcseconds per year.
    """
    if pm_ra_mas_yr is None or pm_dec_mas_yr is None or from_epoch_year == to_epoch_year:
        return ra_deg, dec_deg

    dt_yr = to_epoch_year - from_epoch_year
    # Convert mas to degrees
    mas_to_deg = 1.0 / 3_600_000.0

    dec_rad = math.radians(dec_deg)
    cos_dec = math.cos(dec_rad)
    if abs(cos_dec) < 1e-7:
        cos_dec = 1e-7

    delta_ra_deg = (pm_ra_mas_yr * dt_yr * mas_to_deg) / cos_dec
    delta_dec_deg = pm_dec_mas_yr * dt_yr * mas_to_deg

    new_ra = (ra_deg + delta_ra_deg) % 360.0
    new_dec = max(-90.0, min(90.0, dec_deg + delta_dec_deg))
    return new_ra, new_dec


def angular_distance_arcsec(
    ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float
) -> float:
    """Computes angular distance on the sphere in arcseconds using Haversine formula."""
    r1, d1 = math.radians(ra1_deg), math.radians(dec1_deg)
    r2, d2 = math.radians(ra2_deg), math.radians(dec2_deg)
    d_ra = r2 - r1
    d_dec = d2 - d1

    a = math.sin(d_dec / 2.0) ** 2 + math.cos(d1) * math.cos(d2) * math.sin(d_ra / 2.0) ** 2
    a = min(1.0, max(0.0, a))
    dist_rad = 2.0 * math.asin(math.sqrt(a))
    return math.degrees(dist_rad) * 3600.0


def check_signal_match(
    period1_d: float,
    period1_err: float | None,
    epoch1_bjd: float,
    epoch1_err: float | None,
    period2_d: float,
    period2_err: float | None,
    epoch2_bjd: float,
    epoch2_err: float | None,
    duration1_h: float | None = None,
    duration2_h: float | None = None,
) -> dict[str, Any]:
    """
    Evaluates whether two candidate signals on the same host star represent:
    - 'identical_signal': agreement within 3-sigma combined error + phase overlap > 50%
    - 'harmonic_alias': ratio near 0.5, 2.0, 3.0, 1/3 within 1%
    - 'distinct_signal': different period/phase
    """
    # 1. Check harmonic ratios first
    ratio = period2_d / period1_d
    harmonics = [0.333333, 0.5, 2.0, 3.0]
    for h in harmonics:
        if abs(ratio - h) / h < 0.01:
            return {
                "match_type": "harmonic_alias",
                "harmonic_ratio": ratio,
                "notes": f"Period ratio {ratio:.4f} is within 1% of harmonic {h}",
            }

    # 2. Check period match
    p_err1 = period1_err if (period1_err and period1_err > 0) else period1_d * 1e-4
    p_err2 = period2_err if (period2_err and period2_err > 0) else period2_d * 1e-4
    comb_p_err = math.sqrt(p_err1**2 + p_err2**2)
    p_diff = abs(period1_d - period2_d)
    sigma_p = p_diff / comb_p_err if comb_p_err > 0 else 999.0

    if sigma_p > 3.0 and (p_diff / period1_d) > 0.005:
        return {"match_type": "distinct_signal", "sigma_period": sigma_p}

    # 3. Check epoch phase difference
    # Phase difference between epoch 1 and epoch 2 relative to period
    avg_p = (period1_d + period2_d) / 2.0
    cycles = (epoch2_bjd - epoch1_bjd) / avg_p
    phase_diff = abs(cycles - round(cycles))  # in [0, 0.5]
    time_offset_d = phase_diff * avg_p

    e_err1 = epoch1_err if (epoch1_err and epoch1_err > 0) else 0.05
    e_err2 = epoch2_err if (epoch2_err and epoch2_err > 0) else 0.05
    comb_e_err = math.sqrt(e_err1**2 + e_err2**2)
    sigma_e = time_offset_d / comb_e_err if comb_e_err > 0 else 999.0

    # Phase overlap check using transit duration
    dur_h = duration1_h or duration2_h or 2.5
    dur_d = dur_h / 24.0
    # Overlap fraction of trapezoid/box window
    overlap = max(0.0, 1.0 - (time_offset_d / dur_d)) if dur_d > 0 else 0.0

    if (sigma_p <= 3.0 and sigma_e <= 3.0) or (sigma_p <= 3.0 and overlap >= 0.50):
        return {
            "match_type": "identical_signal",
            "sigma_period": sigma_p,
            "sigma_epoch": sigma_e,
            "phase_overlap": overlap,
        }

    return {
        "match_type": "distinct_signal",
        "sigma_period": sigma_p,
        "sigma_epoch": sigma_e,
        "phase_overlap": overlap,
    }
