"""Queries no engine in the customer's engine scope can serve (#1329).

ADR-031 Decision 2 Amendment 15, maintainer decision 2 on #1181: such a query is listed
as a blocking risk with the reason, left out of the waves and the schema designs, and
never forced onto an engine. The first assignment records a Move to ``unservable``
(the move ledger is the only writer), keeps the query out of ``query_assignments`` and
lists it in ``Assignment.unservable_queries`` with its rule, its reason, each in-scope
engine's gate and the out-of-scope engine the fallback rule names. A group is
unservable as one unit (Decision 4, Amendment 9). With no scope nothing changes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.engine_scope import check_in_scope
from src.agents.referee.move_ledger import replay, state_of
from src.agents.referee.routing_trace import RoutingTrace
from src.agents.referee.triage import triage
from src.contracts.assignment_models import Assignment
from src.contracts.move_ledger import Move
from src.storage.assignment_versioning import engine_scope
from tests.unit.agents.referee.test_engine_scope import _analysis, _collector, _query, _triage_doc

DDB = ["dynamodb"]

# A read of orders DynamoDB serves, two writes of orders (DynamoDB can serve w1 alone,
# never w2's aggregate), and a utility statement only a relational engine runs.
READ = _query("r1", "SELECT id, total FROM orders WHERE id = ?")
W1 = {**_query("w1", "UPDATE orders SET total = ? WHERE id = ?"), "query_type": "UPDATE"}
W2 = {
    **_query(
        "w2",
        "UPDATE orders SET total = (SELECT SUM(total) FROM orders o2 "
        "WHERE o2.customer_ref = orders.customer_ref GROUP BY customer_ref) WHERE id = ?",
    ),
    "query_type": "UPDATE",
}
SHOW = _query("q9", "SHOW TABLES")


def _resolve(
    scope: list[str] | None,
    queries: list[dict],
    source: str | None = "mysql",
    routing_trace: RoutingTrace | None = None,
) -> Assignment:
    collector = _collector(source, queries)
    result = triage(collector, engine_scope=scope)
    analyses = {e: _analysis() for e in result.selected}
    return AssignmentResolver().resolve(
        _triage_doc(result, scope), analyses, collector, routing_trace=routing_trace
    )


def _ids(assignment: Assignment) -> list[str]:
    return [qa.query_id for qa in assignment.query_assignments]


def _unservable(assignment: Assignment) -> dict[str, Any]:
    return {u.query_id: u for u in assignment.unservable_queries}


@pytest.mark.usefixtures("table_confidence_scores")
class TestTheFirstAssignment:
    def test_a_query_no_engine_in_scope_can_serve_is_unservable_not_forced(self) -> None:
        assignment = _resolve(["dynamodb", "documentdb"], [_query("q1"), SHOW])
        assert _ids(assignment) == ["q1"]
        record = _unservable(assignment)["q9"]
        assert record.rule == "fallback.no_score"
        # The engine the fallback rule names, outside the scope: context, never assigned.
        assert record.closest_engine == "aurora_mysql"
        assert {(g.engine, g.gate) for g in record.gates} == {
            ("documentdb", "capability"),
            ("dynamodb", "capability"),
        }
        assert "No engine in the customer's engine scope (DocumentDB, DynamoDB)" in record.reason
        assert "a new assessment whose scope adds Aurora MySQL" in record.reason
        assert record.source_tables == ["app.orders"]

    def test_the_unservable_state_is_a_move_through_the_ledger(self) -> None:
        assignment = _resolve(DDB, [_query("q1"), SHOW])
        (move,) = [m for m in assignment.moves if m.query_id == "q9"]
        assert (move.pass_.value, move.to_engine, move.reason_code.value) == (
            "unservable",
            "unservable",
            "fallback.no_score",
        )
        assert move.from_engine is None and move.tied_engines == []
        assert move.evidence.closest_engine == "aurora_mysql"
        assert [(c.engine, c.gate) for c in move.evidence.candidates] == [
            ("dynamodb", "capability")
        ]
        # v1 replays from its Moves, the unservable query to the unservable state.
        replayed = replay(assignment.moves)
        assert replayed == state_of(assignment.query_assignments, assignment.unservable_queries)
        assert replayed["q9"] == ("unservable", [])

    def test_the_scope_guard_accepts_the_version(self) -> None:
        assignment = _resolve(DDB, [_query("q1"), SHOW])
        dumped = assignment.model_dump(mode="json")
        check_in_scope(dumped["query_assignments"], DDB, "first assignment")
        check_in_scope(assignment.query_assignments, DDB, "first assignment")

    def test_every_query_unservable_leaves_no_query_assigned(self) -> None:
        assignment = _resolve(DDB, [SHOW, _query("q8", "SHOW COLUMNS FROM orders")])
        assert assignment.query_assignments == []
        assert sorted(_unservable(assignment)) == ["q8", "q9"]
        assert assignment.table_assignments == []
        assert assignment.cache_overlay is None
        for engine in ("dynamodb", "aurora_mysql"):
            assert engine_scope(assignment.model_dump(mode="json"), engine).query_ids == set()

    def test_no_scope_still_falls_back_and_lists_nothing(self) -> None:
        assignment = _resolve(None, [_query("q1"), SHOW])
        assert sorted(_ids(assignment)) == ["q1", "q9"]
        assert assignment.unservable_queries == []
        assert all(m.to_engine != "unservable" for m in assignment.moves)

    def test_a_scope_with_the_fallback_engine_in_it_has_nothing_unservable(self) -> None:
        assignment = _resolve(["aurora_mysql", "dynamodb"], [_query("q1"), SHOW])
        assert assignment.unservable_queries == []
        assert {qa.query_id: qa.assigned_engine for qa in assignment.query_assignments}[
            "q9"
        ] == "aurora_mysql"

    def test_a_heterogeneous_source_names_the_generic_relational_fallback(self) -> None:
        assignment = _resolve(DDB, [_query("q1"), SHOW], source="oracle")
        assert _unservable(assignment)["q9"].closest_engine == "aurora"


@pytest.mark.usefixtures("table_confidence_scores")
class TestAGroupIsUnservableAsOneUnit:
    """A table has one writer (ADR-031 Amendment 9): its writes are placed as one unit,
    and a unit no engine can serve falls back as one (Decision 4, Amendment 4). With the
    fallback outside the scope the unit is unservable as one: w1, which DynamoDB could
    serve alone, is never split from w2 onto it, so the table never has two writers.
    The read stays per query, as reads do."""

    def test_the_whole_write_group_is_unservable_and_the_read_stays(self) -> None:
        assignment = _resolve(DDB, [READ, W1, W2])
        assert _ids(assignment) == ["r1"]
        assert assignment.query_assignments[0].assigned_engine == "dynamodb"
        records = _unservable(assignment)
        assert sorted(records) == ["w1", "w2"]
        assert {r.rule for r in records.values()} == {"fallback.no_group_owner"}
        assert len({r.group_key for r in records.values()}) == 1
        # The gate names the member that ruled DynamoDB out for the group.
        for record in records.values():
            assert [(g.engine, g.gate, g.query_id) for g in record.gates] == [
                ("dynamodb", "capability", "w2")
            ]
            assert "every query of its write group, which moves as one" in record.reason
        moves = {m.query_id: m for m in assignment.moves if m.to_engine == "unservable"}
        assert moves["w1"].group_key == moves["w2"].group_key == records["w1"].group_key

    def test_the_stored_groups_leave_out_a_unit_with_no_assigned_member(self) -> None:
        assignment = _resolve(DDB, [READ, W1, W2])
        assert assignment.write_groups == {}
        unscoped = _resolve(None, [READ, W1, W2])
        assert unscoped.write_groups == {"app.orders": ["w1", "w2"]}


@pytest.mark.usefixtures("table_confidence_scores")
class TestTies:
    def test_an_unservable_query_never_joins_a_tie_or_the_settled_counts(self) -> None:
        trace = RoutingTrace()
        # q1 and q2 tie between DynamoDB and DocumentDB (equal fixed scores); q9 has
        # no engine in the scope.
        queries = [_query("q1"), _query("q2", "SELECT id, total FROM orders WHERE id = ?"), SHOW]
        assignment = _resolve(["documentdb", "dynamodb"], queries, routing_trace=trace)
        assert "unservable" not in trace.settled_counts
        for qa in assignment.query_assignments:
            assert qa.tied_engines == ["documentdb", "dynamodb"]
        assert list(_unservable(assignment)) == ["q9"]
        # The tie placeholders are the ones the same workload without q9 gets.
        without = _resolve(["documentdb", "dynamodb"], queries[:2])
        assert [
            (qa.query_id, qa.assigned_engine, qa.tied_engines)
            for qa in assignment.query_assignments
        ] == [
            (qa.query_id, qa.assigned_engine, qa.tied_engines) for qa in without.query_assignments
        ]


class TestTheContract:
    def test_an_assignment_written_before_the_state_loads_with_none(self) -> None:
        old = {
            "contract_version": "1.22",
            "job_id": "j",
            "version": 1,
            "status": "auto_generated",
            "timestamp": "2026-10-21T00:00:00Z",
            "query_assignments": [
                {
                    "query_id": "q1",
                    "assigned_engine": "dynamodb",
                    "confidence": 70,
                    "source_tables": ["app.orders"],
                    "assignment_reason": "highest fit for DynamoDB",
                }
            ],
            "table_assignments": [],
            "co_dependency_groups": [],
            "validation_warnings": [],
        }
        assert Assignment.model_validate(old).unservable_queries == []

    def test_an_unservable_move_round_trips(self) -> None:
        move = Move.model_validate(
            {
                "seq": 1,
                "query_id": "q9",
                "pass": "unservable",
                "to_engine": "unservable",
                "reason_code": "fallback.no_score",
                "evidence": {"closest_engine": "aurora_mysql"},
            }
        )
        again = Move.model_validate(json.loads(json.dumps(move.model_dump(mode="json"))))
        assert again == move
        assert again.evidence.closest_engine == "aurora_mysql"


@pytest.mark.usefixtures("table_confidence_scores")
def test_the_cache_overlay_shares_the_calls_of_assigned_queries_only() -> None:
    """The cache overlay's share of calls is over the queries that have an engine: an
    unservable query's calls are in no engine's workload, so not in the denominator."""
    hot = {"calls_per_second": 80.0, "rows_returned_avg": 1}
    queries = [
        _query("q1"),
        {**_query("h4", "SELECT id, total FROM orders ORDER BY total DESC LIMIT 10"), **hot},
        {**_query("h1", "SELECT COUNT(*) FROM orders WHERE customer_ref = ?"), **hot},
        {**_query("h3", "SELECT MAX(total) FROM orders"), **hot},
    ]
    assignment = _resolve(["dynamodb", "elasticache"], queries)
    assert sorted(_unservable(assignment)) == ["h1", "h3"]
    overlay = assignment.cache_overlay
    assert overlay is not None and overlay.query_count == 2
    assert overlay.call_share_percent == 100.0


def test_the_ledger_never_writes_unservable_as_an_engine() -> None:
    from src.agents.referee.move_ledger import MoveLedger, apply_to_assignment
    from src.contracts.move_ledger import MovePass, ReasonCode

    qa = {"query_id": "q9", "assigned_engine": "dynamodb", "tied_engines": []}
    ledger = MoveLedger({"q9": "dynamodb"})
    with pytest.raises(ValueError, match="unservable_in_assignment"):
        apply_to_assignment(
            ledger,
            qa,
            "unservable",
            pass_=MovePass.UNSERVABLE,
            reason_code=ReasonCode.FALLBACK_NO_SCORE,
        )
    assert qa["assigned_engine"] == "dynamodb" and ledger.moves == []


@pytest.mark.usefixtures("table_confidence_scores")
class TestTheDecisionTrace:
    """The review's item 2: a Move to ``unservable`` reads as no engine in the scope,
    never as an engine named "Unservable", in the trace the results page opens."""

    @pytest.mark.parametrize("qid, what", [("w1", "every query of its group"), ("q9", "it")])
    def test_the_trace_says_no_engine_in_scope(self, qid: str, what: str) -> None:
        from src.shared.query_trace import build_query_trace

        assignment = _resolve(DDB, [READ, W1, W2, SHOW])
        moves = [(1, m) for m in assignment.moves if m.query_id == qid]
        trace = build_query_trace(qid, moves, None, {"dynamodb"}, 1)
        closest = "(closest: Aurora MySQL, outside the scope)"
        assert trace.final_engine == "unservable"
        assert trace.final_engine_name == "No engine in scope"
        assert trace.first_assignment is not None
        assert trace.first_assignment.to_engine_name == "No engine in scope"
        assert trace.first_assignment.explanation == (
            f"No engine in the customer's engine scope can serve {what} {closest}."
        )
        assert trace.summary == (
            "Has no engine after 1 routing step: no engine in the customer's engine scope "
            f"can serve it {closest}."
        )
        assert trace.runner_up is None and trace.outscored_by == []
        dumped = trace.model_dump_json()
        assert "Unservable" not in dumped and "falls back to" not in dumped


def _list_writes(source: str) -> list[str]:
    """Each way ``source`` writes a ``unservable_queries`` field, as "kind:line"."""
    import ast

    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr == "unservable_queries":
                found.append(f"attribute:{t.lineno}")
            if isinstance(t, ast.Subscript) and getattr(t.slice, "value", None) == (
                "unservable_queries"
            ):
                found.append(f"subscript:{t.lineno}")
        if isinstance(node, ast.Call):
            if any(kw.arg == "unservable_queries" for kw in node.keywords):
                found.append(f"keyword:{node.lineno}")
            if isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault":
                if any(getattr(a, "value", None) == "unservable_queries" for a in node.args):
                    found.append(f"setdefault:{node.lineno}")
        if isinstance(node, ast.Dict) and any(
            getattr(k, "value", None) == "unservable_queries" for k in node.keys
        ):
            found.append(f"dict:{node.lineno}")
    return found


# The only writers of ``Assignment.unservable_queries``: the move ledger
# (``unservable_in_assignment``) and the first assignment, which builds v1's list from
# its own Moves to ``unservable``. ``report_facts`` writes ``facts.unservable``'s
# ``unservable_queries``, a different field of the same name (the facts' rows).
LIST_WRITERS = {
    "src/agents/referee/move_ledger.py",
    "src/agents/referee/assignment_resolver.py",
    "src/shared/report_facts.py",
}


def test_only_the_move_ledger_and_the_first_assignment_write_unservable_queries() -> None:
    """Every other writer of a version carries the list as it is, in the version it
    spreads (ADR-031 Amendment 15)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    offenders = [
        f"{rel}:{hit}"
        for path in sorted((root / "src").rglob("*.py"))
        if not (rel := path.relative_to(root).as_posix()).startswith("src/contracts/")
        and rel not in LIST_WRITERS
        for hit in _list_writes(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_guard_sees_each_way_to_write_the_list() -> None:
    source = (
        "a.unservable_queries = []\n"
        "d['unservable_queries'] = []\n"
        "f(unservable_queries=[])\n"
        "x = {**v, 'unservable_queries': []}\n"
        "d.setdefault('unservable_queries', [])\n"
        "y = a.unservable_queries\n"
    )
    assert [h.split(":")[0] for h in _list_writes(source)] == [
        "attribute",
        "subscript",
        "keyword",
        "dict",
        "setdefault",
    ]
