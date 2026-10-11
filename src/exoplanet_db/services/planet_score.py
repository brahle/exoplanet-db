"""Planet score: a 0-100 at-a-glance indication of how likely a signal is a real planet.

Heuristic, not a calibrated probability. It starts from the strongest classification any
source gives the signal and adjusts for independent support and red flags. Every step is
stored as a factor so the number can always be explained.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from exoplanet_db.models import Report, Signal, Target

R_SUN_IN_R_EARTH = 109.076

EUMEMIC_TIER_BASE = {"A": 55, "B": 45, "N": 35, "L1": 30}
# Signals eumemic investigated and did not count: (penalty, why)
EUMEMIC_DISCARD = {
    "LIKELY EB": (-40, "eumemic/exoplanets: likely eclipsing binary"),
    "LIKELY NEARBY FP": (-30, "eumemic/exoplanets: likely false positive from a nearby star"),
    "LIKELY FALSE ALARM": (-30, "eumemic/exoplanets: likely false alarm"),
    "C": (-15, "eumemic/exoplanets: tier C, not counted"),
    "L": (-10, "eumemic/exoplanets: tier L, not counted"),
    "UNTIERED": (-10, "eumemic/exoplanets: listed but not counted (neighbour within 15\u2033)"),
    "REPORTED": (0, "eumemic/exoplanets: already reported elsewhere"),
}
VETTING_SOURCE = "eumemic/exoplanets:vetting"
# Warnings published in exoplanet-candidates' own vetting (vetting.json): (penalty, why)
DOSSIER_WARNINGS = {
    "off_target_diagnostic": (-20, "Difference images place the dip off the target"),
    "binary_catalogue_match": (-10, "Catalogued binary nearby at this period or an alias"),
    "odd_even": (-10, "Odd and even transits differ in depth"),
    "phase_half_secondary": (-10, "Possible secondary eclipse at phase 0.5"),
    "elevated_ruwe": (-5, "Elevated Gaia RUWE (possible unresolved companion)"),
    "nine_phase_scan_uncorrected": (-3, "Dip at another phase in the nine-phase scan"),
    "external_adverse_report": (-10, "Adverse report from another catalogue"),
}
DENSITY_TENSION_WIDTHS = 3.0


@dataclass
class Evidence:
    disposition: str
    sources: set[str] = field(default_factory=set)
    toi_dispositions: set[str] = field(default_factory=set)  # TFOPWG codes: PC, APC, CP, KP, FP, FA
    eumemic_tiers: set[str] = field(default_factory=set)  # counted tiers only
    eumemic_discards: dict[str, str] = field(default_factory=dict)  # tier -> reason
    vetting_fails: set[str] = field(default_factory=set)  # failed LEO-Vetter tests
    eumemic_fpp: float | None = None
    ctoi_dispositions: set[str] = field(default_factory=set)
    localisation: str = "unresolved"
    timing: str = "unresolved"
    depth_ppm: float | None = None
    star_radius_rsun: float | None = None
    published_radius_earth: float | None = None
    other_candidates_on_star: int = 0
    # exoplanet-candidates vetting (report raw_payload["_vetting"])
    dossier_warnings: set[str] = field(default_factory=set)
    dossier_clean_screen: bool = False
    density_tension_widths: float | None = None


def _factor(label: str, delta: int) -> dict[str, Any]:
    return {"label": label, "delta": delta}


def score(e: Evidence) -> tuple[int, list[dict[str, Any]]]:
    factors: list[dict[str, Any]] = []

    # 1. Base: the strongest classification
    if "nasa_pscomppars" in e.sources or (
        e.disposition == "confirmed_planet" and not e.toi_dispositions
    ):
        return 100, [_factor("Confirmed planet in the NASA Exoplanet Archive", 100)]
    if e.toi_dispositions & {"CP", "KP"}:
        return 95, [_factor("TFOPWG lists it as a confirmed or known planet", 95)]
    if e.toi_dispositions & {"FP", "FA"} or (
        e.disposition == "false_positive" and not e.eumemic_discards
    ):
        return 0, [_factor("Classified as a false positive or false alarm", 0)]

    if e.toi_dispositions & {"PC"}:
        base, label = 60, "TESS Object of Interest, TFOPWG planet candidate"
    elif e.toi_dispositions & {"APC"}:
        base, label = 45, "TESS Object of Interest, ambiguous planet candidate"
    elif e.eumemic_tiers:
        tier = min(e.eumemic_tiers, key=lambda t: -EUMEMIC_TIER_BASE.get(t, 0))
        base, label = EUMEMIC_TIER_BASE.get(tier, 30), f"eumemic/exoplanets tier {tier} candidate"
    elif e.ctoi_dispositions:
        base, label = 40, "ExoFOP community TOI"
    elif any(s.startswith("git:") for s in e.sources):
        base, label = 20, "Provisional transit-like signal (exoplanet-candidates dossier)"
    elif e.eumemic_discards:
        base, label = 30, "Transit-like signal documented by eumemic/exoplanets"
    else:
        base, label = 30, "Catalogued candidate"
    factors.append(_factor(label, base))
    total = base

    # 2. Independent support
    # A catalogue that examined the signal and discarded it is not support
    supporting = {
        s
        for s in e.sources
        if s != VETTING_SOURCE and not (s == "eumemic/exoplanets" and not e.eumemic_tiers)
    }
    independent = len(
        {s for s in supporting if not s.startswith("git:")}
        | ({"git"} if any(s.startswith("git:") for s in supporting) else set())
    )
    if independent > 1:
        bonus = min(10, 5 * (independent - 1))
        factors.append(_factor(f"Reported by {independent} independent catalogues", bonus))
        total += bonus
    if e.other_candidates_on_star > 0:
        factors.append(_factor("Other planets or candidates on the same star (multiplicity)", 10))
        total += 10

    # 3. Published false-positive probability
    if e.eumemic_fpp is not None:
        if e.eumemic_fpp < 0.015:
            factors.append(
                _factor(f"TRICERATOPS FPP {e.eumemic_fpp:.3f} (below validation threshold)", 10)
            )
            total += 10
        elif e.eumemic_fpp > 0.5:
            factors.append(_factor(f"TRICERATOPS FPP {e.eumemic_fpp:.2f}", -15))
            total -= 15

    # 4. Investigated and discarded elsewhere (counted in any eumemic release outweighs this)
    if not e.eumemic_tiers:
        for tier, reason in sorted(e.eumemic_discards.items()):
            delta, label = EUMEMIC_DISCARD.get(
                tier, (-10, f"eumemic/exoplanets: tier {tier}, not counted")
            )
            if reason:
                label = f"{label} ({reason})"
            factors.append(_factor(label, delta))
            total += delta
    if e.vetting_fails:
        factors.append(
            _factor(
                "eumemic/exoplanets automated vetting failed: "
                + "; ".join(sorted(e.vetting_fails)),
                -10,
            )
        )
        total -= 10

    # 4b. exoplanet-candidates' own vetting
    for warning in sorted(e.dossier_warnings):
        delta, label = DOSSIER_WARNINGS.get(warning, (-5, warning.replace("_", " ")))
        if warning == "external_adverse_report" and e.eumemic_discards:
            continue  # the eumemic verdict itself is already scored above
        factors.append(_factor(f"exoplanet-candidates vetting: {label}", delta))
        total += delta
    if e.dossier_clean_screen and not e.dossier_warnings:
        factors.append(
            _factor(
                "exoplanet-candidates vetting: initial light-curve and catalogue screen clean", 5
            )
        )
        total += 5
    if e.density_tension_widths is not None and e.density_tension_widths > DENSITY_TENSION_WIDTHS:
        factors.append(
            _factor(
                "Physical-fit review: fitted stellar density "
                f"{e.density_tension_widths:.0f} prior widths from the catalogue star's",
                -15,
            )
        )
        total -= 15

    # 5. Red flags from the transit itself
    radius = e.published_radius_earth
    if radius is None and e.depth_ppm and e.star_radius_rsun and 0 < e.depth_ppm < 1e6:
        radius = e.star_radius_rsun * math.sqrt(e.depth_ppm / 1e6) * R_SUN_IN_R_EARTH
    if radius is not None:
        if radius > 25:
            factors.append(
                _factor(
                    f"Implied radius {radius:.0f} R⊕: too large for a planet (likely eclipsing binary)",
                    -30,
                )
            )
            total -= 30
        elif radius > 16:
            factors.append(
                _factor(f"Implied radius {radius:.0f} R⊕: larger than most known planets", -10)
            )
            total -= 10
    elif e.depth_ppm and e.depth_ppm > 50_000:
        factors.append(
            _factor(f"Depth {e.depth_ppm / 1e4:.0f}%: deeper than a planet usually makes", -15)
        )
        total -= 15

    for status, name, plus, minus in (
        (e.localisation, "Transit source localised on the target", 5, -30),
        (e.timing, "Transit timing consistent", 3, -20),
    ):
        if status == "supported":
            factors.append(_factor(name, plus))
            total += plus
        elif status == "contradicted":
            factors.append(
                _factor(
                    name.replace("localised on", "not localised on").replace(
                        "consistent", "inconsistent"
                    ),
                    minus,
                )
            )
            total += minus

    return max(1, min(99, total)), factors


async def score_signals(session: AsyncSession) -> dict[str, int]:
    """Recompute and store planet_score / score_factors for every signal."""
    signals = (
        await session.execute(
            select(
                Signal.id,
                Signal.target_id,
                Signal.current_disposition,
                Signal.source_localisation_status,
                Signal.timing_consistency_status,
                Signal.primary_depth_ppm,
                Target.radius_rsun,
            ).join(Target, Target.id == Signal.target_id)
        )
    ).all()
    ev: dict[str, Evidence] = {}
    by_target: dict[str, list[str]] = defaultdict(list)
    for sid, tid, disp, loc, timing, depth, rstar in signals:
        ev[sid] = Evidence(
            disposition=disp,
            localisation=loc or "unresolved",
            timing=timing or "unresolved",
            depth_ppm=depth,
            star_radius_rsun=rstar,
        )
        by_target[tid].append(sid)

    for sid, source, reported, raw in (
        await session.execute(
            select(
                Report.signal_id,
                Report.source_name,
                Report.reported_disposition,
                Report.raw_payload,
            )
        )
    ).all():
        e = ev.get(sid)
        if e is None:
            continue
        e.sources.add(source)
        raw = raw if isinstance(raw, dict) else {}
        if source == "nasa_toi":
            code = str(raw.get("tfopwg_disp") or "").strip().upper()
            if code:
                e.toi_dispositions.add(code)
        elif source == "eumemic/exoplanets":
            tier = str(raw.get("tier") or "").strip().upper()
            if tier in EUMEMIC_TIER_BASE:
                e.eumemic_tiers.add(tier)
            elif tier:
                e.eumemic_discards[tier] = str(raw.get("c_reason") or "").strip()
            # A discarded row's FPP is already the reason it was discarded (tier C)
            for key in ("FPP", "FPP_c") if tier in EUMEMIC_TIER_BASE else ():
                try:
                    fpp = float(raw.get(key))
                except (TypeError, ValueError):
                    continue
                if not math.isnan(fpp):
                    e.eumemic_fpp = fpp if e.eumemic_fpp is None else min(e.eumemic_fpp, fpp)
                    break
        elif source == VETTING_SOURCE:
            e.vetting_fails.update(raw.get("fails") or [])
        elif source.startswith("git:"):
            vet = raw.get("_vetting") if isinstance(raw.get("_vetting"), dict) else None
            if vet:
                e.dossier_warnings.update(vet.get("warnings") or [])
                e.dossier_clean_screen = vet.get(
                    "initial_screen"
                ) == "no_initial_warnings" and bool(vet.get("all_checks_available"))
                review = vet.get("physical_fit_review") or {}
                widths = review.get("density_tension_prior_widths")
                if review.get("sampling") == "conditional_sampling_checks_passed" and isinstance(
                    widths, int | float
                ):
                    e.density_tension_widths = widths
        elif source == "exofop_ctoi":
            e.ctoi_dispositions.add((reported or "PC").upper())
        elif source == "nasa_pscomppars":
            try:
                e.published_radius_earth = float(raw.get("pl_rade"))
            except (TypeError, ValueError):
                pass

    for ids in by_target.values():
        live = [i for i in ids if ev[i].disposition != "false_positive"]
        for i in ids:
            ev[i].other_candidates_on_star = len([j for j in live if j != i])

    rows = []
    histogram = {"0": 0, "1-29": 0, "30-59": 0, "60-89": 0, "90-100": 0}
    for sid, e in ev.items():
        value, factors = score(e)
        rows.append({"_id": sid, "planet_score": value, "score_factors": factors})
        bucket = (
            "0"
            if value == 0
            else "1-29"
            if value < 30
            else "30-59"
            if value < 60
            else "60-89"
            if value < 90
            else "90-100"
        )
        histogram[bucket] += 1
    from sqlalchemy import bindparam

    stmt = (
        update(Signal.__table__)
        .where(Signal.__table__.c.id == bindparam("_id"))
        .values(planet_score=bindparam("planet_score"), score_factors=bindparam("score_factors"))
    )
    for i in range(0, len(rows), 2000):
        await session.execute(stmt, rows[i : i + 2000])
    await session.commit()
    return histogram
