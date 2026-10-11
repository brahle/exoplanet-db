from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DispositionEnum(StrEnum):
    UNCLASSIFIED_TRANSIT_LIKE_SIGNAL = "unclassified_transit_like_signal"
    FALSE_POSITIVE = "false_positive"
    KNOWN_SIGNAL = "known_signal"
    PLANET_CANDIDATE = "planet_candidate"
    VALIDATED_PLANET = "validated_planet"
    CONFIRMED_PLANET = "confirmed_planet"


class EvidenceStatusEnum(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    UNRESOLVED = "unresolved"


class NoveltyStatusEnum(StrEnum):
    NO_MATCH_IN_CHECKED_SOURCES = "no_match_in_checked_sources"
    PREVIOUSLY_REPORTED = "previously_reported"
    UNRESOLVED = "unresolved"


class ReproducibilityLevelEnum(StrEnum):
    EVIDENCE_INSPECTION = "evidence_inspection"
    PUBLIC_NUMERICAL_RERUN = "public_numerical_rerun"


class PublicationStateEnum(StrEnum):
    DRAFT = "draft"
    DISCLOSED = "disclosed"
    PUBLISHED = "published"


class Measurement(BaseModel):
    model_config = ConfigDict(extra="allow")
    value: float
    # Upstream schema: null marks an unavailable uncertainty (provisional duration/depth)
    error: float | None = Field(default=..., gt=0)


class StatusDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: EvidenceStatusEnum
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)


class Coordinates(BaseModel):
    model_config = ConfigDict(extra="allow")
    ra_deg: float = Field(ge=0, lt=360)
    dec_deg: float = Field(ge=-90, le=90)
    frame: str = "ICRS"
    epoch: str = "J2000"
    provenance: str


class TargetDossier(BaseModel):
    model_config = ConfigDict(extra="allow")
    tic: str = Field(pattern=r"^[0-9]+$")
    # Null when no unambiguous Gaia DR3 association is established (upstream schema)
    gaia_dr3: str | None = Field(default=..., pattern=r"^[0-9]+$")
    # Schema 1.2: how the catalogue (Gaia/TIC) association was made
    catalogue_association: dict[str, Any] | None = None
    aliases: list[str]
    coordinates: Coordinates


class SignalDossier(BaseModel):
    model_config = ConfigDict(extra="allow")
    period_d: Measurement
    epoch_btjd: Measurement
    epoch_bjd_tdb: float
    bjd_offset: int = 2457000
    time_scale: str = "TDB"
    covariance_epoch_period_d2: list[list[float]] | None = None
    covariance_method: str = Field(min_length=1)
    duration_h: Measurement
    depth_ppm: Measurement
    model: str = Field(min_length=1)
    sectors: list[int] = Field(min_length=1)
    covered_events: int = Field(ge=1)
    limits: list[str] = Field(min_length=1)


class AssessmentDossier(BaseModel):
    model_config = ConfigDict(extra="allow")
    disposition: DispositionEnum
    source_localisation: StatusDetail
    timing_consistency: StatusDetail


class NoveltyDossier(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: NoveltyStatusEnum
    checked_utc: datetime
    evidence: list[str] = Field(min_length=1)
    limits: list[str] = Field(min_length=1)


class ProvenanceDossier(BaseModel):
    model_config = ConfigDict(extra="allow")
    analysis_date: date
    source_revisions: dict[str, str] = Field(default_factory=dict)
    runs: dict[str, str] = Field(default_factory=dict)
    input_lock: str


class ReproducibilityDossier(BaseModel):
    model_config = ConfigDict(extra="allow")
    level: ReproducibilityLevelEnum
    command: str
    limits: list[str] = Field(min_length=1)
    requirements_for_numerical_release: list[str] = Field(default_factory=list)


class PublicationDossier(BaseModel):
    model_config = ConfigDict(extra="allow")
    state: PublicationStateEnum
    first_public_utc: datetime | None = None
    disclosure_url: str | None = None
    release_url: str | None = None
    doi: str | None = None
    authors: list[str] = Field(min_length=1)


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    date: date
    version: str
    change: str


class CandidateDossier(BaseModel):
    """
    Contract for exoplanet-candidates dossiers, schema 1.x.

    Every field we consume is validated; fields added by later schema versions are kept
    (extra="allow") instead of rejected, so the publisher can extend the format without
    breaking ingestion. The full JSON is stored as the report's raw payload either way.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = Field(default="1.0", pattern=r"^1\.[0-9]+$")
    id: str = Field(pattern=r"^tic-[0-9]+-s[0-9]{2}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    title: str = Field(min_length=1)
    target: TargetDossier
    signal: SignalDossier
    assessment: AssessmentDossier
    novelty: NoveltyDossier
    provenance: ProvenanceDossier
    reproducibility: ReproducibilityDossier
    publication: PublicationDossier
    evidence: list[str] = Field(min_length=1)
    history: list[HistoryEntry] = Field(min_length=1)
    # Schema 1.2: catalogue stellar parameters for the aperture target
    host: dict[str, Any] | None = None
