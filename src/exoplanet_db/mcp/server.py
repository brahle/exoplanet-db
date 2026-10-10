from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any

import httpx
from mcp.server import MCPServer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.services.catalog import CatalogService
from exoplanet_db.services.reconciliation import angular_distance_arcsec

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
                return {"found": False, "message": f"No stellar system found matching '{identifier}'"}

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


@mcp.tool(name="check_novelty")
async def check_novelty(
    ra_deg: float,
    dec_deg: float,
    period_d: float,
    cone_radius_arcsec: float = 30.0,
    period_tolerance_percent: float = 1.0,
) -> dict[str, Any]:
    """
    Checks novelty of a candidate signal against published snapshots.
    Strictly returns an evidentiary snapshot (never a boolean):
    - Lists all checked source snapshots with their timestamps and commit SHAs
    - Lists near-matches (targets within cone radius, period harmonics P/2, 2P, 3P)
    - Lists known unindexed sources (e.g. arXiv preprints).
    """
    deg_radius = cone_radius_arcsec / 3600.0
    cos_dec = max(0.01, math.cos(math.radians(dec_deg)))
    ra_delta = deg_radius / cos_dec

    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            service = CatalogService(session)
            snapshots = await service.list_published_snapshots()

            targets, _ = await service.list_systems(
                ra_min=max(0.0, ra_deg - ra_delta),
                ra_max=min(360.0, ra_deg + ra_delta),
                dec_min=max(-90.0, dec_deg - deg_radius),
                dec_max=min(90.0, dec_deg + deg_radius),
                limit=50,
            )

            near_matches = []
            for t in targets:
                dist = angular_distance_arcsec(ra_deg, dec_deg, t.ra_deg, t.dec_deg)
                if dist <= cone_radius_arcsec:
                    for s in t.signals:
                        ratio = s.primary_period_d / period_d
                        p_diff_pct = abs(s.primary_period_d - period_d) / period_d * 100.0

                        is_period_match = p_diff_pct <= period_tolerance_percent
                        is_harmonic = any(abs(ratio - h) / h < 0.01 for h in [0.5, 2.0, 3.0, 0.333333])

                        if is_period_match or is_harmonic:
                            near_matches.append(
                                {
                                    "candidate_id": s.id,
                                    "target_name": t.primary_name,
                                    "tic_id": t.tic_id,
                                    "angular_separation_arcsec": round(dist, 2),
                                    "catalog_period_d": s.primary_period_d,
                                    "period_ratio": round(ratio, 4),
                                    "match_reason": "period_match"
                                    if is_period_match
                                    else "harmonic_alias",
                                    "disposition": s.current_disposition,
                                    "limits": s.scientific_limits,
                                }
                            )

            checked_sources = [
                {
                    "source": snap.source_name,
                    "snapshot_utc": snap.snapshot_utc.isoformat(),
                    "commit_sha": snap.commit_sha,
                    "record_count": snap.record_count,
                }
                for snap in snapshots
            ]

            status = "no_match_in_checked_sources" if not near_matches else "near_matches_found"

            return {
                "status": status,
                "checked_sources": checked_sources,
                "known_unindexed_sources": [
                    "arxiv_astro_ph_ep_recent_preprints",
                    "zenodo_community_unregistered_dossiers",
                ],
                "near_matches": near_matches,
                "epistemic_warning": (
                    "Absence of matches in checked sources does not establish scientific discovery priority. "
                    "Unindexed preprints, literature back-catalogues, or uncalibrated stellar blend systematics may exist."
                ),
            }

    async with httpx.AsyncClient(base_url=EXOPLANET_API_URL, timeout=15.0) as client:
        snaps_resp = await client.get("/v1/snapshots")
        snapshots_data = snaps_resp.json() if snaps_resp.status_code == 200 else []

        targets_resp = await client.get(
            "/v1/systems",
            params={
                "ra_min": max(0.0, ra_deg - ra_delta),
                "ra_max": min(360.0, ra_deg + ra_delta),
                "dec_min": max(-90.0, dec_deg - deg_radius),
                "dec_max": min(90.0, dec_deg + deg_radius),
                "limit": 50,
            },
        )
        targets_items = targets_resp.json().get("items", []) if targets_resp.status_code == 200 else []

        near_matches = []
        for t in targets_items:
            dist = angular_distance_arcsec(ra_deg, dec_deg, t["ra_deg"], t["dec_deg"])
            if dist <= cone_radius_arcsec:
                sys_resp = await client.get(f"/v1/systems/{t['id']}")
                if sys_resp.status_code == 200:
                    candidates = sys_resp.json().get("candidates", [])
                    for s in candidates:
                        p = s["period_d"]
                        ratio = p / period_d
                        p_diff_pct = abs(p - period_d) / period_d * 100.0
                        is_period_match = p_diff_pct <= period_tolerance_percent
                        is_harmonic = any(abs(ratio - h) / h < 0.01 for h in [0.5, 2.0, 3.0, 0.333333])
                        if is_period_match or is_harmonic:
                            near_matches.append(
                                {
                                    "candidate_id": s["id"],
                                    "target_name": t["primary_name"],
                                    "tic_id": t.get("tic_id"),
                                    "angular_separation_arcsec": round(dist, 2),
                                    "catalog_period_d": p,
                                    "period_ratio": round(ratio, 4),
                                    "match_reason": "period_match"
                                    if is_period_match
                                    else "harmonic_alias",
                                    "disposition": s["current_disposition"],
                                    "limits": s.get("scientific_limits", []),
                                }
                            )

        checked_sources = [
            {
                "source": snap.get("source_name"),
                "snapshot_utc": snap.get("snapshot_utc"),
                "commit_sha": snap.get("commit_sha"),
                "record_count": snap.get("record_count"),
            }
            for snap in snapshots_data
        ]
        status = "no_match_in_checked_sources" if not near_matches else "near_matches_found"
        return {
            "status": status,
            "checked_sources": checked_sources,
            "known_unindexed_sources": [
                "arxiv_astro_ph_ep_recent_preprints",
                "zenodo_community_unregistered_dossiers",
            ],
            "near_matches": near_matches,
            "epistemic_warning": (
                "Absence of matches in checked sources does not establish scientific discovery priority. "
                "Unindexed preprints, literature back-catalogues, or uncalibrated stellar blend systematics may exist."
            ),
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
            items = await service.get_observation_queue(
                lat_deg=lat,
                lon_deg=lon,
                start_time=datetime.fromisoformat(start_time_iso),
                end_time=datetime.fromisoformat(end_time_iso),
                min_priority=min_priority,
                limit=limit,
            )
            return {"total": len(items), "items": items}

    async with httpx.AsyncClient(base_url=EXOPLANET_API_URL, timeout=15.0) as client:
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
