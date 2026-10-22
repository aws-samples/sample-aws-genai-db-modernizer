"""The reality-check ``awaiting_llm.json`` marker is a versioned contract (ADR-008 Decision 1, #1267).

The marker was a plain dict, ``{"status": "awaiting_llm", "input_key": ...}`` while an
external-mode preview waits for the model and ``{"status": "finalized"}`` after. The
assignment lineage and the local API's job status read its ``status`` across runs. It is
now ``RealityCheckMarker``: the one writer writes through it, both readers load it
through the contract, and a marker saved before it (no ``contract_version``) still loads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.contracts.reality_check_marker import MarkerStatus, RealityCheckMarker
from src.shared.contract_versioning import (
    INTERNAL_ARTIFACTS,
    ContractVersionError,
    contract_for_artifact,
    current_version,
    is_internal_artifact,
    load_contract,
)
from src.storage.assignment_versioning import _completed_reality_check_output
from src.storage.local_store import LocalArtifactStore

DB, JOB = "mydb", "job-1"
KEY = f"{DB}/{JOB}/reality-check/awaiting_llm.json"
CURRENT = current_version(RealityCheckMarker)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
LEGACY_AWAITING = FIXTURES / "reality_check_awaiting_marker_pre_1267.json"
LEGACY_FINALIZED = FIXTURES / "reality_check_finalized_marker_pre_1267.json"


def _put(tmp_path: Path, data: dict) -> LocalArtifactStore:
    path = tmp_path / KEY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return LocalArtifactStore(base_dir=str(tmp_path))


# --- registration -------------------------------------------------------------


def test_the_marker_is_a_registered_contract_not_an_internal_artifact() -> None:
    assert contract_for_artifact(KEY) is RealityCheckMarker
    assert not is_internal_artifact(KEY)
    assert not any("awaiting_llm" in pattern for pattern, _ in INTERNAL_ARTIFACTS)


# --- the model ----------------------------------------------------------------


def test_status_is_awaiting_llm_or_finalized() -> None:
    assert {s.value for s in MarkerStatus} == {"awaiting_llm", "finalized"}
    with pytest.raises(ValidationError, match="status"):
        RealityCheckMarker.model_validate({"status": "done"})


def test_status_is_required() -> None:
    with pytest.raises(ValidationError, match="status"):
        RealityCheckMarker.model_validate({"input_key": "x"})


# --- reading: old files -------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "status"),
    [(LEGACY_AWAITING, "awaiting_llm"), (LEGACY_FINALIZED, "finalized")],
)
def test_a_marker_saved_before_1267_still_loads(fixture: Path, status: str, tmp_path: Path) -> None:
    legacy = json.loads(fixture.read_text())
    assert "contract_version" not in legacy
    store = _put(tmp_path, legacy)
    read = store.read_json(KEY)
    assert read == legacy
    loaded = load_contract(RealityCheckMarker, read, artifact=KEY)
    assert loaded.status.value == status
    assert loaded.contract_version == CURRENT


def test_the_finalized_marker_has_no_input_key_to_write() -> None:
    dumped = RealityCheckMarker(status=MarkerStatus.FINALIZED).model_dump(
        mode="json", exclude_none=True
    )
    assert dumped == {"contract_version": CURRENT, "status": "finalized"}


def test_the_store_refuses_a_marker_at_another_major(tmp_path: Path) -> None:
    store = _put(tmp_path, {"contract_version": "2.0", "status": "awaiting_llm"})
    with pytest.raises(ContractVersionError, match="RealityCheckMarker"):
        store.read_json(KEY)


def test_the_store_reads_a_marker_at_a_newer_minor(tmp_path: Path) -> None:
    newer = {"contract_version": "1.9", "status": "awaiting_llm", "x_added_later": 1}
    assert _put(tmp_path, newer).read_json(KEY) == newer


# --- the readers --------------------------------------------------------------


# A preview always carries output_assignment_version (null: a preview writes no
# revision); an output without the key was written before #192 (#193).
PREVIEW = {"source_assignment_version": 1, "output_assignment_version": None}


def _preview(store: LocalArtifactStore) -> None:
    store.write_json(f"{DB}/{JOB}/reality-check/output.json", PREVIEW)


def test_the_lineage_reader_ignores_the_preview_while_awaiting(tmp_path: Path) -> None:
    store = _put(tmp_path, {"contract_version": CURRENT, "status": "awaiting_llm"})
    _preview(store)
    assert _completed_reality_check_output(store, DB, JOB) == {}


@pytest.mark.passes_on_main
def test_the_lineage_reader_counts_the_output_once_finalized(tmp_path: Path) -> None:
    store = _put(tmp_path, {"contract_version": CURRENT, "status": "finalized"})
    _preview(store)
    assert _completed_reality_check_output(store, DB, JOB) == PREVIEW


def test_the_lineage_reader_still_reads_a_legacy_awaiting_marker(tmp_path: Path) -> None:
    store = _put(tmp_path, json.loads(LEGACY_AWAITING.read_text()))
    _preview(store)
    assert _completed_reality_check_output(store, DB, JOB) == {}


def test_the_lineage_reader_refuses_a_marker_it_cannot_read(tmp_path: Path) -> None:
    store = _put(tmp_path, {"contract_version": "2.0", "status": "awaiting_llm"})
    _preview(store)
    with pytest.raises(ContractVersionError):
        _completed_reality_check_output(store, DB, JOB)


# --- the local API reader ------------------------------------------------------


def _status(tmp_path: Path) -> str:
    from src.api.services.local_execution import LocalExecutionService  # noqa: PLC0415

    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {"metadata": {"source_database": {"engine": "mysql", "database_name": DB}}},
    )
    execution = LocalExecutionService(store).describe_execution(JOB)
    assert execution is not None
    return str(execution["status"])


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ({"contract_version": CURRENT, "status": "awaiting_llm"}, "WAITING"),
        (json.loads(LEGACY_AWAITING.read_text()), "WAITING"),
        ({"contract_version": CURRENT, "status": "finalized"}, "RUNNING"),
        (json.loads(LEGACY_FINALIZED.read_text()), "RUNNING"),
    ],
)
def test_the_local_api_reads_the_status_from_the_marker(
    tmp_path: Path, marker: dict, expected: str
) -> None:
    _put(tmp_path, marker)
    assert _status(tmp_path) == expected


class _PlainStore:
    """A store that does no registry check, as the lineage reader's duck type allows."""

    def __init__(self, artifacts: dict[str, dict]) -> None:
        self.artifacts = artifacts

    def exists(self, path: str) -> bool:
        return path in self.artifacts

    def read_json(self, path: str) -> dict:
        return self.artifacts[path]


def test_the_lineage_reader_loads_the_marker_itself_on_any_store() -> None:
    output = {"source_assignment_version": 1}
    out_key = f"{DB}/{JOB}/reality-check/output.json"
    bad_major = _PlainStore({KEY: {"contract_version": "2.0", "status": "finalized"}})
    with pytest.raises(ContractVersionError, match="RealityCheckMarker"):
        _completed_reality_check_output(bad_major, DB, JOB)
    unknown = _PlainStore({KEY: {"status": "done"}, out_key: output})
    with pytest.raises(ValidationError, match="status"):
        _completed_reality_check_output(unknown, DB, JOB)
    legacy = _PlainStore({KEY: {"status": "finalized"}, out_key: output})
    assert _completed_reality_check_output(legacy, DB, JOB) == output


# --- a bad marker (review round 1) ---------------------------------------------


def test_the_api_logs_why_a_marker_is_unreadable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _put(tmp_path, {"contract_version": "2.0", "status": "awaiting_llm"})
    with caplog.at_level("WARNING", logger="src.api.services.local_execution"):
        assert _status(tmp_path) == "RUNNING"
    assert "RealityCheckMarker" in caplog.text
    assert "contract_version '2.0'" in caplog.text


def test_the_lineage_fails_open_on_request(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bad = {"contract_version": "2.0", "status": "awaiting_llm"}
    store = _put(tmp_path, bad)
    _preview(store)
    with pytest.raises(ContractVersionError):
        _completed_reality_check_output(store, DB, JOB)
    with caplog.at_level("WARNING"):
        out = _completed_reality_check_output(store, DB, JOB, marker_fails_open=True)
    assert out == {}  # state unknown: the preview does not count as a run
    assert "treated as awaiting" in caplog.text
