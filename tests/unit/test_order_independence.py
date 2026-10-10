from __future__ import annotations

from exoplanet_db.services.reconciliation import check_signal_match


def test_order_independent_signal_association():
    report_a = {
        "period_d": 13.24438,
        "period_err": 0.00003,
        "epoch_bjd": 2458665.030,
        "epoch_err": 0.005,
    }
    report_b = {
        "period_d": 13.24439,
        "period_err": 0.00004,
        "epoch_bjd": 2458665.034,
        "epoch_err": 0.006,
    }

    # Match A against B
    match_ab = check_signal_match(
        period1_d=report_a["period_d"],
        period1_err=report_a["period_err"],
        epoch1_bjd=report_a["epoch_bjd"],
        epoch1_err=report_a["epoch_err"],
        period2_d=report_b["period_d"],
        period2_err=report_b["period_err"],
        epoch2_bjd=report_b["epoch_bjd"],
        epoch2_err=report_b["epoch_err"],
    )

    # Match B against A
    match_ba = check_signal_match(
        period1_d=report_b["period_d"],
        period1_err=report_b["period_err"],
        epoch1_bjd=report_b["epoch_bjd"],
        epoch1_err=report_b["epoch_err"],
        period2_d=report_a["period_d"],
        period2_err=report_a["period_err"],
        epoch2_bjd=report_a["epoch_bjd"],
        epoch2_err=report_a["epoch_err"],
    )

    assert match_ab["match_type"] == "identical_signal"
    assert match_ba["match_type"] == "identical_signal"
    assert abs(match_ab["phase_overlap"] - match_ba["phase_overlap"]) < 1e-6
