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


def test_eumemic_discard_lowers_score_and_is_not_support():
    # Our dossier signal that eumemic investigated and called a likely nearby false positive
    e = Evidence(
        disposition="false_positive",
        sources={"git:brahle/exoplanet-candidates", "eumemic/exoplanets"},
        eumemic_discards={"LIKELY NEARBY FP": ""},
    )
    value, factors = score(e)
    assert value == 1  # 20 - 30, clamped; not the hard 0 of a confirmed false positive
    labels = [f["label"] for f in factors]
    assert any("nearby star" in label for label in labels)
    assert not any("independent" in label for label in labels)

    # A TOI candidate keeps its TOI base; the discard and failed vetting are penalties
    toi = Evidence(
        disposition="planet_candidate",
        sources={"nasa_toi", "eumemic/exoplanets", "eumemic/exoplanets:vetting"},
        toi_dispositions={"PC"},
        eumemic_discards={"C": "grazing"},
        vetting_fails={"odd-even transit differences"},
    )
    value, factors = score(toi)
    assert value == 60 - 15 - 10
    assert any(f["label"].endswith("(grazing)") for f in factors)

    # Counted in any release outweighs a discard elsewhere
    counted = Evidence(
        disposition="planet_candidate",
        sources={"eumemic/exoplanets"},
        eumemic_tiers={"B"},
        eumemic_discards={"C": "MES < 10"},
    )
    assert score(counted)[0] == 45


def test_eumemic_only_discard_has_its_own_base():
    e = Evidence(
        disposition="unclassified_transit_like_signal",
        sources={"eumemic/exoplanets"},
        eumemic_discards={"L": ""},
    )
    assert score(e)[0] == 20


def test_dossier_vetting_warnings_clean_screen_and_density():
    git = {"git:brahle/exoplanet-candidates"}
    warned, factors = score(
        Evidence(
            "unclassified_transit_like_signal",
            sources=git,
            dossier_warnings={"off_target_diagnostic", "elevated_ruwe"},
        )
    )
    assert warned == 1  # 20 - 5 - 20, floored
    assert [f["delta"] for f in factors] == [20, -5, -20]

    clean, factors = score(
        Evidence("unclassified_transit_like_signal", sources=git, dossier_clean_screen=True)
    )
    assert clean == 25 and "screen clean" in factors[-1]["label"]

    dense, factors = score(
        Evidence("unclassified_transit_like_signal", sources=git, density_tension_widths=16.2)
    )
    assert dense == 5 and "stellar density" in factors[-1]["label"]
    assert (
        score(
            Evidence("unclassified_transit_like_signal", sources=git, density_tension_widths=0.3)
        )[0]
        == 20
    )


def test_external_adverse_report_not_double_counted_with_eumemic_verdict():
    both = Evidence(
        "false_positive",
        sources={"git:brahle/exoplanet-candidates", "eumemic/exoplanets"},
        eumemic_discards={"C": "TRICERATOPS FPP >= 0.5"},
        dossier_warnings={"external_adverse_report"},
    )
    _, factors = score(both)
    assert not any("Adverse report" in f["label"] for f in factors)
    alone = Evidence(
        "unclassified_transit_like_signal",
        sources={"git:brahle/exoplanet-candidates"},
        dossier_warnings={"external_adverse_report"},
    )
    assert score(alone)[0] == 10
