"""The post-schema router treats a query's unresolved tie as the reality check does (#811).

ADR-031 Decision 12 makes the tie, not the placeholder's pick, the fact the
customer sees; Decision 11 Amendment 2 lists every move made after approval so
the customer can still settle it. A query flagged on its placeholder pick is
re-routed among its other tied engines first (``_tied_candidate_pool``), by the
shared scorer, and keeps the tie minus the flagged engine when the scorer
leaves it unresolved. Each case also runs the reality check's model-flag path
(``apply_consolidation_flags``) on the same input and checks both agree.

No test calls a model: the flags are written by hand.
"""

from __future__ import annotations

from collections.abc import Mapping

from src.agents.referee.model_flags import apply_consolidation_flags
from src.agents.referee.move_ledger import ledger_for
from src.agents.referee.post_schema_router import route_unsupported_queries
from src.agents.referee.query_scoring import ScoringContext
from src.contracts.model_flag import ModelFlag

ENGINES = ["aurora_mysql", "aurora_postgresql", "documentdb", "dynamodb"]


def _context(scores: Mapping[str, Mapping[str, float]]) -> ScoringContext:
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


def _flag(qids: list[str], engine: str) -> dict:
    return {
        "query_ids": qids,
        "flagged_engine": engine,
        "reason_category": "full_scan",
        "explanation": "no key to query",
    }


def _route(
    scores: Mapping[str, Mapping[str, float]],
    tied: list[str],
    *,
    engines: list[str] = ENGINES,
    groups: list[list[str]] | None = None,
    on: str = "dynamodb",
):
    """The post-schema router's result for q1 flagged on ``on``."""
    qids = sorted({q for g in groups or [] for q in g} | {"q1"})
    return route_unsupported_queries(
        schema_outputs={on: {}},
        active_engines=engines,
        routing_flags_by_engine={on: [_flag(["q1"], on)]},
        scoring_context=_context(scores),
        query_map=_query_map(*qids),
        assigned_engines={q: on for q in qids},
        tied_engines={"q1": tied} if tied else {},
        co_dependency_groups=groups,
    )


def _reality_check(
    scores: Mapping[str, Mapping[str, float]],
    tied: list[str],
    *,
    engines: list[str] = ENGINES,
    on: str = "dynamodb",
) -> dict:
    """The reality check's model-flag path for the same q1: (engine, tie)."""
    revised = [{"query_id": "q1", "assigned_engine": on, "tied_engines": list(tied)}]
    original = [{"query_id": "q1", "assigned_engine": "opensearch"}]
    consolidation = {
        "from_engine": "opensearch",
        "to_engine": on,
        "query_count": 1,
        "reason": "no unique value",
        "saved_cost_estimate": 0,
        "action": "full",
        "queries_retained": [],
        "retention_reason": None,
    }
    updated, _, _, _ = apply_consolidation_flags(
        [ModelFlag.model_validate(_flag(["q1"], on))],
        revised,
        [consolidation],
        original,
        _context(scores),
        _query_map("q1"),
        set(engines),
        ledger=ledger_for(revised),
    )
    (qa,) = updated
    return {"engine": qa["assigned_engine"], "tied": sorted(qa.get("tied_engines") or [])}


def _routed(output) -> dict:
    (routing,) = [r for r in output.routings if r.query_id == "q1"]
    return {"engine": routing.to_engine, "tied": sorted(routing.tied_engines)}


class TestTheTiedEnginesComeFirst:
    def test_a_tied_engine_wins_over_a_higher_scoring_untied_one(self) -> None:
        """q1 is tied between DynamoDB and DocumentDB at 70; Aurora MySQL scores
        80 but was never tied. Flagged off DynamoDB, q1 goes to DocumentDB, the
        one tied engine left, and the tie is settled (one engine is no tie)."""
        scores = {
            "q1": {"dynamodb": 70, "documentdb": 70, "aurora_mysql": 80, "aurora_postgresql": 10}
        }
        tied = ["documentdb", "dynamodb"]

        routed = _routed(_route(scores, tied))

        assert routed == {"engine": "documentdb", "tied": []}
        assert routed == _reality_check(scores, tied)

    def test_the_tie_is_kept_minus_the_flagged_engine(self) -> None:
        """A three-way tie loses the flagged engine and stays unresolved between
        the two that are left; the destination is the shared placeholder pick."""
        scores = {
            "q1": {"dynamodb": 70, "documentdb": 70, "aurora_postgresql": 70, "aurora_mysql": 90}
        }
        tied = ["aurora_postgresql", "documentdb", "dynamodb"]

        routed = _routed(_route(scores, tied))

        assert routed["tied"] == ["aurora_postgresql", "documentdb"]
        assert routed["engine"] in {"documentdb", "aurora_postgresql"}
        assert routed == _reality_check(scores, tied)

    def test_no_tied_engine_left_opens_every_remaining_engine(self) -> None:
        """The "survivors or everything" fallback: the only other tied engine
        is no longer kept, so the best remaining engine wins."""
        scores = {
            "q1": {"dynamodb": 70, "documentdb": 70, "aurora_mysql": 80, "aurora_postgresql": 10}
        }
        tied = ["documentdb", "dynamodb"]
        kept = ["aurora_mysql", "aurora_postgresql", "dynamodb"]

        routed = _routed(_route(scores, tied, engines=kept))

        assert routed == {"engine": "aurora_mysql", "tied": []}
        assert routed == _reality_check(scores, tied, engines=kept)

    def test_an_untied_query_can_end_on_a_fresh_tie(self) -> None:
        """No tie before: the scorer's exact tie among the remaining engines is
        kept unresolved, as the reality check keeps it."""
        scores = {
            "q1": {"dynamodb": 90, "documentdb": 70, "aurora_postgresql": 70, "aurora_mysql": 10}
        }

        routed = _routed(_route(scores, []))

        assert routed["tied"] == ["aurora_postgresql", "documentdb"]
        assert routed == _reality_check(scores, [])

    def test_a_group_routes_on_its_members_tie(self) -> None:
        """A co-dependency group uses the tie a member carries (the reality
        check's rule), and every member records the same tie."""
        scores = {
            "q1": {"dynamodb": 70, "documentdb": 70, "aurora_mysql": 80, "aurora_postgresql": 10},
            "q2": {"dynamodb": 70, "documentdb": 70, "aurora_mysql": 80, "aurora_postgresql": 10},
        }
        output = _route(scores, ["documentdb", "dynamodb"], groups=[["q1", "q2"]])

        assert {(r.query_id, r.to_engine, tuple(r.tied_engines)) for r in output.routings} == {
            ("q1", "documentdb", ()),
            ("q2", "documentdb", ()),
        }
