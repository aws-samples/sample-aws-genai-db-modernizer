"""``--merge`` then ``--finalize`` flag unknown DynamoDB source columns (#1431).

This is the path ``/modernize`` takes in external mode: group drafts, ``--merge``,
``--finalize``. A design whose attribute maps from a column the collector does not
have gets ``validation_passed=false`` and the messages; the phase itself still
completes, like a design_metrics mismatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.agents.schema_design.valid_designs import (
    ddb_attr,
    ddb_design,
    ddb_pattern,
    ddb_table,
)
from tests.unit.scripts.test_run_schema_design_scope import DB, JOB, _run, _store, _write_groups

_COLLECTOR = {
    "contract_version": "3.0",
    "database_schema": {
        "tables": [
            {
                "table_id": "mydb.users",
                "table_name": "users",
                "row_count": 10,
                "columns": [
                    {"column_name": "ID", "data_type": "int", "nullable": False},
                    {"column_name": "email", "data_type": "text", "nullable": True},
                ],
            }
        ]
    },
    "queries": {"query_patterns": []},
}


def _draft(*attributes: dict) -> dict:
    table = ddb_table("Main", "mydb.users", attributes=[ddb_attr("id", "mydb.users", "ID")])
    table["attributes"] = [*table["attributes"], *attributes]
    return ddb_design([table], [ddb_pattern("DDB-AP-1", "Main", "mydb.users", "q-users")])


def _external_run(monkeypatch, capsys, tmp_path: Path, draft: dict):
    store = _store(tmp_path, "dynamodb")
    store.write_json(f"{DB}/{JOB}/collector/output.json", _COLLECTOR)
    _write_groups(store, [draft])
    _, merged = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")
    _, finalized = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--finalize")
    return store, merged, finalized


def test_missing_column_fails_validation_after_merge_and_finalize(
    monkeypatch, capsys, tmp_path
) -> None:
    draft = _draft(ddb_attr("user_id", "mydb.users", "user_id"))
    store, merged, finalized = _external_run(monkeypatch, capsys, tmp_path, draft)
    for status in (merged, finalized):
        assert status["status"] == "complete"
        [warning] = [w for w in status["warnings"] if w.startswith("source columns: ")]
        assert "'user_id' is not a column of 'mydb.users'" in warning
    output = store.read_json(finalized["output_path"])
    assert output["validation_passed"] is False
    assert [f for f in output["validation_failures"] if f.startswith("source columns: ")]


@pytest.mark.passes_on_main  # a correct design must keep passing
def test_correct_design_keeps_validation_passed(monkeypatch, capsys, tmp_path) -> None:
    store, merged, finalized = _external_run(
        monkeypatch, capsys, tmp_path, _draft(ddb_attr("mail", "mydb.users", "EMAIL"))
    )
    output = store.read_json(finalized["output_path"])
    assert output["validation_passed"] is True
    assert not any(w.startswith("source columns") for w in finalized.get("warnings", []))


def test_fixing_the_draft_clears_the_failure(monkeypatch, capsys, tmp_path) -> None:
    store, _, failed = _external_run(
        monkeypatch, capsys, tmp_path, _draft(ddb_attr("user_id", "mydb.users", "user_id"))
    )
    assert store.read_json(failed["output_path"])["validation_passed"] is False
    _write_groups(store, [_draft(ddb_attr("user_id", "mydb.users", "ID"))])
    _, merged = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")
    output = store.read_json(merged["output_path"])
    assert output["validation_passed"] is True
    assert output["validation_failures"] == []
