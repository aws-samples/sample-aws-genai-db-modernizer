"""#786: one fallback rule for a query no analyzed engine can serve.

The first assignment's Aurora fallback (``AssignmentResolver``, Step 7) and the
cache owner fallback (``cache_overlay.owner_decision``) used to disagree when
no Aurora engine was in play: the resolver used the generic ``aurora``
placeholder, the cache path the source-compatible Aurora engine (or
``aurora_postgresql`` for any other source). Both now use
``aurora_choice.fallback_aurora_engine``: the source-compatible Aurora engine
when the source has one (ADR-031 Decision 2 step 1), else the generic
placeholder. The fallback Moves record the fallback rule.
"""

from __future__ import annotations

import pytest

from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.aurora_choice import fallback_aurora_engine
from src.agents.referee.cache_overlay import (
    fallback_owner,
    no_serviceable_candidate_reason,
    normalize_cache_owners,
    owner_decision,
)
from src.agents.referee.move_ledger import ledger_for, replay
from src.contracts.move_ledger import MovePass, ReasonCode

SHOW_TABLES = "SHOW TABLES"


def _query(qid: str = "q1", text: str = SHOW_TABLES) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": "SELECT",
        "tables_accessed": ["db.users"],
        "join_count": 0,
        "has_joins": False,
        "has_aggregations": False,
        "filter_tables": [],
        "calls_per_second": 1.0,
        "rows_returned_avg": 10,
    }


def _collector(queries: list[dict], source_engine: str) -> dict:
    return {
        "job_id": "t",
        "metadata": {"source_database": {"engine": source_engine}},
        "database_schema": {"tables": [{"table_id": "db.users", "table_name": "users"}]},
        "queries": {"query_patterns": queries},
    }


def _triage(engines: list[str]) -> dict:
    return {"selected_agents": [{"agent_type": e} for e in engines], "signals": []}


def _analysis(confidence: int = 95) -> dict:
    return {
        "table_recommendations": [{"table_id": "db.users", "confidence_score": confidence}],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


class TestTheRule:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("mysql", "aurora_mysql"),
            ("MySQL", "aurora_mysql"),
            ("mariadb", "aurora_mysql"),
            ("postgresql", "aurora_postgresql"),
            ("postgres", "aurora_postgresql"),
            # Neither Aurora engine is source-compatible for a heterogeneous
            # source (ADR-031 Decision 6), and an unknown source has none.
            ("sqlserver", "aurora"),
            ("oracle", "aurora"),
            ("", "aurora"),
            (None, "aurora"),
        ],
    )
    def test_source_compatible_aurora_else_the_generic_placeholder(self, source, expected):
        assert fallback_aurora_engine(source) == expected


class TestBothPathsAgree:
    """The issue's repro: a MySQL-source ``SHOW TABLES``, every analyzed engine
    gated, and no Aurora engine in the candidate set."""

    ENGINES = ["dynamodb", "documentdb"]

    def _resolve(self, source: str):
        query = _query()
        collector = _collector([query], source)
        analysis = {e: _analysis() for e in self.ENGINES}
        result = AssignmentResolver().resolve(_triage(self.ENGINES), analysis, collector)
        qa = next(qa for qa in result.query_assignments if qa.query_id == "q1")
        decision = owner_decision(
            query,
            analysis,
            source,
            triage=_triage(self.ENGINES),
            collector=collector,
        )
        return result, qa, decision

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("mysql", "aurora_mysql"), ("postgresql", "aurora_postgresql"), ("sqlserver", "aurora")],
    )
    def test_the_first_assignment_and_the_cache_owner_fallback_pick_the_same_engine(
        self, source, expected
    ):
        result, qa, decision = self._resolve(source)

        assert qa.assigned_engine == expected
        assert decision.owner == expected
        assert decision.fallback_reason == no_serviceable_candidate_reason(expected, source)

    def test_the_first_assignment_fallback_move_records_the_rule(self):
        result, _, _ = self._resolve("mysql")

        [move] = [m for m in result.moves if m.query_id == "q1"]
        assert move.pass_ is MovePass.AURORA_FALLBACK
        assert move.reason_code is ReasonCode.FALLBACK_NO_SCORE
        assert move.to_engine == "aurora_mysql"
        assert move.score_after is None
        assert {c.engine: c.gate for c in move.evidence.candidates} == {
            "documentdb": "capability",
            "dynamodb": "capability",
        }

    def test_no_owner_candidate_at_all_uses_the_same_rule(self):
        for source, expected in (("mysql", "aurora_mysql"), ("", "aurora")):
            owner, tied, reason = fallback_owner(None, [], source, triage=None, collector=None)
            assert (owner, tied) == (expected, [])
            assert reason == no_serviceable_candidate_reason(expected, source)


class TestCacheOwnerFallbackMove:
    """A legacy cache-owned query whose every owner candidate is gated: the
    cache owner normalisation's Move records the fallback rule, the same
    reason codes the first assignment's fallback uses."""

    def _assignment(self) -> dict:
        return {
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "elasticache", "assignment_reason": "old"}
            ]
        }

    def test_all_gated_records_fallback_no_score(self):
        assignment = self._assignment()
        start = {"q1": ("elasticache", [])}
        ledger = ledger_for(assignment["query_assignments"])

        normalize_cache_owners(
            assignment,
            [_query()],
            {"dynamodb": _analysis(), "documentdb": _analysis()},
            "mysql",
            triage=_triage(["dynamodb", "documentdb"]),
            collector=_collector([_query()], "mysql"),
            ledger=ledger,
        )

        [move] = ledger.moves
        assert move.pass_ is MovePass.CACHE_OWNER_NORMALIZE
        assert move.reason_code is ReasonCode.FALLBACK_NO_SCORE
        assert (move.from_engine, move.to_engine) == ("elasticache", "aurora_mysql")
        assert move.score_after is None
        assert replay(ledger.moves, start=start)["q1"] == ("aurora_mysql", [])

    def test_no_owner_candidate_records_fallback_no_owner(self):
        assignment = self._assignment()
        ledger = ledger_for(assignment["query_assignments"])

        normalize_cache_owners(
            assignment, [_query()], [], "postgresql", triage=None, collector=None, ledger=ledger
        )

        [move] = ledger.moves
        assert move.reason_code is ReasonCode.FALLBACK_NO_OWNER
        assert move.to_engine == "aurora_postgresql"

    def test_a_scored_owner_keeps_the_cache_legacy_owner_rule(self):
        assignment = self._assignment()
        ledger = ledger_for(assignment["query_assignments"])

        normalize_cache_owners(
            assignment,
            [_query(text="SELECT * FROM users WHERE id = ?")],
            {"dynamodb": _analysis()},
            "mysql",
            triage=None,
            collector=None,
            ledger=ledger,
        )

        [move] = ledger.moves
        assert move.reason_code is ReasonCode.CACHE_LEGACY_OWNER
        assert move.to_engine == "dynamodb"


class TestFallbackSentence:
    """#816 review: the customer-facing fallback sentence says the query moves
    (it is leaving the cache), and claims the engine runs the source SQL as is
    only when it is the source-compatible engine."""

    @pytest.mark.parametrize(
        ("source", "engine", "display"),
        [
            ("mysql", "aurora_mysql", "Aurora MySQL"),
            ("postgresql", "aurora_postgresql", "Aurora PostgreSQL"),
        ],
    )
    def test_source_compatible_says_it_runs_the_source_sql(self, source, engine, display):
        owner, _, reason = fallback_owner(None, [], source, triage=None, collector=None)
        assert owner == engine
        assert reason == (
            f"No analyzed engine can serve this query; it moves to {display}, which runs "
            "the source SQL as is."
        )

    @pytest.mark.parametrize("source", ["sqlserver", "oracle", "db2", ""])
    def test_placeholder_or_heterogeneous_makes_no_source_sql_claim(self, source):
        owner, _, reason = fallback_owner(None, [], source, triage=None, collector=None)
        assert owner == "aurora"
        assert reason == (
            "No analyzed engine can serve this query; it moves to Aurora, the relational "
            "fallback."
        )
        assert "source SQL" not in reason
        assert "stays on" not in reason
