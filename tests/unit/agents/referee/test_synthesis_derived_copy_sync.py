"""Synthesis writes ``facts.derived_copy_sync`` under the design-conflict risk (#1527)."""

from __future__ import annotations

import copy

from src.agents.referee.synthesis_handler import (
    _write_synthesis_report,
    run_synthesis_deterministic,
)
from src.agents.schema_design.dynamodb_merge import OVERLAP_PREFIX
from src.contracts.synthesis_output import ReportFacts
from tests.unit.agents.referee.test_synthesis_llm_seam import _SCHEMA_DDB, _mock_store

DB, JOB = "mydb", "job-1"


def _overlap_schema() -> dict:
    schema: dict = copy.deepcopy(_SCHEMA_DDB)
    schema["table_definitions"].append({"table_name": "UsersAlt", "source_tables": ["users"]})
    schema["access_patterns"][0]["source_tables"] = ["users"]
    schema["access_patterns"].append(
        {
            "pattern_id": "DDB-AP-3",
            "operation": "PutItem",
            "table_name": "UsersAlt",
            "source_tables": ["users"],
            "design_rps": 3.0,
            "item_size_bytes": 2000,
            "in_scope": True,
            "query_ids": ["DDB-AP-3"],
        }
    )
    schema["trade_offs"] = [
        {
            "description": f"{OVERLAP_PREFIX}users modelled independently",
            "impact": "kept in sync",
            "source_tables": ["users"],
            "target_tables": ["Users", "UsersAlt"],
            "query_ids": [],
            "engine": "dynamodb",
        }
    ]
    return schema


def _report(schema: dict) -> dict:
    store = _mock_store()
    read = store.read_json.side_effect

    def read_json(path: str) -> dict:
        if "schema-dynamodb/" in path and path.endswith("schema_output.json"):
            return schema
        found: dict = read(path)
        return found

    exists = store.exists.side_effect
    assignment = {
        "job_id": JOB,
        "version": 1,
        "query_assignments": [
            {"query_id": q, "assigned_engine": "dynamodb", "in_scope": True}
            for q in ("DDB-AP-1", "DDB-AP-2", "DDB-AP-3")
        ],
        "table_assignments": [],
    }

    def read_with_assignment(path: str) -> dict:
        if path.endswith("assignment/v1/assignment.json"):
            return assignment
        return read_json(path)

    store.read_json.side_effect = read_with_assignment
    ours = ("assignment/v1/assignment.json", "schema-dynamodb/v1/schema_output.json")
    store.exists.side_effect = lambda path: path.endswith(ours) or exists(path)
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    _write_synthesis_report(store, result, assignment_version=1)
    report: dict = store._written[f"{DB}/{JOB}/synthesis/v1/report.json"]
    return report


def test_the_sync_is_listed_under_the_overlap_risk() -> None:
    report = _report(_overlap_schema())

    [risk] = [r for r in report["risk_assessment"]["risks"] if r["risk_type"] == "DATA_CONSISTENCY"]
    sync = report["facts"]["derived_copy_sync"]
    [row] = sync["syncs"]
    assert row["risk_id"] == risk["risk_id"]
    assert row["authoritative_home"] == "Users"
    assert [c["table"] for c in row["derived"]] == ["UsersAlt"]
    assert row["replayed_writes_per_second"] == 3.0
    assert sync["total_monthly_cost_usd"] == row["monthly_cost_usd"] > 0
    ReportFacts.model_validate(report["facts"])  # the contract holds the new block


def test_a_design_without_derived_copies_has_no_fact() -> None:
    report = _report(copy.deepcopy(_SCHEMA_DDB))

    assert report["facts"]["derived_copy_sync"] is None
