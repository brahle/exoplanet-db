from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from exoplanet_db.models.entities import EphemerisFit
from exoplanet_db.services.ephemeris import (
    CoherentEphemerisRequiredError,
    bjd_to_utc,
    find_upcoming_transits,
    propagate_ephemeris_cycle,
)
from exoplanet_db.services.observability import (
    calculate_observability,
    compute_priority_score,
)


def test_ephemeris_covariance_monte_carlo():
    """
    Validates analytic ephemeris propagation with covariance against empirical
    Monte Carlo sampling from a bivariate normal distribution.
    Verifies that empirical std(T_N) matches analytic sigma_T(N) within 2%,
    explicitly testing both positive and negative covariance cases.
    """
    # Negative covariance case
    sigma_t0 = 0.005  # days
    sigma_p = 0.0001  # days
    rho = -0.6
    cov = rho * sigma_t0 * sigma_p
    n_cycle = 120
    period_d = 3.52474859
    epoch_bjd = 2455000.0

    cov_matrix = np.array([[sigma_t0**2, cov], [cov, sigma_p**2]])
    mean = np.array([epoch_bjd, period_d])

    rng = np.random.default_rng(seed=42)
    samples = rng.multivariate_normal(mean, cov_matrix, size=50_000)
    t0_samples = samples[:, 0]
    p_samples = samples[:, 1]

    tn_samples = t0_samples + n_cycle * p_samples
    empirical_std = float(np.std(tn_samples, ddof=1))

    _, analytic_sigma = propagate_ephemeris_cycle(
        period_d=period_d,
        period_err_d=sigma_p,
        epoch_bjd_tdb=epoch_bjd,
        epoch_err_d=sigma_t0,
        n_cycle=n_cycle,
        cov_epoch_period=cov,
    )

    rel_diff = abs(empirical_std - analytic_sigma) / analytic_sigma
    assert rel_diff < 0.02, (
        f"Empirical std ({empirical_std:.6f}) differs from analytic ({analytic_sigma:.6f}) by {rel_diff * 100:.2f}%"
    )

    # Positive covariance case
    rho_pos = 0.5
    cov_pos = rho_pos * sigma_t0 * sigma_p
    cov_matrix_pos = np.array([[sigma_t0**2, cov_pos], [cov_pos, sigma_p**2]])
    samples_pos = rng.multivariate_normal(mean, cov_matrix_pos, size=50_000)
    tn_samples_pos = samples_pos[:, 0] + n_cycle * samples_pos[:, 1]
    empirical_std_pos = float(np.std(tn_samples_pos, ddof=1))

    _, analytic_sigma_pos = propagate_ephemeris_cycle(
        period_d=period_d,
        period_err_d=sigma_p,
        epoch_bjd_tdb=epoch_bjd,
        epoch_err_d=sigma_t0,
        n_cycle=n_cycle,
        cov_epoch_period=cov_pos,
    )

    rel_diff_pos = abs(empirical_std_pos - analytic_sigma_pos) / analytic_sigma_pos
    assert rel_diff_pos < 0.02, (
        f"Positive covariance: empirical ({empirical_std_pos:.6f}) differs by {rel_diff_pos * 100:.2f}%"
    )


def test_bjd_to_utc_light_travel_time_regression():
    """
    Verifies topocentric barycentric inversion against WASP-12b coordinates.
    The Earth-to-Barycenter light-travel time correction for WASP-12 ranges up to ~8.3 min (499 s).
    Confirms bjd_to_utc properly accounts for this light-travel difference.
    """
    # WASP-12: RA = 06h 30m 32.79s (97.6366 deg), Dec = +29d 40m 20.3s (29.6723 deg)
    ra_wasp12 = 97.6366
    dec_wasp12 = 29.6723

    # On winter solstice (~Dec 21, JD 2459570.0), WASP-12 is near opposition/anti-Sun
    # Light travel time from Earth to Barycenter is significant (~7-8 minutes)
    bjd_target = 2459570.5  # BJD_TDB

    utc_dt = bjd_to_utc(
        bjd_tdb=bjd_target,
        ra_deg=ra_wasp12,
        dec_deg=dec_wasp12,
        lat_deg=45.8150,  # Zagreb / Marica reference lat
        lon_deg=15.9819,
    )

    # Convert returned UTC back to JD
    from astropy.time import Time

    time_obj = Time(utc_dt)
    utc_jd = time_obj.jd
    tdb_jd = time_obj.tdb.jd

    ltt_minutes = (bjd_target - tdb_jd) * 1440.0
    total_minutes = (bjd_target - utc_jd) * 1440.0

    # 1. Pure barycentric light-travel time (Roemer delay) must be bounded by 1 AU / c (~8.316 min)
    # For WASP-12 in late December (near opposition), it is ~8.11 minutes
    assert 7.0 <= abs(ltt_minutes) <= 8.32, (
        f"Expected pure light travel time between 7.0 and 8.32 minutes, got {abs(ltt_minutes):.2f} min"
    )

    # 2. Total BJD_TDB - JD_UTC offset includes Roemer delay plus ~69.18s TDB-UTC clock offset (~9.27 min)
    assert 8.0 <= abs(total_minutes) <= 9.6, (
        f"Expected total offset between 8.0 and 9.6 minutes, got {abs(total_minutes):.2f} min"
    )


def test_incoherent_ephemeris_rejected():
    """
    Verifies that Frankenstein composite parameters lacking coherence guarantee
    raise CoherentEphemerisRequiredError.
    """
    incoherent_fit = EphemerisFit(
        id="incoherent-fit-1",
        signal_id="sig-1",
        period_d=3.1415,
        epoch_bjd_tdb=2458000.0,
        is_coherent=False,
        fit_model="composite_frankenstein",
    )

    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 10, tzinfo=UTC)

    with pytest.raises(CoherentEphemerisRequiredError, match="marked incoherent"):
        find_upcoming_transits(
            fit=incoherent_fit,
            ra_deg=100.0,
            dec_deg=20.0,
            start_utc=start,
            end_utc=end,
        )


def test_observability_and_priority_scoring():
    """
    Tests observability metrics calculation (altitude, airmass, sun altitude, moon separation)
    and priority scoring logic.
    """
    # 1. Daytime / sun up test
    day_utc = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    obs_day = calculate_observability(
        ra_deg=97.6366,
        dec_deg=29.6723,
        time_utc=day_utc,
        lat_deg=45.8150,
        lon_deg=15.9819,
    )
    assert not obs_day["is_astronomical_night"]
    assert not obs_day["is_observable"]

    # Priority score for unobservable target must be 0.0
    score_unobs = compute_priority_score(
        disposition="planet_candidate",
        timing_uncertainty_min=15.0,
        depth_ppm=2500.0,
        duration_h=3.0,
        is_observable=False,
        is_coherent=True,
    )
    assert score_unobs == 0.0

    # 2. Priority score for false positive must be 0.0 even if observable
    score_fp = compute_priority_score(
        disposition="false_positive",
        timing_uncertainty_min=15.0,
        depth_ppm=2500.0,
        duration_h=3.0,
        is_observable=True,
        is_coherent=True,
    )
    assert score_fp == 0.0

    # 3. Incoherent ephemeris must score 0.0
    score_incoherent = compute_priority_score(
        disposition="planet_candidate",
        timing_uncertainty_min=15.0,
        depth_ppm=2500.0,
        duration_h=3.0,
        is_observable=True,
        is_coherent=False,
    )
    assert score_incoherent == 0.0

    # 4. Valid observable candidate score > 0.0
    score_candidate = compute_priority_score(
        disposition="planet_candidate",
        timing_uncertainty_min=45.0,
        depth_ppm=5000.0,
        duration_h=2.5,
        is_observable=True,
        is_coherent=True,
    )
    assert score_candidate > 0.0
