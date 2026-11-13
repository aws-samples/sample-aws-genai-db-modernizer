# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis adds the DynamoDB design's GSI cost to the DynamoDB TCO row (#805)."""

from __future__ import annotations

import copy

import pytest

from src.agents.referee.synthesis_handler import (
    _write_synthesis_report,
    run_synthesis_deterministic,
)
from src.contracts.phase_models import SCHEMA_DESIGN_SKIP_REASON_NO_LLM
from src.contracts.synthesis_output import ReportFacts
from src.shared.engine_names import display_engine
from tests.unit.agents.referee.test_synthesis_llm_seam import _COLLECTOR, _SCHEMA_DDB, _mock_store

DB, JOB = "mydb", "job-1"
QUERIES = ("DDB-AP-1", "DDB-AP-2", "DDB-W-1")


def _collector() -> dict:
    collector: dict = copy.deepcopy(_COLLECTOR)
    collector["queries"]["query_patterns"].append(
        {
            "query_id": "DDB-W-1",
            "query_text": "INSERT INTO orders (order_id, user_id) VALUES (?, ?)",
            "query_type": "INSERT",
            "tables_accessed": ["orders"],
            "calls_per_second": 4.0,
        }
    )
    return collector


def _gsi_schema() -> dict:
    schema: dict = copy.deepcopy(_SCHEMA_DDB)
    orders = schema["table_definitions"][1]
    orders["attributes"] = [{"name": n, "type": "S"} for n in ("order_id", "user_id")]
    orders["gsis"] = [
        {
            "gsi_name": "OrdersByUser",
            "partition_key": [{"attribute_name": "user_id", "attribute_type": "S"}],
            "projection": "ALL",
            "item_count": 5_000_000,
            "item_size_bytes": 2048,
        }
    ]
    schema["access_patterns"].append(
        {
            "pattern_id": "DDB-W-1",
            "pattern_group": "order-writes",
            "operation": "PutItem",
            "table_name": "Orders",
            "source_tables": ["orders"],
            "key_condition": "PK = order_id",
            "design_rps": 120.0,
            "item_size_bytes": 2048,
            "in_scope": True,
            "query_ids": ["DDB-W-1"],
        }
    )
    return schema


def _without_gsis() -> dict:
    """The same design with no GSI: the same cost inputs otherwise."""
    schema = _gsi_schema()
    for table in schema["table_definitions"]:
        table["gsis"] = []
    return schema


def _report(schema: dict | None, in_scope: bool = True, *, extra_write: str | None = None) -> dict:
    """``extra_write``: a second write on Orders, ``"out"`` of scope or ``"absent"`` from
    the collection (the design still serves it)."""
    store = _mock_store()
    read = store.read_json.side_effect
    exists = store.exists.side_effect
    collector = _collector()
    queries = list(QUERIES)
    rows = [{"query_id": q, "assigned_engine": "dynamodb", "in_scope": in_scope} for q in queries]
    if extra_write is not None and schema is not None:
        schema = copy.deepcopy(schema)
        schema["access_patterns"].append(
            {**schema["access_patterns"][-1], "pattern_id": "DDB-W-2", "query_ids": ["DDB-W-2"]}
        )
        if extra_write == "out":
            collector["queries"]["query_patterns"].append(
                {
                    **collector["queries"]["query_patterns"][-1],
                    "query_id": "DDB-W-2",
                    "calls_per_second": 40.0,
                }
            )
            rows.append({"query_id": "DDB-W-2", "assigned_engine": "dynamodb", "in_scope": False})
    assignment = {
        "job_id": JOB,
        "version": 1,
        "query_assignments": rows,
        "table_assignments": [],
    }

    def read_json(path: str) -> dict:
        if path.endswith("assignment/v1/assignment.json"):
            return assignment
        if path.endswith("collector/output.json"):
            return collector
        if "schema-dynamodb/" in path and path.endswith("schema_output.json"):
            if schema is None:
                # No design: the skip record a run without a model writes (#2154).
                return {"status": "skipped", "reason": SCHEMA_DESIGN_SKIP_REASON_NO_LLM}
            return schema
        found: dict = read(path)
        return found

    def exists_(path: str) -> bool:
        if "schema-dynamodb/" in path:
            return path.endswith("schema_output.json")
        return path.endswith("assignment/v1/assignment.json") or exists(path)

    store.read_json.side_effect = read_json
    store.exists.side_effect = exists_
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    _write_synthesis_report(store, result, assignment_version=1)
    report: dict = store._written[f"{DB}/{JOB}/synthesis/v1/report.json"]
    return report


def _ddb_row(report: dict) -> dict:
    [row] = [r for r in report["tco_analysis"]["cost_breakdown"] if r["database"] == "dynamodb"]
    return dict(row)


def _gsi_line(report: dict) -> list[str]:
    return [a for a in report["tco_analysis"]["assumptions"] if "global secondary ind" in a]


def _share(line: str) -> float:
    """The GSI total the assumption line states."""
    return float(line.split("figure includes $", 1)[1].split("/mo", 1)[0].replace(",", ""))


def test_the_gsi_cost_is_in_the_dynamodb_row_of_the_tco() -> None:
    base, with_gsi = _report(_without_gsis()), _report(_gsi_schema())

    [line] = _gsi_line(with_gsi)
    share = _share(line)
    assert share > 0
    assert _ddb_row(with_gsi)["monthly_cost_usd"] == round(
        _ddb_row(base)["monthly_cost_usd"] + share, 2
    )


def test_every_cost_surface_moves_together() -> None:
    base, with_gsi = _report(_without_gsis()), _report(_gsi_schema())

    share = _share(_gsi_line(with_gsi)[0])
    tco = with_gsi["tco_analysis"]
    row = _ddb_row(with_gsi)["monthly_cost_usd"]
    assert tco["projected_monthly_cost"] == round(
        sum(r["monthly_cost_usd"] for r in tco["cost_breakdown"]), 2
    )
    assert tco["projected_monthly_cost"] == round(
        base["tco_analysis"]["projected_monthly_cost"] + share, 2
    )
    ranked = {r["target"]: r["monthly_cost_usd"] for r in with_gsi["ranking"] if r.get("target")}
    assert ranked["dynamodb"] == row
    [engine] = [e for e in with_gsi["facts"]["engines"] if e["engine"] == "dynamodb"]
    assert engine["monthly_cost_usd"] == row
    assert with_gsi["facts"]["projected_monthly_cost_usd"] == tco["projected_monthly_cost"]
    ReportFacts.model_validate(with_gsi["facts"])  # no new field: the contract holds


def test_the_tco_states_the_share_once_in_every_deliverables_assumptions() -> None:
    with_gsi, base = _report(_gsi_schema()), _report(_without_gsis())

    [line] = _gsi_line(with_gsi)
    assert f"{display_engine('dynamodb')} figure includes" in line
    assert "1 global secondary index " in line
    assert ";" not in line
    assert line in with_gsi["facts"]["cost_assumptions"]
    assert not _gsi_line(base)


@pytest.mark.passes_on_main
def test_no_schema_design_changes_nothing() -> None:
    """A deterministic run (``--llm-mode none``) writes no design: no GSI, no line."""
    report = _report(None)

    assert not _gsi_line(report)
    assert (
        _ddb_row(report)["monthly_cost_usd"]
        == _ddb_row(_report(_without_gsis()))["monthly_cost_usd"]
    )


@pytest.mark.passes_on_main
def test_no_in_scope_query_on_dynamodb_adds_no_gsi_cost() -> None:
    report = _report(_gsi_schema(), in_scope=False)

    assert _ddb_row(report)["monthly_cost_usd"] == 0
    assert report["tco_analysis"]["projected_monthly_cost"] == 0
    assert not _gsi_line(report)


def test_only_the_in_scope_queries_synthesis_sizes_dynamodb_for_write_the_indexes() -> None:
    """Synthesis passes the engine's in-scope query ids to the GSI cost: a write the
    assignment holds out of scope adds nothing, as if it were not collected at all."""
    out = _report(_gsi_schema(), extra_write="out")
    absent = _report(_gsi_schema(), extra_write="absent")

    assert _gsi_line(out) and _gsi_line(out) == _gsi_line(absent)
    assert _ddb_row(out)["monthly_cost_usd"] == _ddb_row(absent)["monthly_cost_usd"]
