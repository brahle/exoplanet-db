"""Novelty evidence: catalogued signals, eclipsing binaries and variable stars near a
position at the same period or a harmonic.

Shared by the MCP tools and the REST endpoints. Never returns a boolean verdict.
"""

from __future__ import annotations

from typing import Any

from exoplanet_db.services import variables
from exoplanet_db.services.catalog import CatalogService

HARMONIC_RATIOS = (0.25, 1 / 3, 0.5, 2.0, 3.0, 4.0)
SAME_STAR_ARCSEC = 2.5
MAX_CONE_ARCSEC = 3600.0
# A TESS pixel is 21 arcsec and contaminating light reaches about 2 arcmin
DEFAULT_CONE_ARCSEC = 120.0
KNOWN_UNINDEXED_SOURCES = [
    "arxiv_astro_ph_ep_recent_preprints",
    "zenodo_community_unregistered_dossiers",
]
NOVELTY_WARNING = (
    "Absence of matches in checked sources does not establish scientific discovery priority. "
    "Unindexed preprints, literature back-catalogues, or uncalibrated stellar blend systematics may exist."
)


def cone_items_from_db(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for h in hits:
        t = h["target"]
        items.append(
            {
                "id": t.id,
                "primary_name": t.primary_name,
                "tic_id": t.tic_id,
                "separation_arcsec": h["separation_arcsec"],
                "signals": [
                    {
                        "id": s.id,
                        "display_name": s.display_name,
                        "period_d": s.primary_period_d if (s.primary_period_d or 0) > 0 else None,
                        "current_disposition": s.current_disposition,
                        "planet_score": s.planet_score,
                        "sources": sorted({r.source_name for r in s.reports}),
                        "source_keys": sorted({r.source_key for r in s.reports}),
                    }
                    for s in t.signals
                ],
            }
        )
    return items


def near_matches(
    items: list[dict[str, Any]],
    period_d: float,
    period_tolerance_percent: float,
    harmonic_tolerance_percent: float,
) -> list[dict[str, Any]]:
    matches = []
    for t in items:
        sep = t["separation_arcsec"]
        for s in t["signals"]:
            p = s.get("period_d")
            base = {
                "candidate_id": s["id"],
                "display_name": s["display_name"],
                "target_name": t["primary_name"],
                "tic_id": t.get("tic_id"),
                "angular_separation_arcsec": round(sep, 2),
                "disposition": s["current_disposition"],
                "planet_score": s.get("planet_score"),
                "sources": s.get("sources", []),
                "source_keys": s.get("source_keys", []),
            }
            if not p:
                # No period to compare (single transits, imaging): only relevant on the same star
                if sep <= SAME_STAR_ARCSEC:
                    matches.append(
                        {
                            **base,
                            "catalog_period_d": None,
                            "period_ratio": None,
                            "match_reason": "same_star_period_unknown",
                        }
                    )
                continue
            ratio = p / period_d
            if abs(p - period_d) / period_d * 100.0 <= period_tolerance_percent:
                reason = "period_match"
            elif any(
                abs(ratio - h) / h * 100.0 <= harmonic_tolerance_percent for h in HARMONIC_RATIOS
            ):
                reason = "harmonic_alias"
            else:
                continue
            matches.append(
                {
                    **base,
                    "catalog_period_d": p,
                    "period_ratio": round(ratio, 4),
                    "match_reason": reason,
                }
            )
    matches.sort(
        key=lambda m: (m["match_reason"] != "period_match", m["angular_separation_arcsec"])
    )
    return matches


def evidence(
    items: list[dict[str, Any]],
    period_d: float,
    period_tolerance_percent: float,
    harmonic_tolerance_percent: float,
    variable_matches: list[dict[str, Any]] | None = None,
    variable_stars_searched: int = 0,
) -> dict[str, Any]:
    """status: near_matches_found (a catalogued signal), else variable_star_match (an
    eclipsing binary / variable at the period, or an eclipsing binary on the same star),
    else no_match_in_checked_sources."""
    near = near_matches(items, period_d, period_tolerance_percent, harmonic_tolerance_percent)
    variable_matches = variable_matches or []
    status = (
        "near_matches_found"
        if near
        else "variable_star_match"
        if variable_matches
        else "no_match_in_checked_sources"
    )
    return {
        "status": status,
        "cone_targets_searched": len(items),
        "near_matches": near,
        "variable_stars_searched": variable_stars_searched,
        "variable_matches": variable_matches,
    }


async def checked_sources(service: CatalogService) -> list[dict[str, Any]]:
    """Signal catalogues and the variable-star catalogues, each with its coverage."""
    return serialise_sources(
        await service.get_source_status() + await variables.catalogue_status(service.session)
    )


def serialise_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **src,
            "last_ingested_utc": src["last_ingested_utc"].isoformat()
            if src.get("last_ingested_utc") and not isinstance(src["last_ingested_utc"], str)
            else src.get("last_ingested_utc"),
        }
        for src in sources
    ]


async def check_many(
    service: CatalogService,
    queries: list[dict[str, Any]],
    cone_radius_arcsec: float = DEFAULT_CONE_ARCSEC,
    period_tolerance_percent: float = 1.0,
    harmonic_tolerance_percent: float = 1.0,
) -> dict[str, Any]:
    """Novelty evidence for many signals in one pass; checked_sources is reported once.

    Each query is {ra_deg, dec_deg, period_d, label?}. A query with a non-positive period
    gets an "error" entry instead of evidence, so one bad row never sinks the batch.
    """
    radius = min(float(cone_radius_arcsec), MAX_CONE_ARCSEC)
    results = []
    for i, q in enumerate(queries):
        label = q.get("label") if q.get("label") is not None else str(i)
        period = q.get("period_d")
        if not period or period <= 0:
            results.append({"label": label, "error": "period_d must be positive"})
            continue
        ra, dec = float(q["ra_deg"]) % 360.0, float(q["dec_deg"])
        items = cone_items_from_db(await service.cone_search(ra, dec, radius))
        var_hits = await variables.cone(service.session, ra, dec, radius)
        var_matches = variables.match(
            var_hits, period, period_tolerance_percent, harmonic_tolerance_percent
        )
        results.append(
            {
                "label": label,
                **evidence(
                    items,
                    period,
                    period_tolerance_percent,
                    harmonic_tolerance_percent,
                    var_matches,
                    len(var_hits),
                ),
            }
        )
    return {
        "query": {
            "cone_radius_arcsec": radius,
            "period_tolerance_percent": period_tolerance_percent,
            "harmonic_tolerance_percent": harmonic_tolerance_percent,
        },
        "checked_sources": await checked_sources(service),
        "known_unindexed_sources": KNOWN_UNINDEXED_SOURCES,
        "epistemic_warning": NOVELTY_WARNING,
        "results": results,
    }
