from __future__ import annotations

import math
import os
from typing import Any

from mcp.server import MCPServer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.services.catalog import CatalogService
from exoplanet_db.services.reconciliation import angular_distance_arcsec

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///exoplanet_db.sqlite")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

mcp = MCPServer(
    name="exoplanet-db",
    instructions="Agent tools for querying vetted exoplanets, candidate dossiers, reproducibility receipts, and transit signals.",
)


@mcp.tool(name="lookup_target")
async def lookup_target(identifier: str) -> dict[str, Any]:
    """
    Search for an astronomical host star by TIC ID, Gaia DR3 ID, or star name (e.g. 'WASP-12' or '464547401').
    Returns host star details and all known transit signals and candidates.
    """
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


@mcp.tool(name="get_candidate_dossier")
async def get_candidate_dossier(candidate_id: str) -> dict[str, Any]:
    """
    Get full scientific candidate dossier, ephemeris parameters, host star, and evidence limits.
    """
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
    async with AsyncSessionLocal() as session:
        service = CatalogService(session)
        snapshots = await service.list_published_snapshots()

        # Query candidates within approximate coordinate bounding box
        deg_radius = cone_radius_arcsec / 3600.0
        cos_dec = max(0.01, math.cos(math.radians(dec_deg)))
        ra_delta = deg_radius / cos_dec

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

                    # Check direct or harmonic match
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
