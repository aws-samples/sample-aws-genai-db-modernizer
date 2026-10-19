"""Held queries in report.json and the facts (#1033).

The post-schema router keeps a flagged query on its engine when a customer pin
holds its table's write group there (``RouterOutput.held_queries``, #992). That
flag is a design review's finding that the engine cannot serve the query, so
every deliverable states it: synthesis records the query in
``flagged_unservable`` with the cause ``held_by_pin``, and ``facts.unservable``
counts it as its own kind, ``held``, with its own count.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.flagged_unservable import CAUSE_HELD_BY_PIN, flagged_unservable_records
from src.agents.referee.synthesis_handler import (
    _write_synthesis_report,
    run_synthesis_deterministic,
)
from src.contracts.post_schema_router_output import RouterOutput
from src.contracts.synthesis_output import SynthesisOutputContract
from src.shared.capability_labels import FLAG_REDESIGN_HINTS
from src.shared.report_facts import compute_report_facts
from src.storage.local_store import LocalArtifactStore


def _assignment(**engines: str) -> dict[str, Any]:
    return {
        "query_assignments": [
            {"query_id": q, "assigned_engine": e, "in_scope": True} for q, e in engines.items()
        ]
    }


def _held(qid: str, engine: str = "dynamodb", category: str = "missing_feature") -> dict:
    return {"query_id": qid, "flagged_engine": engine, "reason_category": category}


def _terminal(qid: str, engine: str = "dynamodb") -> dict:
    return {
        "query_id": qid,
        "flagged_engine": engine,
        "reason_category": "client_side_join",
        "cause": "no_kept_engine",
    }


class TestTheRouterKeepsTheFlag:
    def test_a_held_query_keeps_the_flag_that_was_held(self) -> None:
        from src.agents.referee.post_schema_router import route_unsupported_queries
        from src.agents.referee.write_groups import assignment_placement_groups
        from tests.unit.agents.referee.test_one_writer_per_table import GATE_QUERIES

        write_groups = {"db.orders": ["w1", "w2"]}
        output = route_unsupported_queries(
            schema_outputs={"dynamodb": {"unsupported_patterns": []}},
            active_engines=["dynamodb", "aurora_postgresql"],
            routing_flags_by_engine={
                "dynamodb": [
                    {
                        "query_ids": ["w2"],
                        "flagged_engine": "dynamodb",
                        "reason_category": "missing_feature",
                        "explanation": "needs a server-side join",
                    }
                ]
            },
            query_map={q["query_id"]: q for q in GATE_QUERIES},
            assigned_engines={q["query_id"]: "dynamodb" for q in GATE_QUERIES},
            customer_pinned={"w1"},
            co_dependency_groups=assignment_placement_groups(
                {"co_dependency_groups": [], "write_groups": write_groups}
            ),
            write_groups=write_groups,
        )
        assert output.held_queries == ["w2"]
        assert [h.model_dump() for h in output.held] == [_held("w2")]
        assert output.terminal == []

    def test_an_unflagged_unit_member_is_held_without_a_flag_record(self) -> None:
        """Only w2 is flagged. w1 is pinned, and the read r1 shares a join group with
        w2, so the unit keeps r1 in place too; r1 is in ``held_queries`` but gets no
        record, since no review found DynamoDB cannot serve it (#1099 review)."""
        from src.agents.referee.post_schema_router import route_unsupported_queries
        from src.agents.referee.write_groups import assignment_placement_groups
        from tests.unit.agents.referee.test_one_writer_per_table import GATE_QUERIES

        write_groups = {"db.orders": ["w1", "w2"]}
        output = route_unsupported_queries(
            schema_outputs={"dynamodb": {"unsupported_patterns": []}},
            active_engines=["dynamodb", "aurora_postgresql"],
            routing_flags_by_engine={
                "dynamodb": [
                    {
                        "query_ids": ["w2"],
                        "flagged_engine": "dynamodb",
                        "reason_category": "missing_feature",
                        "explanation": "needs a server-side join",
                    }
                ]
            },
            query_map={q["query_id"]: q for q in GATE_QUERIES},
            assigned_engines={q["query_id"]: "dynamodb" for q in GATE_QUERIES},
            customer_pinned={"w1"},
            co_dependency_groups=assignment_placement_groups(
                {"co_dependency_groups": [["w2", "r1"]], "write_groups": write_groups}
            ),
            write_groups=write_groups,
        )
        assert "r1" in output.held_queries
        assert [h.query_id for h in output.held] == ["w2"]

    def test_a_router_output_written_before_the_records_still_loads(self) -> None:
        old = RouterOutput.model_validate(
            {"contract_version": "1.2", "job_id": "j", "held_queries": ["w1"]}
        )
        assert old.held_queries == ["w1"] and old.held == []

    def test_a_routed_query_is_not_held(self) -> None:
        out = RouterOutput(job_id="j")
        assert out.held == [] and out.held_queries == []


class TestRecords:
    def test_a_held_query_is_recorded_with_its_own_cause(self) -> None:
        rows = flagged_unservable_records(_assignment(w2="dynamodb"), None, {"held": [_held("w2")]})
        [row] = rows
        assert (row["query_id"], row["source"], row["cause"]) == (
            "w2",
            "design_review",
            CAUSE_HELD_BY_PIN,
        )
        assert CAUSE_HELD_BY_PIN == "held_by_pin"
        assert row["explanation"] == (
            "A design review found DynamoDB cannot serve this query (it needs a feature the "
            "engine lacks). A write on its table is pinned to DynamoDB in the "
            "assignment review, and a table has one writer, so it stays on DynamoDB until "
            "the pin changes."
        )
        assert row["gate"] == "model_flag" and row["gate_text"]

    def test_a_held_query_that_moved_since_or_left_scope_is_not(self) -> None:
        moved = _assignment(w1="aurora_postgresql", w2="dynamodb")
        moved["query_assignments"][1]["in_scope"] = False
        rows = flagged_unservable_records(moved, None, {"held": [_held("w1"), _held("w2")]})
        assert rows == []

    def test_a_query_both_held_and_kept_by_the_reality_check_is_listed_once(self) -> None:
        decision = {
            "query_ids": ["w2"],
            "flagged_engine": "dynamodb",
            "reason_category": "full_scan",
            "outcome": "kept",
        }
        rows = flagged_unservable_records(
            _assignment(w2="dynamodb"),
            {"flag_decisions": [decision]},
            {"held": [_held("w2")]},
        )
        assert [(r["query_id"], r["source"]) for r in rows] == [("w2", "reality_check")]

    def test_held_terminal_and_a_second_engine_sort_by_engine_then_id(self) -> None:
        rows = flagged_unservable_records(
            _assignment(a="opensearch", b="dynamodb", c="dynamodb"),
            None,
            {
                "held": [_held("c"), _held("a", "opensearch")],
                "terminal": [_terminal("b")],
            },
        )
        assert [(r["engine"], r["query_id"], r["cause"]) for r in rows] == [
            ("dynamodb", "b", "no_kept_engine"),
            ("dynamodb", "c", "held_by_pin"),
            ("opensearch", "a", "held_by_pin"),
        ]

    def test_an_old_router_output_without_held_records_gives_none(self) -> None:
        old = {"contract_version": "1.2", "job_id": "j", "held_queries": ["w2"]}
        assert flagged_unservable_records(_assignment(w2="dynamodb"), None, old) == []

    def test_an_empty_reason_reads_as_other(self) -> None:
        [row] = flagged_unservable_records(
            _assignment(w2="dynamodb"),
            None,
            {"held": [{"query_id": "w2", "flagged_engine": "dynamodb"}]},
        )
        assert row["reason_category"] == "other"


def _facts(rows: list[dict]) -> dict[str, Any]:
    facts: dict[str, Any] = compute_report_facts({"flagged_unservable": rows})["unservable"]
    return facts


class TestFacts:
    def _row(self, qid: str = "w2", engine: str = "dynamodb", category: str = "full_scan"):
        [row] = flagged_unservable_records(
            _assignment(**{qid: engine}), None, {"held": [_held(qid, engine, category)]}
        )
        return row

    def test_a_held_query_is_its_own_kind_with_its_own_count(self) -> None:
        facts = _facts([self._row()])
        assert facts["held_query_count"] == 1
        assert facts["flagged_query_count"] == 0 and facts["no_fit_query_count"] == 0
        assert facts["unservable_query_count"] == 1
        [q] = facts["unservable_queries"]
        assert q["kind"] == "held" and q["query_id"] == "w2"
        assert q["redesign_hints"] == ["For DynamoDB: " + FLAG_REDESIGN_HINTS["full_scan"]]

    def test_counts_split_across_the_four_kinds(self) -> None:
        [kept] = flagged_unservable_records(
            _assignment(k="dynamodb"),
            None,
            {"terminal": [_terminal("k")]},
        )
        facts = _facts([kept, self._row("h1"), self._row("h2")])
        assert (
            facts["unservable_query_count"],
            facts["flagged_query_count"],
            facts["held_query_count"],
        ) == (3, 1, 2)

    def test_a_facts_block_written_before_held_has_a_zero_count(self) -> None:
        from src.contracts.synthesis_output import UnservableFacts

        assert UnservableFacts().held_query_count == 0


DB = "shop"
JOB = "job-held"


@pytest.fixture
def store(tmp_path):
    """A one-engine job where the router held q-hold (a design review flagged it
    but a pin on q-pin keeps their table's writes on DynamoDB)."""
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
        for q in ("q-pin", "q-hold")
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
                for q in ("q-pin", "q-hold")
            ],
            "table_assignments": [],
        },
    )
    s.write_json(
        f"{DB}/{JOB}/post-schema-router/router_output.json",
        {
            "contract_version": "1.4",
            "job_id": JOB,
            "held_queries": ["q-hold"],
            "held": [_held("q-hold")],
        },
    )
    return s


def test_synthesis_writes_the_held_query_into_report_json_and_the_facts(store) -> None:
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    _write_synthesis_report(store, result, assignment_version=1)
    report = store.read_json(f"{DB}/{JOB}/synthesis/v1/report.json")
    SynthesisOutputContract.model_validate(report)

    assert [(r["query_id"], r["cause"]) for r in report["flagged_unservable"]] == [
        ("q-hold", "held_by_pin")
    ]
    facts = report["facts"]["unservable"]
    assert facts["held_query_count"] == 1 and facts["unservable_query_count"] == 1
    assert facts["unservable_queries"][0]["kind"] == "held"


def test_the_customer_text_guard_passes_the_held_row_and_its_facts() -> None:
    from src.tools.validation.customer_text_guard import scan_json

    [row] = flagged_unservable_records(_assignment(w2="dynamodb"), None, {"held": [_held("w2")]})
    report: dict[str, Any] = {"flagged_unservable": [row]}
    report["facts"] = compute_report_facts(report)
    assert scan_json(report) == []
    row["explanation"] = "pinned on dynamodb"
    assert [f.path for f in scan_json(report)] == ["$.flagged_unservable[0].explanation"]


def test_the_orchestrator_keeps_the_held_records_in_router_output(tmp_path) -> None:
    """q-1 (flagged on DynamoDB) shares a write group with the pinned q-4, so the
    router holds it; the cumulative router_output.json keeps the flag with it (no
    model call; nothing moves, so no schema designer is reached)."""
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
                {"query_id": "q-4", "assigned_engine": "dynamodb", "customer_override": True},
            ],
            "write_groups": {"db.orders": ["q-1", "q-4"]},
        },
    )
    calls: list = []
    case._run(store, calls)

    assert calls == [] and not store.exists(f"{prefix}/assignment/v2/assignment.json")
    router = store.read_json(f"{prefix}/post-schema-router/router_output.json")
    assert router["held_queries"] == ["q-1"]
    assert router["held"] == [
        {"query_id": "q-1", "flagged_engine": "dynamodb", "reason_category": "full_scan"}
    ]
    assert router["terminal"] == []


@pytest.mark.usefixtures("schema_contract_off")
def test_the_held_records_of_every_pass_are_kept(tmp_path) -> None:
    """Pass 0 holds q-1 (its table's write group holds the pinned q-4) and moves
    q-2 onto DynamoDB, which is redesigned; pass 1 sees no flag on the new
    DynamoDB design. The cumulative router_output.json still names q-1 with its
    flag, not only the last pass's (empty) records."""
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
                {"query_id": "q-2", "assigned_engine": "aurora_mysql"},
                {"query_id": "q-3", "assigned_engine": "documentdb"},
                {"query_id": "q-4", "assigned_engine": "dynamodb", "customer_override": True},
            ],
            "write_groups": {"db.orders": ["q-1", "q-4"]},
        },
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

    last = store.read_json(f"{prefix}/post-schema-router/pass-1/router_output.json")
    assert last["held"] == [] and last["held_queries"] == []
    router = store.read_json(f"{prefix}/post-schema-router/router_output.json")
    assert router["held_queries"] == ["q-1"]
    assert router["held"] == [
        {"query_id": "q-1", "flagged_engine": "dynamodb", "reason_category": "full_scan"}
    ]


def test_synthesis_raises_the_held_query_as_a_risk_before_the_summary(store) -> None:
    """#1148: the flagged rows are read before the risks, so the register, the
    facts and the summary count the held query's risk."""
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    _write_synthesis_report(store, result, assignment_version=1)
    report = store.read_json(f"{DB}/{JOB}/synthesis/v1/report.json")
    [risk] = [r for r in report["risk_assessment"]["risks"] if r.get("rule") == "routing.flagged"]
    assert risk["severity"] == "MEDIUM" and risk["query_ids"] == ["q-hold"]
    assert risk["affected_tables"] == ["t"]
    assert report["facts"]["open_risk_count"] == len(report["risk_assessment"]["risks"])
    assert any(risk["risk_id"] in s for s in report["risk_assessment"]["mitigation_strategies"])
