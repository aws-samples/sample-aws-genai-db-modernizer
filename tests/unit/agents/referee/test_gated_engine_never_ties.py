"""#766: an engine the capability gate excludes for a query is not a candidate
for it (ADR-031 Decision 2 step 2), so it never joins an exact tie.

Before the fix the first assignment scored a gated engine 0 and kept it in the
candidate set, so when every engine also scored 0 the gated one tied with the
rest and could win through ``tie_break.break_tie``. These tests pin the
corrected behaviour: the gated engine is dropped before the tie is formed, a
tie among engines that pass the gate is unchanged, and the Aurora fallback
still applies when no engine passes.
"""

from __future__ import annotations

from src.agents.referee.assignment_resolver import AssignmentResolver, RoutingTrace
from src.contracts.assignment_models import Assignment, QueryAssignment
from src.contracts.move_ledger import MovePass

RECURSIVE_CTE = (
    "WITH RECURSIVE tree AS (SELECT id FROM t1 UNION ALL SELECT id FROM t1) " "SELECT * FROM tree"
)
PLAIN_LOOKUP = "SELECT * FROM t1 WHERE id = ?"


def _query(qid: str, text: str, *, joins: int = 0) -> dict:
    # A join filtered on its joined table is significant, so two such queries
    # on the same table form a co-dependency group (``is_significant_join``).
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": "SELECT",
        "tables_accessed": ["db.t1"],
        "join_count": joins,
        "has_joins": joins > 0,
        "has_aggregations": False,
        "filter_tables": ["db.t1"] if joins else [],
        "calls_per_second": 1.0,
        "rows_returned_avg": 10,
    }


def _collector(queries: list[dict], source_engine: str = "sqlserver") -> dict:
    return {
        "job_id": "t",
        "metadata": {"source_database": {"engine": source_engine}},
        "database_schema": {"tables": [{"table_id": "db.t1", "table_name": "t1"}]},
        "queries": {"query_patterns": queries},
    }


def _triage(engines: list[str]) -> dict:
    return {"selected_agents": [{"agent_type": e} for e in engines], "signals": []}


def _analysis(confidence: int) -> dict:
    return {
        "table_recommendations": [{"table_id": "db.t1", "confidence_score": confidence}],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


def _resolve(
    queries: list[dict],
    analyzed: dict[str, int],
    selected: list[str] | None = None,
    source_engine: str = "sqlserver",
) -> tuple[Assignment, RoutingTrace]:
    trace = RoutingTrace()
    result = AssignmentResolver().resolve(
        _triage(selected or list(analyzed)),
        {engine: _analysis(confidence) for engine, confidence in analyzed.items()},
        _collector(queries, source_engine),
        routing_trace=trace,
    )
    return result, trace


def _qa(result: Assignment, qid: str) -> QueryAssignment:
    return next(qa for qa in result.query_assignments if qa.query_id == qid)


def _gated(trace: RoutingTrace, qid: str) -> set[str]:
    engines = trace.by_query[qid]["engines"]
    return {e for e, v in engines.items() if isinstance(v, dict) and "gate" in v}


class TestIssueRepro:
    """The issue's repro: SQL Server source, a WITH RECURSIVE query, every
    analyzed engine scoring 0, DynamoDB failing the capability gate."""

    def test_the_gated_engine_does_not_win_a_zero_score_tie(self):
        result, trace = _resolve(
            [_query("q1", RECURSIVE_CTE)],
            {"dynamodb": 0, "aurora_postgresql": 0, "aurora_mysql": 0},
        )
        q1 = _qa(result, "q1")

        assert "dynamodb" in _gated(trace, "q1")
        assert q1.assigned_engine == "aurora_postgresql"
        # Only one engine passes the gate: there is no tie to report at all.
        assert q1.tied_engines == []
        assert "tie" not in q1.assignment_reason
        move = next(m for m in result.moves if m.query_id == "q1")
        assert move.pass_ == MovePass.SCORE
        assert move.to_engine == "aurora_postgresql"

    def test_a_gated_engine_is_never_listed_as_tied(self):
        result, trace = _resolve(
            [_query("q1", RECURSIVE_CTE)],
            {"dynamodb": 0, "opensearch": 0, "aurora_postgresql": 0, "aurora_mysql": 0},
        )
        q1 = _qa(result, "q1")
        gated = _gated(trace, "q1")

        assert "dynamodb" in gated
        assert q1.assigned_engine not in gated
        assert not gated & set(q1.tied_engines)


class TestTieAmongEnginesThatPassTheGate:
    def test_a_tie_with_no_gated_engine_is_unchanged(self):
        result, trace = _resolve(
            [_query("q1", PLAIN_LOOKUP)],
            {"dynamodb": 70, "documentdb": 70},
        )
        q1 = _qa(result, "q1")

        assert _gated(trace, "q1") == set()
        assert q1.tied_engines == ["documentdb", "dynamodb"]
        assert trace.tie_break_by_query["q1"] == "tie_unresolved"

    def test_the_tie_among_the_engines_that_pass_survives_dropping_the_gated_one(self):
        """DynamoDB and DocumentDB both lack the scan capability a recursive
        CTE needs; OpenSearch and Aurora PostgreSQL have it and tie at 0. The
        gated engines leave the tie; the remaining two still tie."""
        result, trace = _resolve(
            [_query("q1", RECURSIVE_CTE)],
            {"dynamodb": 0, "documentdb": 0, "opensearch": 0, "aurora_postgresql": 0},
        )
        q1 = _qa(result, "q1")

        assert {"dynamodb", "documentdb"} <= _gated(trace, "q1")
        assert q1.tied_engines == ["aurora_postgresql", "opensearch"]
        assert q1.assigned_engine in {"aurora_postgresql", "opensearch"}


class TestNoEnginePassesTheGate:
    def test_the_aurora_fallback_still_applies(self):
        """Only DynamoDB and DocumentDB were analyzed and both are gated: the
        query falls back to the Aurora engine triage selected, rather than a
        placeholder pick between two engines that cannot run it."""
        result, trace = _resolve(
            [_query("q1", RECURSIVE_CTE)],
            {"dynamodb": 0, "documentdb": 0},
            selected=["dynamodb", "documentdb", "aurora_postgresql"],
        )
        q1 = _qa(result, "q1")

        assert {"dynamodb", "documentdb"} <= _gated(trace, "q1")
        assert q1.assigned_engine == "aurora_postgresql"
        assert q1.tied_engines == []
        move = next(m for m in result.moves if m.query_id == "q1")
        assert move.pass_ == MovePass.AURORA_FALLBACK


class TestCoDependencyGroup:
    """A group moves as one unit (Decision 4), so an engine gated for any
    member cannot host the group."""

    def _group(self) -> list[dict]:
        return [
            _query("qa", "SELECT * FROM t1 a JOIN t1 b ON a.id = b.id", joins=1),
            _query(
                "qb",
                "WITH RECURSIVE tree AS (SELECT id FROM t1) SELECT * FROM t1 JOIN tree "
                "ON t1.id = tree.id",
                joins=1,
            ),
        ]

    def test_a_group_tie_drops_an_engine_gated_for_one_member(self):
        result, trace = _resolve(
            self._group(),
            {"documentdb": 0, "aurora_postgresql": 0, "aurora_mysql": 0},
        )

        assert "documentdb" in _gated(trace, "qb")
        assert "documentdb" not in _gated(trace, "qa")
        for qid in ("qa", "qb"):
            qa = _qa(result, qid)
            assert qa.assigned_engine == "aurora_postgresql"
            assert qa.tied_engines == []

    def test_an_engine_gated_for_one_member_cannot_win_the_group_outright(self):
        result, trace = _resolve(
            self._group(),
            {"documentdb": 95, "aurora_postgresql": 10, "aurora_mysql": 10},
        )

        assert "documentdb" in _gated(trace, "qb")
        assert {_qa(result, q).assigned_engine for q in ("qa", "qb")} == {"aurora_postgresql"}


class TestSignalOverrideRespectsTheGate:
    """A listed override skips Decision 2's steps 3 to 5, never step 2: a
    signal override onto an engine the capability gate excluded is not
    applied, and the query is scored like any other (#766 review)."""

    TABLES = ["db.t1", "db.t2", "db.t3"]

    def _resolve(self, analyzed: list[str]) -> tuple[Assignment, RoutingTrace]:
        query = {
            "query_id": "q1",
            "query_text": (
                "SELECT a.id FROM t1 a JOIN t2 b ON a.id = b.id JOIN t3 c ON c.id = b.id "
                "WHERE MATCH(a.title) AGAINST (?)"
            ),
            "query_type": "SELECT",
            "tables_accessed": self.TABLES,
            "join_count": 2,
            "has_joins": True,
            "has_aggregations": False,
            "filter_tables": [],
            "calls_per_second": 20.0,
            "rows_returned_avg": 10,
        }
        collector = {
            "job_id": "t",
            "metadata": {"source_database": {"engine": "mysql"}},
            "database_schema": {
                "tables": [{"table_id": t, "table_name": t.split(".")[-1]} for t in self.TABLES]
            },
            "queries": {"query_patterns": [query]},
        }
        triage = {
            "selected_agents": [{"agent_type": e} for e in analyzed],
            "signals": [
                {
                    "signal": "text_search",
                    "targets": ["opensearch"],
                    "query_ids": ["q1"],
                    "evidence": "MATCH ... AGAINST",
                },
                {
                    "signal": "complex_joins",
                    "targets": ["aurora_mysql"],
                    "query_ids": ["q1"],
                    "evidence": "three-table join",
                },
            ],
        }
        analysis = {
            engine: {
                "table_recommendations": [
                    {"table_id": t, "confidence_score": 70} for t in self.TABLES
                ],
                "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
            }
            for engine in analyzed
        }
        trace = RoutingTrace()
        result = AssignmentResolver().resolve(triage, analysis, collector, routing_trace=trace)
        return result, trace

    def test_the_override_never_lands_on_a_gated_engine(self):
        result, trace = self._resolve(["opensearch", "dynamodb", "aurora_mysql"])
        q1 = _qa(result, "q1")

        assert "opensearch" in _gated(trace, "q1")
        assert q1.assigned_engine == "aurora_mysql"
        assert q1.signal_override is None
        move = next(m for m in result.moves if m.query_id == "q1")
        assert move.pass_ == MovePass.SCORE

    def test_with_no_engine_passing_the_override_falls_to_aurora(self):
        result, trace = self._resolve(["opensearch", "dynamodb"])
        q1 = _qa(result, "q1")

        assert {"opensearch", "dynamodb"} <= _gated(trace, "q1")
        assert q1.assigned_engine not in {"opensearch", "dynamodb"}
        move = next(m for m in result.moves if m.query_id == "q1")
        assert move.pass_ == MovePass.AURORA_FALLBACK
