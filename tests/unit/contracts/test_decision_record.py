"""The ``decisions/`` records are a versioned contract (ADR-008 Decision 1, #1257).

``decisions/assignment_review.json`` and ``decisions/assignment_approval.json`` are the
job's audit trail of human approvals; ``is_assignment_reviewed``, the local API's job
status and ``scripts/check_decision.py`` read them back across runs. They were plain
dicts with no ``contract_version``. They are now ``DecisionRecord``: the one writer
(``mark_assignment_reviewed``) writes through it, ``check_decision.py`` loads it, the
store checks its version on read, and a record saved before it still loads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts import check_decision
from src.contracts.decision_record import DecisionRecord
from src.shared.contract_versioning import (
    ContractVersionError,
    contract_for_artifact,
    current_version,
    is_internal_artifact,
    load_contract,
)
from src.shared.decision_records import has_valid_decision
from src.shared.job_status import is_assignment_reviewed, mark_assignment_reviewed
from src.storage.local_store import LocalArtifactStore

DB, JOB = "mydb", "job-1"
REVIEW = f"{DB}/{JOB}/decisions/assignment_review.json"
APPROVAL = f"{DB}/{JOB}/decisions/assignment_approval.json"
CURRENT = current_version(DecisionRecord)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
LEGACY_REVIEW = FIXTURES / "decision_assignment_review_pre_1257.json"
LEGACY_APPROVAL = FIXTURES / "decision_assignment_approval_pre_1257.json"


def _put(tmp_path: Path, key: str, data: dict) -> LocalArtifactStore:
    path = tmp_path / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return LocalArtifactStore(base_dir=str(tmp_path))


def _real_job(store: LocalArtifactStore) -> None:
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {"metadata": {"source_database": {"engine": "mysql", "database_name": DB}}},
    )


# --- registration -------------------------------------------------------------


@pytest.mark.parametrize("key", [REVIEW, APPROVAL, f"{DB}/{JOB}/decisions/triage_approval.json"])
def test_a_decision_record_is_a_registered_contract(key: str) -> None:
    assert contract_for_artifact(key) is DecisionRecord
    assert not is_internal_artifact(key)


def test_a_nested_path_under_decisions_is_not_a_record() -> None:
    assert contract_for_artifact(f"{DB}/{JOB}/decisions/nested/x.json") is None


# --- the model ----------------------------------------------------------------


def test_status_is_required() -> None:
    with pytest.raises(ValidationError, match="status"):
        DecisionRecord.model_validate({"source": "inferred"})


def test_a_record_keeps_the_keys_it_does_not_know() -> None:
    loaded = DecisionRecord.model_validate({"status": "approved", "approved_by": "someone"})
    assert loaded.model_dump(mode="json")["approved_by"] == "someone"


# --- writing ------------------------------------------------------------------


def test_the_review_gate_record_is_written_through_the_model(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _real_job(store)

    mark_assignment_reviewed(store, DB, JOB, "synthesis")

    written = json.loads((tmp_path / REVIEW).read_text())
    recorded_at = written.pop("recorded_at")
    assert recorded_at
    assert written == {
        "contract_version": CURRENT,
        "status": "completed",
        "source": "inferred",
        "inferred_from": "synthesis",
    }
    assert is_assignment_reviewed(store, DB, JOB)


# --- reading: old records -----------------------------------------------------


@pytest.mark.parametrize(("fixture", "key"), [(LEGACY_REVIEW, REVIEW), (LEGACY_APPROVAL, APPROVAL)])
def test_a_record_saved_before_1257_still_loads(fixture: Path, key: str, tmp_path: Path) -> None:
    legacy = json.loads(fixture.read_text())
    assert "contract_version" not in legacy
    store = _put(tmp_path, key, legacy)
    read = store.read_json(key)
    assert read == legacy
    loaded = load_contract(DecisionRecord, read, artifact=key)
    assert loaded.contract_version == CURRENT
    assert loaded.model_dump(mode="json", exclude_none=True) == {
        "contract_version": CURRENT,
        **legacy,
    }
    assert is_assignment_reviewed(store, DB, JOB) is True


def test_the_store_refuses_a_record_at_another_major(tmp_path: Path) -> None:
    store = _put(tmp_path, APPROVAL, {"contract_version": "2.0", "status": "approved"})
    with pytest.raises(ContractVersionError, match="DecisionRecord"):
        store.read_json(APPROVAL)


def test_the_store_reads_a_record_at_a_newer_minor(tmp_path: Path) -> None:
    newer = {"contract_version": "1.9", "status": "approved", "x_added_later": True}
    assert _put(tmp_path, APPROVAL, newer).read_json(APPROVAL) == newer


# --- check_decision.py --------------------------------------------------------


def _check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    monkeypatch.delenv("MODERNIZER_CI_SANDBOX", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_decision.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--decision",
            "assignment_approval",
            "--artifact-root",
            str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        check_decision.main()
    return int(exc.value.code), capsys.readouterr().out


def test_check_decision_prints_a_legacy_record_as_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = json.loads(LEGACY_APPROVAL.read_text())
    _put(tmp_path, APPROVAL, legacy)
    code, out = _check(tmp_path, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out) == legacy


def test_check_decision_still_exits_1_when_there_is_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _check(tmp_path, monkeypatch, capsys)
    assert (code, out) == (1, "")


def test_check_decision_refuses_a_record_that_is_not_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _put(tmp_path, APPROVAL, {"approved": True})
    code, out = _check(tmp_path, monkeypatch, capsys)
    assert code == 2
    result = json.loads(out)
    assert result["status"] == "error"
    assert "status" in result["message"]


def test_check_decision_refuses_a_record_at_another_major(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _put(tmp_path, APPROVAL, {"contract_version": "2.0", "status": "approved"})
    code, out = _check(tmp_path, monkeypatch, capsys)
    assert code == 2
    assert "DecisionRecord" in json.loads(out)["message"]


# --- a record that is not valid (review round 1) --------------------------------

BAD_RECORDS = [
    pytest.param("{not json", id="invalid-json"),
    pytest.param("[1, 2]", id="not-an-object"),
    pytest.param('{"approved": true}', id="no-status"),
    pytest.param('{"status": ""}', id="empty-status"),
    pytest.param('{"contract_version": "2.0", "status": "approved"}', id="other-major"),
]


def _put_text(tmp_path: Path, key: str, text: str) -> LocalArtifactStore:
    path = tmp_path / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return LocalArtifactStore(base_dir=str(tmp_path))


@pytest.mark.parametrize("text", BAD_RECORDS)
def test_check_decision_exits_2_with_the_error_line_for_any_bad_record(
    text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _put_text(tmp_path, APPROVAL, text)
    code, out = _check(tmp_path, monkeypatch, capsys)
    assert code == 2
    result = json.loads(out)
    assert result["status"] == "error"
    assert result["message"]


def test_the_error_does_not_echo_what_the_file_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _put(tmp_path, APPROVAL, {"status": ["secret-value-in-the-file"], "note": "also-secret"})
    code, out = _check(tmp_path, monkeypatch, capsys)
    assert code == 2
    assert "secret-value-in-the-file" not in out
    assert "also-secret" not in out
    assert "input_value" not in out
    assert "status" in json.loads(out)["message"]


@pytest.mark.parametrize("text", BAD_RECORDS)
def test_a_bad_record_does_not_count_as_an_approval(text: str, tmp_path: Path) -> None:
    for key in (REVIEW, APPROVAL):
        store = _put_text(tmp_path, key, text)
        _real_job(store)
        assert is_assignment_reviewed(store, DB, JOB) is False
        (tmp_path / key).unlink()


@pytest.mark.parametrize("key", [REVIEW, APPROVAL])
def test_a_valid_record_counts_at_either_path(key: str, tmp_path: Path) -> None:
    store = _put(tmp_path, key, {"status": "approved"})
    _real_job(store)
    assert is_assignment_reviewed(store, DB, JOB) is True


def test_a_refused_record_is_logged_without_its_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = _put(tmp_path, APPROVAL, {"status": ["secret-value"]})
    with caplog.at_level("WARNING"):
        assert has_valid_decision(store, APPROVAL) is False
    assert "decision record ignored" in caplog.text
    assert "secret-value" not in caplog.text


def test_the_local_api_keeps_waiting_when_the_approval_record_is_bad(tmp_path: Path) -> None:
    from src.api.services.local_execution import LocalExecutionService  # noqa: PLC0415

    store = _put(tmp_path, APPROVAL, {"approved": True})
    _real_job(store)
    store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", {"query_assignments": []})
    service = LocalExecutionService(store)
    execution = service.describe_execution(JOB)
    assert execution is not None
    assert execution["status"] == "WAITING"

    store.write_json(APPROVAL, {"status": "approved"})
    execution = service.describe_execution(JOB)
    assert execution is not None
    assert execution["status"] != "WAITING"
