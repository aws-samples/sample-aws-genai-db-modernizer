# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ATX review gate presents pending engine choices per table and applies a
pick per table, or "decide for me" when the customer asks (#1003)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from src.atx_orchestrator import tools
from src.atx_orchestrator.runtime.store import upgrade_store
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.storage.local_store import LocalArtifactStore

DB = "discourse"
JOB = "job-choices"
PAIR = ["documentdb", "dynamodb"]


@pytest.fixture
def store(tmp_path):
    s = upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[
            QueryAssignment(
                query_id=q,
                assigned_engine="dynamodb",
                confidence=78,
                source_tables=["t.theme_fields"],
                assignment_reason="tie",
                tied_engines=PAIR,
            )
            for q in ("q1", "q2")
        ],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )
    s.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json"))
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {
                "query_patterns": [
                    {"query_id": q, "tables_accessed": ["t.theme_fields"]} for q in ("q1", "q2")
                ]
            },
            "database_schema": {"tables": []},
        },
    )
    for engine in PAIR:
        s.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})
    return s


def test_present_lists_one_choice_per_table(store) -> None:
    with patch("src.atx_orchestrator.tools._make_store", return_value=store):
        out = json.loads(tools.present_assignment_review(JOB, DB))
    (choice,) = out["pending_choices"]
    assert choice["choice_id"] == "t.theme_fields" and choice["queries"] == 2
    assert "Pending engine choices (1 table(s), 2 queries)" in out["summary_markdown"]
    assert "decide for me" in out["summary_markdown"]


def test_finalize_applies_a_table_pick_to_every_query(store) -> None:
    with patch("src.atx_orchestrator.tools._make_store", return_value=store):
        out = json.loads(
            tools.finalize_assignment_review(JOB, DB, "", table_choices="theme_fields=documentdb")
        )
    assert out["status"] == "approved", out
    assert out["changed"] is True and out["assignment_version"] == 2
    v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
    assert {qa["assigned_engine"] for qa in v2["query_assignments"]} == {"documentdb"}
    assert (v2["status"], v2["source"]) == ("customer_modified", "customer_gate")
    assert all(not qa["tied_engines"] for qa in v2["query_assignments"])


def test_finalize_decides_for_the_customer_only_when_asked(store) -> None:
    with patch("src.atx_orchestrator.tools._make_store", return_value=store):
        out = json.loads(tools.finalize_assignment_review(JOB, DB, "", decide_for_me=True))
    assert out["status"] == "approved", out
    v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
    assert (v2["status"], v2["source"]) == ("auto_decided", "auto_decided")
    (decided,) = v2["auto_decided_choices"]
    assert decided["query_ids"] == ["q1", "q2"]
    assert {m["actor"] for m in v2["moves"]} == {"rule"}


def test_finalize_refuses_a_pick_for_an_untied_engine(store) -> None:
    with patch("src.atx_orchestrator.tools._make_store", return_value=store):
        out = json.loads(
            tools.finalize_assignment_review(JOB, DB, "", table_choices="theme_fields=opensearch")
        )
    assert out["status"] == "invalid_edit"
    assert "not one of the tied engines" in out["error"]
    assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")
