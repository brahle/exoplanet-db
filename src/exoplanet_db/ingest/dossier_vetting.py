"""Vetting evidence published next to exoplanet-candidates dossiers.

Each dossier may carry vetting.json (progress, warnings and the dated scientific assessment),
evidence/vetting-findings.json (the numbers behind those warnings), evidence/external-report.json
(a matching report from another catalogue) and the repository may hold reviews/*.json
(supplementary physical-fit reviews). They are condensed into one summary stored on the
dossier report as raw_payload["_vetting"], which scoring and the API read.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VETTING_KEY = "_vetting"
LIMIT_PREFIX = "exoplanet-candidates vetting"

WARNING_LABELS = {
    "odd_even": "odd and even transits differ in depth",
    "phase_half_secondary": "possible secondary eclipse at phase 0.5",
    "nine_phase_scan_uncorrected": "dip at another phase in the nine-phase scan (uncorrected)",
    "binary_catalogue_match": "catalogued binary nearby at this period or an alias",
    "elevated_ruwe": "elevated Gaia RUWE (possible unresolved companion)",
    "external_adverse_report": "adverse report from another catalogue",
    "off_target_diagnostic": "difference images place the dip off the target",
}

# vetting.json scientific_assessment.status -> our disposition
ASSESSMENT_DISPOSITIONS = {
    "unclassified": "unclassified_transit_like_signal",
    "candidate": "planet_candidate",
    "likely_planet": "planet_candidate",
    "false_positive": "false_positive",
    "validated_planet": "validated_planet",
    "confirmed_planet": "confirmed_planet",
    "known_signal": "known_signal",
}

# A fitted stellar density this many declared prior widths from the catalogue star's
DENSITY_TENSION_WIDTHS = 3.0


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def _review_kind(entry: dict[str, Any]) -> str:
    if "conditional_median_log_density_difference_in_declared_prior_widths" in entry:
        return "physical_fit_review"  # sampled conditional chains
    if "point_density_ratio_to_assumed_prior_center" in entry:
        return "point_fit_review"  # bounded point refits (exposure-integration review)
    if "fixed_vectors" in entry or str(entry.get("status", "")).startswith("replay"):
        return "replay"
    return "other_review"


_SIGNAL_ID = re.compile(r"tic-[0-9]+-s[0-9]{2}")
MENTIONED = "mentioned_in"


def load_reviews(repo_dir: Path) -> dict[str, dict[str, Any]]:
    """Supplementary reviews (reviews/*.json) per signal id.

    Per-signal entries ({"signals": [{"signal_id": ...}]}) are kept latest per kind:
    physical_fit_review (sampled chains), point_fit_review (point refits), replay (a
    fresh-environment replay certificate) and other_review. Every review file, whatever its
    shape (many are batch-level summaries), is also listed under MENTIONED for each signal id
    it names, so a new kind of review is visible rather than dropped. A file that cannot be
    read is skipped: supplementary reviews never block the dossier ingest.
    """
    out: dict[str, dict[str, Any]] = {}
    reviews = repo_dir / "reviews"
    if not reviews.is_dir():
        return out
    for path in sorted(reviews.glob("*.json")):
        rel = f"reviews/{path.name}"
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, ValueError) as exc:
            logger.warning("Skipping unreadable review %s: %s", rel, exc)
            continue
        for sid in sorted(set(_SIGNAL_ID.findall(text))):
            out.setdefault(sid, {}).setdefault(MENTIONED, []).append(rel)
        entries = data.get("signals") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            sid = entry.get("signal_id") if isinstance(entry, dict) else None
            if not isinstance(sid, str):
                continue
            kind = _review_kind(entry)
            review = {
                "file": rel,
                "reviewed_utc": data.get("reviewed_utc") or data.get("assessment_date"),
                "entry": entry,
            }
            current = out.setdefault(sid, {}).get(kind)
            if current is None or str(review["reviewed_utc"] or "") >= str(
                current["reviewed_utc"] or ""
            ):
                out[sid][kind] = review
    return out


def _median(q: dict[str, Any], key: str) -> float | None:
    v = q.get(key)
    return v[1] if isinstance(v, list) and len(v) == 3 else None


def _physical_fit(review: dict[str, Any]) -> dict[str, Any]:
    e = review["entry"]
    q = e.get("conditional_draw_quantiles_16_50_84") or {}
    bounds = e.get("prior_boundary_draw_fractions") or {}
    return {
        "file": review["file"],
        "reviewed_utc": review["reviewed_utc"],
        "scenario": (e.get("scenario") or {}).get("name"),
        "sampling": (e.get("sampling") or {}).get("status"),
        "radius_ratio": _median(q, "radius_ratio"),
        "impact_parameter": _median(q, "impact_parameter"),
        "a_over_rstar": _median(q, "a_over_rstar"),
        "density_ratio_to_catalogue": e.get("conditional_median_density_ratio_to_prior_center"),
        "density_tension_prior_widths": e.get(
            "conditional_median_log_density_difference_in_declared_prior_widths"
        ),
        "draws_at_prior_bound": {k: v for k, v in bounds.items() if (v or 0) >= 0.1} or None,
    }


def _point_fit(review: dict[str, Any], replay: dict[str, Any] | None) -> dict[str, Any]:
    e = review["entry"]
    return {
        "file": review["file"],
        "reviewed_utc": review["reviewed_utc"],
        "density_ratio_to_catalogue": e.get("point_density_ratio_to_assumed_prior_center"),
        "optimizer_successes": e.get("optimizer_successes"),
        "optimizer_starts": e.get("optimizer_starts"),
        "numerically_stable": e.get("all_reviewed_vectors_within_numerical_triggers"),
        "physical_model_qualified": e.get("physical_model_qualified"),
        "replay": (replay or {}).get("entry", {}).get("status"),
        "replay_file": (replay or {}).get("file"),
    }


def _reviews_summary(reviews: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "physical_fit_review" in reviews:
        out["physical_fit_review"] = _physical_fit(reviews["physical_fit_review"])
    if "point_fit_review" in reviews:
        out["point_fit_review"] = _point_fit(reviews["point_fit_review"], reviews.get("replay"))
    elif "replay" in reviews:
        out["replay"] = {
            "file": reviews["replay"]["file"],
            "status": reviews["replay"]["entry"].get("status"),
        }
    summarised = {r["file"] for k, r in reviews.items() if k != MENTIONED}
    others = [f for f in reviews.get(MENTIONED, []) if f not in summarised]
    if "other_review" in reviews:
        others = sorted({*others, reviews["other_review"]["file"]})
    if others:
        out["other_reviews"] = others
    return out


def _offsets(findings: dict[str, Any]) -> list[float] | None:
    values = [
        o["target_offset_pixels"]
        for o in findings.get("source_fit_offsets") or []
        if o.get("fit_success")
        and not o.get("bound_hit")
        and not o.get("blur_pixels")
        and isinstance(o.get("target_offset_pixels"), int | float)
    ]
    return [round(min(values), 2), round(max(values), 2)] if values else None


def summarise(
    vetting: dict[str, Any] | None,
    findings: dict[str, Any] | None,
    external: dict[str, Any] | None,
    reviews: dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """One compact, JSON-ready view of every vetting file published for a dossier."""
    if not (vetting or findings or external or reviews):
        return None
    vetting = vetting or {}
    findings = findings or {}
    progress = vetting.get("progress") or {}
    assessment = vetting.get("scientific_assessment") or {}
    screen = vetting.get("initial_screen") or {}
    out: dict[str, Any] = {
        "assessed_utc": vetting.get("assessed_utc") or findings.get("assessed_utc"),
        "stage": progress.get("stage"),
        "stages": progress.get("stages"),
        "assessment": assessment.get("status"),
        "basis": assessment.get("basis"),
        "initial_screen": screen.get("outcome"),
        "all_checks_available": screen.get("all_checks_available"),
        "warnings": list(vetting.get("warnings") or []),
        "open_requirements": list(vetting.get("open_requirements") or []),
    }
    lc = findings.get("lightcurve") or {}
    if lc:
        out["lightcurve"] = {
            "odd_even_contrast": lc.get("odd_even_nominal_contrast_multiple"),
            "phase_half_secondary_depth": lc.get("phase_half_secondary_nominal_depth_multiple"),
            "flags": {k: v for k, v in (lc.get("flags") or {}).items() if v},
        }
    cat = findings.get("catalogues") or {}
    if cat:
        out["catalogues"] = {
            "ruwe": cat.get("ruwe"),
            "binary_matches": [
                {
                    "name": (m.get("raw_record") or {}).get("Name")
                    or (m.get("raw_record") or {}).get("source_id"),
                    "catalogue": m.get("catalogue"),
                    "separation_arcsec": m.get("separation_arcsec"),
                    "period_d": m.get("period_d"),
                    "period_ratios": m.get("period_ratios"),
                }
                for m in (cat.get("matches") or [])
                if m.get("warning")
            ][:5],
        }
    src = findings.get("source_diagnostics")
    if src:
        out["source_diagnostics"] = {
            "status": src.get("scientific_status"),
            "sectors": [s.get("sector") for s in src.get("sectors") or []],
            "target_offset_pixels": _offsets(findings),
        }
    pilot = findings.get("physical_pilot") or []
    if pilot:
        out["physical_pilot"] = {
            "scenarios": len(pilot),
            "sampling_qualified": sum(
                1 for p in pilot if p.get("sampling_status") == "sampling_qualified"
            ),
        }
    if external:
        rec = external.get("source_record") or {}
        rep = external.get("reported_evidence") or {}
        out["external_report"] = {
            "source_file": rec.get("source_file"),
            "tier": rep.get("tier") or (rec.get("reported_disposition") or {}).get("tier"),
            "reason": rep.get("c_reason") or None,
            "FPP": rep.get("FPP") or None,
            "NFPP": rep.get("NFPP") or None,
            "centres_agree": external.get("nominal_centres_agree"),
        }
    out.update(_reviews_summary(reviews or {}))
    return out


def read_dossier(
    dossier_dir: Path, reviews: dict[str, dict[str, Any]] | None, sealed: set[str]
) -> dict[str, Any] | None:
    """Summary of the vetting files in a dossier; only files its evidence manifest seals count."""

    def sealed_file(rel: str) -> dict[str, Any] | None:
        return _load(dossier_dir / rel) if rel in sealed else None

    return summarise(
        sealed_file("vetting.json"),
        sealed_file("evidence/vetting-findings.json"),
        sealed_file("evidence/external-report.json"),
        reviews,
    )


def disposition(summary: dict[str, Any] | None) -> str | None:
    return ASSESSMENT_DISPOSITIONS.get((summary or {}).get("assessment") or "")


def density_tension(summary: dict[str, Any] | None) -> float | None:
    review = (summary or {}).get("physical_fit_review") or {}
    if review.get("sampling") != "conditional_sampling_checks_passed":
        return None
    widths = review.get("density_tension_prior_widths")
    return widths if isinstance(widths, int | float) else None


def limit_lines(summary: dict[str, Any] | None) -> list[str]:
    """Plain-language lines for scientific_limits; all start with LIMIT_PREFIX."""
    if not summary:
        return []
    date = (summary.get("assessed_utc") or "")[:10]
    head = f"{LIMIT_PREFIX} {date}".strip()
    stage = (summary.get("stage") or "").replace("_", " ")
    screen = {
        "warnings": "warnings",
        "no_initial_warnings": "no warnings",
        "incomplete": "incomplete",
    }.get(summary.get("initial_screen") or "", summary.get("initial_screen"))
    lines = [f"{head}: stage {stage or 'unknown'}; initial light-curve/catalogue screen {screen}"]
    warnings = [WARNING_LABELS.get(w, w.replace("_", " ")) for w in summary.get("warnings") or []]
    if warnings:
        lines.append(f"{head} warnings: " + "; ".join(warnings))
    offsets = (summary.get("source_diagnostics") or {}).get("target_offset_pixels")
    if offsets and "off_target_diagnostic" in (summary.get("warnings") or []):
        lines.append(
            f"{head}: difference-image source {offsets[0]}–{offsets[1]} px from the target"
        )
    pilot = summary.get("physical_pilot")
    if pilot:
        lines.append(
            f"{head}: physical pilot {pilot['sampling_qualified']} of {pilot['scenarios']} "
            "scenarios passed sampling qualification"
        )
    widths = density_tension(summary)
    ratio = (summary.get("physical_fit_review") or {}).get("density_ratio_to_catalogue")
    if widths is not None and isinstance(ratio, int | float):
        tension = "; in tension with a planet transiting the catalogue star"
        lines.append(
            f"{head} physical-fit review: fitted stellar density {ratio:.3g}× the catalogue "
            f"star's ({widths:.1f} prior widths)"
            + (tension if widths > DENSITY_TENSION_WIDTHS else "")
        )
    point = summary.get("point_fit_review") or {}
    if isinstance(point.get("density_ratio_to_catalogue"), int | float):
        replay = {"replay_passed": "; fresh-environment replay passed"}.get(
            point.get("replay") or "", f"; replay {point['replay']}" if point.get("replay") else ""
        )
        lines.append(
            f"{head} point-fit review: stellar density "
            f"{point['density_ratio_to_catalogue']:.3g}\u00d7 the catalogue star's at the point "
            f"fit ({point.get('optimizer_successes')}/{point.get('optimizer_starts')} optimizer "
            f"starts converged{replay})"
        )
    return lines


def strip_lines(limits: list[str] | None) -> list[str]:
    return [line for line in limits or [] if not line.startswith(LIMIT_PREFIX)]


def github_url(repo: str, sha: str, dossier: str) -> str:
    return (
        f"https://github.com/{repo}/blob/{sha}/candidates/{dossier}/evidence/vetting-findings.json"
    )
