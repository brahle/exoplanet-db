from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from exoplanet_db.models.base import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(UTC)


class Target(Base):
    __tablename__ = "targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tic_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    gaia_dr3_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    two_mass_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    kic_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    toi_tid: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    primary_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    pm_ra_mas_yr: Mapped[float | None] = mapped_column(Float, nullable=True)
    pm_dec_mas_yr: Mapped[float | None] = mapped_column(Float, nullable=True)
    epoch: Mapped[str] = mapped_column(String(32), default="J2000", nullable=False)
    frame: Mapped[str] = mapped_column(String(32), default="ICRS", nullable=False)

    v_mag: Mapped[float | None] = mapped_column(Float, nullable=True)
    tess_mag: Mapped[float | None] = mapped_column(Float, nullable=True)
    teff_k: Mapped[float | None] = mapped_column(Float, nullable=True)
    radius_rsun: Mapped[float | None] = mapped_column(Float, nullable=True)
    mass_msun: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    signals: Mapped[list[Signal]] = relationship(
        "Signal", back_populates="target", cascade="all, delete-orphan"
    )
    reports: Mapped[list[Report]] = relationship("Report", back_populates="target")

    __table_args__ = (Index("idx_targets_coords", "ra_deg", "dec_deg"),)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Primary / representative parameters
    display_name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    primary_period_d: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    primary_period_err_d: Mapped[float | None] = mapped_column(Float, nullable=True)
    primary_epoch_bjd: Mapped[float] = mapped_column(Float, nullable=False)
    primary_epoch_err_d: Mapped[float | None] = mapped_column(Float, nullable=True)
    primary_duration_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    primary_depth_ppm: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Mandatory scientific states
    current_disposition: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_localisation_status: Mapped[str] = mapped_column(
        String(32), default="unresolved", nullable=False
    )
    timing_consistency_status: Mapped[str] = mapped_column(
        String(32), default="unresolved", nullable=False
    )
    scientific_limits: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    # Heuristic 0-100 "how likely a real planet" score and its factors (services/planet_score.py)
    planet_score: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    score_factors: Mapped[list[dict] | None] = mapped_column(JSONType, nullable=True)
    # Catalogued variable stars / eclipsing binaries near the host at this period or a
    # harmonic (services/variables.py); None until cross-matched
    variable_matches: Mapped[list[dict] | None] = mapped_column(JSONType, nullable=True)

    # Preferred coherent fit pointer (for scheduling)
    preferred_ephemeris_fit_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("ephemeris_fits.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    target: Mapped[Target] = relationship("Target", back_populates="signals")
    reports: Mapped[list[Report]] = relationship("Report", back_populates="signal")
    ephemeris_fits: Mapped[list[EphemerisFit]] = relationship(
        "EphemerisFit", back_populates="signal", foreign_keys="EphemerisFit.signal_id"
    )
    harmonics: Mapped[list[SignalHarmonic]] = relationship(
        "SignalHarmonic", foreign_keys="SignalHarmonic.signal_id", back_populates="signal"
    )


class SignalHarmonic(Base):
    __tablename__ = "signal_harmonics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    signal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    harmonic_signal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    harmonic_ratio: Mapped[float] = mapped_column(Float, nullable=False)  # e.g. 0.5, 2.0, 3.0
    phase_offset: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    signal: Mapped[Signal] = relationship(
        "Signal", foreign_keys=[signal_id], back_populates="harmonics"
    )
    harmonic_signal: Mapped[Signal] = relationship("Signal", foreign_keys=[harmonic_signal_id])


class SignalLineage(Base):
    __tablename__ = "signal_lineage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # merge, split, reclassification
    parent_signal_ids: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    child_signal_ids: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    signal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="SET NULL"), nullable=True, index=True
    )

    source_name: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # nasa_ps, nasa_toi, exofop_ctoi, eumemic, dossier
    source_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    retrieval_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)

    author_or_team: Mapped[str] = mapped_column(String(256), nullable=False)
    bibcode_or_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    license: Mapped[str] = mapped_column(
        String(64), default="Public Domain / Fair Use", nullable=False
    )

    reported_disposition: Mapped[str] = mapped_column(String(64), nullable=False)
    period_d: Mapped[float | None] = mapped_column(Float, nullable=True)
    period_err_d: Mapped[float | None] = mapped_column(Float, nullable=True)
    epoch_bjd: Mapped[float | None] = mapped_column(Float, nullable=True)
    epoch_err_d: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    depth_ppm: Mapped[float | None] = mapped_column(Float, nullable=True)

    raw_payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    target: Mapped[Target] = relationship("Target", back_populates="reports")
    signal: Mapped[Signal | None] = relationship("Signal", back_populates="reports")
    ephemeris_fit: Mapped[EphemerisFit | None] = relationship(
        "EphemerisFit", back_populates="report", uselist=False
    )
    reproducibility_run: Mapped[ReproducibilityRun | None] = relationship(
        "ReproducibilityRun", back_populates="report", uselist=False
    )


class EphemerisFit(Base):
    __tablename__ = "ephemeris_fits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    report_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    signal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="CASCADE"), nullable=False, index=True
    )

    period_d: Mapped[float] = mapped_column(Float, nullable=False)
    period_err_d: Mapped[float | None] = mapped_column(Float, nullable=True)
    epoch_bjd_tdb: Mapped[float] = mapped_column(Float, nullable=False)
    epoch_err_d: Mapped[float | None] = mapped_column(Float, nullable=True)

    duration_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_err_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    depth_ppm: Mapped[float | None] = mapped_column(Float, nullable=True)
    depth_err_ppm: Mapped[float | None] = mapped_column(Float, nullable=True)

    covariance_matrix: Mapped[list[list[float]] | None] = mapped_column(JSONType, nullable=True)
    covariance_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_scale: Mapped[str] = mapped_column(String(16), default="TDB", nullable=False)
    fit_model: Mapped[str] = mapped_column(Text, default="linear_ephemeris", nullable=False)
    is_coherent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    report: Mapped[Report] = relationship("Report", back_populates="ephemeris_fit")
    signal: Mapped[Signal] = relationship(
        "Signal", foreign_keys=[signal_id], back_populates="ephemeris_fits"
    )


class ReproducibilityRun(Base):
    __tablename__ = "reproducibility_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    report_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    pipeline_name: Mapped[str] = mapped_column(String(128), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    level: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # evidence_inspection, public_numerical_rerun
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default="unresolved", nullable=False
    )  # supported, contradicted, unresolved
    metrics: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    limits: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    report: Mapped[Report] = relationship("Report", back_populates="reproducibility_run")


class PublishedSnapshot(Base):
    __tablename__ = "published_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="validated", nullable=False
    )  # validated, failed
    validation_metrics: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class VariableStar(Base):
    """A periodic variable or eclipsing binary from an external catalogue (TESS EB, Gaia DR3
    eclipsing binaries, VSX). Reference data for contamination checks, not signals."""

    __tablename__ = "variable_stars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    catalogue: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ra_deg: Mapped[float] = mapped_column(Float, nullable=False)
    dec_deg: Mapped[float] = mapped_column(Float, nullable=False)
    period_d: Mapped[float] = mapped_column(Float, nullable=False)
    var_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mag: Mapped[float | None] = mapped_column(Float, nullable=True)
    mag_band: Mapped[str | None] = mapped_column(String(8), nullable=True)
    epoch_bjd: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (Index("idx_variable_stars_coords", "dec_deg", "ra_deg"),)
