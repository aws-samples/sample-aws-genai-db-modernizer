"""scripts._job_status.run_main: the shared catch-all every scripts/run_*.py
entry point's ``if __name__ == "__main__":`` guard uses (#517).

The marker/review primitives themselves (``write_failure_marker``,
``clear_failure_marker``, ``mark_assignment_reviewed``, re-exported here for
convenience) have their own dedicated tests at
tests/unit/shared/test_job_status.py, since the implementation lives in
src.shared.job_status (so LocalOrchestrator can use it too, #517 round-1
review I3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts._job_status import run_main
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-001"


def _store(tmp_path: Path) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", {"metadata": {}})
    return store


def test_system_exit_is_not_recorded_again(tmp_path: Path) -> None:
    """SystemExit means _error() already handled (and recorded, if
    appropriate) the failure -- run_main must not also write for it."""
    store = _store(tmp_path)

    def _main() -> None:
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        run_main(_main, lambda: (store, DB, JOB, "triage", None, True))

    assert not store.exists(f"{DB}/{JOB}/_status.json")


def test_unhandled_exception_records_a_fixed_content_free_message(tmp_path: Path) -> None:
    """#517 round-1 review C1: never str(exc). A pydantic ValidationError's
    own repr includes the invalid input verbatim -- here, a sentinel SQL
    value standing in for customer-derived content -- which must not reach
    the marker, only the exception's type and a fixed message."""
    from pydantic import BaseModel, ValidationError

    class _Model(BaseModel):
        value: int

    sentinel = "DROP TABLE super_secret_customers"
    store = _store(tmp_path)

    def _main() -> None:
        try:
            _Model(value=sentinel)  # type: ignore[arg-type]
        except ValidationError as exc:
            assert sentinel in str(exc)  # sanity: pydantic really does echo it
            raise

    with pytest.raises(ValidationError):
        run_main(_main, lambda: (store, DB, JOB, "triage", None, True))

    marker = store.read_json(f"{DB}/{JOB}/_status.json")
    assert marker["status"] == "FAILED"
    assert marker["error_type"] == "ValidationError"
    assert sentinel not in marker["message"]
    assert sentinel not in str(marker)


def test_keyboard_interrupt_records_interrupted(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def _main() -> None:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_main(_main, lambda: (store, DB, JOB, "synthesis", None, True))

    marker = store.read_json(f"{DB}/{JOB}/_status.json")
    assert marker["error_type"] == "KeyboardInterrupt"
    assert marker["message"] == "interrupted"


def test_no_job_context_yet_is_a_noop(tmp_path: Path) -> None:
    def _main() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run_main(_main, lambda: None)  # job/db never became known


def test_records_the_engine_for_a_fan_out_phase(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def _main() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run_main(_main, lambda: (store, DB, JOB, "schema_design", "dynamodb", True))

    marker = store.read_json(f"{DB}/{JOB}/_status.json")
    assert marker["engine"] == "dynamodb"


def test_require_existing_job_false_is_passed_through(tmp_path: Path) -> None:
    """run_collect.py's own phase: the job it is creating has no
    collector/output.json yet by definition (#517 round-1 review, I2)."""
    store = LocalArtifactStore(base_dir=str(tmp_path))  # no collector output

    def _main() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run_main(_main, lambda: (store, DB, JOB, "collect", None, False))

    marker = store.read_json(f"{DB}/{JOB}/_status.json")
    assert marker["status"] == "FAILED"


def test_success_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)

    run_main(lambda: None, lambda: (store, DB, JOB, "triage", None, True))

    assert not store.exists(f"{DB}/{JOB}/_status.json")
