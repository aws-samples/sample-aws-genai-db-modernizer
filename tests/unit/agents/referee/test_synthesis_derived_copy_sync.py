# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis writes ``facts.derived_copy_sync`` under the design-conflict risk (#1527)."""

from __future__ import annotations

import copy

import pytest

from src.agents.referee.synthesis_handler import (
    _write_synthesis_report,
    run_synthesis_deterministic,
)
from src.agents.schema_design.dynamodb_merge import OVERLAP_PREFIX
from src.contracts.synthesis_output import ReportFacts
from src.shared import catalog
from src.shared.engine_names import display_engine
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


def _report(schema: dict, in_scope: bool = True) -> dict:
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
            {"query_id": q, "assigned_engine": "dynamodb", "in_scope": in_scope}
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


def _without_overlap() -> dict:
    """The same design with the overlap's trade-off removed: no sync, same cost inputs."""
    schema = _overlap_schema()
    schema["trade_offs"] = []
    return schema


def _ddb_row(report: dict) -> dict:
    [row] = [r for r in report["tco_analysis"]["cost_breakdown"] if r["database"] == "dynamodb"]
    return dict(row)


def test_the_sync_cost_is_in_the_dynamodb_row_of_the_tco() -> None:
    """#1566: the DynamoDB figure counts the sync, so the cost table adds up."""
    base, synced = _report(_without_overlap()), _report(_overlap_schema())

    total = synced["facts"]["derived_copy_sync"]["total_monthly_cost_usd"]
    assert total > 0
    assert _ddb_row(synced)["monthly_cost_usd"] == round(
        _ddb_row(base)["monthly_cost_usd"] + total, 2
    )


def test_every_cost_surface_moves_together() -> None:
    base, synced = _report(_without_overlap()), _report(_overlap_schema())

    total = synced["facts"]["derived_copy_sync"]["total_monthly_cost_usd"]
    tco = synced["tco_analysis"]
    assert tco["projected_monthly_cost"] == round(
        sum(r["monthly_cost_usd"] for r in tco["cost_breakdown"]), 2
    )
    assert tco["projected_monthly_cost"] == round(
        base["tco_analysis"]["projected_monthly_cost"] + total, 2
    )
    ranked = {r["target"]: r["monthly_cost_usd"] for r in synced["ranking"] if r.get("target")}
    assert ranked["dynamodb"] == _ddb_row(synced)["monthly_cost_usd"]
    [engine] = [e for e in synced["facts"]["engines"] if e["engine"] == "dynamodb"]
    assert engine["monthly_cost_usd"] == _ddb_row(synced)["monthly_cost_usd"]


def test_the_tco_states_what_the_sync_figure_rests_on() -> None:
    synced, base = _report(_overlap_schema()), _report(_without_overlap())

    [line] = [a for a in synced["tco_analysis"]["assumptions"] if "stream sync" in a]
    total = synced["facts"]["derived_copy_sync"]["total_monthly_cost_usd"]
    streams_bytes = catalog.pricing("dynamodb").streams_read_unit_bytes
    assert f"${total:,.2f}/mo" in line
    assert f"{display_engine('dynamodb')} figure includes" in line
    assert f"{streams_bytes // (1024 * 1024)} MB" in line  # the catalog's batch, not a literal
    assert "lower bound" in line
    [left_out] = [a for a in synced["tco_analysis"]["assumptions"] if "leaves out" in a]
    assert "compute" in left_out and "free tier" in left_out
    for a in (line, left_out):
        assert ";" not in a  # the reports join assumptions with "; "
        assert a in synced["facts"]["cost_assumptions"]
    assert not [a for a in base["tco_analysis"]["assumptions"] if "stream sync" in a]


@pytest.mark.passes_on_main
def test_a_design_without_derived_copies_keeps_its_cost() -> None:
    base = _report(copy.deepcopy(_SCHEMA_DDB))

    assert (
        _ddb_row(base)["monthly_cost_usd"]
        == _ddb_row(_report(_without_overlap()))["monthly_cost_usd"]
    )


def test_the_sync_is_computed_once_for_the_cost_and_the_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1566 (ADR-032 Decision 1): the TCO row and facts.derived_copy_sync share one value."""
    from src.agents.referee import derived_copy_sync as module

    calls: list[int] = []
    real = module.derived_copy_sync_facts

    def counted(schema, risks):  # type: ignore[no-untyped-def]
        calls.append(1)
        return real(schema, risks)

    monkeypatch.setattr(module, "derived_copy_sync_facts", counted)
    report = _report(_overlap_schema())

    assert len(calls) == 1
    total = report["facts"]["derived_copy_sync"]["total_monthly_cost_usd"]
    base = _report(_without_overlap())
    assert _ddb_row(report)["monthly_cost_usd"] - _ddb_row(base)[
        "monthly_cost_usd"
    ] == pytest.approx(total)


@pytest.mark.passes_on_main
def test_no_in_scope_query_on_dynamodb_adds_no_sync_to_the_cost() -> None:
    """The row is sized to $0 when nothing is in scope; the sync is not added to it. The
    sync stays listed under its risk, since it is sized from the design's write rate."""
    report = _report(_overlap_schema(), in_scope=False)

    assert _ddb_row(report)["monthly_cost_usd"] == 0
    assert report["tco_analysis"]["projected_monthly_cost"] == 0
    assert not [a for a in report["tco_analysis"]["assumptions"] if "stream sync" in a]
    assert report["facts"]["derived_copy_sync"] is not None
