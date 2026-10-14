"""Model flags re-route by score (#647, #667): synthetic flags, their destinations.

A flag says "these queries cannot be served on this engine". The shared scorer
treats that engine as unable to serve them and picks the best remaining engine
(``src.agents.referee.model_flags``). These tests use synthetic flags, since a
deterministic run never has any.
"""

from __future__ import annotations

from src.agents.referee.consolidation_validator import sanity_sweep
from src.agents.referee.model_flags import (
    apply_consolidation_flags,
    parse_model_flags,
    route_flagged_unit,
)
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.agents.referee.tie_break import TIE_KIND_SOURCE_COMPATIBLE
from src.contracts.model_flag import ModelFlag
from src.contracts.reality_check_output import FlagDecision


def _query_map(*query_ids: str, **overrides: dict) -> dict[str, dict]:
    """One SELECT per id on its own table ``t_<id>``, so each scores independently."""
    queries = {
        qid: {
            "query_id": qid,
            "query_text": f"SELECT * FROM t_{qid} WHERE id = ?",
            "query_type": "SELECT",
            "tables_accessed": [f"t_{qid}"],
        }
        for qid in query_ids
    }
    for qid, extra in overrides.items():
        queries[qid] = {**queries[qid], **extra}
    return queries


def _context(scores: dict[str, dict[str, float]]) -> ScoringContext:
    """``scores[qid][engine]`` becomes that query's score on that engine."""
    analysis: dict[str, dict] = {}
    for qid, per_engine in scores.items():
        for engine, score in per_engine.items():
            analysis.setdefault(engine, {"table_recommendations": []})[
                "table_recommendations"
            ].append({"table_id": f"t_{qid}", "confidence_score": score})
    return ScoringContext(analysis_outputs=analysis)


def _flag(qids: list[str], engine: str, category: str = "client_side_join") -> ModelFlag:
    return ModelFlag.model_validate(
        {
            "query_ids": qids,
            "flagged_engine": engine,
            "reason_category": category,
            "explanation": "needs a server-side join",
        }
    )


def _consolidation(src: str, dst: str, count: int) -> dict:
    return {
        "from_engine": src,
        "to_engine": dst,
        "query_count": count,
        "reason": "no unique value",
        "saved_cost_estimate": 250,
        "action": "full",
        "queries_retained": [],
        "retention_reason": None,
    }


class TestTheFlaggedEngineIsGated:
    def test_the_scorer_gates_a_flagged_engine_for_that_query_only(self) -> None:
        ctx = _context({"q1": {"dynamodb": 90}, "q2": {"dynamodb": 90}})
        flagged = ctx.with_flag_gaps({"q1": ["dynamodb"]})
        queries = _query_map("q1", "q2")

        gated = score_query_for_engine(queries["q1"], "dynamodb", flagged)
        assert gated.score is None
        assert gated.gate_kind == "model_flag"
        assert score_query_for_engine(queries["q2"], "dynamodb", flagged).score == 90
        # The original context is untouched (a copy carries the gap).
        assert score_query_for_engine(queries["q1"], "dynamodb", ctx).score == 90


class TestRouteFlaggedUnit:
    def test_goes_to_the_best_scoring_remaining_engine(self) -> None:
        ctx = _context({"q1": {"aurora_mysql": 60, "documentdb": 70, "dynamodb": 80}})
        routing = route_flagged_unit(
            ["q1"],
            "aurora_mysql",
            ["aurora_mysql", "documentdb", "dynamodb"],
            ctx.with_flag_gaps({"q1": ["aurora_mysql"]}),
            _query_map("q1"),
        )
        assert routing.destination == "dynamodb"
        assert routing.score_before == 60
        assert routing.score_after == 80

    def test_the_flagged_engine_is_never_the_destination_even_when_it_scores_best(self) -> None:
        ctx = _context({"q1": {"aurora_mysql": 99, "documentdb": 10}})
        routing = route_flagged_unit(
            ["q1"],
            "aurora_mysql",
            ["aurora_mysql", "documentdb"],
            ctx.with_flag_gaps({"q1": ["aurora_mysql"]}),
            _query_map("q1"),
        )
        assert routing.destination == "documentdb"
        assert routing.score_before == 99

    def test_no_other_engine_means_it_stays_with_a_reason(self) -> None:
        ctx = _context({"q1": {"dynamodb": 50}})
        routing = route_flagged_unit(
            ["q1"],
            "dynamodb",
            ["dynamodb"],
            ctx.with_flag_gaps({"q1": ["dynamodb"]}),
            _query_map("q1"),
        )
        assert routing.destination is None
        assert routing.blocked_reason

    def test_an_exact_tie_goes_to_the_source_compatible_engine(self) -> None:
        ctx = _context({"q1": {"dynamodb": 40, "documentdb": 70, "aurora_mysql": 70}})
        routing = route_flagged_unit(
            ["q1"],
            "dynamodb",
            ["aurora_mysql", "documentdb", "dynamodb"],
            ctx.with_flag_gaps({"q1": ["dynamodb"]}),
            _query_map("q1"),
            source_engine="mysql",
        )
        assert routing.destination == "aurora_mysql"
        assert routing.tie_kind == TIE_KIND_SOURCE_COMPATIBLE

    def test_a_tie_with_no_source_compatible_engine_uses_the_settled_workload(self) -> None:
        """ADR-031 Decision 12, through the shared ``break_tie``: an Oracle source
        has no source-compatible engine, so the settled workload decides."""
        ctx = _context({"q1": {"dynamodb": 40, "aurora_mysql": 70, "aurora_postgresql": 70}})
        routing = route_flagged_unit(
            ["q1"],
            "dynamodb",
            ["aurora_mysql", "aurora_postgresql", "dynamodb"],
            ctx.with_flag_gaps({"q1": ["dynamodb"]}),
            _query_map("q1"),
            source_engine="oracle",
            settled={"aurora_postgresql": 3, "aurora_mysql": 1},
        )
        assert routing.destination == "aurora_postgresql"
        assert routing.tie_kind == "tie_unresolved"
        assert routing.tied == ("aurora_mysql", "aurora_postgresql")

    def test_a_write_never_lands_on_opensearch(self) -> None:
        queries = _query_map(
            "w1",
            w1={"query_text": "INSERT INTO t_w1 VALUES (?)", "query_type": "INSERT"},
        )
        ctx = _context({"w1": {"documentdb": 50, "opensearch": 95, "aurora_mysql": 30}})
        routing = route_flagged_unit(
            ["w1"],
            "documentdb",
            ["aurora_mysql", "documentdb", "opensearch"],
            ctx.with_flag_gaps({"w1": ["documentdb"]}),
            queries,
        )
        assert routing.destination == "aurora_mysql"

    def test_a_group_moves_as_one_unit_by_its_combined_score(self) -> None:
        ctx = _context(
            {
                "g1": {"dynamodb": 50, "documentdb": 90, "aurora_mysql": 60},
                "g2": {"dynamodb": 50, "documentdb": 20, "aurora_mysql": 60},
            }
        )
        routing = route_flagged_unit(
            ["g1", "g2"],
            "dynamodb",
            ["aurora_mysql", "documentdb", "dynamodb"],
            ctx.with_flag_gaps({"g1": ["dynamodb"]}),
            _query_map("g1", "g2"),
        )
        # documentdb: (90 + 20) / 2 = 55; aurora_mysql: 60.
        assert routing.destination == "aurora_mysql"
        assert routing.score_after == 60


class TestApplyConsolidationFlags:
    def _moved_into_aurora_mysql(self) -> tuple[list[dict], list[dict], list[dict]]:
        """q1 and q2 were moved DynamoDB -> Aurora MySQL; q3 always sat on Aurora MySQL."""
        original = [
            {"query_id": "q1", "assigned_engine": "dynamodb"},
            {"query_id": "q2", "assigned_engine": "dynamodb"},
            {"query_id": "q3", "assigned_engine": "aurora_mysql"},
        ]
        revised = [
            {"query_id": "q1", "assigned_engine": "aurora_mysql", "assignment_reason": "moved"},
            {"query_id": "q2", "assigned_engine": "aurora_mysql", "assignment_reason": "moved"},
            {"query_id": "q3", "assigned_engine": "aurora_mysql", "assignment_reason": "kept"},
        ]
        return original, revised, [_consolidation("dynamodb", "aurora_mysql", 2)]

    def test_a_flag_on_a_move_into_aurora_goes_back_by_score_not_to_aurora(self) -> None:
        """#667: before, a flag on a move INTO Aurora MySQL left the query on Aurora
        MySQL with "Redirected to Aurora MySQL (relational patterns unserviceable on
        Aurora MySQL)". Now the flagged engine is gated and the scorer picks."""
        original, revised, consolidations = self._moved_into_aurora_mysql()
        ctx = _context({"q1": {"aurora_mysql": 70, "dynamodb": 65}})

        updated, records, decisions, gaps = apply_consolidation_flags(
            [_flag(["q1"], "aurora_mysql")],
            revised,
            consolidations,
            original,
            ctx,
            _query_map("q1", "q2", "q3"),
            {"aurora_mysql", "dynamodb"},
        )

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in updated}
        assert engine_of == {"q1": "dynamodb", "q2": "aurora_mysql", "q3": "aurora_mysql"}
        (decision,) = decisions
        assert decision["outcome"] == "moved"
        assert decision["destination_engine"] == "dynamodb"
        assert decision["score_before"] == 70
        assert decision["score_after"] == 65
        assert gaps == {"q1": {"aurora_mysql"}}
        texts = [qa["assignment_reason"] for qa in updated if qa["query_id"] == "q1"] + [
            r.get("retention_reason") or "" for r in records
        ]
        for text in texts:
            assert "Redirected to Aurora MySQL" not in text
        (record,) = records
        assert record["query_count"] == 1
        assert record["queries_retained"] == ["q1"]
        assert "DynamoDB scores best" in record["retention_reason"]
        FlagDecision.model_validate(decision)

    def test_a_flag_whose_original_engine_scores_worse_goes_to_the_better_one(self) -> None:
        original, revised, consolidations = self._moved_into_aurora_mysql()
        original.append({"query_id": "d1", "assigned_engine": "documentdb"})
        revised.append({"query_id": "d1", "assigned_engine": "documentdb"})
        ctx = _context({"q1": {"aurora_mysql": 70, "dynamodb": 30, "documentdb": 55}})

        updated, records, decisions, _ = apply_consolidation_flags(
            [_flag(["q1"], "aurora_mysql")],
            revised,
            consolidations,
            original,
            ctx,
            _query_map("q1", "q2", "q3", "d1"),
            {"aurora_mysql", "dynamodb", "documentdb"},
        )

        assert {qa["query_id"]: qa["assigned_engine"] for qa in updated}["q1"] == "documentdb"
        assert decisions[0]["destination_engine"] == "documentdb"
        # It left the DynamoDB -> Aurora MySQL move, but not back to DynamoDB.
        assert records[0]["query_count"] == 1
        assert records[0]["queries_retained"] == []

    def test_a_flag_with_nowhere_else_to_go_stays_with_an_honest_reason(self) -> None:
        original, revised, consolidations = self._moved_into_aurora_mysql()
        # DynamoDB hard-excludes nothing here, so gate it by capability instead.
        ctx = ScoringContext(
            analysis_outputs=_context({"q1": {"aurora_mysql": 70}}).analysis_outputs,
            query_capabilities={"q1": ["complex_joins"]},
        )

        updated, records, decisions, gaps = apply_consolidation_flags(
            [_flag(["q1"], "aurora_mysql")],
            revised,
            consolidations,
            original,
            ctx,
            _query_map("q1", "q2", "q3"),
            {"aurora_mysql", "dynamodb"},
        )

        assert {qa["query_id"]: qa["assigned_engine"] for qa in updated}["q1"] == "aurora_mysql"
        (decision,) = decisions
        assert decision["outcome"] == "kept"
        assert decision["destination_engine"] is None
        assert "stays on Aurora MySQL" in decision["reason"]
        assert records == consolidations
        assert gaps == {"q1": {"aurora_mysql"}}

    def test_a_flag_on_a_query_no_consolidation_moved_is_ignored(self) -> None:
        original, revised, consolidations = self._moved_into_aurora_mysql()
        ctx = _context({"q3": {"aurora_mysql": 70, "dynamodb": 90}})

        updated, records, decisions, gaps = apply_consolidation_flags(
            [_flag(["q3"], "aurora_mysql"), _flag(["q1"], "dynamodb")],
            revised,
            consolidations,
            original,
            ctx,
            _query_map("q1", "q2", "q3"),
            {"aurora_mysql", "dynamodb"},
        )

        assert {qa["assigned_engine"] for qa in updated} == {"aurora_mysql"}
        assert [d["outcome"] for d in decisions] == ["ignored", "ignored"]
        assert gaps == {}
        assert records == consolidations

    def test_a_co_dependency_group_moves_together(self) -> None:
        original, revised, consolidations = self._moved_into_aurora_mysql()
        ctx = _context(
            {"q1": {"aurora_mysql": 70, "dynamodb": 60}, "q2": {"aurora_mysql": 70, "dynamodb": 60}}
        )

        updated, records, decisions, gaps = apply_consolidation_flags(
            [_flag(["q1"], "aurora_mysql")],
            revised,
            consolidations,
            original,
            ctx,
            _query_map("q1", "q2", "q3"),
            {"aurora_mysql", "dynamodb"},
            co_dependency_groups=[["q1", "q2"]],
        )

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in updated}
        assert engine_of["q1"] == engine_of["q2"] == "dynamodb"
        (decision,) = decisions
        assert decision["query_ids"] == ["q1"]
        assert decision["moved_with_group"] == ["q2"]
        assert gaps == {"q1": {"aurora_mysql"}}
        assert records == []  # the whole move went back


class TestTheSweepKeepsHonoringAFlag:
    def test_the_sweep_never_moves_a_flagged_query_back_onto_its_flagged_engine(self) -> None:
        """o1 sits on a tiny orphan engine after its flag; DynamoDB (the flagged
        engine) is the only other committed engine, so the sweep keeps the orphan."""
        assignments = [{"query_id": "o1", "assigned_engine": "opensearch"}] + [
            {"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)
        ]
        ctx = _context({"o1": {"dynamodb": 90, "opensearch": 10}})
        queries = _query_map("o1", *[f"y{i}" for i in range(6)])

        unflagged, _, _ = sanity_sweep([dict(a) for a in assignments], [], ctx, queries)
        flagged, _, decisions = sanity_sweep(
            [dict(a) for a in assignments], [], ctx.with_flag_gaps({"o1": ["dynamodb"]}), queries
        )

        assert {qa["query_id"]: qa["assigned_engine"] for qa in unflagged}["o1"] == "dynamodb"
        assert {qa["query_id"]: qa["assigned_engine"] for qa in flagged}["o1"] == "opensearch"
        assert decisions[0]["decision"] == "kept"
        assert "flagged as unable" in decisions[0]["reason"]


class TestParseModelFlags:
    def test_valid_entries_parse_and_invalid_ones_are_rejected_whole(self) -> None:
        flags, rejected = parse_model_flags(
            [
                {
                    "query_ids": ["q1"],
                    "flagged_engine": "dynamodb",
                    "reason_category": "full_scan",
                    "explanation": "no partition key",
                },
                {"query_id": "q2", "original_engine": "aurora_mysql", "reason": "joins"},
                "not an object",
            ]
        )
        assert [f.query_ids for f in flags] == [["q1"]]
        assert len(rejected) == 2
