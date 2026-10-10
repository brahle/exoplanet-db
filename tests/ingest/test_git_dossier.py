from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.ingest.git_dossier import GitDossierIngest
from exoplanet_db.models import (
    Base,
    EphemerisFit,
    PublishedSnapshot,
    Report,
    ReproducibilityRun,
    Signal,
    Target,
)


@pytest.mark.asyncio
async def test_git_dossier_ingest():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    dossier_path = (
        Path(__file__).parents[3] / "exoplanet-candidates" / "candidates" / "tic-464547401-s01"
    )
    if not dossier_path.exists():
        pytest.skip("exoplanet-candidates repository not present on disk")

    async with session_factory() as session:
        ingest = GitDossierIngest(session)
        signal = await ingest.ingest_dossier_directory(
            dossier_dir=dossier_path,
            commit_sha="c78b1ce43f110793b827e8a939f50cb36a71ab52",
        )
        assert signal is not None
        assert signal.primary_period_d is not None

    async with session_factory() as session:
        # Verify Target
        target_res = await session.execute(select(Target).where(Target.tic_id == "464547401"))
        target = target_res.scalars().first()
        assert target is not None
        assert target.primary_name == "TIC 464547401"

        # Verify Signal
        sig_res = await session.execute(select(Signal).where(Signal.target_id == target.id))
        sig = sig_res.scalars().first()
        assert sig is not None
        assert sig.current_disposition == "unclassified_transit_like_signal"
        assert sig.source_localisation_status == "unresolved"
        assert sig.timing_consistency_status == "unresolved"
        assert len(sig.scientific_limits) > 0

        # Verify EphemerisFit
        fit_res = await session.execute(
            select(EphemerisFit).where(EphemerisFit.signal_id == sig.id)
        )
        fit = fit_res.scalars().first()
        assert fit is not None
        assert fit.time_scale == "TDB"

        # Verify Report
        rep_res = await session.execute(select(Report).where(Report.signal_id == sig.id))
        rep = rep_res.scalars().first()
        assert rep is not None
        assert rep.source_name == "git:brahle/exoplanet-candidates"
        assert rep.license == "CC BY 4.0"

        # Verify ReproducibilityRun
        repro_res = await session.execute(
            select(ReproducibilityRun).where(ReproducibilityRun.report_id == rep.id)
        )
        repro = repro_res.scalars().first()
        assert repro is not None
        assert repro.level == "evidence_inspection"

        # Verify PublishedSnapshot
        snap_res = await session.execute(
            select(PublishedSnapshot).where(
                PublishedSnapshot.commit_sha == "c78b1ce43f110793b827e8a939f50cb36a71ab52"
            )
        )
        snap = snap_res.scalars().first()
        assert snap is not None
        assert snap.status == "validated"

    await engine.dispose()
