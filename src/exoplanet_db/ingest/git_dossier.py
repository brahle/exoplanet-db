from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exoplanet_db.ingest.common import (
    signal_for_existing_report,
    upsert_fit,
    upsert_report,
    upsert_repro_run,
)
from exoplanet_db.models import (
    EphemerisFit,
    PublishedSnapshot,
    Report,
    ReproducibilityRun,
    Signal,
    Target,
)
from exoplanet_db.schemas.dossier import CandidateDossier
from exoplanet_db.services.reconciliation import match_signal_to_existing

logger = logging.getLogger(__name__)


class DossierValidationError(Exception):
    pass


class GitDossierIngest:
    """
    Ingests vetted candidate dossiers from git repositories (such as brahle/exoplanet-candidates).
    Enforces evidence manifest SHA-256 integrity, lossless schema adoption,
    and publishes staged snapshots.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    def verify_evidence_manifest(self, dossier_dir: Path) -> dict[str, str]:
        """
        Verify all evidence files match SHA-256 hashes listed in evidence-manifest.json.
        Returns a mapping of relative path -> verified sha256.
        """
        manifest_file = dossier_dir / "evidence-manifest.json"
        if not manifest_file.exists():
            raise DossierValidationError(f"Missing evidence-manifest.json in {dossier_dir}")

        with open(manifest_file, encoding="utf-8") as f:
            manifest_data = json.load(f)

        verified_hashes: dict[str, str] = {}
        files = manifest_data.get("files", [])
        for file_entry in files:
            rel_path = file_entry["path"]
            expected_sha = file_entry["sha256"]
            target_path = dossier_dir / rel_path

            if not target_path.exists():
                raise DossierValidationError(
                    f"Evidence file {rel_path} declared in manifest but missing on disk"
                )

            with open(target_path, "rb") as bf:
                actual_sha = hashlib.sha256(bf.read()).hexdigest()

            if actual_sha.lower() != expected_sha.lower():
                raise DossierValidationError(
                    f"Checksum mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}"
                )

            verified_hashes[rel_path] = actual_sha

        return verified_hashes

    async def ingest_dossier_directory(
        self,
        dossier_dir: Path,
        commit_sha: str,
        git_repo: str = "brahle/exoplanet-candidates",
    ) -> Signal:
        """
        Validates and ingests a single candidate dossier directory.
        """
        # 1. Verify manifest
        verified_hashes = self.verify_evidence_manifest(dossier_dir)

        # 2. Parse candidate.json losslessly
        candidate_file = dossier_dir / "candidate.json"
        with open(candidate_file, encoding="utf-8") as f:
            candidate_raw = json.load(f)

        dossier = CandidateDossier.model_validate(candidate_raw)

        # 3. Read receipt if present
        receipt_file = dossier_dir / "publication-receipt.json"
        receipt_data: dict[str, Any] = {}
        repro_level = "evidence_inspection"
        if receipt_file.exists():
            with open(receipt_file, encoding="utf-8") as f:
                receipt_data = json.load(f)
                repro_level = receipt_data.get("reproducibility_level", repro_level)

        # 4. Target upsert / reconciliation
        tic = str(dossier.target.tic)
        target_name = f"TIC {tic}"
        aliases = dossier.target.aliases or [f"TIC {tic}"]

        res = await self.session.execute(
            select(Target).where((Target.tic_id == tic) | (Target.primary_name == target_name))
        )
        target = res.scalars().first()
        if not target:
            target = Target(
                primary_name=target_name,
                tic_id=tic,
                gaia_dr3_id=str(dossier.target.gaia_dr3) if dossier.target.gaia_dr3 else None,
                ra_deg=dossier.target.coordinates.ra_deg,
                dec_deg=dossier.target.coordinates.dec_deg,
                epoch=str(dossier.target.coordinates.epoch or "J2000"),
                aliases=aliases,
            )
            self.session.add(target)
            await self.session.flush()

        # 5. Signal matching & creation
        # Collect limits from all sections
        combined_limits: list[str] = []
        if dossier.signal.limits:
            combined_limits.extend(dossier.signal.limits)
        if dossier.novelty and dossier.novelty.limits:
            combined_limits.extend(dossier.novelty.limits)
        if dossier.reproducibility and dossier.reproducibility.limits:
            combined_limits.extend(dossier.reproducibility.limits)

        existing_signals_res = await self.session.execute(
            select(Signal).where(Signal.target_id == target.id)
        )
        existing_signals = list(existing_signals_res.scalars().all())

        matched_signal = await signal_for_existing_report(
            self.session, f"git:{git_repo}", dossier.id
        ) or match_signal_to_existing(
            existing_signals=existing_signals,
            cand_period=dossier.signal.period_d.value,
            cand_epoch=dossier.signal.epoch_bjd_tdb,
            period_err=dossier.signal.period_d.error,
            epoch_err=dossier.signal.epoch_btjd.error,
        )

        signal_disposition = (
            str(dossier.assessment.disposition) if dossier.assessment else "planet_candidate"
        )
        source_loc = (
            str(dossier.assessment.source_localisation.status)
            if dossier.assessment
            else "supported"
        )
        timing_cons = (
            str(dossier.assessment.timing_consistency.status) if dossier.assessment else "supported"
        )

        if matched_signal:
            signal = matched_signal
            other_sources = (
                await self.session.execute(
                    select(Report.id).where(
                        Report.signal_id == signal.id,
                        Report.source_name != f"git:{git_repo}",
                    )
                )
            ).first()
            # Our provisional assessment only classifies signals no catalogue reports;
            # it never downgrades a TOI or a confirmed planet to "unclassified".
            if not other_sources:
                signal.current_disposition = signal_disposition
                signal.source_localisation_status = source_loc
                signal.timing_consistency_status = timing_cons
            signal.scientific_limits = list(
                dict.fromkeys((signal.scientific_limits or []) + combined_limits)
            )
        else:
            signal = Signal(
                target_id=target.id,
                display_name=f"{target.primary_name}.01",
                primary_period_d=dossier.signal.period_d.value,
                primary_period_err_d=dossier.signal.period_d.error,
                primary_epoch_bjd=dossier.signal.epoch_bjd_tdb,
                primary_duration_h=dossier.signal.duration_h.value,
                primary_depth_ppm=dossier.signal.depth_ppm.value,
                current_disposition=signal_disposition,
                source_localisation_status=source_loc,
                timing_consistency_status=timing_cons,
                scientific_limits=combined_limits,
            )
            self.session.add(signal)
            await self.session.flush()

        # 6. Report record
        now = datetime.now(UTC)
        report = Report(
            target_id=target.id,
            signal_id=signal.id,
            source_name=f"git:{git_repo}",
            source_key=dossier.id,
            retrieval_utc=now,
            commit_sha=commit_sha,
            author_or_team=git_repo,
            bibcode_or_url=f"https://github.com/{git_repo}/tree/{commit_sha}/candidates/{dossier_dir.name}",
            reported_disposition=signal_disposition,
            period_d=dossier.signal.period_d.value,
            period_err_d=dossier.signal.period_d.error,
            epoch_bjd=dossier.signal.epoch_bjd_tdb,
            epoch_err_d=dossier.signal.epoch_btjd.error,
            depth_ppm=dossier.signal.depth_ppm.value,
            duration_h=dossier.signal.duration_h.value,
            raw_payload=candidate_raw,
            license="CC BY 4.0",
        )
        report = await upsert_report(self.session, report)

        # 7. Ephemeris fit record
        ephem_fit = EphemerisFit(
            report_id=report.id,
            signal_id=signal.id,
            period_d=dossier.signal.period_d.value,
            period_err_d=dossier.signal.period_d.error,
            epoch_bjd_tdb=dossier.signal.epoch_bjd_tdb,
            epoch_err_d=dossier.signal.epoch_btjd.error,
            covariance_matrix=dossier.signal.covariance_epoch_period_d2,
            covariance_method=dossier.signal.covariance_method,
            duration_h=dossier.signal.duration_h.value,
            depth_ppm=dossier.signal.depth_ppm.value,
            duration_err_h=dossier.signal.duration_h.error,
            depth_err_ppm=dossier.signal.depth_ppm.error,
            time_scale=dossier.signal.time_scale,
            fit_model=dossier.signal.model,
            is_coherent=True,
        )
        ephem_fit = await upsert_fit(self.session, ephem_fit)

        # 8. ReproducibilityRun record
        cmd = None
        repro_limits: list[str] = []
        if dossier.reproducibility:
            cmd = dossier.reproducibility.command
            repro_limits = dossier.reproducibility.limits

        repro_run = ReproducibilityRun(
            report_id=report.id,
            pipeline_name="exoplanet-candidates",
            pipeline_version=commit_sha[:8],
            execution_utc=now,
            level=str(repro_level),
            command=cmd,
            status=source_loc,
            metrics={"verified_files_count": len(verified_hashes)},
            limits=repro_limits,
        )
        repro_run = await upsert_repro_run(self.session, repro_run)

        # 9. Published snapshot record
        snapshot = PublishedSnapshot(
            source_name=f"git:{git_repo}",
            snapshot_utc=now,
            commit_sha=commit_sha,
            record_count=1,
            status="validated",
            validation_metrics={
                "candidate": dossier.id,
                "verified_files": len(verified_hashes),
            },
            published_at=now,
        )
        self.session.add(snapshot)
        await self.session.commit()
        return signal

    async def ingest_repository(
        self,
        repo_dir: Path,
        commit_sha: str | None = None,
        git_repo: str = "brahle/exoplanet-candidates",
    ) -> list[Signal]:
        """
        Scans a candidate git repository and ingests all candidate dossiers found.
        """
        import subprocess

        if commit_sha is None:
            try:
                out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True)
                commit_sha = out.strip()
            except Exception:
                commit_sha = "HEAD"

        candidates_dir = repo_dir / "candidates"
        ingested = []
        failures: list[str] = []
        if candidates_dir.exists():
            for item in sorted(candidates_dir.iterdir()):
                if item.is_dir() and (item / "candidate.json").exists():
                    # One invalid dossier must not block the rest; each one commits on its own
                    try:
                        sig = await self.ingest_dossier_directory(
                            dossier_dir=item,
                            commit_sha=commit_sha,
                            git_repo=git_repo,
                        )
                    except Exception as exc:
                        await self.session.rollback()
                        failures.append(f"{item.name}: {exc}")
                        logger.warning("Dossier %s rejected: %s", item.name, exc)
                        continue
                    ingested.append(sig)
        if failures:
            raise DossierValidationError(
                f"{len(failures)} of {len(failures) + len(ingested)} dossiers rejected "
                f"({len(ingested)} ingested): " + "; ".join(failures[:5])
            )
        return ingested


CANDIDATES_REPO = "brahle/exoplanet-candidates"


async def fetch_and_ingest_github_dossiers(
    session: AsyncSession, repo: str = CANDIDATES_REPO, ref: str = "main"
) -> int:
    """Downloads `repo` at the current commit of `ref` and ingests every dossier.

    Uses the GitHub tarball (no git binary needed) and records the resolved commit, so
    each report links to a revision that exists and evidence hashes are checked against it.
    """
    import tarfile
    import tempfile

    import httpx

    from exoplanet_db.ingest.common import github_head_sha

    sha = await github_head_sha(repo, ref)
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        resp = await client.get(f"https://codeload.github.com/{repo}/tar.gz/{sha}")
        resp.raise_for_status()
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "repo.tar.gz"
        archive.write_bytes(resp.content)
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise DossierValidationError(f"Unexpected archive layout for {repo}@{sha}")
        signals = await GitDossierIngest(session).ingest_repository(
            roots[0], commit_sha=sha, git_repo=repo
        )
    return len(signals)
