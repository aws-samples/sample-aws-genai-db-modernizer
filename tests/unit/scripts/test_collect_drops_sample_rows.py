"""Every collection ingest drops sample rows and states how many (#387).

A collector output or raw collection written before #387 may carry table rows
(``sample_data``). ``run_assessment.py``, ``run_collect.py`` and the library ingest
(``ingest_offline_collection``, which ``assess()`` uses) write ``collector/output.json``
without them; the scripts' collect status line says how many rows were dropped.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import run_assessment, run_collect
from src.atx_orchestrator import core
from src.storage.local_store import LocalArtifactStore


def _contract() -> dict[str, Any]:
    return {
        "contract_version": "3.6",
        "job_id": "old",
        "metadata": {
            "collection_timestamp": "2026-10-16T10:00:00Z",
            "collector_version": "1.0.0",
            "source_database": {
                "engine": "mysql",
                "version": "8.0.45",
                "hostname": "h",
                "database_name": "shop",
            },
        },
        "database_schema": {
            "tables": [
                {
                    "table_id": "shop.orders",
                    "table_name": "orders",
                    "row_count": 2,
                    "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
                    "sample_data": [{"id": 1, "email": "a@example.com"}, {"id": 2}],
                }
            ]
        },
        "queries": {"query_patterns": []},
        "metrics": {"performance_metrics": {}},
    }


def _raw() -> dict[str, Any]:
    return {
        "collection_version": 1,
        "metadata": {"database_name": "shop", "version": "8.0.36"},
        "tables": [
            {"table_name": "orders", "row_count": 1, "sample_data": [{"email": "a@example.com"}]}
        ],
        "columns": [
            {
                "table_name": "orders",
                "column_name": "id",
                "data_type": "int",
                "is_nullable": False,
                "ordinal_position": 1,
            }
        ],
        "queries": [{"query_text": "SELECT id FROM orders", "digest": "d1"}],
        "global_stats": {},
    }


def _write(tmp_path: Path, data: dict) -> str:
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(data))
    return str(path)


def _status(capsys: pytest.CaptureFixture[str]) -> dict:
    lines = [ln for ln in capsys.readouterr().out.strip().splitlines() if ln.startswith("{")]
    return next(
        json.loads(ln) for ln in reversed(lines) if json.loads(ln).get("status") == "complete"
    )


def _no_file_holds_the_row(store_root: Path) -> None:
    """No file the tool wrote under the job (the uploads copy included) holds a row."""
    files = [p for p in store_root.rglob("*") if p.is_file()]
    assert files
    for path in files:
        assert "a@example.com" not in path.read_text(errors="ignore"), path


def _written(store_root: Path) -> dict:
    [path] = list(store_root.rglob("collector/output.json"))
    written: dict = json.loads(path.read_text())
    return written


@pytest.mark.parametrize(("data", "dropped"), [(_contract(), 2), (_raw(), 1)])
def test_run_assessment_drops_and_counts_rows(tmp_path, capsys, data, dropped) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    run_assessment.phase_collect(_write(tmp_path, data), None, store)

    status = _status(capsys)
    assert status["sample_rows_dropped"] == dropped
    written = _written(tmp_path / "artifacts")
    assert all("sample_data" not in t for t in written["database_schema"]["tables"])
    _no_file_holds_the_row(tmp_path / "artifacts")


@pytest.mark.passes_on_main
def test_run_assessment_states_nothing_when_there_are_no_rows(tmp_path, capsys) -> None:
    data = _contract()
    del data["database_schema"]["tables"][0]["sample_data"]
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    run_assessment.phase_collect(_write(tmp_path, data), None, store)
    assert "sample_rows_dropped" not in _status(capsys)


@pytest.mark.parametrize(("data", "dropped"), [(_contract(), 2), (_raw(), 1)])
def test_run_collect_drops_and_counts_rows(tmp_path, capsys, monkeypatch, data, dropped) -> None:
    root = tmp_path / "artifacts"
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_collect.py", "--file", _write(tmp_path, data), "--artifact-root", str(root)],
    )
    run_collect.main()

    assert _status(capsys)["sample_rows_dropped"] == dropped
    written = _written(root)
    assert all("sample_data" not in t for t in written["database_schema"]["tables"])
    _no_file_holds_the_row(root)


@pytest.mark.parametrize("script", ["run_assessment", "run_collect"])
def test_the_uploads_copy_of_a_raw_collection_holds_no_rows(
    tmp_path, capsys, monkeypatch, script
) -> None:
    root = tmp_path / "artifacts"
    path = _write(tmp_path, _raw())
    if script == "run_assessment":
        run_assessment.phase_collect(path, None, LocalArtifactStore(base_dir=str(root)))
    else:
        monkeypatch.setattr(
            sys, "argv", ["run_collect.py", "--file", path, "--artifact-root", str(root)]
        )
        run_collect.main()
    [upload] = list(root.rglob("uploads/collector-output.json"))
    copy = json.loads(upload.read_text())
    assert [t["table_name"] for t in copy["tables"]] == ["orders"]
    assert all("sample_data" not in t for t in copy["tables"])
    assert _status(capsys)["sample_rows_dropped"] == 1


class _FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}

    def write_json(self, path: str, data: dict) -> None:
        self.objects[path] = data


@pytest.mark.parametrize("data", [_contract(), _raw()])
def test_library_ingest_drops_rows(data) -> None:
    store = _FakeStore()
    result = core.ingest_offline_collection(store, "job-1", "shop", data)
    [written] = store.objects.values()
    for out in (result, written):
        assert all("sample_data" not in t for t in out["database_schema"]["tables"])
        assert "a@example.com" not in json.dumps(out, default=str)
