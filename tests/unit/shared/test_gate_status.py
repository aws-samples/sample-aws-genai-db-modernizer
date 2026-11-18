# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2326: the gate's figures describe the assignment version the gate states.

``src.shared.gate_status.gate_status`` builds the status data the chat gate and the UI gate
both read. A customer edit newer than the reality-check record is counted from the edited
version, the version's report's facts win once synthesis wrote it, and the queries counted
are the in-scope ones, the report's meaning."""

from __future__ import annotations

from src.shared.gate_facts import gate_block_facts
from src.shared.gate_status import gate_status
from src.storage.local_store import LocalArtifactStore

DB, JOB = "shop", "job-1"


def _qa(qid: str, engine: str, in_scope: bool = True) -> dict:
    return {"query_id": qid, "assigned_engine": engine, "in_scope": in_scope}


def _store(tmp_path, versions: dict[int, list[dict]], rc: dict | None = None):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    for version, qas in versions.items():
        store.write_json(
            f"{DB}/{JOB}/assignment/v{version}/assignment.json",
            {"version": version, "query_assignments": qas},
        )
    if rc is not None:
        store.write_json(f"{DB}/{JOB}/reality-check/output.json", rc)
    return store


RC = {
    "before_distribution": {"dynamodb": 2, "documentdb": 1},
    "after_distribution": {"dynamodb": 3},
    "eliminated_engines": ["documentdb"],
    "consolidations": [{"from_engine": "documentdb", "to_engine": "dynamodb"}],
}


def test_a_customer_edit_newer_than_the_record_is_counted_from_the_edit(tmp_path) -> None:
    # v2 is the reality check's result; v3 the customer's edit, which moves a query back
    # onto the engine the reality check removed.
    store = _store(
        tmp_path,
        {
            2: [_qa("a", "dynamodb"), _qa("b", "dynamodb"), _qa("c", "dynamodb")],
            3: [_qa("a", "dynamodb"), _qa("b", "dynamodb"), _qa("c", "documentdb")],
        },
        RC,
    )
    facts = gate_block_facts(gate_status(store, DB, JOB, 3))
    assert [(r["engine"], r["before"], r["after"], r["dropped"]) for r in facts["rows"]] == [
        ("dynamodb", 2, 2, False),
        ("documentdb", 1, 1, False),
    ]
    assert facts["surviving"] == ["dynamodb", "documentdb"]


def test_an_engine_an_edit_empties_is_dropped(tmp_path) -> None:
    store = _store(
        tmp_path,
        {3: [_qa("a", "aurora_mysql"), _qa("b", "aurora_mysql"), _qa("c", "aurora_mysql")]},
        RC,
    )
    facts = gate_block_facts(gate_status(store, DB, JOB, 3))
    assert {r["engine"]: r["dropped"] for r in facts["rows"]} == {
        "aurora_mysql": False,
        "dynamodb": True,
        "documentdb": True,
    }
    assert facts["surviving"] == ["aurora_mysql"]


def test_an_out_of_scope_query_is_not_counted(tmp_path) -> None:
    # The report counts in-scope queries (query_total_count); so does the gate.
    store = _store(
        tmp_path,
        {2: [_qa("a", "dynamodb"), _qa("b", "dynamodb"), _qa("c", "dynamodb", in_scope=False)]},
        {**RC, "after_distribution": {"dynamodb": 3}},
    )
    facts = gate_block_facts(gate_status(store, DB, JOB, 2))
    assert facts["total_after"] == 2
    assert facts["query_total_count"] == 2


def test_the_queries_with_no_engine_join_the_total_under_the_new_key(tmp_path) -> None:
    store = _store(tmp_path, {2: [_qa("a", "dynamodb")]}, RC)
    raw = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
    raw["unservable_queries"] = [{"query_id": "x"}, {"query_id": "y"}]
    store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", raw)
    gate = gate_status(store, DB, JOB, 2)
    assert gate["unservable_query_count"] == 2
    assert "unservable_queries" not in gate
    facts = gate_block_facts(gate)
    assert (facts["unservable_query_count"], facts["query_total_count"]) == (2, 3)


def test_the_versions_report_states_the_figures_once_synthesis_wrote_it(tmp_path) -> None:
    store = _store(tmp_path, {2: [_qa("a", "dynamodb"), _qa("b", "dynamodb")]}, RC)
    raw = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
    raw["cache_overlay"] = {"engine": "elasticache", "query_count": 1, "call_share_percent": 5.0}
    store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", raw)
    store.write_json(
        f"{DB}/{JOB}/synthesis/v2/report.json",
        {
            "facts": {
                "engine_query_counts": {"dynamodb": 7},
                "removed_engines": [{"engine": "documentdb"}],
                "no_engine_in_scope_query_count": 1,
                "query_total_count": 8,
                "source_table_count": 12,
                "cache_layer": {
                    "candidate_read_count": 4,
                    "candidate_call_share_percent": 20.0,
                    "designed_read_count": 3,
                    "designed_call_share_percent": 15.0,
                },
            }
        },
    )
    facts = gate_block_facts(gate_status(store, DB, JOB, 2))
    assert facts["total_after"] == 7
    assert facts["query_total_count"] == 8
    assert facts["source_table_count"] == 12
    assert [(r["engine"], r["after"], r["dropped"]) for r in facts["rows"]] == [
        ("dynamodb", 7, False),
        ("documentdb", 0, True),
    ]
    cache = facts["cache_candidates"]
    assert (cache["read_count"], cache["call_share_percent"]) == (4, 20.0)


def test_another_versions_report_is_not_read(tmp_path) -> None:
    store = _store(tmp_path, {2: [_qa("a", "dynamodb")], 3: [_qa("a", "dynamodb")]}, RC)
    store.write_json(
        f"{DB}/{JOB}/synthesis/v2/report.json",
        {"facts": {"engine_query_counts": {"dynamodb": 9}, "query_total_count": 9}},
    )
    assert gate_block_facts(gate_status(store, DB, JOB, 3))["total_after"] == 1
