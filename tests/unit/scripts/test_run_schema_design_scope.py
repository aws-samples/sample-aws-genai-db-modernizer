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


# ---------------------------------------------------------------------------
# unsupported_patterns warnings, stale failures, robustness
# ---------------------------------------------------------------------------


def test_finalize_reports_unsupported_pattern_ids_as_warnings(
    monkeypatch, capsys, tmp_path, contract_ok
):
    store = _store(tmp_path, "documentdb")
    design = _design("documentdb", "mydb.users", "q-users")
    design["unsupported_patterns"] = [{"source_query_ids": ["q-orders"]}]
    store.write_json(f"{DB}/{JOB}/llm_responses/schema_design_documentdb.json", design)

    code, status = _run(monkeypatch, capsys, tmp_path, "documentdb", "--finalize")

    assert code == 0
    assert status["status"] == "complete"
    assert len(status["warnings"]) == 1 and "q-orders" in status["warnings"][0]
    written = store.read_json(status["output_path"])
    assert written["validation_passed"] is True
    assert not written.get("validation_failures")


def test_hand_fixed_design_refinalizes_clean(monkeypatch, capsys, tmp_path, contract_ok):
    """Out-of-scope finalize, then the response is fixed by editing the written
    output (stale scope messages and validation_passed=false copied along) and
    finalized again: the stale messages go and validation_passed comes back."""
    store = _store(tmp_path, "elasticache")
    response_key = f"{DB}/{JOB}/llm_responses/schema_design_elasticache.json"
    store.write_json(response_key, _design("elasticache", "mydb.orders", "q-users"))
    _, failed = _run(monkeypatch, capsys, tmp_path, "elasticache", "--finalize")
    assert failed["status"] == "validation_failed"

    fixed = store.read_json(failed["output_path"])
    assert fixed["validation_passed"] is False and fixed["validation_failures"]
    fixed["key_designs"][0]["source_tables"] = ["mydb.users"]
    store.write_json(response_key, fixed)

    code, status = _run(monkeypatch, capsys, tmp_path, "elasticache", "--finalize")

    assert code == 0
    assert status["status"] == "complete"
    written = store.read_json(status["output_path"])
    assert written["validation_passed"] is True
    assert written["validation_failures"] == []


def test_dynamodb_hand_fixed_merged_output_finalizes_clean(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path, "dynamodb")
    _write_groups(store, [_design("dynamodb", "mydb.orders", "q-users")])
    _, failed = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")
    assert failed["status"] == "validation_failed"

    merged = store.read_json(failed["output_path"])
    for key in ("table_definitions", "access_patterns"):
        merged[key][0]["source_tables"] = ["mydb.users"]
    store.write_json(failed["output_path"], merged)

    code, status = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--finalize")

    assert status["status"] == "complete"
    written = store.read_json(status["output_path"])
    assert written["validation_passed"] is True
    assert written["validation_failures"] == []


def test_check_schema_scope_never_raises(tmp_path, monkeypatch):
    store = _store(tmp_path, "dynamodb")

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected shape")

    monkeypatch.setattr(handler, "assess_schema_scope", boom)
    report = handler.check_schema_scope(
        store, DB, JOB, "dynamodb", _design("dynamodb", "mydb.orders", "q-orders"), 1
    )
    assert report.violations == [] and report.warnings == []

    monkeypatch.setattr(handler, "read_assignment", boom)
    report = handler.check_schema_scope(store, DB, JOB, "dynamodb", {}, 1)
    assert report.violations == []


# ---------------------------------------------------------------------------
# Bedrock path (no real model calls: the agent dispatch is patched)
# ---------------------------------------------------------------------------


def _bedrock_store(tmp_path: Path, engine: str) -> LocalArtifactStore:
    store = _store(tmp_path, engine)
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "database_schema": {
                "tables": [{"table_id": "mydb.users"}, {"table_id": "mydb.orders"}]
            },
            "queries": {
                "query_patterns": [
                    {"query_id": "q-users", "tables_accessed": ["mydb.users"]},
                    {"query_id": "q-orders", "tables_accessed": ["mydb.orders"]},
                ]
            },
        },
    )
    store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {})
    return store


@pytest.mark.parametrize("engine", ["documentdb", "aurora_mysql"])
def test_bedrock_out_of_scope_design_fails_validation(engine, monkeypatch, capsys, tmp_path):
    store = _bedrock_store(tmp_path, engine)
    design = _design(engine, "mydb.orders", "q-orders")
    monkeypatch.setattr(
        handler, "_dispatch_schema_agent", lambda *_a, **_k: (json.dumps(design), None)
    )

    code, status = _run(monkeypatch, capsys, tmp_path, engine, "--llm-mode", "bedrock")

    assert code == 0
    assert status["status"] == "validation_failed"
    assert status["errors"] and all(e.startswith("Out of scope for ") for e in status["errors"])
    written = store.read_json(f"{DB}/{JOB}/schema-{engine}/v1/schema_output.json")
    assert written["validation_passed"] is False
    assert all(e in written["validation_failures"] for e in status["errors"])


def test_bedrock_in_scope_design_is_complete(monkeypatch, capsys, tmp_path):
    store = _bedrock_store(tmp_path, "documentdb")
    design = _design("documentdb", "mydb.users", "q-users")
    monkeypatch.setattr(
        handler, "_dispatch_schema_agent", lambda *_a, **_k: (json.dumps(design), None)
    )

    code, status = _run(monkeypatch, capsys, tmp_path, "documentdb", "--llm-mode", "bedrock")

    assert status["status"] == "complete"
    assert "errors" not in status
    written = store.read_json(f"{DB}/{JOB}/schema-documentdb/v1/schema_output.json")
    assert written["validation_passed"] is True


def test_bedrock_grouped_path_returns_merge_violations(monkeypatch, tmp_path):
    """run_schema_design_auto's split -> groups -> merge path no longer discards
    run_schema_merge's scope report."""
    store = _bedrock_store(tmp_path, "dynamodb")
    # Two in-scope DynamoDB queries and a group size of 1 force the split path.
    assignment = store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
    assignment["query_assignments"].append(
        {
            "query_id": "q-users-2",
            "assigned_engine": "dynamodb",
            "source_tables": ["mydb.users"],
            "in_scope": True,
        }
    )
    store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", assignment)
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    collector["queries"]["query_patterns"].append(
        {"query_id": "q-users-2", "tables_accessed": ["mydb.users"]}
    )
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    monkeypatch.setattr("src.agents.schema_design.group_splitter.MAX_GROUP_SIZE", 1)
    design = _design("dynamodb", "mydb.orders", "q-orders")
    monkeypatch.setattr(
        handler, "_dispatch_schema_agent", lambda *_a, **_k: (json.dumps(design), None)
    )

    report = handler.run_schema_design_auto(JOB, DB, "dynamodb", store, assignment_version=1)

    assert store.exists(f"{DB}/{JOB}/schema-dynamodb/v1/groups_manifest.json")
    assert report.violations
    merged = store.read_json(f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json")
    assert merged["validation_passed"] is False


# ---------------------------------------------------------------------------
# DynamoDB merge: one home per source table (issue #223)
# ---------------------------------------------------------------------------


def _overlapping_drafts() -> list[dict]:
    """Two groups each give mydb.users its own table, with different key schemas."""
    first = _design("dynamodb", "mydb.users", "q-users")
    first["table_definitions"][0]["partition_key"] = {"attribute_name": "id"}
    second = _design("dynamodb", "mydb.users", "q-users")
    second["table_definitions"][0].update(
        table_name="UsersByEmail", partition_key={"attribute_name": "email"}
    )
    return [first, second]


def _conflicting_drafts() -> list[dict]:
    """Two groups use the table name Main for different designs (a true conflict)."""
    first = _design("dynamodb", "mydb.users", "q-users")
    first["table_definitions"][0]["partition_key"] = {"attribute_name": "id"}
    second = _design("dynamodb", "mydb.users", "q-users")
    second["table_definitions"][0]["partition_key"] = {"attribute_name": "email"}
    return [first, second]


def test_merge_independent_homes_warn_and_complete(monkeypatch, capsys, tmp_path):
    from src.agents.schema_design.group_merger import OVERLAP_PREFIX

    store = _store(tmp_path, "dynamodb")
    _write_groups(store, _overlapping_drafts())

    code, status = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")

    assert code == 0
    assert status["status"] == "complete"
    [warning] = status["warnings"]
    assert warning.startswith(OVERLAP_PREFIX)
    assert "Main (group 0; PK id)" in warning and "UsersByEmail (group 1; PK email)" in warning
    merged = store.read_json(status["output_path"])
    assert merged["validation_passed"] is True
    assert any(t["description"] == warning for t in merged["trade_offs"])


def test_merge_overlap_justified_by_trade_off_has_no_warning(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path, "dynamodb")
    drafts = _overlapping_drafts()
    drafts[1]["trade_offs"].append(
        {
            "description": "login by email needs its own table; writes update both",
            "impact": "i",
            "source_tables": ["mydb.users"],
            "target_tables": ["Main", "UsersByEmail"],
        }
    )
    _write_groups(store, drafts)

    code, status = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")

    assert status["status"] == "complete"
    assert "warnings" not in status


def test_merge_true_conflict_fails_validation(monkeypatch, capsys, tmp_path):
    from src.agents.schema_design.group_merger import MERGE_FAILURE_PREFIX

    store = _store(tmp_path, "dynamodb")
    _write_groups(store, _conflicting_drafts())

    code, status = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")

    assert code == 0
    assert status["status"] == "validation_failed"
    [error] = status["errors"]
    assert error.startswith(MERGE_FAILURE_PREFIX) and "'Main'" in error
    merged = store.read_json(status["output_path"])
    assert merged["validation_passed"] is False
    assert merged["validation_failures"] == [error]


def test_dynamodb_finalize_reports_merge_failures_and_warnings(monkeypatch, capsys, tmp_path):
    store = _store(tmp_path, "dynamodb")
    _write_groups(store, _conflicting_drafts())
    _, merged = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")

    code, status = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--finalize")

    assert code == 0
    assert status["status"] == "validation_failed"
    assert status["errors"] == merged["errors"]

    _write_groups(store, _overlapping_drafts())
    _, merged = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--merge")
    _, status = _run(monkeypatch, capsys, tmp_path, "dynamodb", "--finalize")

    assert status["status"] == "complete"
    assert status["warnings"] == merged["warnings"]


def test_merge_renumbers_pattern_ids_in_the_combined_design_trace(tmp_path):
    store = _store(tmp_path, "dynamodb")
    first = _design("dynamodb", "mydb.users", "q-users")
    second = _design("dynamodb", "mydb.users", "q-users")
    second["table_definitions"][0]["table_name"] = "Other"
    _write_groups(store, [first, second])
    base = f"{DB}/{JOB}/schema-dynamodb/v1"
    for i in range(2):
        store.write_json(f"{base}/design_trace_group_{i}.json", {"decision": "DDB-AP-1 by id"})

    handler.run_schema_merge(JOB, DB, "dynamodb", store, assignment_version=1)

    merged = store.read_json(f"{base}/schema_output.json")
    assert [ap["pattern_id"] for ap in merged["access_patterns"]] == ["DDB-AP-1", "DDB-AP-2"]
    trace = store.read_json(f"{base}/design_trace.json")
    assert trace["groups"] == [{"decision": "DDB-AP-1 by id"}, {"decision": "DDB-AP-2 by id"}]
    # Group traces are left as written, so a re-merge renumbers from the originals.
    assert store.read_json(f"{base}/design_trace_group_1.json") == {"decision": "DDB-AP-1 by id"}
