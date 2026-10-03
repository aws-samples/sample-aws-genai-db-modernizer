"""Finalize and ``--merge`` reject out-of-scope schema designs (issue #203).

A design that references a source table or query ID the effective assignment
does not put in scope for its engine is written with ``validation_passed=false``
and the scope messages in ``validation_failures``, and the script reports
``{"status": "validation_failed", "errors": [...]}`` instead of ``complete``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import run_schema_design
from src.agents.schema_design import handler
from src.storage.local_store import LocalArtifactStore

DB, JOB = "mydb", "job-001"

_ASSIGNMENT = {
    "version": 1,
    "query_assignments": [
        {
            "query_id": "q-users",
            "assigned_engine": "{engine}",
            "source_tables": ["mydb.users"],
            "in_scope": True,
        },
        {
            "query_id": "q-orders",
            "assigned_engine": "{other}",
            "source_tables": ["mydb.orders"],
            "in_scope": True,
        },
    ],
}

_OTHER = {
    "dynamodb": "elasticache",
    "documentdb": "dynamodb",
    "opensearch": "dynamodb",
    "elasticache": "dynamodb",
    "aurora_mysql": "dynamodb",
    "aurora_postgresql": "dynamodb",
}


def _design(engine: str, table: str, query: str) -> dict:
    """A minimal design for ``engine`` referencing ``table`` and ``query``."""
    common = {"trade_offs": [{"description": "d", "impact": "i"}], "validation_passed": True}
    if engine == "dynamodb":
        body = {
            "table_definitions": [{"table_name": "Main", "source_tables": [table]}],
            "access_patterns": [
                {"pattern_id": "DDB-AP-1", "query_ids": [query], "source_tables": [table]}
            ],
        }
    elif engine == "documentdb":
        body = {
            "collections": [{"collection_name": "c", "source_tables": [table]}],
            "access_patterns": [{"pattern_id": "DOC-AP-1", "source_query_ids": [query]}],
        }
    elif engine == "opensearch":
        body = {
            "index_designs": [{"index_name": "i", "source_tables": [table]}],
            "access_patterns": [{"pattern_id": "OS-AP-1", "query_ids": [query]}],
        }
    elif engine == "elasticache":
        body = {
            "key_designs": [{"key_pattern": "k", "source_tables": [table]}],
            "access_patterns": [{"pattern_id": "EC-AP-1", "source_query_ids": [query]}],
        }
    else:
        body = {"table_definitions": [{"table_name": table.split(".")[-1]}]}
    return {**common, **body}


def _store(tmp_path: Path, engine: str) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    assignment = json.loads(
        json.dumps(_ASSIGNMENT).replace("{engine}", engine).replace("{other}", _OTHER[engine])
    )
    store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", assignment)
    return store


def _run(monkeypatch, capsys, root: Path, engine: str, *extra: str) -> tuple[int, dict]:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_schema_design.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--engine",
            engine,
            "--artifact-root",
            str(root),
            *extra,
        ],
    )
    code = 0
    try:
        run_schema_design.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return code, json.loads(lines[-1])


@pytest.fixture
def contract_ok(monkeypatch):
    """Isolate the scope check from per-engine contract validation."""
    monkeypatch.setattr(handler, "validate_schema_design_output", lambda *_: {"valid": True})


_FINALIZE_ENGINES = sorted(e for e in _OTHER if e != "dynamodb")


@pytest.mark.parametrize("engine", _FINALIZE_ENGINES)
def test_finalize_in_scope_design_is_complete(engine, monkeypatch, capsys, tmp_path, contract_ok):
    store = _store(tmp_path, engine)
    store.write_json(
        f"{DB}/{JOB}/llm_responses/schema_design_{engine}.json",
        _design(engine, "mydb.users", "q-users"),
    )

    code, status = _run(monkeypatch, capsys, tmp_path, engine, "--finalize")

    assert code == 0
    assert status["status"] == "complete"
    written = store.read_json(status["output_path"])
    assert written["validation_passed"] is True


@pytest.mark.parametrize("engine", _FINALIZE_ENGINES)
def test_finalize_out_of_scope_design_fails_validation(
    engine, monkeypatch, capsys, tmp_path, contract_ok
):
    store = _store(tmp_path, engine)
    store.write_json(
        f"{DB}/{JOB}/llm_responses/schema_design_{engine}.json",
        _design(engine, "mydb.orders", "q-orders"),
    )

    code, status = _run(monkeypatch, capsys, tmp_path, engine, "--finalize")

    assert code == 0  # reported, not crashed
    assert status["status"] == "validation_failed"
    assert status["assignment_version"] == 1
    assert status["errors"]
    assert any("orders" in e and _OTHER[engine] in e for e in status["errors"])
    written = store.read_json(status["output_path"])
    assert written["validation_passed"] is False
    assert all(e in written["validation_failures"] for e in status["errors"])


def test_finalize_without_assignment_skips_scope_check(monkeypatch, capsys, tmp_path, contract_ok):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        f"{DB}/{JOB}/llm_responses/schema_design_documentdb.json",
        _design("documentdb", "mydb.orders", "q-orders"),
    )

    code, status = _run(monkeypatch, capsys, tmp_path, "documentdb", "--finalize")

    assert code == 0
    assert status["status"] == "complete"


def _write_groups(store: LocalArtifactStore, drafts: list[dict]) -> None:
    base = f"{DB}/{JOB}/schema-dynamodb/v1"
    store.write_json(
        f"{base}/groups_manifest.json",
        {"groups": [{"group_index": i} for i in range(len(drafts))]},
    )
    for i, draft in enumerate(drafts):
        store.write_json(f"{base}/schema_draft_group_{i}.json", draft)


def test_merge_in_scope_design_is_complete(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path, "dynamodb")
    _write_groups(store, [_design("dynamodb", "mydb.users", "q-users")])

    code, status = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")

    assert code == 0
    assert status["status"] == "complete"
    assert store.read_json(status["output_path"])["validation_passed"] is True


def test_merge_out_of_scope_design_fails_validation(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path, "dynamodb")
    _write_groups(
        store,
        [_design("dynamodb", "mydb.users", "q-users"), _design("dynamodb", "orders", "q-orders")],
    )

    code, status = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")

    assert code == 0
    assert status["status"] == "validation_failed"
    assert status["output_path"].endswith("schema-dynamodb/v1/schema_output.json")
    assert len(status["errors"]) == 2  # one for the table, one for the query
    assert any("'orders'" in e and "elasticache" in e for e in status["errors"])
    assert any("'q-orders'" in e and "elasticache" in e for e in status["errors"])
    merged = store.read_json(status["output_path"])
    assert merged["validation_passed"] is False
    assert all(e in merged["validation_failures"] for e in status["errors"])


def test_merge_rerun_after_fixing_drafts_is_complete(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path, "dynamodb")
    _write_groups(store, [_design("dynamodb", "mydb.orders", "q-orders")])
    assert (
        _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")[1]["status"]
        == "validation_failed"
    )

    _write_groups(store, [_design("dynamodb", "mydb.users", "q-users")])
    code, status = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")

    assert status["status"] == "complete"
    merged = store.read_json(status["output_path"])
    assert merged["validation_passed"] is True
    assert not merged.get("validation_failures")


def test_dynamodb_finalize_reports_out_of_scope_merged_output(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path, "dynamodb")
    output_key = f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json"
    store.write_json(output_key, _design("dynamodb", "mydb.orders", "q-users"))

    code, status = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--finalize")

    assert code == 0
    assert status["status"] == "validation_failed"
    assert status["output_path"] == output_key
    assert len(status["errors"]) == 1
    assert store.read_json(output_key)["validation_passed"] is False


def test_handler_finalize_with_real_dynamodb_contract(tmp_path):
    """End to end through the real DynamoDB contract (no validation patch)."""
    from tests.unit.agents.schema_design.test_schema_llm_seam import _VALID_DYNAMODB_OUTPUT

    store = _store(tmp_path, "dynamodb")
    store.write_json(
        f"{DB}/{JOB}/llm_responses/schema_design_dynamodb.json", _VALID_DYNAMODB_OUTPUT
    )

    # _VALID_DYNAMODB_OUTPUT designs mydb.users for query "DDB-AP-1", which the
    # assignment does not contain: the table is in scope, the query is not.
    result = handler.finalize_schema_design(JOB, DB, "dynamodb", store, assignment_version=1)

    assert result["status"] == "validation_failed"
    assert result["errors"] == [
        "Out of scope for dynamodb: query 'DDB-AP-1' (referenced by access_patterns[DDB-AP-1]) "
        "is not in the assignment (v1). Remove it from the design."
    ]
