"""Flagged queries no kept engine can take, in report.json and the facts (#888).

Two kinds of unservable query never reach the move ledger: a reality-check
model flag whose outcome is ``kept`` and a post-schema router terminal query.
Synthesis records both in ``flagged_unservable``; ``facts.unservable`` states
them as a third kind, ``flagged``, next to the fallback ones, with the reason
category's redesign hint, so every deliverable flags them (#729's maintainer
decision covers them too).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.flagged_unservable import flagged_unservable_records
from src.agents.referee.synthesis_handler import (
    _write_synthesis_report,
    run_synthesis_deterministic,
)
from src.contracts.synthesis_output import SynthesisOutputContract, UnservableFacts
from src.shared.capability_labels import FLAG_REDESIGN_HINTS
from src.shared.report_facts import compute_report_facts, unservable_query_facts
from src.storage.local_store import LocalArtifactStore


def _assignment(**engines: str) -> dict[str, Any]:
    return {
        "query_assignments": [
            {"query_id": q, "assigned_engine": e, "in_scope": True} for q, e in engines.items()
        ]
    }


def _decision(qids: list[str], outcome: str, engine: str = "dynamodb", **kw: Any) -> dict:
    return {
        "query_ids": qids,
        "flagged_engine": engine,
        "reason_category": kw.pop("category", "full_scan"),
        "explanation": "MODEL TEXT never rendered",
        "outcome": outcome,
        "reason": "Flagged on DynamoDB",
        **kw,
    }


def _terminal(qid: str, engine: str = "dynamodb", cause: str = "no_kept_engine", **kw) -> dict:
    return {
        "query_id": qid,
        "flagged_engine": engine,
        "reason_category": kw.get("category", "client_side_join"),
        "cause": cause,
    }


class TestRecords:
    def test_a_kept_reality_check_flag_is_recorded_per_query(self) -> None:
        rows = flagged_unservable_records(
            _assignment(q1="dynamodb", q2="dynamodb"),
            {"flag_decisions": [_decision(["q2", "q1"], "kept")]},
            None,
            {"q1": ["aggregation"]},
        )
        assert [r["query_id"] for r in rows] == ["q1", "q2"]
        row = rows[0]
        assert row["engine"] == "dynamodb" and row["engine_name"] == "DynamoDB"
        assert (row["source"], row["cause"], row["reason_category"]) == (
            "reality_check",
            "no_kept_engine",
            "full_scan",
        )
        assert row["explanation"] == (
            "The reality check's review found DynamoDB cannot serve this query (it would need "
            "a full scan), and no other engine the plan keeps can take it, so it stays on "
            "DynamoDB."
        )
        assert row["gate"] == "model_flag" and row["gate_text"]
        assert row["required_capabilities"] == ["aggregation"]
        assert "MODEL TEXT" not in str(rows)

    @pytest.mark.parametrize("outcome", ["moved", "ignored"])
    def test_a_moved_or_ignored_flag_is_not(self, outcome: str) -> None:
        rows = flagged_unservable_records(
            _assignment(q1="dynamodb"), {"flag_decisions": [_decision(["q1"], outcome)]}, None
        )
        assert rows == []

    def test_a_terminal_query_is_recorded_with_its_cause(self) -> None:
        rows = flagged_unservable_records(
            _assignment(q1="dynamodb", q2="opensearch"),
            None,
            {"terminal": [_terminal("q1"), _terminal("q2", "opensearch", "depth_limit")]},
        )
        assert [(r["query_id"], r["source"], r["cause"]) for r in rows] == [
            ("q1", "design_review", "no_kept_engine"),
            ("q2", "design_review", "depth_limit"),
        ]
        assert rows[0]["explanation"].startswith("A design review found DynamoDB cannot serve")
        assert "re-routing stopped at its last pass, so it stays on OpenSearch" in (
            rows[1]["explanation"]
        )

    def test_a_query_that_moved_since_or_left_scope_is_not(self) -> None:
        assignment = _assignment(q1="aurora_mysql", q2="dynamodb")
        assignment["query_assignments"][1]["in_scope"] = False
        rows = flagged_unservable_records(
            assignment,
            {"flag_decisions": [_decision(["q1", "q2"], "kept")]},
            {"terminal": [_terminal("q1"), _terminal("q2")]},
        )
        assert rows == []

    def test_a_query_both_kept_and_terminal_is_listed_once(self) -> None:
        rows = flagged_unservable_records(
            _assignment(q1="dynamodb"),
            {"flag_decisions": [_decision(["q1"], "kept")]},
            {"terminal": [_terminal("q1")]},
        )
        assert [(r["query_id"], r["source"]) for r in rows] == [("q1", "reality_check")]

    def test_old_artifacts_and_no_artifacts_give_none(self) -> None:
        # A router output written before 1.1 has only terminal_queries.
        old_router = {"contract_version": "1.0", "job_id": "j", "terminal_queries": ["q1"]}
        assert flagged_unservable_records(_assignment(q1="dynamodb"), {}, old_router) == []
        assert flagged_unservable_records(None, None, None) == []


def _report(flagged: list[dict], traced: list[dict] | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {"flagged_unservable": flagged}
    if traced is not None:
        report["query_traces"] = {"has_ledger": True, "query_count": 3, "unservable": traced}
    return report


class TestFacts:
    def test_a_flagged_query_is_a_third_kind_with_the_categorys_hint(self) -> None:
        flagged = flagged_unservable_records(
            _assignment(q1="dynamodb"), {"flag_decisions": [_decision(["q1"], "kept")]}, None
        )
        facts = unservable_query_facts(_report(flagged, []))
        assert facts is not None
        assert facts["unservable_query_count"] == 1
        assert (facts["fallback_query_count"], facts["gated_everywhere_query_count"]) == (0, 0)
        assert facts["flagged_query_count"] == 1
        [q] = facts["unservable_queries"]
        assert (q["query_id"], q["engine"], q["kind"]) == ("q1", "dynamodb", "flagged")
        assert q["explanation"] == flagged[0]["explanation"]
        assert q["blocking_gates"] == [
            {"engine": "dynamodb", "gate": "model_flag", "gate_text": flagged[0]["gate_text"]}
        ]
        assert q["redesign_hints"] == ["For DynamoDB: " + FLAG_REDESIGN_HINTS["full_scan"]]

    @pytest.mark.parametrize("category", sorted(FLAG_REDESIGN_HINTS))
    def test_every_reason_category_has_its_own_hint(self, category: str) -> None:
        flagged = flagged_unservable_records(
            _assignment(q1="documentdb"),
            {"flag_decisions": [_decision(["q1"], "kept", "documentdb", category=category)]},
            None,
        )
        [q] = unservable_query_facts(_report(flagged))["unservable_queries"]  # type: ignore[index]
        assert q["redesign_hints"] == ["For DocumentDB: " + FLAG_REDESIGN_HINTS[category]]

    def test_the_trace_row_wins_for_a_query_listed_in_both(self) -> None:
        traced = [
            {
                "query_id": "q1",
                "engine": "aurora_mysql",
                "kind": "fallback",
                "rule": "fallback.no_score",
                "blocking_gates": [],
            }
        ]
        flagged = [{**_flag_row("q1", "aurora_mysql")}, _flag_row("q2", "dynamodb")]
        facts = unservable_query_facts(_report(flagged, traced))
        assert facts is not None
        assert [(q["query_id"], q["kind"]) for q in facts["unservable_queries"]] == [
            ("q1", "fallback"),
            ("q2", "flagged"),
        ]
        assert (facts["fallback_query_count"], facts["flagged_query_count"]) == (1, 1)
        assert facts["unservable_query_count"] == 2

    def test_counts_and_order_across_the_three_kinds(self) -> None:
        traced = [
            {"query_id": "g1", "engine": "opensearch", "kind": "gated_everywhere"},
            {"query_id": "f1", "engine": "aurora_mysql", "kind": "fallback"},
        ]
        facts = unservable_query_facts(_report([_flag_row("k1", "dynamodb")], traced))
        assert facts is not None
        assert [q["query_id"] for q in facts["unservable_queries"]] == ["f1", "k1", "g1"]
        assert (
            facts["fallback_query_count"],
            facts["gated_everywhere_query_count"],
            facts["flagged_query_count"],
            facts["unservable_query_count"],
        ) == (1, 1, 1, 3)
        UnservableFacts.model_validate(facts)

    def test_a_report_with_neither_list_has_none(self) -> None:
        assert unservable_query_facts({}) is None
        assert unservable_query_facts({"flagged_unservable": []}) is None
        # A report written before #888: the trace's list alone, as before.
        facts = unservable_query_facts({"query_traces": {"unservable": []}})
        assert facts is not None and facts["flagged_query_count"] == 0

    def test_flagged_alone_without_query_traces_is_stated(self) -> None:
        facts = compute_report_facts(_report([_flag_row("k1", "dynamodb")]))
        assert facts["unservable"]["flagged_query_count"] == 1


def _flag_row(qid: str, engine: str) -> dict[str, Any]:
    [row] = flagged_unservable_records(
        {"query_assignments": [{"query_id": qid, "assigned_engine": engine}]},
        {"flag_decisions": [_decision([qid], "kept", engine)]},
        None,
    )
    return row


DB = "shop"
JOB = "job-flagged"


@pytest.fixture
def store(tmp_path):
    """A one-engine job: the reality check kept a flag on q-scan and the
    post-schema router left q-join terminal; q-ok has no flag."""
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": "dynamodb"}], "signals": []},
    )
    queries = [
        {
            "query_id": q,
            "query_text": f"SELECT * FROM t WHERE id = ? -- {q}",
            "query_type": "SELECT",
            "calls_per_second": 1.0,
            "rows_returned_avg": 1,
            "tables_accessed": ["t"],
        }
        for q in ("q-ok", "q-scan", "q-join")
    ]
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": "mysql"}},
            "database_schema": {"tables": [{"table_id": "t"}]},
            "queries": {"query_patterns": queries},
        },
    )
    s.write_json(
        f"{DB}/{JOB}/analysis-dynamodb/analysis.json",
        {
            "table_recommendations": [{"table_id": "t", "confidence_score": 90}],
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
            "cost_estimate": {"monthly_cost_usd": 10},
        },
    )
    s.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "job_id": JOB,
            "version": 1,
            "query_assignments": [
                {
                    "query_id": q,
                    "assigned_engine": "dynamodb",
                    "source_tables": ["t"],
                    "assignment_reason": "highest score",
                }
                for q in ("q-ok", "q-scan", "q-join")
            ],
            "table_assignments": [],
        },
    )
    s.write_json(
        f"{DB}/{JOB}/reality-check/output.json",
        {
            "source_assignment_version": 1,
            "consolidations": [],
            "architectural_patterns": [],
            "recommendations": [],
            "before_distribution": {"dynamodb": 3},
            "after_distribution": {"dynamodb": 3},
            "flag_decisions": [_decision(["q-scan"], "kept")],
        },
    )
    s.write_json(
        f"{DB}/{JOB}/post-schema-router/router_output.json",
        {
            "contract_version": "1.1",
            "job_id": JOB,
            "terminal_queries": ["q-join"],
            "terminal": [_terminal("q-join")],
        },
    )
    return s


def test_synthesis_writes_both_into_report_json_and_the_facts(store) -> None:
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    _write_synthesis_report(store, result, assignment_version=1)
    report = store.read_json(f"{DB}/{JOB}/synthesis/v1/report.json")
    SynthesisOutputContract.model_validate(report)

    assert [(r["query_id"], r["source"]) for r in report["flagged_unservable"]] == [
        ("q-join", "design_review"),
        ("q-scan", "reality_check"),
    ]
    facts = report["facts"]["unservable"]
    assert facts["flagged_query_count"] == 2 and facts["unservable_query_count"] == 2
    assert [q["query_id"] for q in facts["unservable_queries"]] == ["q-join", "q-scan"]
    assert facts["unservable_queries"][0]["redesign_hints"] == [
        "For DynamoDB: " + FLAG_REDESIGN_HINTS["client_side_join"]
    ]


def test_synthesis_without_either_artifact_writes_an_empty_list(store, tmp_path) -> None:
    (tmp_path / DB / JOB / "reality-check" / "output.json").unlink()
    (tmp_path / DB / JOB / "post-schema-router" / "router_output.json").unlink()
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    _write_synthesis_report(store, result, assignment_version=1)
    report = store.read_json(f"{DB}/{JOB}/synthesis/v1/report.json")
    assert report["flagged_unservable"] == []
    assert not (report["facts"].get("unservable") or {}).get("flagged_query_count")


def test_the_customer_text_guard_passes_the_rows_and_their_facts() -> None:
    """The ids and codes are exempt by path; the sentences are still scanned."""
    from src.tools.validation.customer_text_guard import scan_json

    report = _report([_flag_row("q1", "dynamodb")])
    report["facts"] = compute_report_facts(report)
    assert scan_json(report) == []
    report["flagged_unservable"][0]["explanation"] = "stays on dynamodb"
    assert [f.path for f in scan_json(report)] == ["$.flagged_unservable[0].explanation"]


def test_the_orchestrator_keeps_the_terminal_records_in_router_output(tmp_path) -> None:
    """Every query on DynamoDB, so the flagged q-1 has no kept engine to go to:
    the cumulative router_output.json keeps it with its flag (no model call;
    the schema designer is never reached because nothing moves)."""
    from tests.unit.tools.schema.test_pe_routing_flags import TestTheOrchestratorRoutesAFlagByScore

    case = TestTheOrchestratorRoutesAFlagByScore()
    store = LocalArtifactStore(base_dir=str(tmp_path))
    case._seed(store)
    prefix = f"{case.DB}/{case.JOB}"
    store.write_json(
        f"{prefix}/assignment/v1/assignment.json",
        {
            "version": 1,
            "query_assignments": [
                {"query_id": "q-1", "assigned_engine": "dynamodb"},
                {"query_id": "q-4", "assigned_engine": "dynamodb"},
            ],
        },
    )
    calls: list = []
    case._run(store, calls)

    assert calls == [] and not store.exists(f"{prefix}/assignment/v2/assignment.json")
    router = store.read_json(f"{prefix}/post-schema-router/router_output.json")
    assert router["terminal_queries"] == ["q-1"]
    assert router["terminal"] == [
        {
            "query_id": "q-1",
            "flagged_engine": "dynamodb",
            "reason_category": "full_scan",
            "cause": "no_kept_engine",
        }
    ]


@pytest.mark.usefixtures("schema_contract_off")
def test_the_terminal_records_of_every_pass_are_kept(tmp_path) -> None:
    """Pass 0 moves q-1 off DynamoDB and leaves q-2 terminal (it needs a
    capability DynamoDB and DocumentDB lack, so no other kept engine can take
    it off Aurora MySQL); pass 1 sees no flag on the redesigned version. The
    cumulative router_output.json still names q-2 with its flag, not only the
    last pass's (empty) records."""
    from tests.unit.tools.schema.test_pe_routing_flags import TestTheOrchestratorRoutesAFlagByScore

    case = TestTheOrchestratorRoutesAFlagByScore()
    store = LocalArtifactStore(base_dir=str(tmp_path))
    case._seed(store)
    prefix = f"{case.DB}/{case.JOB}"
    triage = store.read_json(f"{prefix}/referee-triage/triage.json")
    store.write_json(
        f"{prefix}/referee-triage/triage.json",
        {**triage, "query_capabilities": {"q-2": ["scan_engine"]}},
    )
    store.write_json(
        f"{prefix}/schema-aurora_mysql/v1/design_trace.json",
        {
            "routing_flags": [
                {
                    "query_ids": ["q-2"],
                    "flagged_engine": "aurora_mysql",
                    "reason_category": "other",
                    "explanation": "too slow",
                }
            ]
        },
    )
    case._run(store, [])

    v2 = store.read_json(f"{prefix}/assignment/v2/assignment.json")
    engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in v2["query_assignments"]}
    assert (engine_of["q-1"], engine_of["q-2"]) == ("aurora_mysql", "aurora_mysql")
    last = store.read_json(f"{prefix}/post-schema-router/pass-1/router_output.json")
    assert last["terminal"] == []
    router = store.read_json(f"{prefix}/post-schema-router/router_output.json")
    assert router["terminal_queries"] == ["q-2"]
    assert router["terminal"] == [
        {
            "query_id": "q-2",
            "flagged_engine": "aurora_mysql",
            "reason_category": "other",
            "cause": "no_kept_engine",
        }
    ]
