"""LocalExecutionService reports the real source engine for script-run jobs.

Jobs started by the local scripts and Claude Code skills have no ``_meta.json``.
describe_execution used to fall back to a hardcoded "postgresql", so a MySQL
collection showed up as PostgreSQL in the UI.
"""

from __future__ import annotations

from pathlib import Path

from src.api.services.local_execution import LocalExecutionService
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-001"


def _service(tmp_path: Path) -> tuple[LocalExecutionService, LocalArtifactStore]:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    return LocalExecutionService(store), store


def _write_collector(store: LocalArtifactStore, engine: str) -> None:
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {"metadata": {"source_database": {"engine": engine, "database_name": DB}}},
    )


def test_engine_comes_from_collector_without_meta(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    _write_collector(store, "mysql")
    result = service.describe_execution(JOB)
    assert result is not None
    assert result["input"]["source_database_type"] == "mysql"


def test_meta_input_still_wins(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    _write_collector(store, "mysql")
    store.write_json(
        f"{DB}/{JOB}/_meta.json",
        {"input": {"database_name": DB, "source_database_type": "oracle"}},
    )
    result = service.describe_execution(JOB)
    assert result is not None
    assert result["input"]["source_database_type"] == "oracle"


def test_unknown_engine_is_none_not_postgresql(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    (tmp_path / DB / JOB).mkdir(parents=True)
    result = service.describe_execution(JOB)
    assert result is not None
    assert result["input"]["source_database_type"] is None
