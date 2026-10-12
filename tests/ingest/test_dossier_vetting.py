from __future__ import annotations

import copy
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from exoplanet_db.ingest.dossier_vetting import LIMIT_PREFIX
from exoplanet_db.ingest.git_dossier import GitDossierIngest, stream_repository
from exoplanet_db.models import Base, Report, Signal
from exoplanet_db.services.catalog import CatalogService

FIXTURE = Path(__file__).parents[1] / "fixtures" / "tic-464547401-s01.json"
DOSSIER = "tic-464547401-s01"


def _vetting(warnings, screen="warnings", status="unclassified"):
    return {
        "schema_version": "1.0",
        "signal_id": DOSSIER,
        "assessed_utc": "2026-10-11T20:12:12Z",
        "progress": {
            "stage": "source_vetting",
            "stages": {
                "screening": "complete",
                "source_vetting": "partial",
                "physical_vetting": "not_started",
                "followup": "not_started",
                "validation": "not_started",
                "confirmation": "not_started",
            },
        },
        "scientific_assessment": {
            "status": status,
            "planet_probability": None,
            "basis": "Transit-like signal.",
            "review_evidence": [],
        },
        "initial_screen": {"outcome": screen, "all_checks_available": True},
        "warnings": warnings,
        "open_requirements": ["timing_consistency"],
        "evidence": [{"path": "evidence/vetting-findings.json", "sha256": "0" * 64}],
    }


FINDINGS = {
    "signal_id": DOSSIER,
    "lightcurve": {
        "odd_even_nominal_contrast_multiple": 3.2,
        "phase_half_secondary_nominal_depth_multiple": 0.4,
        "flags": {"odd_even": True, "phase_half_secondary": False},
    },
    "catalogues": {"ruwe": 1.1, "matches": []},
    "source_diagnostics": {
        "scientific_status": "unresolved",
        "sectors": [{"sector": 37}, {"sector": 64}],
    },
    "source_fit_offsets": [
        {"fit_success": True, "bound_hit": False, "blur_pixels": 0.0, "target_offset_pixels": 4.3},
        {"fit_success": True, "bound_hit": False, "blur_pixels": 0.0, "target_offset_pixels": 4.25},
        {"fit_success": True, "bound_hit": False, "blur_pixels": 1.0, "target_offset_pixels": 9.0},
    ],
    "physical_pilot": [{"sampling_status": "sampling_not_qualified"}] * 6,
}


def _write_dossier(root: Path, candidate: dict, vetting: dict | None, seal_vetting: bool = True):
    d = root / "candidates" / DOSSIER
    (d / "evidence").mkdir(parents=True, exist_ok=True)
    files = {"candidate.json": candidate, "evidence/vetting-findings.json": FINDINGS}
    if vetting is not None:
        files["vetting.json"] = vetting
    entries = []
    for rel, data in files.items():
        raw = json.dumps(data).encode()
        (d / rel).write_bytes(raw)
        if rel != "vetting.json" or seal_vetting:
            entries.append({"path": rel, "sha256": hashlib.sha256(raw).hexdigest()})
    (d / "evidence-manifest.json").write_text(json.dumps({"files": entries}))
    return d


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _signal(factory):
    async with factory() as s:
        sig = (await s.execute(select(Signal))).scalars().one()
        rep = (await s.execute(select(Report))).scalars().one()
        return sig, rep


async def test_vetting_is_summarised_into_report_and_limits(factory, tmp_path):
    candidate = json.loads(FIXTURE.read_text())
    _write_dossier(tmp_path, candidate, _vetting(["odd_even", "off_target_diagnostic"]))
    async with factory() as s:
        await GitDossierIngest(s).ingest_repository(tmp_path, commit_sha="a" * 40)
    sig, rep = await _signal(factory)

    vet = rep.raw_payload["_vetting"]
    assert vet["stage"] == "source_vetting" and vet["warnings"] == [
        "odd_even",
        "off_target_diagnostic",
    ]
    assert vet["lightcurve"]["odd_even_contrast"] == 3.2
    assert vet["source_diagnostics"] == {
        "status": "unresolved",
        "sectors": [37, 64],
        "target_offset_pixels": [4.25, 4.3],  # blurred treatment excluded
    }
    assert vet["physical_pilot"] == {"scenarios": 6, "sampling_qualified": 0}
    # candidate.json is still stored losslessly beside the summary
    assert rep.raw_payload["id"] == DOSSIER

    lines = [line for line in sig.scientific_limits if line.startswith(LIMIT_PREFIX)]
    assert any("odd and even transits differ" in line for line in lines)
    assert any("4.25–4.3 px from the target" in line for line in lines)
    assert any("0 of 6 scenarios" in line for line in lines)

    async with factory() as s:
        detail = await CatalogService(s).get_candidate_detail(sig.id)
    src = detail["sources"][0]
    assert src["vetting"]["url"].endswith(f"/candidates/{DOSSIER}/evidence/vetting-findings.json")


async def test_reingest_replaces_previous_version_lines(factory, tmp_path):
    candidate = json.loads(FIXTURE.read_text())
    _write_dossier(tmp_path, candidate, _vetting(["odd_even"]))
    async with factory() as s:
        await GitDossierIngest(s).ingest_repository(tmp_path, commit_sha="a" * 40)

    newer = copy.deepcopy(candidate)
    newer["signal"]["limits"] = ["Rewritten limit in the next dossier version."]
    _write_dossier(tmp_path, newer, _vetting([], screen="no_initial_warnings"))
    async with factory() as s:
        await GitDossierIngest(s).ingest_repository(tmp_path, commit_sha="b" * 40)
    sig, rep = await _signal(factory)

    assert "Rewritten limit in the next dossier version." in sig.scientific_limits
    for old in candidate["signal"]["limits"]:
        assert old not in sig.scientific_limits
    assert not any("odd and even" in line for line in sig.scientific_limits)
    assert len(sig.scientific_limits) == len(set(sig.scientific_limits))
    assert rep.raw_payload["_vetting"]["initial_screen"] == "no_initial_warnings"


async def test_vetting_status_sets_disposition_and_unsealed_files_are_ignored(factory, tmp_path):
    candidate = json.loads(FIXTURE.read_text())
    _write_dossier(tmp_path, candidate, _vetting([], status="false_positive"), seal_vetting=False)
    async with factory() as s:
        await GitDossierIngest(s).ingest_repository(tmp_path, commit_sha="a" * 40)
    sig, rep = await _signal(factory)
    # vetting.json is not in the manifest: only the sealed findings are used
    assert rep.raw_payload["_vetting"]["assessment"] is None
    assert sig.current_disposition == "unclassified_transit_like_signal"

    _write_dossier(tmp_path, candidate, _vetting([], status="false_positive"))
    async with factory() as s:
        await GitDossierIngest(s).ingest_repository(tmp_path, commit_sha="b" * 40)
    sig, _ = await _signal(factory)
    assert sig.current_disposition == "false_positive"


async def test_streamed_archive_hashes_everything_and_keeps_only_json(factory, tmp_path):
    src = tmp_path / "src"
    _write_dossier(src, json.loads(FIXTURE.read_text()), _vetting(["odd_even"]))
    figure = src / "candidates" / DOSSIER / "evidence" / "figures" / "big.png"
    figure.parent.mkdir(parents=True)
    figure.write_bytes(b"\x89PNG" + b"0" * 5000)
    # the figure is sealed too, so verification needs its hash without the file
    manifest_path = src / "candidates" / DOSSIER / "evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"].append(
        {
            "path": "evidence/figures/big.png",
            "sha256": hashlib.sha256(figure.read_bytes()).hexdigest(),
        }
    )
    manifest_path.write_text(json.dumps(manifest))
    (src / "reviews").mkdir()
    (src / "reviews" / "fit.json").write_text(
        json.dumps(
            {
                "reviewed_utc": "2026-10-11T21:32:58Z",
                "signals": [
                    {
                        "signal_id": DOSSIER,
                        "scenario": {"name": "catalogue-white"},
                        "sampling": {"status": "conditional_sampling_checks_passed"},
                        "conditional_draw_quantiles_16_50_84": {
                            "radius_ratio": [0.18, 0.187, 0.19]
                        },
                        "conditional_median_density_ratio_to_prior_center": 41.7,
                        "conditional_median_log_density_difference_in_declared_prior_widths": 16.2,
                    }
                ],
            }
        )
    )

    # A later review of another kind must not displace the sampled one
    (src / "reviews" / "point.json").write_text(
        json.dumps(
            {
                "reviewed_utc": "2026-10-11T21:53:34Z",
                "signals": [
                    {
                        "signal_id": DOSSIER,
                        "point_density_ratio_to_assumed_prior_center": 42.04,
                        "optimizer_starts": 4,
                        "optimizer_successes": 4,
                    }
                ],
            }
        )
    )
    (src / "reviews" / "point-replay.json").write_text(
        json.dumps(
            {"signals": [{"signal_id": DOSSIER, "status": "replay_passed", "fixed_vectors": 10}]}
        )
    )
    (src / "reviews" / "novel.json").write_text(
        json.dumps({"reviewed_utc": "2026-10-12", "signals": [{"signal_id": DOSSIER, "x": 1}]})
    )
    # batch-level reviews: "signals" is a count, the dossier is named elsewhere
    (src / "reviews" / "batch.json").write_text(
        json.dumps({"signals": 24, "groups": [{"members": [DOSSIER, "tic-1-s01"]}]})
    )
    (src / "reviews" / "broken.json").write_text("{not json")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(src, arcname="exoplanet-candidates-abc")
        evil = tarfile.TarInfo("exoplanet-candidates-abc/candidates/../../escape.json")
        evil.size = 2
        tar.addfile(evil, io.BytesIO(b"{}"))
    data = buf.getvalue()

    dest = tmp_path / "dest"
    hashes = stream_repository((data[i : i + 1000] for i in range(0, len(data), 1000)), dest)
    assert f"candidates/{DOSSIER}/evidence/figures/big.png" in hashes
    assert not (dest / "candidates" / DOSSIER / "evidence" / "figures").exists()
    assert (dest / "candidates" / DOSSIER / "vetting.json").exists()
    assert not (tmp_path / "escape.json").exists() and not any("escape" in k for k in hashes)

    async with factory() as s:
        await GitDossierIngest(s, known_hashes=hashes).ingest_repository(dest, commit_sha="c" * 40)
    sig, rep = await _signal(factory)
    review = rep.raw_payload["_vetting"]["physical_fit_review"]
    assert review["radius_ratio"] == 0.187 and review["density_tension_prior_widths"] == 16.2
    point = rep.raw_payload["_vetting"]["point_fit_review"]
    assert point["density_ratio_to_catalogue"] == 42.04 and point["replay"] == "replay_passed"
    assert rep.raw_payload["_vetting"]["other_reviews"] == [
        "reviews/batch.json",
        "reviews/novel.json",
    ]
    assert any(
        "4/4 optimizer starts converged; fresh-environment replay passed" in line
        for line in sig.scientific_limits
    )
    assert any("in tension with a planet" in line for line in sig.scientific_limits)


async def test_missing_sealed_file_in_archive_rejects_dossier(factory, tmp_path):
    _write_dossier(tmp_path, json.loads(FIXTURE.read_text()), _vetting([]))
    async with factory() as s:
        with pytest.raises(Exception, match="missing from the archive"):
            await GitDossierIngest(s, known_hashes={}).ingest_repository(
                tmp_path, commit_sha="a" * 40
            )
