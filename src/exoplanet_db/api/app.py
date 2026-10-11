from __future__ import annotations

import gzip
import importlib.metadata
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import prometheus_client
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from exoplanet_db.ingest.nasa_tap import NASA_ACKNOWLEDGEMENT
from exoplanet_db.services.catalog import CatalogService

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///exoplanet_db.sqlite")

# Normalize database URL
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))
ENABLE_METRICS_SERVER = os.getenv("ENABLE_METRICS_SERVER", "false").lower() in ("1", "true", "yes")

SYSTEMS_SEARCH_TOTAL = prometheus_client.Counter(
    "exoplanet_systems_search_total", "Count of target system searches"
)
CANDIDATES_SEARCH_TOTAL = prometheus_client.Counter(
    "exoplanet_candidates_search_total", "Count of candidate queries"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if ENABLE_METRICS_SERVER:
        try:
            prometheus_client.start_http_server(METRICS_PORT)
        except OSError:
            pass
    yield
    await engine.dispose()


try:
    APP_VERSION = importlib.metadata.version("exoplanet-db")
except importlib.metadata.PackageNotFoundError:
    APP_VERSION = "0.0.0+unknown"

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://exoplanets.brahle.com").rstrip("/")

app = FastAPI(
    title="Exoplanet DB API",
    version=APP_VERSION,
    description="AI agent-friendly, lightweight database of exoplanets and transit candidate dossiers with reproducibility tracking.",
    servers=[
        {"url": PUBLIC_BASE_URL, "description": "Production (marica)"},
        {"url": "/", "description": "Current Origin"},
    ],
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount MCP Remote SSE application for AI Agents
from exoplanet_db.mcp.server import mcp

# Keep DNS-rebinding protection on; allow the public host plus local/in-cluster access.
_public_host = urlparse(PUBLIC_BASE_URL).netloc
mcp_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        _public_host,
        "localhost:*",
        "127.0.0.1:*",
        "exoplanet-db-api:*",
        "exoplanet-db-api.exoplanet-db.svc:*",
        "exoplanet-db-api.exoplanet-db.svc.cluster.local:*",
    ],
    allowed_origins=[PUBLIC_BASE_URL, "http://localhost:*", "http://127.0.0.1:*"],
)
app.mount("/mcp", mcp.sse_app(transport_security=mcp_security))
app.mount("/sse", mcp.sse_app(sse_path="/", transport_security=mcp_security))

PORTAL_STATIC_DIR = Path(__file__).resolve().parent.parent / "portal" / "static"
if not (PORTAL_STATIC_DIR / "index.html").exists():
    for fallback in [
        Path("/app/static"),
        Path("/app/src/exoplanet_db/portal/static"),
        Path("src/exoplanet_db/portal/static"),
    ]:
        if (fallback / "index.html").exists():
            PORTAL_STATIC_DIR = fallback
            break

if PORTAL_STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(PORTAL_STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse, tags=["Portal"], include_in_schema=False)
async def serve_portal():
    index_file = PORTAL_STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>Exoplanet DB Portal</h1>")


ATTRIBUTION_METADATA = {
    "nasa_exoplanet_archive": NASA_ACKNOWLEDGEMENT,
    "community_data_license": "CC BY 4.0 (Creative Commons Attribution 4.0 International)",
    "code_license": "MIT",
}


def primary_source_name(signal: Any) -> str:
    """Source of the signal's preferred ephemeris, else its most recent report.

    Only reads relationships that are already loaded (async sessions cannot lazy-load).
    """
    unloaded = sa_inspect(signal).unloaded
    reports = [] if "reports" in unloaded else list(signal.reports or [])
    if not reports:
        return resolve_source_name(signal)
    fits = {} if "ephemeris_fits" in unloaded else {f.id: f for f in signal.ephemeris_fits or []}
    preferred = fits.get(signal.preferred_ephemeris_fit_id)
    if preferred is not None:
        for r in reports:
            if r.id == preferred.report_id:
                return r.source_name
    return max(reports, key=lambda r: r.retrieval_utc).source_name


def resolve_source_name(signal: Any) -> str:
    limits = getattr(signal, "scientific_limits", None) or []
    for l in limits:
        if "eumemic" in l or "source_file" in l:
            return "eumemic/exoplanets"
        if "TOI" in l or "TFOPWG" in l:
            return "nasa_toi"
        if "composite row" in l:
            return "nasa_pscomppars"
        if "Absence from checked" in l or "Conditional diagnostic" in l:
            return "git:brahle/exoplanet-candidates"
        if "ExoFOP" in l:
            return "exofop_tess_ctoi"
    disp = getattr(signal, "current_disposition", "")
    if disp == "confirmed_planet":
        return "nasa_pscomppars"
    if disp == "planet_candidate":
        return "nasa_toi"
    return "git:brahle/exoplanet-candidates"


# --- Pydantic API Response Models ---


class TargetResponse(BaseModel):
    id: str
    primary_name: str
    tic_id: str | None = None
    gaia_dr3_id: str | None = None
    ra_deg: float
    dec_deg: float
    v_mag: float | None = None
    teff_k: float | None = None
    aliases: list[str] = Field(default_factory=list)


class CandidateSummary(BaseModel):
    id: str
    target_id: str
    target_name: str | None = None
    tic_id: str | None = None
    source_name: str | None = None
    display_name: str
    period_d: float
    period_err_d: float | None = None
    epoch_bjd: float
    duration_h: float | None = None
    depth_ppm: float | None = None
    # Mandatory epistemic fields
    current_disposition: str
    source_localisation_status: str
    timing_consistency_status: str
    scientific_limits: list[str]


class SourceReference(BaseModel):
    source_name: str
    source_key: str | None = None
    url: str | None = Field(
        None, description="Public landing page for this report, when one exists"
    )
    author_or_team: str | None = None
    license: str | None = None
    commit_sha: str | None = None
    retrieval_utc: datetime | None = None
    reported_disposition: str | None = None


class HostStarResponse(TargetResponse):
    radius_rsun: float | None = None
    mass_msun: float | None = None
    tess_mag: float | None = None


class PlanetPhysics(BaseModel):
    id: str
    display_name: str
    current_disposition: str
    period_d: float | None = None
    period_err_d: float | None = None
    epoch_bjd: float | None = None
    epoch_err_d: float | None = None
    duration_h: float | None = None
    duration_err_h: float | None = None
    depth_ppm: float | None = None
    depth_err_ppm: float | None = None
    radius_earth: float | None = None
    radius_basis: str | None = Field(
        None, description="'catalogue' (published) or 'transit_depth' (R* x sqrt(depth), undiluted)"
    )
    semi_major_axis_au: float | None = None
    semi_major_axis_basis: str | None = Field(
        None, description="'kepler3_catalogue_mass' or 'kepler3_assumed_solar_mass'"
    )
    fit_model: str | None = None


class CandidateDetailResponse(CandidateSummary):
    host_star: HostStarResponse | None = None
    epoch_err_d: float | None = None
    planet: PlanetPhysics | None = None
    primary_source: SourceReference | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    system: list[PlanetPhysics] = Field(
        default_factory=list, description="Every signal on the same host, ordered by period"
    )
    attribution: dict[str, str] = Field(default_factory=lambda: ATTRIBUTION_METADATA)


class SystemDetailResponse(TargetResponse):
    candidates: list[CandidateSummary] = Field(default_factory=list)
    attribution: dict[str, str] = Field(default_factory=lambda: ATTRIBUTION_METADATA)


class PaginatedResponse(BaseModel):
    items: list[Any]
    total: int
    limit: int
    offset: int
    attribution: dict[str, str] = Field(default_factory=lambda: ATTRIBUTION_METADATA)


class ObservationQueueItem(BaseModel):
    signal_id: str
    target_id: str
    target_name: str
    signal_name: str
    current_disposition: str
    transit_mid_utc: datetime
    transit_start_utc: datetime
    transit_end_utc: datetime
    duration_h: float
    timing_uncertainty_min: float
    priority_score: float
    target_altitude_deg: float
    target_airmass: float
    sun_altitude_deg: float
    moon_separation_deg: float
    is_observable: bool
    fit_model: str | None = None


class ObservationQueueResponse(BaseModel):
    items: list[ObservationQueueItem]
    total: int
    observer: dict[str, float]
    window: dict[str, str]
    attribution: dict[str, str] = Field(default_factory=lambda: ATTRIBUTION_METADATA)


class SourceStats(BaseModel):
    source_name: str
    signals: int = Field(description="Distinct signals with at least one report from this source")
    reports: int
    last_ingested_utc: datetime | None = None
    commit_sha: str | None = Field(None, description="Pinned revision for git-hosted sources")


class ConeSignal(BaseModel):
    id: str
    display_name: str
    period_d: float | None = None
    epoch_bjd: float | None = None
    current_disposition: str
    sources: list[str] = Field(description="Every catalogue reporting this signal")
    source_keys: list[str] = Field(default_factory=list)


class ConeTarget(BaseModel):
    id: str
    primary_name: str
    tic_id: str | None = None
    ra_deg: float
    dec_deg: float
    separation_arcsec: float
    signals: list[ConeSignal]


class ConeSearchResponse(BaseModel):
    ra_deg: float
    dec_deg: float
    radius_arcsec: float
    total: int
    items: list[ConeTarget]
    sources: list[SourceStats] = Field(description="Coverage of every source at query time")


class CatalogStatsResponse(BaseModel):
    sources: list[SourceStats] = Field(default_factory=list)
    confirmed_in_nasa_archive: int = Field(
        0, description="Confirmed planets with a row in the NASA Exoplanet Archive composite table"
    )
    confirmed_tess_discoveries: int = Field(
        0, description="Of those, planets whose discovery facility is TESS"
    )
    total_signals: int
    confirmed_planets: int
    planet_candidates: int
    unclassified_signals: int
    candidates_total: int
    false_positives: int
    total_targets: int
    disposition_counts: dict[str, int]
    attribution: dict[str, str] = Field(default_factory=lambda: ATTRIBUTION_METADATA)


# --- Endpoints ---


@app.get("/healthz", tags=["Operations"])
async def healthz():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/v1/stats", response_model=CatalogStatsResponse, tags=["Catalog"])
async def get_catalog_stats(
    session: AsyncSession = Depends(get_session),
):
    service = CatalogService(session)
    stats = await service.get_stats()
    return CatalogStatsResponse(**stats)


@app.get("/v1/sources", response_model=list[SourceStats], tags=["Catalog"])
async def list_sources(session: AsyncSession = Depends(get_session)):
    """Current coverage per source: indexed reports/signals, last ingest, pinned commit."""
    return [SourceStats(**s) for s in await CatalogService(session).get_source_status()]


@app.get("/v1/cone", response_model=ConeSearchResponse, tags=["Catalog"])
async def cone_search(
    ra_deg: float = Query(..., ge=0.0, lt=360.0),
    dec_deg: float = Query(..., ge=-90.0, le=90.0),
    radius_arcsec: float = Query(30.0, gt=0.0, le=3600.0),
    session: AsyncSession = Depends(get_session),
):
    """Every catalogued target within the radius (no paging, RA-wrap safe), nearest first."""
    service = CatalogService(session)
    hits = await service.cone_search(ra_deg, dec_deg, radius_arcsec)
    items = []
    for h in hits:
        t = h["target"]
        items.append(
            ConeTarget(
                id=t.id,
                primary_name=t.primary_name,
                tic_id=t.tic_id,
                ra_deg=t.ra_deg,
                dec_deg=t.dec_deg,
                separation_arcsec=round(h["separation_arcsec"], 3),
                signals=[
                    ConeSignal(
                        id=s.id,
                        display_name=s.display_name,
                        period_d=s.primary_period_d if (s.primary_period_d or 0) > 0 else None,
                        epoch_bjd=s.primary_epoch_bjd
                        if (s.primary_epoch_bjd or 0) > 2400000
                        else None,
                        current_disposition=s.current_disposition,
                        sources=sorted({r.source_name for r in s.reports}),
                        source_keys=sorted({r.source_key for r in s.reports}),
                    )
                    for s in t.signals
                ],
            )
        )
    return ConeSearchResponse(
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        radius_arcsec=radius_arcsec,
        total=len(items),
        items=items,
        sources=[SourceStats(**s) for s in await service.get_source_status()],
    )


@app.get("/v1/systems", response_model=PaginatedResponse, tags=["Catalog"])
async def list_systems(
    search: str | None = Query(None, description="Search by name, TIC ID, or Gaia ID"),
    ra_min: float | None = Query(None, ge=0.0, le=360.0),
    ra_max: float | None = Query(None, ge=0.0, le=360.0),
    dec_min: float | None = Query(None, ge=-90.0, le=90.0),
    dec_max: float | None = Query(None, ge=-90.0, le=90.0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    SYSTEMS_SEARCH_TOTAL.inc()
    service = CatalogService(session)
    targets, total = await service.list_systems(
        search=search,
        ra_min=ra_min,
        ra_max=ra_max,
        dec_min=dec_min,
        dec_max=dec_max,
        limit=limit,
        offset=offset,
    )
    items = [
        TargetResponse(
            id=t.id,
            primary_name=t.primary_name,
            tic_id=t.tic_id,
            gaia_dr3_id=t.gaia_dr3_id,
            ra_deg=t.ra_deg,
            dec_deg=t.dec_deg,
            v_mag=t.v_mag,
            teff_k=t.teff_k,
            aliases=t.aliases,
        )
        for t in targets
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


@app.get("/v1/systems/{system_id}", response_model=SystemDetailResponse, tags=["Catalog"])
async def get_system(
    system_id: str,
    session: AsyncSession = Depends(get_session),
):
    service = CatalogService(session)
    target = await service.get_system_by_id(system_id)
    if not target:
        raise HTTPException(status_code=404, detail=f"System '{system_id}' not found")

    candidates = [
        CandidateSummary(
            id=s.id,
            target_id=s.target_id,
            target_name=target.primary_name,
            tic_id=target.tic_id,
            source_name=resolve_source_name(s),
            display_name=s.display_name,
            period_d=s.primary_period_d,
            period_err_d=s.primary_period_err_d,
            epoch_bjd=s.primary_epoch_bjd,
            duration_h=s.primary_duration_h,
            depth_ppm=s.primary_depth_ppm,
            current_disposition=s.current_disposition,
            source_localisation_status=s.source_localisation_status,
            timing_consistency_status=s.timing_consistency_status,
            scientific_limits=s.scientific_limits,
        )
        for s in target.signals
    ]

    return SystemDetailResponse(
        id=target.id,
        primary_name=target.primary_name,
        tic_id=target.tic_id,
        gaia_dr3_id=target.gaia_dr3_id,
        ra_deg=target.ra_deg,
        dec_deg=target.dec_deg,
        v_mag=target.v_mag,
        teff_k=target.teff_k,
        aliases=target.aliases,
        candidates=candidates,
    )


@app.get("/v1/candidates", response_model=PaginatedResponse, tags=["Catalog"])
async def list_candidates(
    search: str | None = Query(
        None, description="Search by candidate name, target name, TIC ID, or Gaia ID"
    ),
    source: str | None = Query(
        None,
        description="Filter by data source: nasa_pscomppars, nasa_toi, exofop_ctoi, eumemic/exoplanets, git:brahle/exoplanet-candidates",
    ),
    disposition: str | None = Query(None, description="Filter by scientific disposition"),
    min_period_d: float | None = Query(None, ge=0.0),
    max_period_d: float | None = Query(None, ge=0.0),
    min_depth_ppm: float | None = Query(None, ge=0.0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    CANDIDATES_SEARCH_TOTAL.inc()
    service = CatalogService(session)
    signals, total = await service.list_candidates(
        search=search,
        source=source,
        disposition=disposition,
        min_period_d=min_period_d,
        max_period_d=max_period_d,
        min_depth_ppm=min_depth_ppm,
        limit=limit,
        offset=offset,
    )
    items = [
        CandidateSummary(
            id=s.id,
            target_id=s.target_id,
            target_name=s.target.primary_name if s.target else None,
            tic_id=s.target.tic_id if s.target else None,
            source_name=primary_source_name(s),
            display_name=s.display_name,
            period_d=s.primary_period_d,
            period_err_d=s.primary_period_err_d,
            epoch_bjd=s.primary_epoch_bjd,
            duration_h=s.primary_duration_h,
            depth_ppm=s.primary_depth_ppm,
            current_disposition=s.current_disposition,
            source_localisation_status=s.source_localisation_status,
            timing_consistency_status=s.timing_consistency_status,
            scientific_limits=s.scientific_limits,
        )
        for s in signals
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


@app.get("/v1/candidates/{candidate_id}", response_model=CandidateDetailResponse, tags=["Catalog"])
async def get_candidate(
    candidate_id: str,
    session: AsyncSession = Depends(get_session),
):
    service = CatalogService(session)
    detail = await service.get_candidate_detail(candidate_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Candidate '{candidate_id}' not found")
    signal = detail["signal"]
    target = detail["target"]
    star = detail["star"]
    primary = detail["primary_source"]

    host_star = HostStarResponse(
        id=target.id,
        primary_name=target.primary_name,
        tic_id=target.tic_id,
        gaia_dr3_id=target.gaia_dr3_id,
        ra_deg=target.ra_deg,
        dec_deg=target.dec_deg,
        v_mag=target.v_mag,
        teff_k=star["teff_k"],
        aliases=target.aliases,
        radius_rsun=star["radius_rsun"],
        mass_msun=star["mass_msun"],
        tess_mag=star["tess_mag"],
    )

    return CandidateDetailResponse(
        id=signal.id,
        target_id=signal.target_id,
        target_name=target.primary_name,
        tic_id=target.tic_id,
        source_name=primary["source_name"] if primary else resolve_source_name(signal),
        display_name=signal.display_name,
        period_d=signal.primary_period_d,
        period_err_d=signal.primary_period_err_d,
        epoch_bjd=signal.primary_epoch_bjd,
        epoch_err_d=detail["planet"]["epoch_err_d"],
        duration_h=signal.primary_duration_h,
        depth_ppm=signal.primary_depth_ppm,
        current_disposition=signal.current_disposition,
        source_localisation_status=signal.source_localisation_status,
        timing_consistency_status=signal.timing_consistency_status,
        scientific_limits=signal.scientific_limits,
        host_star=host_star,
        planet=PlanetPhysics(**detail["planet"]),
        primary_source=SourceReference(**primary) if primary else None,
        sources=[SourceReference(**src) for src in detail["sources"]],
        system=[PlanetPhysics(**p) for p in detail["system"]],
    )


_POPULATION_CACHE: dict[str, Any] = {"at": 0.0, "body": None}
POPULATION_CACHE_SECONDS = 600


@app.get("/v1/population", tags=["Catalog"])
async def get_population(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Columnar period / depth / radius for every periodic signal (population context plots).

    radius_earth is the published radius when the catalogue has one, otherwise R* x sqrt(depth).
    """
    now = time.monotonic()
    if (
        _POPULATION_CACHE["body"] is None
        or now - _POPULATION_CACHE["at"] > POPULATION_CACHE_SECONDS
    ):
        cols = await CatalogService(session).get_population()
        body = json.dumps({**cols, "count": len(cols["id"])}, separators=(",", ":")).encode()
        _POPULATION_CACHE.update(at=now, body=body, gz=gzip.compress(body, 6))
    headers = {"Cache-Control": "public, max-age=600", "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(
            _POPULATION_CACHE["gz"],
            media_type="application/json",
            headers={**headers, "Content-Encoding": "gzip"},
        )
    return Response(_POPULATION_CACHE["body"], media_type="application/json", headers=headers)


@app.get("/v1/snapshots", tags=["Catalog"])
async def list_snapshots(
    limit: int = Query(200, ge=1, le=2000, description="Newest first"),
    session: AsyncSession = Depends(get_session),
):
    """Ingest history (one row per source per run). For current coverage use /v1/sources."""
    service = CatalogService(session)
    snapshots = await service.list_published_snapshots(limit=limit)
    return [
        {
            "id": s.id,
            "source_name": s.source_name,
            "snapshot_utc": s.snapshot_utc.isoformat(),
            "commit_sha": s.commit_sha,
            "record_count": s.record_count,
            "status": s.status,
            "published_at": s.published_at.isoformat(),
        }
        for s in snapshots
    ]


@app.get("/v1/observations/queue", response_model=ObservationQueueResponse, tags=["Observations"])
async def get_observation_queue(
    lat: float = Query(..., ge=-90.0, le=90.0, description="Observer geodetic latitude in degrees"),
    lon: float = Query(
        ..., ge=-180.0, le=180.0, description="Observer geodetic longitude in degrees"
    ),
    start_time: datetime = Query(..., description="Observation window start in UTC (ISO-8601)"),
    end_time: datetime = Query(..., description="Observation window end in UTC (ISO-8601)"),
    min_priority: float = Query(0.0, ge=0.0, description="Minimum priority score cutoff"),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    service = CatalogService(session)
    items = await service.get_observation_queue(
        lat_deg=lat,
        lon_deg=lon,
        start_time=start_time,
        end_time=end_time,
        min_priority=min_priority,
        limit=limit,
    )
    return ObservationQueueResponse(
        items=[ObservationQueueItem(**item) for item in items],
        total=len(items),
        observer={"lat": lat, "lon": lon},
        window={"start": start_time.isoformat(), "end": end_time.isoformat()},
    )
