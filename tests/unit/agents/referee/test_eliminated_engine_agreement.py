"""Cross-deliverable agreement on "eliminated engine" (#235).

Before this fix, an engine left with only out-of-scope queries after the reality
check was reported inconsistently: the reality-check handler's own
``eliminated_engines`` (used to drop stale per-query warnings) and the UI's
``eliminatedEngines`` (``AssignmentGate.js``) counted ANY remaining query and so
called the engine still active, while synthesis grounding and the consolidation
records counted only in-scope queries and called it eliminated.

This test drives the real reality-check write path and the real synthesis
deterministic path against the same scenario -- an engine ("documentdb") that
keeps one out-of-scope query after its one in-scope query moves to dynamodb --
and asserts the two deliverables now agree: the reality-check output's
``eliminated_engines`` and synthesis's own ``eliminated_engines`` name the same
engine, even though ``after_distribution`` still shows the engine owning a query.
"""

from __future__ import annotations

from src.agents.referee.reality_check_handler import write_reality_check_result
from src.agents.referee.synthesis_handler import run_synthesis_deterministic
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.referee.ledger_fakes import recorded_ledger, records_from_moves

DB = "shop"
JOB = "job-elim-agree"


def _qa(qid: str, engine: str, *, in_scope: bool = True) -> dict:
    return {"query_id": qid, "assigned_engine": engine, "in_scope": in_scope}


def _seed_pipeline_artifacts(store: LocalArtifactStore) -> None:
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "documentdb"}]},
    )
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "database_schema": {"tables": [{"table_id": "shop.users"}]},
            "queries": {"query_patterns": []},
        },
    )
    store.write_json(
        f"{DB}/{JOB}/analysis-dynamodb/analysis.json",
        {"workload_analysis": {"anti_patterns_detected": []}},
    )
    store.write_json(
        f"{DB}/{JOB}/analysis-documentdb/analysis.json",
        {"workload_analysis": {"anti_patterns_detected": []}},
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "version": 1,
            "query_assignments": [
                _qa("ddb-1", "dynamodb"),
                _qa("doc-1", "documentdb"),
                _qa("doc-2", "documentdb", in_scope=False),
            ],
        },
    )


def _run_reality_check(store: LocalArtifactStore) -> dict:
    """Write a settled reality check: doc-1 (in scope) moves to dynamodb; doc-2
    (out of scope) stays on documentdb untouched -- the engine still owns a
    query, just not an in-scope one."""
    before = [
        _qa("ddb-1", "dynamodb"),
        _qa("doc-1", "documentdb"),
        _qa("doc-2", "documentdb", in_scope=False),
    ]
    revised = [
        _qa("ddb-1", "dynamodb"),
        _qa("doc-1", "dynamodb"),
        _qa("doc-2", "documentdb", in_scope=False),
    ]
    result = {
        "revised_assignments": revised,
        "move_ledger": recorded_ledger(before, revised),
        "consolidations": records_from_moves(before, revised, []),
        "architectural_patterns": [],
        "recommendations": [],
        "unique_value_assessment": {},
        "executive_summary": None,
        "before_distribution": {"dynamodb": 1, "documentdb": 2},
        "after_distribution": {"dynamodb": 2, "documentdb": 1},
        "lightweight_recommendations": [],
        "assignment": {"version": 1, "query_assignments": before},
        "collector_output": {},
        "analysis_outputs": {},
    }
    write_reality_check_result(store, JOB, DB, result, 1)
    return store.read_json(f"{DB}/{JOB}/reality-check/output.json")


def test_reality_check_and_synthesis_agree_on_an_out_of_scope_leftover(tmp_path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _seed_pipeline_artifacts(store)
    rc_output = _run_reality_check(store)

    # The any-query distribution still shows documentdb owning a query --
    # this is the exact shape that used to fool the handler's and the UI's
    # old rule into calling the engine active.
    assert rc_output["after_distribution"].get("documentdb") == 1
    # The rule (#235) still calls it eliminated.
    assert rc_output["eliminated_engines"] == ["documentdb"]

    report = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)

    assert "documentdb" in report["eliminated_engines"]
    assert report["eliminated_engines"]["documentdb"] == "dynamodb"
    # The effective architecture (what the UI's Results page and every
    # deliverable render) does not list documentdb as a live engine either.
    effective_names = {e["engine"] for e in report["effective_architecture"]["engines"]}
    assert "documentdb" not in effective_names
