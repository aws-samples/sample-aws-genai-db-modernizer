"""#727: a co-dependency group goes to the best engine that can serve every member.

ADR-031 Decision 4, Amendment 4 (maintainer decision, 2026-10-14): only an
engine that can serve every member of the group (it passes every member's hard
and capability gates and may own every member) is a candidate for the group;
among those, the group's score is the mean of its members' own scores, and the
usual tie-break applies. ``co_dependency.group_engine_scores`` is that rule,
and these tests pin it in each pass that places a group.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.assignment_resolver import AssignmentResolver, RoutingTrace
from src.agents.referee.co_dependency import group_engine_scores
from src.agents.referee.model_flags import route_flagged_unit
from src.agents.referee.query_scoring import ScoringContext
from src.agents.referee.reality_check import run_reality_check
from src.contracts.move_ledger import MovePass

# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class TestTheRule:
    SCORES = {
        ("g1", "dynamodb"): 95.0,
        ("g2", "dynamodb"): None,
        ("g1", "aurora_mysql"): 40.0,
        ("g2", "aurora_mysql"): 60.0,
        ("g1", "documentdb"): 70.0,
        ("g2", "documentdb"): 70.0,
    }

    def _score(self, qid: str, engine: str) -> float | None:
        return self.SCORES[(qid, engine)]

    def test_an_engine_that_cannot_serve_one_member_is_not_a_candidate(self) -> None:
        scores = group_engine_scores(["g1", "g2"], ["dynamodb", "aurora_mysql"], self._score)
        # DynamoDB's 95 for g1 alone (or 47.5 counting g2 as 0) never competes.
        assert scores == {"aurora_mysql": 50.0}

    def test_the_group_score_is_the_mean_of_the_members_scores(self) -> None:
        scores = group_engine_scores(
            ["g1", "g2"], ["documentdb", "aurora_mysql", "dynamodb"], self._score
        )
        assert scores == {"documentdb": 70.0, "aurora_mysql": 50.0}
        assert list(scores) == ["documentdb", "aurora_mysql"]

    def test_no_engine_serving_every_member_gives_no_candidates(self) -> None:
        assert group_engine_scores(["g1", "g2"], ["dynamodb"], self._score) == {}
        assert group_engine_scores([], ["dynamodb"], self._score) == {}

    def test_scoring_an_engine_stops_at_the_first_member_it_cannot_serve(self) -> None:
        seen: list[tuple[str, str]] = []

        def score(qid: str, engine: str) -> float | None:
            seen.append((qid, engine))
            return None

        group_engine_scores(["g1", "g2"], ["dynamodb"], score)
        assert seen == [("g1", "dynamodb")]


# ---------------------------------------------------------------------------
# The first assignment
# ---------------------------------------------------------------------------


def _resolver_query(qid: str, text: str) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": "SELECT",
        "tables_accessed": ["db.t1"],
        "join_count": 1,
        "has_joins": True,
        "has_aggregations": False,
        "filter_tables": ["db.t1"],
        "calls_per_second": 1.0,
        "rows_returned_avg": 10,
    }


class TestTheFirstAssignment:
    def test_a_group_never_lands_on_an_engine_gated_for_one_member(self) -> None:
        """DocumentDB scores 95 for both members on the analysis, but the
        recursive CTE in ``qb`` fails its capability gate: DocumentDB is not
        a candidate, and the Move's group scores do not list it."""
        queries = [
            _resolver_query("qa", "SELECT * FROM t1 a JOIN t1 b ON a.id = b.id"),
            _resolver_query(
                "qb",
                "WITH RECURSIVE tree AS (SELECT id FROM t1) SELECT * FROM t1 JOIN tree "
                "ON t1.id = tree.id",
            ),
        ]
        analyzed = {"documentdb": 95, "aurora_postgresql": 10, "aurora_mysql": 10}
        trace = RoutingTrace()
        result = AssignmentResolver().resolve(
            {"selected_agents": [{"agent_type": e} for e in analyzed], "signals": []},
            {
                e: {
                    "table_recommendations": [{"table_id": "db.t1", "confidence_score": c}],
                    "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
                }
                for e, c in analyzed.items()
            },
            {
                "job_id": "t",
                "metadata": {"source_database": {"engine": "sqlserver"}},
                "database_schema": {"tables": [{"table_id": "db.t1", "table_name": "t1"}]},
                "queries": {"query_patterns": queries},
            },
            routing_trace=trace,
        )

        engines = {qa.query_id: qa.assigned_engine for qa in result.query_assignments}
        assert engines["qa"] == engines["qb"] != "documentdb"
        group_moves = [m for m in result.moves if m.pass_ == MovePass.CO_DEPENDENCY]
        assert {m.query_id for m in group_moves} == {"qa", "qb"}
        for move in group_moves:
            assert "documentdb" not in move.evidence.group_scores
        assert "documentdb" not in trace.by_query["qa"]["group"]["scores"]


# ---------------------------------------------------------------------------
# The reality check's justification floor
# ---------------------------------------------------------------------------


def _floor_inputs(
    g2_capabilities: list[str] | None,
    *,
    tied: list[str] | None = None,
    engine: str = "documentdb",
) -> tuple[dict, dict, dict, dict]:
    """DocumentDB is mandatory (a signal override) and holds only the group
    (g1, g2), far below its cost-share floor, so the floor moves the group.

    g1 scores 95 on DynamoDB on its own; with ``g2_capabilities`` set to
    ``["sql_admin"]``, DynamoDB cannot serve g2 at all, so the only engine that
    serves both is Aurora MySQL. ``tied`` gives both members that unresolved
    tie; ``engine="opensearch"`` puts the group on OpenSearch instead (a
    text_search override on two shallow joins, which fail OpenSearch's own
    #326 floor).
    """
    n_dynamo = 40
    signal = "flexible_schema" if engine == "documentdb" else "text_search"
    group_qa: dict = {
        "assigned_engine": engine,
        "assignment_reason": f"signal override: {signal}",
        "signal_override": signal,
    }
    if tied:
        group_qa["tied_engines"] = list(tied)
    assignment = {
        "version": 1,
        "query_assignments": (
            [
                {"query_id": f"d{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
                for i in range(n_dynamo)
            ]
            + [
                {"query_id": u, "assigned_engine": "aurora_mysql", "assignment_reason": "t"}
                for u in ("util1", "util2")
            ]
            + [{"query_id": qid, **group_qa} for qid in ("g1", "g2")]
        ),
    }
    capabilities: dict[str, list[str]] = {"util1": ["sql_admin"], "util2": ["sql_admin"]}
    if g2_capabilities:
        capabilities["g2"] = g2_capabilities
    triage = {
        "selected_agents": [
            {"agent_type": "dynamodb"},
            {"agent_type": "aurora_mysql"},
            {"agent_type": engine},
        ],
        "signals": [],
        "query_capabilities": capabilities,
    }
    collector = {
        "queries": {
            "query_patterns": (
                [
                    {"query_id": f"d{i}", "tables_accessed": ["db.d"], "query_type": "SELECT"}
                    for i in range(n_dynamo)
                ]
                + [
                    {
                        "query_id": "util1",
                        "tables_accessed": ["db.util"],
                        "query_text": "SHOW FULL FIELDS FROM db.util",
                    },
                    {
                        "query_id": "util2",
                        "tables_accessed": ["db.util"],
                        "query_text": "SET SESSION SQL_BIG_SELECTS = ?",
                    },
                ]
                + [
                    {
                        "query_id": qid,
                        "tables_accessed": ["db.shared", f"db.{qid}"],
                        "query_type": "SELECT",
                        "has_joins": True,
                        "has_aggregations": True,
                    }
                    for qid in ("g1", "g2")
                ]
            )
        }
    }
    analysis = {
        "dynamodb": {
            "table_recommendations": [
                {"table_id": "db.d", "confidence_score": 90},
                {"table_id": "db.util", "confidence_score": 30},
                {"table_id": "db.shared", "confidence_score": 85},
                {"table_id": "db.g1", "confidence_score": 95},
                {"table_id": "db.g2", "confidence_score": 20},
            ]
        },
        "aurora_mysql": {
            "table_recommendations": [
                {"table_id": "db.d", "confidence_score": 50},
                {"table_id": "db.util", "confidence_score": 60},
                {"table_id": "db.shared", "confidence_score": 40},
                {"table_id": "db.g1", "confidence_score": 20},
                {"table_id": "db.g2", "confidence_score": 60},
            ]
        },
        engine: {
            "table_recommendations": [
                {"table_id": "db.shared", "confidence_score": 70},
                {"table_id": "db.g1", "confidence_score": 70},
                {"table_id": "db.g2", "confidence_score": 70},
            ]
        },
    }
    return assignment, triage, analysis, collector


def _group_result(
    g2_capabilities: list[str] | None, **kwargs: Any
) -> tuple[dict[str, str], list[dict], dict]:
    result = run_reality_check(*_floor_inputs(g2_capabilities, **kwargs))
    engines = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
    group = [qa for qa in result["revised_assignments"] if qa["query_id"] in {"g1", "g2"}]
    return engines, group, result


class TestTheJustificationFloor:
    def test_the_floor_never_moves_a_group_onto_an_engine_that_strands_a_member(self) -> None:
        """Before #727 the floor counted no member of a mandatory group when it
        asked which engines could take it, and scored an engine the group had no
        mean for by the first member's own score. The group went to DynamoDB on
        g1's 90, leaving g2 (a utility statement) on an engine that cannot run it."""
        engines, group, _ = _group_result(["sql_admin"])

        assert engines["g1"] == engines["g2"] == "aurora_mysql"
        for qa in group:
            assert "Aurora MySQL scores" in qa["assignment_reason"], qa["assignment_reason"]

    def test_the_floor_asks_every_member_what_a_lone_query_is_asked(self) -> None:
        """Every member must pass the reality check's own eligibility rule, the
        same one a lone floor move passes: a join consolidated off DocumentDB
        never lands on DynamoDB (review of #375). Before #727 the group skipped
        it and went to DynamoDB on its 71 mean; now the only engine that can
        take both members is Aurora MySQL, at the group's mean there."""
        engines, _, result = _group_result(None)

        assert engines["g1"] == engines["g2"] == "aurora_mysql"
        moves = [
            m
            for m in result["move_ledger"].moves
            if m.query_id in {"g1", "g2"} and m.pass_ == MovePass.FLOOR
        ]
        assert {m.query_id for m in moves} == {"g1", "g2"}
        for move in moves:
            assert set(move.evidence.group_scores) == {"aurora_mysql"}
            assert move.score_after == move.evidence.group_scores["aurora_mysql"]

    @pytest.mark.parametrize(
        ("engine", "tied"),
        [("documentdb", ["documentdb", "dynamodb"]), ("opensearch", ["dynamodb", "opensearch"])],
    )
    def test_a_tied_group_counts_every_tied_engine_as_current(
        self, engine: str, tied: list[str]
    ) -> None:
        """ADR-031 Decision 12, paragraph 4: for a tied query, every tied engine
        counts as current for the join-onto-DynamoDB block. Both members are
        tied with DynamoDB, so the block does not apply and the group goes to
        DynamoDB on its mean (the #864 review's probe, on DocumentDB and on
        OpenSearch)."""
        engines, _, result = _group_result(None, tied=tied, engine=engine)

        assert engines["g1"] == engines["g2"] == "dynamodb"
        moves = [
            m
            for m in result["move_ledger"].moves
            if m.query_id in {"g1", "g2"} and m.pass_ == MovePass.FLOOR
        ]
        assert {m.query_id for m in moves} == {"g1", "g2"}
        for move in moves:
            assert move.score_after == move.evidence.group_scores["dynamodb"]
            assert move.score_after == max(move.evidence.group_scores.values())

    def test_the_before_score_is_the_groups_score_on_the_engine_it_leaves(self) -> None:
        """The Move's ``score_before`` is the group's mean on the engine it
        leaves, and none when that engine cannot serve every member: g2 is gated
        on DocumentDB, so there is no group score there (not g1's own 70)."""
        engines, group, result = _group_result(["sql_admin"])

        moves = [
            m
            for m in result["move_ledger"].moves
            if m.query_id in {"g1", "g2"} and m.pass_ == MovePass.FLOOR
        ]
        assert moves
        for move in moves:
            assert move.score_before is None
        for qa in group:
            assert "DocumentDB scores n/a" in qa["assignment_reason"], qa["assignment_reason"]

        _, _, served = _group_result(None)
        for move in served["move_ledger"].moves:
            if move.query_id in {"g1", "g2"} and move.pass_ == MovePass.FLOOR:
                assert move.score_before == 70.0


# ---------------------------------------------------------------------------
# A model's flag
# ---------------------------------------------------------------------------


def _context(scores: dict[str, dict[str, float]]) -> ScoringContext:
    analysis: dict[str, dict] = {}
    for qid, per_engine in scores.items():
        for engine, score in per_engine.items():
            analysis.setdefault(engine, {"table_recommendations": []})[
                "table_recommendations"
            ].append({"table_id": f"t_{qid}", "confidence_score": score})
    return ScoringContext(analysis_outputs=analysis)


def _query_map(*query_ids: str) -> dict[str, dict]:
    return {
        qid: {
            "query_id": qid,
            "query_text": f"SELECT * FROM t_{qid} WHERE id = ?",
            "query_type": "SELECT",
            "tables_accessed": [f"t_{qid}"],
        }
        for qid in query_ids
    }


class TestAFlaggedGroup:
    def test_a_flagged_group_never_moves_onto_an_engine_gated_for_one_member(self) -> None:
        """g1 scores 99 on DocumentDB, but a second flag gates DocumentDB for g2:
        DocumentDB is not a candidate (not even at g1's score, or a mean with g2
        at 0), and the group goes to Aurora MySQL."""
        ctx = _context(
            {
                "g1": {"dynamodb": 50, "documentdb": 99, "aurora_mysql": 40},
                "g2": {"dynamodb": 50, "documentdb": 99, "aurora_mysql": 40},
            }
        )
        routing = route_flagged_unit(
            ["g1", "g2"],
            "dynamodb",
            ["aurora_mysql", "documentdb", "dynamodb"],
            ctx.with_flag_gaps({"g1": ["dynamodb"], "g2": ["dynamodb", "documentdb"]}),
            _query_map("g1", "g2"),
        )

        assert routing.destination == "aurora_mysql"
        assert dict(routing.scores) == {"aurora_mysql": 40}
