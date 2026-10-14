"""Guards for the sub-rules of ADR-031 Decision 7 (#648).

Decision 7 says more than "one scoring path": a mandatory-engine candidate wins
an exact score tie (and only that), every removed engine and moved query is
reported, and a saving is claimed only for an engine the first assignment
actually justified. One class per rule.
"""

from __future__ import annotations

from src.agents.referee.query_scoring import ScoringContext
from src.agents.referee.reality_check import resettle_after_corrections
from src.agents.referee.reality_check_floors import _find_best_absorber_for_query
from src.agents.referee.reality_check_patterns import _build_recommendations
from src.agents.referee.reality_check_place import _place_settled_queries
from src.agents.referee.reality_check_records import reconcile_consolidations

# --- the mandatory-engine tie bonus ------------------------------------------


def _absorb(
    scores: dict[str, int],
    *,
    mandatory: set[str],
    primary: str = "dynamodb",
    source_compatible: str | None = None,
) -> dict | None:
    """Ask the justification floor's absorber where one query from OpenSearch goes."""
    analysis = {
        engine: {"table_recommendations": [{"table_id": "t", "confidence_score": score}]}
        for engine, score in scores.items()
    }
    qa = {"query_id": "q1", "assigned_engine": "opensearch"}
    query_map = {"q1": {"query_id": "q1", "query_text": "SELECT 1", "tables_accessed": ["t"]}}
    return _find_best_absorber_for_query(
        qa=qa,
        committed_engines=set(scores),
        source_engine="opensearch",
        query_signals={},
        query_map=query_map,
        engine_queries={engine: [] for engine in scores},
        mandatory_committed_engines=mandatory,
        primary_engine=primary,
        ctx=ScoringContext(analysis_outputs=analysis),
        source_compatible_engine=source_compatible,
    )


class TestMandatorySecondaryBonus:
    def test_an_equal_score_goes_to_the_mandatory_secondary(self) -> None:
        for mandatory in ("aurora_mysql", "documentdb"):
            other = "documentdb" if mandatory == "aurora_mysql" else "aurora_mysql"
            result = _absorb({"aurora_mysql": 70, "documentdb": 70}, mandatory={mandatory})
            assert result is not None
            assert result["target_engine"] == mandatory, other
            assert result["tied_engines"] == []

    def test_the_bonus_never_beats_a_higher_score(self) -> None:
        result = _absorb({"aurora_mysql": 71, "documentdb": 70}, mandatory={"documentdb"})
        assert result is not None
        assert result["target_engine"] == "aurora_mysql"

    def test_the_primary_engine_is_not_a_secondary(self) -> None:
        """Mandatory for the primary engine is no bonus: the tie is still a tie."""
        result = _absorb(
            {"aurora_mysql": 70, "documentdb": 70},
            mandatory={"documentdb"},
            primary="documentdb",
            source_compatible="aurora_mysql",
        )
        assert result is not None
        assert result["target_engine"] == "aurora_mysql"
        assert "tie: source-compatible engine" in result["reason"]

    def test_a_tie_that_survives_the_bonus_goes_to_the_decision_2_tie_break(self) -> None:
        """Both mandatory, same score: the bonus decides nothing, the source-compatible
        engine wins as it does everywhere else."""
        both = {"aurora_mysql", "documentdb"}
        result = _absorb(
            {"aurora_mysql": 70, "documentdb": 70}, mandatory=both, source_compatible="documentdb"
        )
        assert result is not None
        assert result["target_engine"] == "documentdb"
        assert "tie: source-compatible engine" in result["reason"]

    def test_with_no_source_compatible_engine_the_surviving_tie_is_reported_unresolved(
        self,
    ) -> None:
        both = {"aurora_mysql", "documentdb"}
        result = _absorb({"aurora_mysql": 70, "documentdb": 70}, mandatory=both)
        assert result is not None
        assert result["tied_engines"] == ["aurora_mysql", "documentdb"]


# --- reporting every removed engine and moved query ------------------------------


def _resettle_documentdb_into_dynamodb() -> tuple[list[dict], list[dict]]:
    tables = (
        {f"a{i}": "db.main" for i in range(10)}
        | {f"k{i}": "db.kv" for i in range(10)}
        | {"d1": "db.docs", "d2": "db.docs"}
    )
    scores = {
        "aurora_postgresql": {"db.main": 90, "db.kv": 50, "db.docs": 60},
        "dynamodb": {"db.main": 30, "db.kv": 90, "db.docs": 70},
        "documentdb": {"db.main": 30, "db.kv": 40, "db.docs": 65},
    }
    analysis = {
        engine: {
            "table_recommendations": [
                {"table_id": t, "confidence_score": s} for t, s in per_table.items()
            ]
        }
        for engine, per_table in scores.items()
    }
    placement = {
        "aurora_postgresql": [f"a{i}" for i in range(10)],
        "dynamodb": [f"k{i}" for i in range(10)],
        "documentdb": ["d1", "d2"],
    }
    assignments = [
        {"query_id": q, "assigned_engine": e, "assignment_reason": "test"}
        for e, qids in placement.items()
        for q in qids
    ]
    collector = {
        "queries": {
            "query_patterns": [
                {"query_id": q, "tables_accessed": [t], "query_type": "SELECT"}
                for q, t in tables.items()
            ]
        }
    }
    return resettle_after_corrections(
        assignments, {}, analysis, collector, source_engine="postgresql"
    )


class TestRemovedEnginesAndMovedQueriesAreReported:
    def test_a_removed_engine_gets_one_record_that_counts_exactly_its_moved_queries(self) -> None:
        revised, records = _resettle_documentdb_into_dynamodb()
        moved = [q["query_id"] for q in revised if "consolidated from" in q["assignment_reason"]]
        assert sorted(moved) == ["d1", "d2"]
        assert [(c["from_engine"], c["to_engine"], c["query_count"]) for c in records] == [
            ("documentdb", "dynamodb", len(moved))
        ]

    def test_each_moved_query_states_where_it_went_and_the_score_there(self) -> None:
        revised, _ = _resettle_documentdb_into_dynamodb()
        for qa in revised:
            if qa["query_id"] in {"d1", "d2"}:
                assert "DocumentDB → DynamoDB" in qa["assignment_reason"]
                assert "DynamoDB scores 70.0" in qa["assignment_reason"]

    def test_each_moved_query_states_the_score_on_the_engine_it_left(self) -> None:
        revised, _ = _resettle_documentdb_into_dynamodb()
        for qa in revised:
            if qa["query_id"] in {"d1", "d2"}:
                assert "DocumentDB scores 65.0" in qa["assignment_reason"]
                assert "n/a" not in qa["assignment_reason"]

    def test_a_moved_co_dependency_group_states_the_group_score_on_both_engines(self) -> None:
        group = {"key": "g", "scores": {"documentdb": 65, "dynamodb": 70}}
        trace = {
            "g1": {"engines": {"documentdb": 65, "dynamodb": 70}, "group": group},
            "g2": {"engines": {"documentdb": 65, "dynamodb": 70}, "group": group},
        }
        revised, _ = _place_settled_queries(
            query_assignments=[
                {"query_id": q, "assigned_engine": "documentdb", "assignment_reason": "t"}
                for q in ("g1", "g2")
            ],
            committed_engines={"dynamodb"},
            mandatory_query_ids=set(),
            current_engine_by_qid={"g1": "documentdb", "g2": "documentdb"},
            query_signals={},
            query_map={},
            query_capabilities=None,
            trace_by_query=trace,
            source_compatible_engine=None,
            workload_justified={},
        )
        for qa in revised:
            reason = qa["assignment_reason"]
            assert "co-dependency group moves together" in reason
            assert "DocumentDB scores 65.0" in reason
            assert "DynamoDB scores 70.0" in reason


# --- savings only for an engine the first assignment justified --------------------


def _record(src: str, dst: str, *, justified: bool | None) -> dict:
    return {
        "from_engine": src,
        "to_engine": dst,
        "query_count": 2,
        "reason": "no unique value",
        "saved_cost_estimate": 450,
        "action": "full",
        "queries_retained": [],
        "retention_reason": None,
        "workload_justified": justified,
    }


class TestSavingsAreClaimedOnlyForAJustifiedEngine:
    @staticmethod
    def _qas(engine: str) -> list[dict]:
        return [{"query_id": f"{engine}-{i}", "assigned_engine": engine} for i in range(2)]

    def test_a_never_justified_engine_claims_no_saving_in_the_record_or_the_text(self) -> None:
        before = self._qas("opensearch")
        after = [{**qa, "assigned_engine": "aurora_postgresql"} for qa in before]
        records = reconcile_consolidations(
            before, after, [_record("opensearch", "aurora_postgresql", justified=False)]
        )
        assert records[0]["action"] == "full"
        assert records[0]["saved_cost_estimate"] == 0
        text = _build_recommendations([], records, [], {}, {"opensearch": 240.96})[0]
        assert "No saving is claimed" in text
        assert "$240.96" not in text

    def test_a_justified_engine_still_claims_its_saving(self) -> None:
        before = self._qas("opensearch")
        after = [{**qa, "assigned_engine": "aurora_postgresql"} for qa in before]
        records = reconcile_consolidations(
            before, after, [_record("opensearch", "aurora_postgresql", justified=True)]
        )
        assert records[0]["saved_cost_estimate"] == 450
        text = _build_recommendations([], records, [], {}, {"opensearch": 240.96})[0]
        assert "$240.96" in text

    def test_a_partial_move_claims_no_saving_either(self) -> None:
        before = self._qas("opensearch")
        after = [dict(before[0], assigned_engine="aurora_postgresql"), before[1]]
        records = reconcile_consolidations(
            before, after, [_record("opensearch", "aurora_postgresql", justified=True)]
        )
        assert records[0]["action"] == "partial"
        assert records[0]["saved_cost_estimate"] == 0
