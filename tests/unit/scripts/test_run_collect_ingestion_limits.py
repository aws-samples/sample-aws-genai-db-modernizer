# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_collect.py`` enforces upload size/row caps before ingestion (#142).

Mirrors ``test_run_assessment_ingestion_limits.py`` for the standalone
``run_collect.py`` entry point used directly (not through ``run_assessment.py``).
"""

from __future__ import annotations

import json
import sys

import pytest

from scripts import run_collect

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


def _run(monkeypatch, tmp_path, collector_file: str) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_collect.py",
            "--file",
            collector_file,
            "--db",
            "testdb",
            "--artifact-root",
            str(tmp_path / "artifacts"),
        ],
    )
    run_collect.main()


def test_passes_under_default_limits(tmp_path, monkeypatch, capsys):
    collector_file = _write_collection(tmp_path, RAW_COLLECTION)

    _run(monkeypatch, tmp_path, collector_file)

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "complete"


def test_rejects_oversized_file_before_parsing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_BYTES", "10")
    collector_file = _write_collection(tmp_path, RAW_COLLECTION)

    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path, collector_file)

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "MODERNIZER_MAX_UPLOAD_BYTES" in out["message"]


def test_rejects_too_many_tables(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_TABLES", "3")
    collector_file = _write_collection(tmp_path, RAW_COLLECTION)

    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path, collector_file)

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "table" in out["message"]


def test_rejects_too_many_queries(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_QUERIES", "3")
    collector_file = _write_collection(tmp_path, RAW_COLLECTION)

    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path, collector_file)

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "quer" in out["message"]


def test_rejects_zip_archive_with_clear_message(tmp_path, monkeypatch, capsys):
    """#142 round 1 (I1): a zip that is not a collection archive must fail with
    a clear message. (A sample zip with its expected member is extracted, #331.)"""
    import zipfile

    bogus = tmp_path / "bogus.zip"
    with zipfile.ZipFile(bogus, "w") as zf:
        zf.writestr("something-else.txt", "x")
    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path, str(bogus))

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "zip archive" in out["message"]
    assert "unzip" in out["message"]


def test_rejects_non_utf8_file_with_clear_message(tmp_path, monkeypatch, capsys):
    collector_file = tmp_path / "collection.json"
    collector_file.write_bytes(b"\xff\xfe\x00\x01not utf-8")

    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path, str(collector_file))

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "UTF-8" in out["message"]
