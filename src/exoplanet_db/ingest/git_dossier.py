from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exoplanet_db.ingest import dossier_vetting
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


HOST_PARAMETER_FIELDS = {
    "radius_Rsun": "radius_rsun",
    "mass_Msun": "mass_msun",
    "teff_K": "teff_k",
    "tmag": "tess_mag",
}


def host_parameters(host: dict[str, Any] | None) -> dict[str, float]:
    """Reported values from a dossier's host.parameters block (schema 1.2)."""
    params = (host or {}).get("parameters") or {}
    out: dict[str, float] = {}
    for key, attr in HOST_PARAMETER_FIELDS.items():
        p = params.get(key) or {}
        if p.get("status") == "reported" and isinstance(p.get("value"), int | float):
            out[attr] = float(p["value"])
    return out


class GitDossierIngest:
    """
    Ingests vetted candidate dossiers from git repositories (such as brahle/exoplanet-candidates).
    Enforces evidence manifest SHA-256 integrity, lossless schema adoption,
    and publishes staged snapshots.
    """

    def __init__(
        self,
        session: AsyncSession,
        known_hashes: dict[str, str] | None = None,
        reviews: dict[str, dict[str, Any]] | None = None,
    ):
        self.session = session
        # Repo-relative path -> sha256, for files hashed while streaming and never written out
        self.known_hashes = known_hashes
        # Supplementary physical-fit reviews by dossier id (reviews/*.json)
        self.reviews = reviews or {}

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
            if self.known_hashes is not None:
                key = f"{dossier_dir.parent.name}/{dossier_dir.name}/{rel_path}"
                if key not in self.known_hashes:
                    raise DossierValidationError(
                        f"Evidence file {rel_path} declared in manifest but missing from the archive"
                    )
                actual_sha = self.known_hashes[key]
            else:
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
        record_snapshot: bool = True,
    ) -> Signal:
        """
        Validates and ingests a single candidate dossier directory.

        record_snapshot=False when called from ingest_repository, which records one
        snapshot for the whole run instead of one per dossier.
        """
        # 1. Verify manifest
        verified_hashes = self.verify_evidence_manifest(dossier_dir)

        # 2. Parse candidate.json losslessly
        candidate_file = dossier_dir / "candidate.json"
        with open(candidate_file, encoding="utf-8") as f:
            candidate_raw = json.load(f)

        dossier = CandidateDossier.model_validate(candidate_raw)

        # 2b. Vetting evidence, read only from files the manifest seals
        vetting = dossier_vetting.read_dossier(
            dossier_dir, self.reviews.get(dossier.id), sealed=set(verified_hashes)
        )

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
        # Schema 1.2 publishes catalogue stellar parameters; fill whatever the star lacks
        for attr, value in host_parameters(dossier.host).items():
            if getattr(target, attr) is None:
                setattr(target, attr, value)
        if target.gaia_dr3_id is None and dossier.target.gaia_dr3:
            target.gaia_dr3_id = str(dossier.target.gaia_dr3)

        # 5. Signal matching & creation
        # Collect limits from all sections
        combined_limits: list[str] = []
        if dossier.signal.limits:
            combined_limits.extend(dossier.signal.limits)
        if dossier.novelty and dossier.novelty.limits:
            combined_limits.extend(dossier.novelty.limits)
        if dossier.reproducibility and dossier.reproducibility.limits:
            combined_limits.extend(dossier.reproducibility.limits)
        combined_limits.extend(dossier_vetting.limit_lines(vetting))

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

        # The dated assessment in vetting.json supersedes the one in candidate.json
        signal_disposition = dossier_vetting.disposition(vetting) or (
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
            # Drop what the previous version of this dossier contributed, then add the current
            previous = await self._previous_limits(git_repo, dossier.id)
            kept = [
                line
                for line in dossier_vetting.strip_lines(signal.scientific_limits)
                if line not in previous
            ]
            signal.scientific_limits = list(dict.fromkeys(kept + combined_limits))
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
            raw_payload=(
                {**candidate_raw, dossier_vetting.VETTING_KEY: vetting}
                if vetting
                else candidate_raw
            ),
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
        if record_snapshot:
            self.session.add(
                PublishedSnapshot(
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
            )
        await self.session.commit()
        return signal

    async def _previous_limits(self, git_repo: str, dossier_id: str) -> set[str]:
        """Limit lines the stored version of this dossier added to its signal."""
        raw = (
            await self.session.execute(
                select(Report.raw_payload).where(
                    Report.source_name == f"git:{git_repo}", Report.source_key == dossier_id
                )
            )
        ).scalar()
        if not isinstance(raw, dict):
            return set()
        lines: set[str] = set()
        for section in ("signal", "novelty", "reproducibility"):
            lines.update(((raw.get(section) or {}).get("limits")) or [])
        return lines

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

        if not self.reviews:
            self.reviews = dossier_vetting.load_reviews(repo_dir)
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
                            record_snapshot=False,
                        )
                    except Exception as exc:
                        await self.session.rollback()
                        failures.append(f"{item.name}: {exc}")
                        logger.warning("Dossier %s rejected: %s", item.name, exc)
                        continue
                    ingested.append(sig)
        now = datetime.now(UTC)
        self.session.add(
            PublishedSnapshot(
                source_name=f"git:{git_repo}",
                snapshot_utc=now,
                commit_sha=commit_sha,
                record_count=len(ingested),
                status="validated" if not failures else "partial",
                validation_metrics={"ingested": len(ingested), "rejected": failures[:50]},
                published_at=now,
            )
        )
        await self.session.commit()
        if failures:
            raise DossierValidationError(
                f"{len(failures)} of {len(failures) + len(ingested)} dossiers rejected "
                f"({len(ingested)} ingested): " + "; ".join(failures[:5])
            )
        return ingested


CANDIDATES_REPO = "brahle/exoplanet-candidates"

# The only files the ingest reads; everything else under candidates/ is hashed and dropped
_READ_FILES = re.compile(
    r"^(candidates/[^/]+/(candidate|evidence-manifest|publication-receipt|vetting)\.json"
    r"|candidates/[^/]+/evidence/(vetting-findings|external-report)\.json"
    r"|reviews/[^/]+\.json)$"
)


class _IterReader:
    """File-like read() over an iterator of byte chunks, for tarfile's stream mode."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._buf = b""

    def read(self, n: int = -1) -> bytes:
        while n < 0 or len(self._buf) < n:
            chunk = next(self._chunks, None)
            if chunk is None:
                break
            self._buf += chunk
        if n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out


def stream_repository(chunks, dest: Path) -> dict[str, str]:
    """Read a GitHub tarball once, as it arrives.

    Every file under candidates/ and reviews/ is hashed (for the evidence manifests); only
    the JSON the ingest reads is written to dest. Figures and arrays never touch disk or
    memory as a whole, so the repository can grow without the ingest pod growing with it.
    """
    import tarfile

    hashes: dict[str, str] = {}
    with tarfile.open(fileobj=_IterReader(chunks), mode="r|gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            parts = member.name.split("/", 1)
            if len(parts) != 2 or not parts[1].startswith(("candidates/", "reviews/")):
                continue
            rel = parts[1]
            if ".." in Path(rel).parts:
                continue
            keep = _READ_FILES.match(rel) is not None
            fh = tar.extractfile(member)
            if fh is None:
                continue
            digest, kept = hashlib.sha256(), []
            while chunk := fh.read(1 << 20):
                digest.update(chunk)
                if keep:
                    kept.append(chunk)
            hashes[rel] = digest.hexdigest()
            if keep:
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"".join(kept))
    return hashes


def _download_and_stream(url: str, dest: Path) -> dict[str, str]:
    import httpx

    with httpx.Client(timeout=300.0, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            return stream_repository(resp.iter_bytes(1 << 20), dest)


async def fetch_and_ingest_github_dossiers(
    session: AsyncSession, repo: str = CANDIDATES_REPO, ref: str = "main"
) -> int:
    """Streams `repo` at the current commit of `ref` and ingests every dossier.

    Uses the GitHub tarball (no git binary needed) and records the resolved commit, so
    each report links to a revision that exists and evidence hashes are checked against it.
    """
    import asyncio
    import tempfile

    from exoplanet_db.ingest.common import github_head_sha

    sha = await github_head_sha(repo, ref)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        hashes = await asyncio.to_thread(
            _download_and_stream, f"https://codeload.github.com/{repo}/tar.gz/{sha}", root
        )
        if not any(k.startswith("candidates/") for k in hashes):
            raise DossierValidationError(f"No candidates/ in the archive of {repo}@{sha}")
        signals = await GitDossierIngest(session, known_hashes=hashes).ingest_repository(
            root, commit_sha=sha, git_repo=repo
        )
    return len(signals)
