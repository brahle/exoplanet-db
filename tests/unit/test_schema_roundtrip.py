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


def test_upstream_nullable_fields_accepted():
    """brahle/exoplanet-candidates schema allows null Gaia ids and null provisional errors."""
    import pytest
    from pydantic import ValidationError

    from exoplanet_db.schemas.dossier import Measurement, TargetDossier

    assert Measurement(value=2.05, error=None).error is None
    with pytest.raises(ValidationError):
        Measurement(value=2.05, error=0.0)
    with pytest.raises(ValidationError):
        Measurement(value=2.05)  # still required, just nullable
    target = TargetDossier(
        tic="101909456",
        gaia_dr3=None,
        aliases=["TIC 101909456"],
        coordinates={"ra_deg": 153.44, "dec_deg": -43.49, "provenance": "TIC"},
    )
    assert target.gaia_dr3 is None


def test_later_schema_versions_extend_without_breaking():
    """Schema 1.2 adds target.catalogue_association and host; unknown fields are kept."""
    import json
    from pathlib import Path

    from exoplanet_db.ingest.git_dossier import host_parameters
    from exoplanet_db.schemas.dossier import CandidateDossier

    raw = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "tic-464547401-s01.json").read_text()
    )
    raw["schema_version"] = "1.2"
    raw["target"]["catalogue_association"] = {"status": "associated", "gaia_dr2": "1"}
    raw["host"] = {
        "status": "unresolved",
        "parameters": {
            "radius_Rsun": {"status": "reported", "value": 0.77},
            "mass_Msun": {"status": "reported", "value": 0.84},
            "teff_K": {"status": "unavailable", "value": None},
        },
    }
    raw["some_future_block"] = {"anything": True}
    dossier = CandidateDossier.model_validate(raw)
    assert dossier.target.catalogue_association["status"] == "associated"
    assert host_parameters(dossier.host) == {"radius_rsun": 0.77, "mass_msun": 0.84}
