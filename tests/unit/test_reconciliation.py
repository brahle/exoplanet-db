from __future__ import annotations

import pytest

from exoplanet_db.services.reconciliation import (
    angular_distance_arcsec,
    check_signal_match,
    propagate_coordinates,
)


def test_pi_mensae_proper_motion_propagation():
    """
    Test high-proper-motion star Pi Mensae (HD 39091, TIC 261136674).
    Pi Men has proper motion ~ 1.09 arcsec/yr.
    Between J2000 (TIC epoch) and J2016.0 (Gaia DR3 epoch) is 16 years,
    giving ~ 17.5 arcseconds of proper motion.
    Without proper motion propagation, Gaia DR3 and TIC positions do not match within 15 arcseconds.
    With proper motion propagation to a common epoch, they match within 0.1 arcseconds!
    """
    # TIC J2000 coordinates
    tic_ra_2000 = 84.29114
    tic_dec_2000 = -80.46912
    pm_ra = 311.3  # mas/yr
    pm_dec = 1049.5  # mas/yr

    # Compute unpropagated distance across 16 years
    gaia_ra_2016, gaia_dec_2016 = propagate_coordinates(
        tic_ra_2000, tic_dec_2000, pm_ra, pm_dec, 2000.0, 2016.0
    )
    raw_distance = angular_distance_arcsec(tic_ra_2000, tic_dec_2000, gaia_ra_2016, gaia_dec_2016)
    assert raw_distance > 15.0  # Would fall out of standard 15" cone!

    # Propagate Gaia position back to J2000
    gaia_ra_at_2000, gaia_dec_at_2000 = propagate_coordinates(
        gaia_ra_2016, gaia_dec_2016, pm_ra, pm_dec, 2016.0, 2000.0
    )
    reconciled_distance = angular_distance_arcsec(
        tic_ra_2000, tic_dec_2000, gaia_ra_at_2000, gaia_dec_at_2000
    )
    assert reconciled_distance < 0.05  # Within 0.05 arcseconds!


def test_signal_matching_identical():
    # Signal A and Signal B on same star with slightly different fits
    # Period: 13.24438 d vs 13.24439 d (within 3 sigma)
    # Epoch: 1665.030 d vs 1665.035 d
    result = check_signal_match(
        period1_d=13.24438,
        period1_err=0.00003,
        epoch1_bjd=2458665.030,
        epoch1_err=0.005,
        period2_d=13.24439,
        period2_err=0.00003,
        epoch2_bjd=2458665.033,
        epoch2_err=0.005,
        duration1_h=2.5,
        duration2_h=2.5,
    )
    assert result["match_type"] == "identical_signal"
    assert result["phase_overlap"] > 0.5


def test_signal_matching_multi_planet_distinct():
    # Host star with two distinct planets (e.g. 3.5-day and 13.2-day)
    result = check_signal_match(
        period1_d=3.5412,
        period1_err=0.0001,
        epoch1_bjd=2458660.100,
        epoch1_err=0.005,
        period2_d=13.24438,
        period2_err=0.00003,
        epoch2_bjd=2458665.030,
        epoch2_err=0.005,
    )
    assert result["match_type"] == "distinct_signal"


def test_signal_matching_harmonic_alias():
    # Period 2 is double period 1 (e.g. single-transit vs true period)
    result = check_signal_match(
        period1_d=6.62219,
        period1_err=0.00002,
        epoch1_bjd=2458665.030,
        epoch1_err=0.005,
        period2_d=13.24438,
        period2_err=0.00003,
        epoch2_bjd=2458665.030,
        epoch2_err=0.005,
    )
    assert result["match_type"] == "harmonic_alias"
    assert result["harmonic_ratio"] == pytest.approx(2.0, rel=1e-3)
