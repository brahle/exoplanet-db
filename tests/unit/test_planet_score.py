from __future__ import annotations

from exoplanet_db.services.planet_score import Evidence, score


def test_confirmed_and_false_positive_anchor_the_scale():
    assert score(Evidence("confirmed_planet", sources={"nasa_pscomppars"}))[0] == 100
    assert (
        score(Evidence("confirmed_planet", sources={"nasa_toi"}, toi_dispositions={"CP"}))[0] == 95
    )
    assert score(Evidence("false_positive", sources={"nasa_toi"}, toi_dispositions={"FP"}))[0] == 0


def test_toi_candidate_with_support_and_multiplicity():
    value, factors = score(
        Evidence(
            "planet_candidate",
            sources={"nasa_toi", "exofop_ctoi"},
            toi_dispositions={"PC"},
            other_candidates_on_star=2,
        )
    )
    assert value == 60 + 5 + 10
    assert [f["delta"] for f in factors] == [60, 5, 10]


def test_eclipsing_binary_red_flag_and_fpp():
    big, factors = score(
        Evidence(
            "planet_candidate",
            sources={"nasa_toi"},
            toi_dispositions={"PC"},
            depth_ppm=60_000,
            star_radius_rsun=1.0,
        )
    )
    assert big == 30 and "too large" in factors[-1]["label"]
    validated, _ = score(
        Evidence(
            "planet_candidate",
            sources={"eumemic/exoplanets"},
            eumemic_tiers={"A"},
            eumemic_fpp=0.01,
        )
    )
    assert validated == 65


def test_dossier_and_contradictions_stay_in_range():
    value, _ = score(
        Evidence(
            "unclassified_transit_like_signal",
            sources={"git:brahle/exoplanet-candidates"},
            localisation="contradicted",
            timing="contradicted",
        )
    )
    assert value == 1  # clamped, never 0 unless classified false positive
