from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import prometheus_client
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
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


app = FastAPI(
    title="Exoplanet DB API",
    version="0.1.0",
    description="AI agent-friendly, lightweight database of exoplanets and transit candidate dossiers with reproducibility tracking.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PORTAL_STATIC_DIR = Path(__file__).resolve().parent.parent / "portal" / "static"
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


def resolve_source_name(signal: Any) -> str:
    limits = getattr(signal, "scientific_limits", None) or []
    for l in limits:
        if "NASA" in l:
            return "nasa_pscomppars"
        if "Absence from checked" in l or "Conditional diagnostic" in l:
            return "git:brahle/exoplanet-candidates"
        if "ExoFOP" in l:
            return "exofop_tess_ctoi"
        if "eumemic" in l:
            return "eumemic_catalog"
    if getattr(signal, "current_disposition", "") == "confirmed_planet":
        return "nasa_pscomppars"
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


class CandidateDetailResponse(CandidateSummary):
    host_star: TargetResponse | None = None
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


# --- Endpoints ---


@app.get("/healthz", tags=["Operations"])
async def healthz():
    return {"status": "ok", "version": "0.1.0"}


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
    search: str | None = Query(None, description="Search by candidate name, target name, TIC ID, or Gaia ID"),
    source: str | None = Query(None, description="Filter by data source (e.g. nasa_pscomppars, git:brahle/exoplanet-candidates)"),
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
        for s in signals
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


@app.get("/v1/candidates/{candidate_id}", response_model=CandidateDetailResponse, tags=["Catalog"])
async def get_candidate(
    candidate_id: str,
    session: AsyncSession = Depends(get_session),
):
    service = CatalogService(session)
    signal = await service.get_candidate_by_id(candidate_id)
    if not signal:
        raise HTTPException(status_code=404, detail=f"Candidate '{candidate_id}' not found")

    host_star = None
    if signal.target:
        host_star = TargetResponse(
            id=signal.target.id,
            primary_name=signal.target.primary_name,
            tic_id=signal.target.tic_id,
            gaia_dr3_id=signal.target.gaia_dr3_id,
            ra_deg=signal.target.ra_deg,
            dec_deg=signal.target.dec_deg,
            v_mag=signal.target.v_mag,
            teff_k=signal.target.teff_k,
            aliases=signal.target.aliases,
        )

    return CandidateDetailResponse(
        id=signal.id,
        target_id=signal.target_id,
        target_name=signal.target.primary_name if signal.target else None,
        tic_id=signal.target.tic_id if signal.target else None,
        source_name=resolve_source_name(signal),
        display_name=signal.display_name,
        period_d=signal.primary_period_d,
        period_err_d=signal.primary_period_err_d,
        epoch_bjd=signal.primary_epoch_bjd,
        duration_h=signal.primary_duration_h,
        depth_ppm=signal.primary_depth_ppm,
        current_disposition=signal.current_disposition,
        source_localisation_status=signal.source_localisation_status,
        timing_consistency_status=signal.timing_consistency_status,
        scientific_limits=signal.scientific_limits,
        host_star=host_star,
    )


@app.get("/v1/snapshots", tags=["Catalog"])
async def list_snapshots(
    session: AsyncSession = Depends(get_session),
):
    service = CatalogService(session)
    snapshots = await service.list_published_snapshots()
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
