# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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


def test_the_list_reads_the_engine_without_parsing_the_collector_file(tmp_path: Path) -> None:
    """#2345: the list reads one field per row; the whole file is not parsed (it can be MBs)."""
    service, store = _service(tmp_path)
    queries = [{"sql": "select 1 " + "x" * 200} for _ in range(10_000)]
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": "postgresql", "database_name": DB}},
            "queries": queries,
        },
    )
    assert (tmp_path / DB / JOB / "collector" / "output.json").stat().st_size > 2_000_000

    def refuse(path: str) -> dict:
        raise AssertionError(f"parsed the whole file: {path}")

    store.read_json = refuse  # type: ignore[method-assign]
    assert service.read_source_engine(DB, JOB) == "postgresql"


def test_an_engine_past_the_prefix_is_still_found(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {"padding": "x" * 100_000, "metadata": {"source_database": {"engine": "oracle"}}},
    )
    assert service.read_source_engine(DB, JOB) == "oracle"


def test_a_file_with_no_engine_has_none(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    store.write_json(f"{DB}/{JOB}/collector/output.json", {"metadata": {}})
    assert service.read_source_engine(DB, JOB) is None
    assert service.read_source_engine(DB, "no-such-job") is None


def test_an_engine_id_over_the_cap_is_not_returned_by_either_path(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {"metadata": {"source_database": {"engine": "x" * 65}}},
    )
    assert service.read_source_engine(DB, JOB) is None
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {"padding": "x" * 100_000, "metadata": {"source_database": {"engine": "y" * 65}}},
    )
    assert service.read_source_engine(DB, JOB) is None
