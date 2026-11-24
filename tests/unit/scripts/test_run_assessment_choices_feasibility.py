# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_assessment.py --choose`` runs the feasibility review on the edit it applies
(ADR-029 Decision C, #2519) and prints its warnings on the ``engine_choices`` line.

A per-table choice moves a whole placement unit, so it cannot split a JOIN group; what
it can do is put one on an engine with no server-side JOIN, an advisory finding. A
blocking finding refuses the edit in the shared gate both paths call
(``tests/unit/test_assignment_overrides.py::TestFeasibilityReviewOnEdits``), and the
command prints that refusal as its error line."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from scripts import run_assessment
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.storage.local_store import LocalArtifactStore

DB, JOB = "shop", "job-1"
TIED = ["aurora_postgresql", "dynamodb"]


def _query(qid: str, table: str) -> dict:
    return {
        "query_id": qid,
        "query_type": "SELECT",
        "query_text": f"SELECT * FROM shop.{table} JOIN shop.c ON shop.{table}.id = shop.c.ref",
        "tables_accessed": [f"shop.{table}", "shop.c"],
        "has_joins": True,
        "join_count": 2,
    }


@pytest.fixture
def store(tmp_path):
    """q1 and q2 share a JOIN on shop.c (one co-dependency group, one pending choice),
    tied between Aurora PostgreSQL and DynamoDB and listed on Aurora PostgreSQL."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[
            QueryAssignment(
                query_id=qid,
                assigned_engine="aurora_postgresql",
                confidence=78,
                source_tables=[f"shop.{table}", "shop.c"],
                assignment_reason="test",
                tied_engines=TIED,
            )
            for qid, table in (("q1", "a"), ("q2", "b"))
        ],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
    )
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {"query_patterns": [_query("q1", "a"), _query("q2", "b")]},
            "database_schema": {"tables": []},
        },
    )
    for engine in TIED:
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})
    return store


def _line(capsys) -> dict:
    lines: list[dict] = [
        json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")
    ]
    (line,) = lines
    return line


def test_a_choice_that_puts_a_join_group_on_dynamodb_prints_the_advisory(store, capsys) -> None:
    run_assessment.phase_engine_choices(store, JOB, DB, ["shop.c=dynamodb"], False)
    line = _line(capsys)
    assert (line["phase"], line["status"]) == ("engine_choices", "complete")
    (warning,) = line["feasibility_warnings"]
    assert warning.startswith(
        "WARNING [MEDIUM]: Co-dependent queries (q1, q2) share a significant JOIN and are "
        "all routed to DynamoDB"
    )
    saved = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
    assert warning in saved["validation_warnings"]


def test_a_feasible_choice_prints_no_warning(store, capsys) -> None:
    run_assessment.phase_engine_choices(store, JOB, DB, ["shop.c=aurora_postgresql"], False)
    line = _line(capsys)
    assert line["status"] == "complete"
    assert line["feasibility_warnings"] == []


def test_decide_for_me_runs_the_review_on_the_rule_s_pick(store, capsys) -> None:
    # Three settled DynamoDB reads make "decide for me" pick DynamoDB for shop.c
    # (fewer engines), which puts the JOIN group where no server-side JOIN runs.
    raw = store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    for i in range(3):
        raw["query_assignments"].append(
            {
                **raw["query_assignments"][0],
                "query_id": f"k{i}",
                "assigned_engine": "dynamodb",
                "tied_engines": [],
                "source_tables": [f"shop.k{i}"],
            }
        )
        collector["queries"]["query_patterns"].append(
            {
                "query_id": f"k{i}",
                "query_type": "SELECT",
                "query_text": f"SELECT * FROM shop.k{i} WHERE id = ?",
                "tables_accessed": [f"shop.k{i}"],
            }
        )
    store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", raw)
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    run_assessment.phase_engine_choices(store, JOB, DB, [], True)
    line = _line(capsys)
    assert [(d["choice"], d["engine"]) for d in line["decided"]] == [("shop.c", "dynamodb")]
    (warning,) = line["feasibility_warnings"]
    assert warning.startswith(
        "WARNING [MEDIUM]: Co-dependent queries (q1, q2) share a significant JOIN and are "
        "all routed to DynamoDB"
    )
