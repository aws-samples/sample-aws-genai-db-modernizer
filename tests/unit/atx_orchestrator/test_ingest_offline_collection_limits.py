"""``ingest_offline_collection`` enforces the table/query ingestion ceiling
for contract-format input (#142).

Non-contract (raw collection-script) input is counted inside
``parse_offline_collection`` (see ``tests/unit/tools/database/test_offline_parser.py``),
which this function calls unchanged. Contract-format input -- already in the
collector-output shape -- bypasses that parser, so it needs its own check,
exercised here.
"""

from __future__ import annotations

import pytest

from src.atx_orchestrator import core
from src.tools.validation.ingestion_limits import IngestionLimitError


class _FakeStore:
    def __init__(self) -> None:
        self._objects: dict[str, dict] = {}

    def write_json(self, path: str, data: dict) -> None:
        self._objects[path] = data

    def exists(self, path: str) -> bool:
        return path in self._objects


def _contract(table_count: int, query_count: int) -> dict:
    return {
        "contract_version": "3.0",
        "database_schema": {"tables": [{"table_name": f"t{i}"} for i in range(table_count)]},
        "queries": {"query_patterns": [{"query_id": f"q{i}"} for i in range(query_count)]},
    }


def test_passes_under_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_TABLES", "10")
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_QUERIES", "10")
    store = _FakeStore()

    result = core.ingest_offline_collection(store, "job-1", "testdb", _contract(2, 2))

    assert len(result["database_schema"]["tables"]) == 2


def test_rejects_too_many_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_TABLES", "3")
    store = _FakeStore()

    with pytest.raises(IngestionLimitError, match="table"):
        core.ingest_offline_collection(store, "job-1", "testdb", _contract(5, 0))

    assert not store.exists("testdb/job-1/collector/output.json")


def test_rejects_too_many_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_QUERIES", "3")
    store = _FakeStore()

    with pytest.raises(IngestionLimitError, match="quer"):
        core.ingest_offline_collection(store, "job-1", "testdb", _contract(0, 5))

    assert not store.exists("testdb/job-1/collector/output.json")


def test_explicit_null_database_schema_and_queries_do_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#142 round 1 (S3): explicit JSON ``null`` for ``database_schema``,
    ``tables``, or ``queries`` must count as zero, not raise."""
    store = _FakeStore()
    contract = {
        "contract_version": "3.0",
        "database_schema": {"tables": None},
        "queries": None,
    }

    result = core.ingest_offline_collection(store, "job-1", "testdb", contract)

    assert result == contract
