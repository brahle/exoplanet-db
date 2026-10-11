from __future__ import annotations

import functools
import inspect
import json
import os
from datetime import date, datetime
from typing import Any, Literal

import httpx
from mcp.server import MCPServer
from mcp.types import TextContent
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.mcp import compact as cx
from exoplanet_db.services import bulk, novelty
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
        "Read-only access to an exoplanet catalogue that merges the NASA Exoplanet Archive "
        "(confirmed planets, TOIs), ExoFOP community TOIs, eumemic/exoplanets and published "
        "candidate dossiers, de-duplicated into one record per signal with a 0-100 planet score "
        "(heuristic, not a probability).\n"
        "Batch tools: get_candidates (many ids), resolve_targets (many TIC/Gaia ids or names), "
        "check_novelty_bulk (many signals). Prefer them over calling a single-item tool in a loop.\n"
        'Every list tool takes format="compact" (the default for batch tools): results come as '
        '{"cols": [...], "rows": [[...]]} tables with 12-character id prefixes (accepted back by '
        "every tool), short codes decoded in a per-result legend, and rounded numbers. Use "
        'format="full" when you need every field with full ids.'
    ),
)

Format = Literal["compact", "full"]


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def tool(name: str):
    """Register fn as an MCP tool whose result is sent once, as minified JSON text.

    The library default sends every dict twice (structured content plus a 2-space indented
    text copy). The Python function itself is returned unchanged, so it still returns a dict.
    """

    def decorator(fn):
        @functools.wraps(fn)
        async def wire(*args, **kwargs):
            result = await fn(*args, **kwargs)
            return TextContent(
                type="text",
                text=json.dumps(
                    result, separators=(",", ":"), ensure_ascii=False, default=_json_default
                ),
            )

        # Resolved (not string) annotations: the schema is built outside this module
        signature = inspect.signature(fn, eval_str=True)
        wire.__signature__ = signature.replace(return_annotation=TextContent)
        wire.__annotations__ = {
            **{k: p.annotation for k, p in signature.parameters.items()},
            "return": TextContent,
        }
        mcp.add_tool(wire, name=name, description=inspect.getdoc(fn), structured_output=False)
        return fn

    return decorator


def _client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=EXOPLANET_API_URL, timeout=timeout)


def _legacy_target(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": t["id"],
        "primary_name": t["primary_name"],
        "tic_id": t.get("tic_id"),
        "gaia_dr3_id": t.get("gaia_dr3_id"),
        "coordinates": {"ra_deg": t["ra_deg"], "dec_deg": t["dec_deg"], "epoch": "J2000"},
        "v_mag": t.get("v_mag"),
        "teff_k": t.get("teff_k"),
        "aliases": t.get("aliases", []),
        "candidates": [
            {
                "id": s["id"],
                "display_name": s["display_name"],
                "period_d": s.get("period_d"),
                "epoch_bjd": s.get("epoch_bjd"),
                "duration_h": s.get("duration_h"),
                "depth_ppm": s.get("depth_ppm"),
                "disposition": s.get("disposition"),
                "planet_score": s.get("planet_score"),
                "sources": s.get("sources", []),
                "limits": s.get("limits", []),
            }
            for s in t.get("signals", [])
        ],
    }


def _signal_from_api(c: dict[str, Any]) -> dict[str, Any]:
    """REST CandidateSummary -> the shared signal record shape."""
    return {
        "id": c["id"],
        "display_name": c["display_name"],
        "target_id": c.get("target_id"),
        "target_name": c.get("target_name"),
        "tic_id": c.get("tic_id"),
        "ra_deg": c.get("ra_deg"),
        "dec_deg": c.get("dec_deg"),
        "period_d": c.get("period_d") if (c.get("period_d") or 0) > 0 else None,
        "period_err_d": c.get("period_err_d"),
        "epoch_bjd": c.get("epoch_bjd") if (c.get("epoch_bjd") or 0) > 2400000 else None,
        "epoch_err_d": c.get("epoch_err_d"),
        "duration_h": c.get("duration_h"),
        "depth_ppm": c.get("depth_ppm"),
        "disposition": c.get("current_disposition"),
        "planet_score": c.get("planet_score"),
        "localisation": c.get("source_localisation_status"),
        "timing": c.get("timing_consistency_status"),
        "sources": c.get("source_names") or [],
        "limits": c.get("scientific_limits", []),
    }


def _target_from_api(d: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            k: d.get(k)
            for k in ("id", "primary_name", "tic_id", "gaia_dr3_id", "ra_deg", "dec_deg")
        },
        **{k: d.get(k) for k in ("v_mag", "tess_mag", "teff_k", "radius_rsun", "mass_msun")},
        "aliases": d.get("aliases", []),
        "signals": [_signal_from_api(c) for c in d.get("candidates", [])],
    }


@tool("lookup_target")
async def lookup_target(identifier: str, format: Format = "full") -> dict[str, Any]:
    """
    Search for host stars whose name, TIC ID or Gaia DR3 ID contains the text (e.g. 'WASP-12'
    or '464547401'); up to 10 systems with all their signals. For exact lookups of many
    identifiers at once use resolve_targets.
    """
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            found, _ = await CatalogService(session).list_systems(search=identifier, limit=10)
            targets = await bulk.load_targets(session, [t.id for t in found])
    else:
        async with _client() as client:
            resp = await client.get("/v1/systems", params={"search": identifier, "limit": 10})
            if resp.status_code != 200:
                return {"found": False, "message": f"API request error: status {resp.status_code}"}
            targets = []
            for item in resp.json().get("items", []):
                detail = await client.get(f"/v1/systems/{item['id']}")
                if detail.status_code == 200:
                    targets.append(_target_from_api(detail.json()))
    if not targets:
        return {"found": False, "message": f"No stellar system found matching '{identifier}'"}
    if format == "compact":
        legend = cx.Legend()
        return {
            "found": True,
            "count": len(targets),
            **cx.targets_and_signals(targets, legend),
            "legend": legend.render(),
        }
    return {"found": True, "count": len(targets), "systems": [_legacy_target(t) for t in targets]}


@tool("search_signals")
async def search_signals(
    search: str | None = None,
    source: list[str] | None = None,
    disposition: list[str] | None = None,
    min_period_d: float | None = None,
    max_period_d: float | None = None,
    min_depth_ppm: float | None = None,
    max_depth_ppm: float | None = None,
    min_duration_h: float | None = None,
    max_duration_h: float | None = None,
    localisation: list[str] | None = None,
    timing: list[str] | None = None,
    min_score: int | None = None,
    max_score: int | None = None,
    sort: str = "period",
    limit: int = 20,
    offset: int = 0,
    format: Format = "full",
) -> dict[str, Any]:
    """
    Filter catalogued signals. Any combination of:
    - search: name, TIC/Gaia id or any source identifier (substring)
    - source (any of): nasa_pscomppars, nasa_toi, exofop_ctoi, eumemic/exoplanets,
      git:brahle/exoplanet-candidates
    - disposition (any of): confirmed_planet, validated_planet, planet_candidate,
      unclassified_transit_like_signal, false_positive
    - period (days), depth (ppm), duration (hours) and planet score (0-100) ranges; a period
      bound excludes signals whose period is unknown
    - localisation / timing (any of): supported, contradicted, unresolved
    sort: period, -period, depth, -depth, duration, -duration, score, -score, name.
    limit up to 500 (use offset to page); total is the full match count.
    """
    limit = max(1, min(int(limit), 500))
    filters: dict[str, Any] = {
        "search": search,
        "source": source,
        "disposition": disposition,
        "min_period_d": min_period_d,
        "max_period_d": max_period_d,
        "min_depth_ppm": min_depth_ppm,
        "max_depth_ppm": max_depth_ppm,
        "min_duration_h": min_duration_h,
        "max_duration_h": max_duration_h,
        "localisation": localisation,
        "timing": timing,
        "min_score": min_score,
        "max_score": max_score,
    }
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            found, total = await CatalogService(session).list_candidates(
                **filters, sort=sort, limit=limit, offset=offset
            )
            records = [bulk.signal_record(s) for s in found]
    else:
        params = {k: v for k, v in filters.items() if v is not None}
        records, total = [], 0
        async with _client() as client:
            while len(records) < limit:
                page = min(200, limit - len(records))
                resp = await client.get(
                    "/v1/candidates",
                    params={**params, "sort": sort, "limit": page, "offset": offset + len(records)},
                )
                if resp.status_code != 200:
                    return {"error": f"API request error: status {resp.status_code}"}
                data = resp.json()
                total = data.get("total", 0)
                records.extend(_signal_from_api(c) for c in data.get("items", []))
                if len(data.get("items", [])) < page:
                    break
    out: dict[str, Any] = {"total": total, "returned": len(records), "offset": offset}
    if format == "compact":
        legend = cx.Legend()
        return {**out, "signals": cx.signals(records, legend), "legend": legend.render()}
    return {**out, "candidates": records}


@tool("get_candidates")
async def get_candidates(ids: list[str], format: Format = "compact") -> dict[str, Any]:
    """
    Batch fetch up to 500 candidates by id in one call (full ids or unique prefixes of at least
    8 characters, e.g. the 12-character ids in compact results). Returns them in request order
    with ephemeris, depth, duration, disposition, planet score, localisation/timing status and
    every reporting catalogue. Unknown ids are listed in not_found; a prefix matching several
    candidates is listed in ambiguous with the full ids.
    """
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            result = await bulk.get_signals(session, ids)
    else:
        async with _client(60.0) as client:
            resp = await client.post("/v1/candidates/batch", json={"ids": ids[: bulk.MAX_BATCH]})
            resp.raise_for_status()
            result = resp.json()
    if format == "compact":
        legend = cx.Legend()
        out: dict[str, Any] = {"signals": cx.signals(result["items"], legend)}
        if result["not_found"]:
            out["not_found"] = result["not_found"]
        if result["ambiguous"]:
            out["ambiguous"] = result["ambiguous"]
        return {**out, "legend": legend.render()}
    return result


@tool("resolve_targets")
async def resolve_targets(identifiers: list[str], format: Format = "compact") -> dict[str, Any]:
    """
    Batch exact lookup of up to 500 identifiers in one call: TIC ids ('TIC 123' or '123'), Gaia
    DR3 ids, star names ('WASP-12'), signal names ('WASP-12 b'), any catalogue identifier
    ('TOI-1234.01', CTOI or eumemic keys) and TOI hosts ('TOI-1234'). Names match ignoring case,
    spaces, hyphens and underscores. Returns each identifier's host star(s) with all their
    signals, plus not_found. Each host is listed once even if several identifiers point at it.
    """
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            result = await bulk.resolve_targets(session, identifiers)
    else:
        async with _client(60.0) as client:
            resp = await client.post(
                "/v1/systems/resolve", json={"identifiers": identifiers[: bulk.MAX_BATCH]}
            )
            resp.raise_for_status()
            result = resp.json()
    if format == "compact":
        legend = cx.Legend()
        out: dict[str, Any] = {
            "resolved": cx.table(
                ["query", "by", "targets", "signals"],
                [
                    [
                        r["query"],
                        r["matched_by"],
                        [cx.sid(t) for t in r["target_ids"]],
                        [cx.sid(s) for s in r["signal_ids"]],
                    ]
                    for r in result["resolved"]
                ],
            ),
            **cx.targets_and_signals(result["targets"], legend),
        }
        if result["not_found"]:
            out["not_found"] = result["not_found"]
        return {**out, "legend": legend.render()}
    return result


@tool("get_candidate_dossier")
async def get_candidate_dossier(candidate_id: str) -> dict[str, Any]:
    """
    Full record for one candidate (full id or unique prefix of at least 8 characters): ephemeris
    with uncertainties, host star, disposition, planet score with the factors behind it,
    localisation/timing status, scientific limits and every source with a link. For
    exoplanet-candidates dossiers, "vetting" holds the published vetting progress, warnings,
    diagnostics, any matching external report and physical-fit review.
    """
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            service = CatalogService(session)
            if len(candidate_id) != 36:
                hit = await bulk.get_signals(session, [candidate_id])
                if hit["ambiguous"]:
                    return {"found": False, "ambiguous": hit["ambiguous"]}
                if not hit["items"]:
                    return {"found": False, "message": f"Candidate '{candidate_id}' not found"}
                candidate_id = hit["items"][0]["id"]
            detail = await service.get_candidate_detail(candidate_id)
            if not detail:
                return {"found": False, "message": f"Candidate '{candidate_id}' not found"}
            with_harmonics = await service.get_candidate_by_id(candidate_id)
            signal, target, planet = detail["signal"], detail["target"], detail["planet"]
            return {
                "found": True,
                "id": signal.id,
                "display_name": signal.display_name,
                "host_star": {
                    "name": target.primary_name,
                    "tic": target.tic_id,
                    "gaia_dr3": target.gaia_dr3_id,
                    "ra_deg": target.ra_deg,
                    "dec_deg": target.dec_deg,
                    **detail["star"],
                },
                "signal": {
                    "period_d": planet["period_d"],
                    "period_err_d": planet["period_err_d"],
                    "epoch_bjd": planet["epoch_bjd"],
                    "epoch_err_d": planet["epoch_err_d"],
                    "duration_h": planet["duration_h"],
                    "depth_ppm": planet["depth_ppm"],
                    "radius_earth": planet["radius_earth"],
                    "radius_basis": planet["radius_basis"],
                },
                "assessment": {
                    "disposition": signal.current_disposition,
                    "planet_score": signal.planet_score,
                    "score_factors": signal.score_factors or [],
                    "source_localisation": signal.source_localisation_status,
                    "timing_consistency": signal.timing_consistency_status,
                    "limits": signal.scientific_limits,
                },
                "sources": [
                    {
                        k: src[k]
                        for k in ("source_name", "source_key", "url", "reported_disposition")
                    }
                    for src in detail["sources"]
                ],
                "vetting": next(
                    (s["vetting"] for s in detail["sources"] if s.get("vetting")), None
                ),
                "harmonic_links": [
                    {
                        "harmonic_candidate_id": h.harmonic_signal_id,
                        "ratio": h.harmonic_ratio,
                        "notes": h.notes,
                    }
                    for h in (with_harmonics.harmonics if with_harmonics else [])
                ],
            }

    async with _client() as client:
        if len(candidate_id) != 36:
            hit = await client.post("/v1/candidates/batch", json={"ids": [candidate_id]})
            data = hit.json() if hit.status_code == 200 else {"items": [], "ambiguous": {}}
            if data.get("ambiguous"):
                return {"found": False, "ambiguous": data["ambiguous"]}
            if not data.get("items"):
                return {"found": False, "message": f"Candidate '{candidate_id}' not found"}
            candidate_id = data["items"][0]["id"]
        resp = await client.get(f"/v1/candidates/{candidate_id}")
        if resp.status_code != 200:
            return {"found": False, "message": f"Candidate '{candidate_id}' not found"}
        c = resp.json()
    host = c.get("host_star") or {}
    planet = c.get("planet") or {}
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
            "radius_rsun": host.get("radius_rsun"),
            "mass_msun": host.get("mass_msun"),
            "teff_k": host.get("teff_k"),
            "tess_mag": host.get("tess_mag"),
        }
        if host
        else None,
        "signal": {
            "period_d": c.get("period_d"),
            "period_err_d": c.get("period_err_d"),
            "epoch_bjd": c.get("epoch_bjd"),
            "epoch_err_d": c.get("epoch_err_d"),
            "duration_h": c.get("duration_h"),
            "depth_ppm": c.get("depth_ppm"),
            "radius_earth": planet.get("radius_earth"),
            "radius_basis": planet.get("radius_basis"),
        },
        "assessment": {
            "disposition": c.get("current_disposition"),
            "planet_score": c.get("planet_score"),
            "score_factors": c.get("score_factors", []),
            "source_localisation": c.get("source_localisation_status"),
            "timing_consistency": c.get("timing_consistency_status"),
            "limits": c.get("scientific_limits", []),
        },
        "sources": [
            {k: src.get(k) for k in ("source_name", "source_key", "url", "reported_disposition")}
            for src in c.get("sources", [])
        ],
        "vetting": next((s["vetting"] for s in c.get("sources", []) if s.get("vetting")), None),
        "harmonic_links": [],
    }


def _novelty_compact(result: dict[str, Any], legend: cx.Legend) -> dict[str, Any]:
    out = {"status": result["status"], "cone_targets": result["cone_targets_searched"]}
    if result["near_matches"]:
        out["near"] = cx.near_matches(result["near_matches"], legend)
    return out


@tool("check_novelty")
async def check_novelty(
    ra_deg: float,
    dec_deg: float,
    period_d: float,
    cone_radius_arcsec: float = 30.0,
    period_tolerance_percent: float = 1.0,
    harmonic_tolerance_percent: float = 1.0,
    format: Format = "full",
) -> dict[str, Any]:
    """
    Checks a signal against every indexed catalogue. Returns evidence, never a boolean:
    - checked_sources: one entry per catalogue with its current coverage (reports and
      signals indexed, last ingest time, pinned commit for git-hosted sources)
    - near_matches: catalogued signals inside the cone at the same period or a harmonic
      (1/4, 1/3, 1/2, 2, 3, 4), with every catalogue that reports each one
    - cone_targets_searched: all catalogued targets inside the cone (searched exhaustively,
      including across RA 0/360)
    cone_radius_arcsec is capped at 3600. For many signals use check_novelty_bulk.
    """
    if not period_d or period_d <= 0:
        raise ValueError("period_d must be positive")
    radius = min(float(cone_radius_arcsec), novelty.MAX_CONE_ARCSEC)

    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            service = CatalogService(session)
            items = novelty.cone_items_from_db(await service.cone_search(ra_deg, dec_deg, radius))
            sources = novelty.serialise_sources(await service.get_source_status())
    else:
        async with _client() as client:
            resp = await client.get(
                "/v1/cone",
                params={"ra_deg": ra_deg % 360.0, "dec_deg": dec_deg, "radius_arcsec": radius},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data["items"]
            sources = data["sources"]

    result = novelty.evidence(items, period_d, period_tolerance_percent, harmonic_tolerance_percent)
    query = {
        "ra_deg": ra_deg,
        "dec_deg": dec_deg,
        "period_d": period_d,
        "cone_radius_arcsec": radius,
        "period_tolerance_percent": period_tolerance_percent,
        "harmonic_tolerance_percent": harmonic_tolerance_percent,
    }
    if format == "compact":
        legend = cx.Legend()
        out = _novelty_compact(result, legend)
        if "near" in out:
            out["near"] = cx.table(cx.NEAR_MATCH_COLS, out["near"])
        return {
            **out,
            "query": query,
            "checked_sources": cx.checked_sources(sources),
            "known_unindexed_sources": novelty.KNOWN_UNINDEXED_SOURCES,
            "legend": legend.render(),
            "epistemic_warning": novelty.NOVELTY_WARNING,
        }
    return {
        "status": result["status"],
        "query": query,
        "checked_sources": sources,
        "cone_targets_searched": result["cone_targets_searched"],
        "near_matches": result["near_matches"],
        "known_unindexed_sources": novelty.KNOWN_UNINDEXED_SOURCES,
        "epistemic_warning": novelty.NOVELTY_WARNING,
    }


@tool("check_novelty_bulk")
async def check_novelty_bulk(
    signals: list[dict[str, Any]],
    cone_radius_arcsec: float = 30.0,
    period_tolerance_percent: float = 1.0,
    harmonic_tolerance_percent: float = 1.0,
    format: Format = "compact",
) -> dict[str, Any]:
    """
    check_novelty for up to 200 signals in one call. Each signal is
    {"ra_deg": float, "dec_deg": float, "period_d": float, "label": optional str}; results come
    back in order, keyed by label (or the index). checked_sources, the unindexed-source list
    and the epistemic warning are reported once for the whole batch. A signal with a
    non-positive period gets an error entry instead of evidence. Never returns a boolean.
    """
    queries = signals[:200]
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            result = await novelty.check_many(
                CatalogService(session),
                queries,
                cone_radius_arcsec,
                period_tolerance_percent,
                harmonic_tolerance_percent,
            )
    else:
        async with _client(120.0) as client:
            resp = await client.post(
                "/v1/novelty/batch",
                json={
                    "signals": queries,
                    "cone_radius_arcsec": min(float(cone_radius_arcsec), novelty.MAX_CONE_ARCSEC),
                    "period_tolerance_percent": period_tolerance_percent,
                    "harmonic_tolerance_percent": harmonic_tolerance_percent,
                },
            )
            resp.raise_for_status()
            result = resp.json()
    if format != "compact":
        return result
    legend = cx.Legend()
    summary, near_rows = [], []
    for r in result["results"]:
        if "error" in r:
            summary.append([r["label"], "error: " + r["error"], None, 0])
            continue
        summary.append(
            [r["label"], r["status"], r["cone_targets_searched"], len(r["near_matches"])]
        )
        near_rows.extend([r["label"], *row] for row in cx.near_matches(r["near_matches"], legend))
    return {
        "query": result["query"],
        "results": cx.table(["label", "status", "cone_targets", "near"], summary),
        "near": cx.table(["label", *cx.NEAR_MATCH_COLS], near_rows),
        "checked_sources": cx.checked_sources(result["checked_sources"]),
        "known_unindexed_sources": result["known_unindexed_sources"],
        "legend": legend.render(),
        "epistemic_warning": result["epistemic_warning"],
    }


@tool("get_observation_queue")
async def get_observation_queue(
    lat: float,
    lon: float,
    start_time_iso: str,
    end_time_iso: str,
    min_priority: float = 0.0,
    limit: int = 20,
    elevation_m: float = 0.0,
    min_altitude_deg: float = 30.0,
    max_sun_altitude_deg: float = -18.0,
    min_moon_separation_deg: float = 30.0,
    max_timing_uncertainty_min: float = 120.0,
    observable_only: bool = True,
    source: list[str] | None = None,
    min_planet_score: int | None = None,
    format: Format = "full",
) -> dict[str, Any]:
    """
    Transits observable from one site in a window of at most 7 days, best first. A transit
    counts only if the whole event, padded by its 1-sigma timing error, is above
    min_altitude_deg with the Sun below max_sun_altitude_deg (-18 astronomical, -12 nautical,
    -6 civil) and the Moon at least min_moon_separation_deg away. source limits it to signals
    reported by any of the given catalogues; min_planet_score drops unlikely planets.
    """
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            result = await CatalogService(session).get_observation_queue(
                lat_deg=lat,
                lon_deg=lon,
                start_time=datetime.fromisoformat(start_time_iso.replace("Z", "+00:00")),
                end_time=datetime.fromisoformat(end_time_iso.replace("Z", "+00:00")),
                min_priority=min_priority,
                limit=limit,
                min_altitude_deg=min_altitude_deg,
                max_sun_altitude_deg=max_sun_altitude_deg,
                min_moon_separation_deg=min_moon_separation_deg,
                max_timing_uncertainty_min=max_timing_uncertainty_min,
                observable_only=observable_only,
                height_m=elevation_m,
                sources=source or None,
                min_planet_score=min_planet_score,
            )
    else:
        params: dict[str, Any] = {
            "lat": lat,
            "lon": lon,
            "start_time": start_time_iso,
            "end_time": end_time_iso,
            "min_priority": min_priority,
            "limit": limit,
            "elevation_m": elevation_m,
            "min_altitude_deg": min_altitude_deg,
            "max_sun_altitude_deg": max_sun_altitude_deg,
            "min_moon_separation_deg": min_moon_separation_deg,
            "max_timing_uncertainty_min": max_timing_uncertainty_min,
            "observable_only": observable_only,
        }
        if source:
            params["source"] = source
        if min_planet_score is not None:
            params["min_planet_score"] = min_planet_score
        async with _client(90.0) as client:
            resp = await client.get("/v1/observations/queue", params=params)
            if resp.status_code != 200:
                return {"total": 0, "items": [], "error": resp.text[:500]}
            result = resp.json()
    if format == "compact":
        legend = cx.Legend()
        return {
            "total": result["total"],
            "transits": cx.queue(result["items"], legend),
            "legend": legend.render(),
        }
    return {"total": result["total"], "items": result["items"]}


@tool("get_catalog_stats")
async def get_catalog_stats() -> dict[str, Any]:
    """
    Get current summary statistics of the Exoplanet DB catalog:
    total signals/planets, confirmed planets, transit candidates, and stellar host systems.
    """
    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            service = CatalogService(session)
            return await service.get_stats()

    async with _client(15.0) as client:
        resp = await client.get("/v1/stats")
        if resp.status_code != 200:
            return {"error": f"Failed to fetch stats: HTTP {resp.status_code}"}
        return resp.json()


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
