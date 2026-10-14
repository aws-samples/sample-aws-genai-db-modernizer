"""ADR-031 Decision 11 guard: a model's flag is evidence; the scorer decides (#647).

A model may flag queries an engine cannot serve (``ModelFlag``). It can never
name or force a destination: the flagged engine is gated for those queries and
the shared scorer (``score_query_for_engine`` + ``tie_break.break_tie``) picks
the best remaining engine. These tests fail if model output can steer a query
anywhere the scorer would not send it.
"""

from __future__ import annotations

import random
from typing import get_args

import pytest
from pydantic import ValidationError

from src.agents.referee.model_flags import apply_consolidation_flags, route_flagged_unit
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.agents.referee.reality_check_handler import apply_reality_check_llm_output
from src.agents.referee.tie_break import break_tie
from src.contracts.model_flag import FlaggableEngine, FlagReason, ModelFlag
from src.shared.engine_names import ENGINE_DISPLAY_NAMES

# Engines a plain SELECT may land on with no specialty signal (OpenSearch only
# absorbs search/aggregation queries, ElastiCache owns nothing).
OWNABLE = ["aurora_mysql", "aurora_postgresql", "documentdb", "dynamodb"]


def _query(qid: str) -> dict:
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM t_{qid} WHERE id = ?",
        "query_type": "SELECT",
        "tables_accessed": [f"t_{qid}"],
    }


def _context(scores: dict[str, dict[str, int]]) -> ScoringContext:
    analysis: dict[str, dict] = {}
    for qid, per_engine in scores.items():
        for engine, score in per_engine.items():
            analysis.setdefault(engine, {"table_recommendations": []})[
                "table_recommendations"
            ].append({"table_id": f"t_{qid}", "confidence_score": score})
    return ScoringContext(analysis_outputs=analysis)


def _flag(qid: str, engine: str, *, category: str = "other", explanation: str = "gap") -> dict:
    return {
        "query_ids": [qid],
        "flagged_engine": engine,
        "reason_category": category,
        "explanation": explanation,
    }


def _oracle(query: dict, flagged: str, ctx: ScoringContext, source_engine: str) -> str:
    """The scorer's own answer, computed independently of ``model_flags``."""
    scores = {e: score_query_for_engine(query, e, ctx).score for e in OWNABLE if e != flagged}
    top = max(s for s in scores.values() if s is not None)
    tied = sorted(e for e, s in scores.items() if s == top)
    if len(tied) == 1:
        return tied[0]
    source_compatible = {"mysql": "aurora_mysql", "postgresql": "aurora_postgresql"}.get(
        source_engine
    )
    winner, _ = break_tie(
        tied, set(), source_compatible, settled_counts={}, query_id=query["query_id"]
    )
    return winner


class TestFlagDestinationIsTheScorersChoice:
    @pytest.mark.parametrize("seed", range(40))
    def test_every_flag_lands_where_the_scorer_says(self, seed: int) -> None:
        rng = random.Random(seed)
        qid = f"q{seed}"
        # Coarse scores so exact ties (and the tie-break) show up often.
        scores = {qid: {e: rng.choice([20, 40, 60, 80]) for e in OWNABLE}}
        ctx = _context(scores)
        flagged = rng.choice(OWNABLE)
        source_engine = rng.choice(["mysql", "postgresql", "oracle"])

        routing = route_flagged_unit(
            [qid],
            flagged,
            OWNABLE,
            ctx.with_flag_gaps({qid: [flagged]}),
            {qid: _query(qid)},
            source_engine=source_engine,
        )

        assert routing.destination == _oracle(_query(qid), flagged, ctx, source_engine)
        assert routing.destination != flagged


class TestModelOutputCannotNameOrForceAnEngine:
    @pytest.mark.parametrize(
        "key",
        ["target_engine", "to_engine", "destination", "destination_engine", "original_engine"],
    )
    def test_a_flag_that_names_a_destination_is_rejected(self, key: str) -> None:
        with pytest.raises(ValidationError):
            ModelFlag.model_validate({**_flag("q1", "dynamodb"), key: "documentdb"})

    def test_an_unknown_engine_or_category_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelFlag.model_validate(_flag("q1", "some_engine"))
        with pytest.raises(ValidationError):
            ModelFlag.model_validate(_flag("q1", "dynamodb", category="move_to_documentdb"))

    def test_the_contract_lists_exactly_the_modeled_engines(self) -> None:
        modeled = set(ENGINE_DISPLAY_NAMES) - {"aurora", "neptune", "keyspaces"}
        assert set(get_args(FlaggableEngine)) == modeled

    # Every way a flag's text could name an engine other than the scorer's
    # pick: raw ids, display names, and "route to"/"target_engine=" phrasing
    # (#712 review: a mutation that routes to a raw id found in the explanation
    # passed the display-name-only probes).
    SMUGGLED = [
        text
        for engine in ("documentdb", "aurora_postgresql")
        for text in (
            engine,
            f"route to {engine}",
            f"target_engine={engine}",
            ENGINE_DISPLAY_NAMES[engine],
            f"move this to {ENGINE_DISPLAY_NAMES[engine]}",
        )
    ]

    @pytest.mark.parametrize("explanation", ["gap", *SMUGGLED])
    @pytest.mark.parametrize("category", [c.value for c in FlagReason])
    def test_neither_the_explanation_nor_the_category_changes_the_destination(
        self, explanation: str, category: str
    ) -> None:
        ctx = _context(
            {"q1": {"aurora_mysql": 70, "documentdb": 60, "dynamodb": 65, "aurora_postgresql": 50}}
        )
        flag = ModelFlag.model_validate(
            _flag("q1", "aurora_mysql", category=category, explanation=explanation)
        )
        updated, _, decisions, _ = apply_consolidation_flags(
            [flag],
            [{"query_id": "q1", "assigned_engine": "aurora_mysql"}],
            [
                {
                    "from_engine": "dynamodb",
                    "to_engine": "aurora_mysql",
                    "query_count": 1,
                    "reason": "r",
                    "saved_cost_estimate": 0,
                }
            ],
            [{"query_id": "q1", "assigned_engine": "dynamodb"}],
            ctx,
            {"q1": _query("q1")},
            {"aurora_mysql", "aurora_postgresql", "documentdb", "dynamodb"},
        )
        assert updated[0]["assigned_engine"] == "dynamodb"
        assert decisions[0]["destination_engine"] == "dynamodb"

    @pytest.mark.parametrize("explanation", SMUGGLED)
    def test_an_engine_named_in_a_response_explanation_is_never_the_destination(
        self, explanation: str
    ) -> None:
        """End to end through the response path: DocumentDB scores q1 highest
        but is outside this check's architecture, so neither its raw id nor
        its name in the text can bring it in."""
        det = self._det()
        flag = _flag("q1", "aurora_mysql", explanation=explanation)

        result = apply_reality_check_llm_output(det, {"consolidation_corrections": [flag]})

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["q1"] == "dynamodb"

    @staticmethod
    def _det() -> dict:
        """q1 moved DynamoDB -> Aurora MySQL by the reality check; d1..d6 stay on DynamoDB."""
        dynamodb_ids = [f"d{i}" for i in range(6)]
        original = [{"query_id": "q1", "assigned_engine": "dynamodb"}] + [
            {"query_id": q, "assigned_engine": "dynamodb"} for q in dynamodb_ids
        ]
        revised = [
            {"query_id": "q1", "assigned_engine": "aurora_mysql", "assignment_reason": "moved"}
        ] + [{"query_id": q, "assigned_engine": "dynamodb"} for q in dynamodb_ids]
        return {
            "assignment": {"query_assignments": original, "co_dependency_groups": []},
            "revised_assignments": revised,
            "consolidations": [
                {
                    "from_engine": "dynamodb",
                    "to_engine": "aurora_mysql",
                    "query_count": 1,
                    "reason": "r",
                    "saved_cost_estimate": 0,
                    "action": "partial",
                    "queries_retained": [],
                    "retention_reason": None,
                }
            ],
            "before_distribution": {"dynamodb": 7},
            "after_distribution": {"dynamodb": 6, "aurora_mysql": 1},
            "triage": {"signals": [], "query_capabilities": {}},
            "collector_output": {
                "queries": {"query_patterns": [_query("q1")] + [_query(q) for q in dynamodb_ids]}
            },
            "analysis_outputs": _context(
                {"q1": {"aurora_mysql": 80, "dynamodb": 60, "documentdb": 99}}
            ).analysis_outputs,
            "architectural_patterns": [],
            "recommendations": [],
            "unique_value_assessment": {},
        }

    def test_a_response_entry_naming_a_destination_changes_nothing(self) -> None:
        det = self._det()
        entry = {**_flag("q1", "aurora_mysql"), "target_engine": "documentdb"}

        result = apply_reality_check_llm_output(det, {"consolidation_corrections": [entry]})

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["q1"] == "aurora_mysql"
        assert result["model_flags_rejected"] == 1
        assert not result.get("flag_decisions")

    def test_the_legacy_correction_shape_changes_nothing(self) -> None:
        det = self._det()
        legacy = {"query_id": "q1", "original_engine": "dynamodb", "reason": "joins"}

        result = apply_reality_check_llm_output(det, {"consolidation_corrections": [legacy]})

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["q1"] == "aurora_mysql"
        assert result["model_flags_rejected"] == 1

    def test_a_flag_goes_to_the_scorers_engine_even_one_the_architecture_dropped(self) -> None:
        """DocumentDB scores q1 highest but is not in this check's architecture
        (neither before nor after): a flag cannot add it. DynamoDB is the best
        remaining engine."""
        det = self._det()

        result = apply_reality_check_llm_output(
            det, {"consolidation_corrections": [_flag("q1", "aurora_mysql")]}
        )

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["q1"] == "dynamodb"
        (decision,) = result["flag_decisions"]
        assert decision["destination_engine"] == "dynamodb"
        assert (decision["score_before"], decision["score_after"]) == (80, 60)

    def test_a_flag_on_an_engine_the_query_is_not_on_moves_nothing(self) -> None:
        det = self._det()

        result = apply_reality_check_llm_output(
            det, {"consolidation_corrections": [_flag("q1", "dynamodb")]}
        )

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine_of["q1"] == "aurora_mysql"
        assert [d["outcome"] for d in result["flag_decisions"]] == ["ignored"]

    def test_no_flag_means_no_change(self) -> None:
        det = self._det()
        before = [dict(qa) for qa in det["revised_assignments"]]

        result = apply_reality_check_llm_output(det, {"consolidation_corrections": []})

        assert result["revised_assignments"] == before
