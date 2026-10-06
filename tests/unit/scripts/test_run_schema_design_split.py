"""Schema split CLI rejects single-pass Aurora engines before writing groups."""

from __future__ import annotations

import json
import sys

import pytest

from scripts import run_schema_design
from src.storage.local_store import LocalArtifactStore


@pytest.mark.parametrize(
    ("engine", "source_table"),
    [
        ("aurora_postgresql", "pg_catalog.pg_type"),
        ("aurora_mysql", "information_schema.tables"),
    ],
)
def test_split_rejects_aurora_without_writing_groups(
    engine, source_table, tmp_path, monkeypatch, capsys
):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        "mydb/job-001/collector/output.json",
        {
            "database_schema": {"tables": [{"table_id": "mydb.users", "table_name": "users"}]},
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "catalog-query",
                        "query_text": f"SELECT * FROM {source_table}",
                        "tables_accessed": [source_table],
                    }
                ]
            },
        },
    )
    store.write_json(f"mydb/job-001/analysis-{engine}/analysis.json", {})
    store.write_json(
        "mydb/job-001/assignment/v1/assignment.json",
        {
            "query_assignments": [
                {
                    "query_id": "catalog-query",
                    "assigned_engine": engine,
                    "source_tables": [source_table],
                    "in_scope": True,
                }
            ]
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_schema_design.py",
            "--job-id",
            "job-001",
            "--db",
            "mydb",
            "--engine",
            engine,
            "--artifact-root",
            str(tmp_path),
            "--assignment-version",
            "1",
            "--split",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        run_schema_design.main()

    assert exc.value.code == 1
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "error"
    assert engine in status["message"]
    assert "omit --split" in status["message"]
    assert not (tmp_path / "mydb" / "job-001" / f"schema-{engine}").exists()
