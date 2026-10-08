"""``cache_safety_net_notes`` does not carry forward across assignment
versions (#459 round 2 review).

``cache_safety_net_notes`` is a version's own customer-facing explanation of
what the post-schema-design safety net changed on *that* version's overlay
since the assignment gate -- not a cumulative log like ``cache_notes``. A new
version (a customer edit, or Reality Check) has its own, different overlay
numbers; carrying the previous version's note forward would misdescribe them
(the review's own repro: a v2 note saying "10 cached reads ... remain" left
in the same paragraph as v3's "11 reads, 73.7%").
"""

from __future__ import annotations

import pytest

from src.agents.referee.assignment_overrides import apply_assignment_overrides
from src.agents.referee.reality_check_handler import write_reality_check_result
from src.agents.referee.synthesis_handler import run_synthesis
from src.storage.local_store import LocalArtifactStore

DB = "shop"
JOB = "job-safety-net-reset"


def _query(qid: str, cps: float, table: str) -> dict:
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM {table} WHERE id = ?",  # nosec B608 -- test fixture, never executed
        "query_type": "SELECT",
        "calls_per_second": cps,
        "rows_returned_avg": 1,
        "tables_accessed": [table],
    }


QUERIES = [
    _query("hot1", 6.0, "users"),
    _query("hot2", 2.0, "orders"),
    _query("cold", 1.0, "orders"),
]

ASSIGNMENT_V2 = {
    "job_id": JOB,
    "version": 2,
    # Full shape (status, timestamp, per-query confidence, table_assignments,
    # co_dependency_groups, validation_warnings): apply_assignment_overrides
    # strictly validates the stored assignment against the Assignment model
    # (unlike run_synthesis, which works on the raw dict), so this fixture
    # needs the complete contract shape, not just what synthesis reads.
    "status": "auto_generated",
    "timestamp": "2026-10-08T00:00:00Z",
    "table_assignments": [],
    "co_dependency_groups": [],
    "validation_warnings": [],
    "query_assignments": [
        {
            "query_id": "hot1",
            "assigned_engine": "dynamodb",
            "confidence": 60,
            "source_tables": ["users"],
            "assignment_reason": "x",
            "cache_engine": "elasticache",
            "cache_pattern": "point_lookup",
        },
        {
            "query_id": "hot2",
            "assigned_engine": "aurora_mysql",
            "confidence": 55,
            "source_tables": ["orders"],
            "assignment_reason": "x",
            "cache_engine": "elasticache",
            "cache_pattern": "point_lookup",
        },
        {
            "query_id": "cold",
            "assigned_engine": "aurora_mysql",
            "confidence": 55,
            "source_tables": ["orders"],
            "assignment_reason": "x",
        },
    ],
}


def _analysis(conf: int) -> dict:
    return {
        "table_recommendations": [
            {"table_id": "users", "confidence_score": conf},
            {"table_id": "orders", "confidence_score": conf},
        ],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        "cost_estimate": {"monthly_cost_usd": 10},
    }


# Only hot1 is covered: the safety net drops hot2 on every version (#296).
CACHE_SCHEMA = {"access_patterns": [{"pattern_id": "p1", "source_query_ids": ["hot1"]}]}


def _seed(store) -> None:
    engines = ["dynamodb", "aurora_mysql", "elasticache"]
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in engines], "signals": []},
    )
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "database_schema": {"tables": [{"table_id": "users"}, {"table_id": "orders"}]},
            "queries": {"query_patterns": QUERIES},
        },
    )
    conf = {"dynamodb": 60, "aurora_mysql": 55, "elasticache": 90}
    for engine in engines:
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", _analysis(conf[engine]))
    store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", ASSIGNMENT_V2)
    store.write_json(f"{DB}/{JOB}/schema-elasticache/v2/schema_output.json", CACHE_SCHEMA)


@pytest.fixture
def store(tmp_path):
    return LocalArtifactStore(base_dir=str(tmp_path))


def test_customer_edit_resets_safety_net_notes_but_keeps_cache_notes_cumulative(store):
    _seed(store)
    run_synthesis(JOB, DB, store, assignment_version=2, llm_mode="none")

    v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
    assert len(v2["cache_safety_net_notes"]) == 1
    old_note = v2["cache_safety_net_notes"][0]
    assert old_note in v2["cache_notes"]

    # A customer edit writes v3 -- even a no-op edit (no overrides) is a new
    # version, and that version has not had its own safety net evaluated yet.
    result = apply_assignment_overrides(store, DB, JOB, overrides=[])
    v3 = result.assignment
    assert v3.version == 3
    assert v3.cache_safety_net_notes == []
    # cache_notes is the cumulative audit trail: the old note is still there.
    assert old_note in v3.cache_notes

    # Re-synthesizing v3 with a schema that now covers nothing (hot1 -- hot2
    # was already dropped going into v3) writes exactly one note for v3 --
    # not two (the carried-forward v2 note plus a new one).
    store.write_json(
        f"{DB}/{JOB}/schema-elasticache/v3/schema_output.json", {"access_patterns": []}
    )
    run_synthesis(JOB, DB, store, assignment_version=3, llm_mode="none")
    v3_after = store.read_json(f"{DB}/{JOB}/assignment/v3/assignment.json")
    assert len(v3_after["cache_safety_net_notes"]) == 1
    assert v3_after["cache_safety_net_notes"][0] != old_note


def test_reality_check_resets_safety_net_notes(tmp_path):
    """``write_reality_check_result`` builds ``revised_assignment`` with
    ``**result["assignment"]`` (reality_check_handler.py) -- without an
    explicit reset, a prior version's safety-net note would spread forward
    into the consolidated version untouched. Exercised directly against
    ``write_reality_check_result`` (the same minimal-result shape
    ``test_reality_check_pattern_refresh.py``'s ``_rc_result`` uses) rather
    than hoping a tiny hand-built assignment happens to trigger a real
    consolidation end to end.
    """
    store = LocalArtifactStore(base_dir=str(tmp_path))
    old_note = (
        "2 hot reads (80.0% of calls) were assigned at the assignment gate. "
        "The ElastiCache schema design covers 1 of them; the other 1 is no "
        "longer cached and stays served by its owner engine. 1 cached read "
        "(60.0% of calls) remains."
    )
    # Original: q1 on opensearch, q2 already on aurora_mysql. Revised: both on
    # aurora_mysql -- a real net move, so reconcile_consolidations (run by
    # write_reality_check_result's _settle_records) keeps the record instead
    # of discarding it as a no-op.
    original = [
        {"query_id": "q1", "assigned_engine": "opensearch", "in_scope": True},
        {"query_id": "q2", "assigned_engine": "aurora_mysql", "in_scope": True},
    ]
    revised = [
        {"query_id": "q1", "assigned_engine": "aurora_mysql", "in_scope": True},
        {"query_id": "q2", "assigned_engine": "aurora_mysql", "in_scope": True},
    ]
    result = {
        "revised_assignments": revised,
        "consolidations": [
            {
                "from_engine": "opensearch",
                "to_engine": "aurora_mysql",
                "query_count": 1,
                "reason": "test consolidation",
                "saved_cost_estimate": 1.0,
                "action": "full",
                "queries_retained": [],
                "retention_reason": None,
            }
        ],
        "architectural_patterns": [],
        "recommendations": [],
        "unique_value_assessment": {},
        "executive_summary": None,
        "before_distribution": {"opensearch": 1, "aurora_mysql": 1},
        "after_distribution": {"aurora_mysql": 2},
        "lightweight_recommendations": [],
        "collector_output": {},
        "analysis_outputs": {},
        "assignment": {
            "query_assignments": original,
            "cache_safety_net_notes": [old_note],
            "cache_notes": [old_note],
        },
    }

    new_version = write_reality_check_result(store, "job-1", "mydb", result, assignment_version=1)

    assert new_version is not None
    revised_assignment = store.read_json(f"mydb/job-1/assignment/v{new_version}/assignment.json")
    assert revised_assignment["cache_safety_net_notes"] == []
    # cache_notes is the cumulative audit trail: the old note is still there.
    assert old_note in revised_assignment["cache_notes"]
