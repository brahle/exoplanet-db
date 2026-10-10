from __future__ import annotations

import json
from pathlib import Path

import pytest

from exoplanet_db.schemas.dossier import (
    CandidateDossier,
    DispositionEnum,
    EvidenceStatusEnum,
)


def test_roundtrip_tic_464547401_dossier():
    fixture_path = Path(__file__).parent.parent / "fixtures" / "tic-464547401-s01.json"
    with open(fixture_path, encoding="utf-8") as f:
        data = json.load(f)

    # 1. Parse into strict Pydantic model
    dossier = CandidateDossier.model_validate(data)

    assert dossier.id == "tic-464547401-s01"
    assert dossier.target.tic == "464547401"
    assert dossier.signal.period_d.value == pytest.approx(13.2443785, rel=1e-6)
    assert dossier.assessment.disposition == DispositionEnum.UNCLASSIFIED_TRANSIT_LIKE_SIGNAL
    assert dossier.assessment.source_localisation.status == EvidenceStatusEnum.UNRESOLVED
    assert dossier.assessment.timing_consistency.status == EvidenceStatusEnum.UNRESOLVED
    assert len(dossier.signal.limits) >= 1

    # 2. Dump back to JSON and verify field fidelity
    dumped = dossier.model_dump(mode="json")
    assert dumped["id"] == data["id"]
    assert dumped["schema_version"] == "1.0"
    assert dumped["target"]["tic"] == data["target"]["tic"]
    assert dumped["signal"]["period_d"]["value"] == data["signal"]["period_d"]["value"]


def test_all_disposition_states():
    fixture_path = Path(__file__).parent.parent / "fixtures" / "tic-464547401-s01.json"
    with open(fixture_path, encoding="utf-8") as f:
        data = json.load(f)

    # Test all valid dispositions from schema 1.0
    for disp in [
        DispositionEnum.UNCLASSIFIED_TRANSIT_LIKE_SIGNAL,
        DispositionEnum.KNOWN_SIGNAL,
        DispositionEnum.PLANET_CANDIDATE,
        DispositionEnum.VALIDATED_PLANET,
        DispositionEnum.CONFIRMED_PLANET,
        DispositionEnum.FALSE_POSITIVE,
    ]:
        data["assessment"]["disposition"] = disp.value
        dossier = CandidateDossier.model_validate(data)
        assert dossier.assessment.disposition == disp


def test_all_evidence_status_states():
    fixture_path = Path(__file__).parent.parent / "fixtures" / "tic-464547401-s01.json"
    with open(fixture_path, encoding="utf-8") as f:
        data = json.load(f)

    for status in [
        EvidenceStatusEnum.SUPPORTED,
        EvidenceStatusEnum.CONTRADICTED,
        EvidenceStatusEnum.UNRESOLVED,
    ]:
        data["assessment"]["source_localisation"]["status"] = status.value
        data["assessment"]["timing_consistency"]["status"] = status.value
        dossier = CandidateDossier.model_validate(data)
        assert dossier.assessment.source_localisation.status == status
        assert dossier.assessment.timing_consistency.status == status
