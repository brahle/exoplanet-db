from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx
from mcp.server import MCPServer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.services.catalog import CatalogService

DATABASE_URL = os.getenv("DATABASE_URL")
EXOPLANET_API_URL = os.getenv("EXOPLANET_API_URL", "https://exoplanets.brahle.com").rstrip("/")

AsyncSessionLocal = None
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(DATABASE_URL, echo=False)
    AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

mcp = MCPServer(
    name="exoplanet-db",
    instructions=(
        "Agent tools for querying vetted exoplanets, candidate dossiers, reproducibility receipts, "
        "and transit signals via the public read-only Exoplanet DB API."
    ),
)


@mcp.tool(name="lookup_target")
async def lookup_target(identifier: str) -> dict[str, Any]:
    """
    Search for an astronomical host star by TIC ID, Gaia DR3 ID, or star name (e.g. 'WASP-12' or '464547401').
    Returns host star details and all known transit signals and candidates.
    """
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            service = CatalogService(session)
            targets, _ = await service.list_systems(search=identifier, limit=10)
            if not targets:
                return {
                    "found": False,
                    "message": f"No stellar system found matching '{identifier}'",
                }

            results = []
            for t in targets:
                target_data = await service.get_system_by_id(t.id)
                if not target_data:
                    continue
                results.append(
                    {
                        "id": target_data.id,
                        "primary_name": target_data.primary_name,
                        "tic_id": target_data.tic_id,
                        "gaia_dr3_id": target_data.gaia_dr3_id,
                        "coordinates": {
                            "ra_deg": target_data.ra_deg,
                            "dec_deg": target_data.dec_deg,
                            "epoch": target_data.epoch,
                        },
                        "v_mag": target_data.v_mag,
                        "teff_k": target_data.teff_k,
                        "aliases": target_data.aliases,
                        "candidates": [
                            {
                                "id": s.id,
                                "display_name": s.display_name,
                                "period_d": s.primary_period_d,
                                "epoch_bjd": s.primary_epoch_bjd,
                                "depth_ppm": s.primary_depth_ppm,
                                "disposition": s.current_disposition,
                                "limits": s.scientific_limits,
                            }
                            for s in target_data.signals
                        ],
                    }
                )
            return {"found": True, "count": len(results), "systems": results}

    async with httpx.AsyncClient(base_url=EXOPLANET_API_URL, timeout=15.0) as client:
        resp = await client.get("/v1/systems", params={"search": identifier, "limit": 10})
        if resp.status_code != 200:
            return {"found": False, "message": f"API request error: status {resp.status_code}"}
        data = resp.json()
        items = data.get("items", [])
        if not items:
            return {"found": False, "message": f"No stellar system found matching '{identifier}'"}

        results = []
        for item in items:
            detail_resp = await client.get(f"/v1/systems/{item['id']}")
            if detail_resp.status_code != 200:
                continue
            detail = detail_resp.json()
            results.append(
                {
                    "id": detail["id"],
                    "primary_name": detail["primary_name"],
                    "tic_id": detail.get("tic_id"),
                    "gaia_dr3_id": detail.get("gaia_dr3_id"),
                    "coordinates": {
                        "ra_deg": detail["ra_deg"],
                        "dec_deg": detail["dec_deg"],
                        "epoch": "J2000",
                    },
                    "v_mag": detail.get("v_mag"),
                    "teff_k": detail.get("teff_k"),
                    "aliases": detail.get("aliases", []),
                    "candidates": [
                        {
                            "id": c["id"],
                            "display_name": c["display_name"],
                            "period_d": c["period_d"],
                            "epoch_bjd": c["epoch_bjd"],
                            "depth_ppm": c.get("depth_ppm"),
                            "disposition": c["current_disposition"],
                            "limits": c.get("scientific_limits", []),
                        }
                        for c in detail.get("candidates", [])
                    ],
                }
            )
        return {"found": True, "count": len(results), "systems": results}


@mcp.tool(name="search_signals")
async def search_signals(
    min_period_d: float | None = None,
    max_period_d: float | None = None,
    min_depth_ppm: float | None = None,
    disposition: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """
    Search and filter candidate transit signals by orbital period (days), transit depth (ppm), or scientific disposition.
    """
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            service = CatalogService(session)
            signals, total = await service.list_candidates(
                disposition=disposition,
                min_period_d=min_period_d,
                max_period_d=max_period_d,
                min_depth_ppm=min_depth_ppm,
                limit=limit,
            )
            return {
                "total": total,
                "returned": len(signals),
                "candidates": [
                    {
                        "id": s.id,
                        "display_name": s.display_name,
                        "target_name": s.target.primary_name if s.target else "Unknown",
                        "tic_id": s.target.tic_id if s.target else None,
                        "period_d": s.primary_period_d,
                        "depth_ppm": s.primary_depth_ppm,
                        "duration_h": s.primary_duration_h,
                        "disposition": s.current_disposition,
                        "limits": s.scientific_limits,
                    }
                    for s in signals
                ],
            }

    async with httpx.AsyncClient(base_url=EXOPLANET_API_URL, timeout=15.0) as client:
        params: dict[str, Any] = {"limit": limit}
        if min_period_d is not None:
            params["min_period_d"] = min_period_d
        if max_period_d is not None:
            params["max_period_d"] = max_period_d
        if min_depth_ppm is not None:
            params["min_depth_ppm"] = min_depth_ppm
        if disposition is not None:
            params["disposition"] = disposition

        resp = await client.get("/v1/candidates", params=params)
        if resp.status_code != 200:
            return {"total": 0, "returned": 0, "candidates": []}
        data = resp.json()
        items = data.get("items", [])
        return {
            "total": data.get("total", len(items)),
            "returned": len(items),
            "candidates": [
                {
                    "id": c["id"],
                    "display_name": c["display_name"],
                    "target_id": c.get("target_id"),
                    "period_d": c.get("period_d"),
                    "depth_ppm": c.get("depth_ppm"),
                    "duration_h": c.get("duration_h"),
                    "disposition": c.get("current_disposition"),
                    "limits": c.get("scientific_limits", []),
                }
                for c in items
            ],
        }


@mcp.tool(name="get_candidate_dossier")
async def get_candidate_dossier(candidate_id: str) -> dict[str, Any]:
    """
    Get full scientific candidate dossier, ephemeris parameters, host star, and evidence limits.
    """
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            service = CatalogService(session)
            signal = await service.get_candidate_by_id(candidate_id)
            if not signal:
                return {"found": False, "message": f"Candidate '{candidate_id}' not found"}

            return {
                "found": True,
                "id": signal.id,
                "display_name": signal.display_name,
                "host_star": {
                    "name": signal.target.primary_name,
                    "tic": signal.target.tic_id,
                    "gaia_dr3": signal.target.gaia_dr3_id,
                    "ra_deg": signal.target.ra_deg,
                    "dec_deg": signal.target.dec_deg,
                }
                if signal.target
                else None,
                "signal": {
                    "period_d": signal.primary_period_d,
                    "period_err_d": signal.primary_period_err_d,
                    "epoch_bjd": signal.primary_epoch_bjd,
                    "epoch_err_d": signal.primary_epoch_err_d,
                    "duration_h": signal.primary_duration_h,
                    "depth_ppm": signal.primary_depth_ppm,
                },
                "assessment": {
                    "disposition": signal.current_disposition,
                    "source_localisation": signal.source_localisation_status,
                    "timing_consistency": signal.timing_consistency_status,
                    "limits": signal.scientific_limits,
                },
                "harmonic_links": [
                    {
                        "harmonic_candidate_id": h.harmonic_signal_id,
                        "ratio": h.harmonic_ratio,
                        "notes": h.notes,
                    }
                    for h in signal.harmonics
                ],
            }

    async with httpx.AsyncClient(base_url=EXOPLANET_API_URL, timeout=15.0) as client:
        resp = await client.get(f"/v1/candidates/{candidate_id}")
        if resp.status_code != 200:
            return {"found": False, "message": f"Candidate '{candidate_id}' not found"}
        c = resp.json()
        host = c.get("host_star") or {}
        return {
            "found": True,
            "id": c["id"],
            "display_name": c["display_name"],
            "host_star": {
                "name": host.get("primary_name"),
                "tic": host.get("tic_id"),
                "gaia_dr3": host.get("gaia_dr3_id"),
                "ra_deg": host.get("ra_deg"),
                "dec_deg": host.get("dec_deg"),
            }
            if host
            else None,
            "signal": {
                "period_d": c.get("period_d"),
                "period_err_d": c.get("period_err_d"),
                "epoch_bjd": c.get("epoch_bjd"),
                "epoch_err_d": None,
                "duration_h": c.get("duration_h"),
                "depth_ppm": c.get("depth_ppm"),
            },
            "assessment": {
                "disposition": c.get("current_disposition"),
                "source_localisation": c.get("source_localisation_status"),
                "timing_consistency": c.get("timing_consistency_status"),
                "limits": c.get("scientific_limits", []),
            },
            "harmonic_links": [],
        }


HARMONIC_RATIOS = (0.25, 1 / 3, 0.5, 2.0, 3.0, 4.0)
SAME_STAR_ARCSEC = 2.5
KNOWN_UNINDEXED_SOURCES = [
    "arxiv_astro_ph_ep_recent_preprints",
    "zenodo_community_unregistered_dossiers",
]
NOVELTY_WARNING = (
    "Absence of matches in checked sources does not establish scientific discovery priority. "
    "Unindexed preprints, literature back-catalogues, or uncalibrated stellar blend systematics may exist."
)


def _cone_items_from_db(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                        "sources": sorted({r.source_name for r in s.reports}),
                        "source_keys": sorted({r.source_key for r in s.reports}),
                    }
                    for s in t.signals
                ],
            }
        )
    return items


def _near_matches(
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


@mcp.tool(name="check_novelty")
async def check_novelty(
    ra_deg: float,
    dec_deg: float,
    period_d: float,
    cone_radius_arcsec: float = 30.0,
    period_tolerance_percent: float = 1.0,
    harmonic_tolerance_percent: float = 1.0,
) -> dict[str, Any]:
    """
    Checks a signal against every indexed catalogue. Returns evidence, never a boolean:
    - checked_sources: one entry per catalogue with its current coverage (reports and
      signals indexed, last ingest time, pinned commit for git-hosted sources)
    - near_matches: catalogued signals inside the cone at the same period or a harmonic
      (1/4, 1/3, 1/2, 2, 3, 4), with every catalogue that reports each one
    - cone_targets_searched: all catalogued targets inside the cone (searched exhaustively,
      including across RA 0/360)
    cone_radius_arcsec is capped at 3600.
    """
    if not period_d or period_d <= 0:
        raise ValueError("period_d must be positive")
    radius = min(float(cone_radius_arcsec), 3600.0)

    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            service = CatalogService(session)
            items = _cone_items_from_db(await service.cone_search(ra_deg, dec_deg, radius))
            sources = [
                {
                    **src,
                    "last_ingested_utc": src["last_ingested_utc"].isoformat()
                    if src["last_ingested_utc"]
                    else None,
                }
                for src in await service.get_source_status()
            ]
    else:
        async with httpx.AsyncClient(base_url=EXOPLANET_API_URL, timeout=30.0) as client:
            resp = await client.get(
                "/v1/cone",
                params={"ra_deg": ra_deg % 360.0, "dec_deg": dec_deg, "radius_arcsec": radius},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data["items"]
            sources = data["sources"]

    near = _near_matches(items, period_d, period_tolerance_percent, harmonic_tolerance_percent)
    return {
        "status": "near_matches_found" if near else "no_match_in_checked_sources",
        "query": {
            "ra_deg": ra_deg,
            "dec_deg": dec_deg,
            "period_d": period_d,
            "cone_radius_arcsec": radius,
            "period_tolerance_percent": period_tolerance_percent,
            "harmonic_tolerance_percent": harmonic_tolerance_percent,
        },
        "checked_sources": sources,
        "cone_targets_searched": len(items),
        "near_matches": near,
        "known_unindexed_sources": KNOWN_UNINDEXED_SOURCES,
        "epistemic_warning": NOVELTY_WARNING,
    }


@mcp.tool(name="get_observation_queue")
async def get_observation_queue(
    lat: float,
    lon: float,
    start_time_iso: str,
    end_time_iso: str,
    min_priority: float = 0.0,
    limit: int = 20,
) -> dict[str, Any]:
    """
    Get prioritized real-time topocentric observation queue for ground telescopes.
    Filters by transit observability (darkness, minimum altitude, airmass, Moon separation).
    """
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            service = CatalogService(session)
            result = await service.get_observation_queue(
                lat_deg=lat,
                lon_deg=lon,
                start_time=datetime.fromisoformat(start_time_iso.replace("Z", "+00:00")),
                end_time=datetime.fromisoformat(end_time_iso.replace("Z", "+00:00")),
                min_priority=min_priority,
                limit=limit,
            )
            for it in result["items"]:
                for key in ("transit_mid_utc", "transit_start_utc", "transit_end_utc"):
                    it[key] = it[key].isoformat()
            return result

    async with httpx.AsyncClient(base_url=EXOPLANET_API_URL, timeout=90.0) as client:
        resp = await client.get(
            "/v1/observations/queue",
            params={
                "lat": lat,
                "lon": lon,
                "start_time": start_time_iso,
                "end_time": end_time_iso,
                "min_priority": min_priority,
                "limit": limit,
            },
        )
        if resp.status_code != 200:
            return {"total": 0, "items": []}
        return resp.json()


@mcp.tool(name="get_catalog_stats")
async def get_catalog_stats() -> dict[str, Any]:
    """
    Get current summary statistics of the Exoplanet DB catalog:
    total signals/planets, confirmed planets, transit candidates, and stellar host systems.
    """
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            service = CatalogService(session)
            return await service.get_stats()

    async with httpx.AsyncClient(base_url=EXOPLANET_API_URL, timeout=15.0) as client:
        resp = await client.get("/v1/stats")
        if resp.status_code != 200:
            return {"error": f"Failed to fetch stats: HTTP {resp.status_code}"}
        return resp.json()


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
