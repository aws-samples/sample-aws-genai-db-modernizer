"""``run_assessment.py``'s collect phase enforces upload size/row caps (#142).

A pathological or oversized offline collection must be rejected with a clear
message before (or instead of) the pipeline doing real work on it. These
tests exercise ``phase_collect`` directly against small, deterministic
fixtures with the ingestion limits dialed down via env vars, rather than
against the full bundled samples.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_assessment
from src.storage.local_store import LocalArtifactStore

REPO_ROOT = Path(__file__).resolve().parents[3]
WORDPRESS_ZIP = REPO_ROOT / "docs" / "examples" / "wordpress" / "wordpress.zip"

RAW_COLLECTION = {
    "collection_version": 1,
    "metadata": {"database_name": "testdb", "version": "8.0.0"},
    "tables": [{"table_name": f"t{i}", "row_count": 1} for i in range(5)],
    "columns": [
        {
            "table_name": f"t{i}",
            "column_name": "id",
            "data_type": "int",
            "is_nullable": False,
            "ordinal_position": 1,
        }
        for i in range(5)
    ],
    "indexes": [],
    "foreign_keys": [],
    "primary_keys": [],
    "queries": [{"query_text": f"SELECT {i}", "digest": f"d{i}"} for i in range(5)],
    "global_stats": {},
}


def _write_collection(tmp_path, data: dict) -> str:
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_passes_under_default_limits(tmp_path, capsys):
    collector_file = _write_collection(tmp_path, RAW_COLLECTION)
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))

    job_id, db_name = run_assessment.phase_collect(collector_file, None, store)

    assert db_name == "testdb"
    assert job_id


def test_rejects_oversized_file_before_parsing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_BYTES", "10")
    collector_file = _write_collection(tmp_path, RAW_COLLECTION)
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))

    with pytest.raises(SystemExit):
        run_assessment.phase_collect(collector_file, None, store)

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "exceeds" in out["message"]
    assert "MODERNIZER_MAX_UPLOAD_BYTES" in out["message"]


def test_rejects_too_many_tables(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_TABLES", "3")
    collector_file = _write_collection(tmp_path, RAW_COLLECTION)
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))

    with pytest.raises(SystemExit):
        run_assessment.phase_collect(collector_file, None, store)

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "table" in out["message"]


def test_rejects_too_many_queries(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_QUERIES", "3")
    collector_file = _write_collection(tmp_path, RAW_COLLECTION)
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))

    with pytest.raises(SystemExit):
        run_assessment.phase_collect(collector_file, None, store)

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "quer" in out["message"]


def test_rejects_oversized_contract_format_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_TABLES", "1")
    contract = {
        "contract_version": "3.0",
        "metadata": {"source_database": {"database_name": "testdb"}},
        "database_schema": {"tables": [{"table_name": "a"}, {"table_name": "b"}]},
        "queries": {"query_patterns": []},
    }
    collector_file = _write_collection(tmp_path, contract)
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))

    with pytest.raises(SystemExit):
        run_assessment.phase_collect(collector_file, None, store)

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "table" in out["message"]


def test_rejects_zip_archive_with_clear_message(tmp_path, capsys):
    """#142 round 1 (I1): the bundled samples are shipped as .zip; passing one
    directly (instead of the .json inside) must fail with a clear, actionable
    message rather than an opaque JSON-decode error."""
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))

    with pytest.raises(SystemExit):
        run_assessment.phase_collect(str(WORDPRESS_ZIP), None, store)

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "zip archive" in out["message"]
    assert "unzip" in out["message"]


def test_rejects_non_utf8_file_with_clear_message(tmp_path, capsys):
    collector_file = tmp_path / "collection.json"
    collector_file.write_bytes(b"\xff\xfe\x00\x01not utf-8")

    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))

    with pytest.raises(SystemExit):
        run_assessment.phase_collect(str(collector_file), None, store)

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "UTF-8" in out["message"]
