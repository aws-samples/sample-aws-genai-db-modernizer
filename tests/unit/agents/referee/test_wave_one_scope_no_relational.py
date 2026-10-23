"""Wave 1 when the customer's engine scope has no relational engine (#1369).

A scope that leaves out every Aurora engine (or whose Aurora choice serves no query)
plans no conversion to Aurora. Wave 1 then says so, and its gate names what the plan does
instead; it never calls a homogeneous source "heterogeneous", never asks to pick an
Aurora engine, and never names an engine the scope leaves out. A source with no Aurora
dialect of its own and no scope keeps the heterogeneous wording.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.migration_waves import build_migration_waves

SCOPE_CHOICE = {
    "source_engine": "mysql",
    "engine": "aurora_postgresql",
    "basis": "engine_scope",
    "reason": "The customer's engine scope leaves out Aurora MySQL, the source-compatible engine",
}


def _wave_one(
    scope: list[str] | None,
    source: str = "mysql",
    choice: dict[str, Any] | None = None,
    ranking: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ranking = ranking or [{"target": "dynamodb", "assigned_queries": 1, "workload_percent": 100.0}]
    waves = build_migration_waves(
        ranking=ranking,
        table_assignments=[
            {"table_id": "app.orders", "primary_engine": "dynamodb", "query_count": 1}
        ],
        query_assignments=[
            {"query_id": "r1", "assigned_engine": "dynamodb", "source_tables": ["app.orders"]}
        ],
        co_dependency_groups=[],
        cache_overlay=None,
        source_engine=source,
        source_version="8.0.45",
        known_tables={"app.orders"},
        engine_scope=scope,
        aurora_engine_choice=choice,
    )
    assert waves
    return waves[0]


def _text(wave: dict[str, Any]) -> str:
    return " ".join([wave["title"], wave["rationale"], wave["gate"]])


class TestAScopeWithNoRelationalEngine:
    def test_wave_one_says_the_scope_has_no_relational_engine(self) -> None:
        wave = _wave_one(["dynamodb"])
        assert wave["engines"] == []
        assert wave["title"] == "No move to Aurora: the engine scope has no relational engine"
        assert "engine scope has no relational engine" in wave["rationale"]
        assert "not converted to Aurora in this plan" in wave["rationale"]

    def test_it_never_calls_a_homogeneous_source_heterogeneous(self) -> None:
        wave = _wave_one(["dynamodb"])
        assert "heterogeneous" not in _text(wave).lower()

    def test_it_never_asks_to_pick_an_aurora_engine(self) -> None:
        text = _text(_wave_one(["dynamodb"]))
        assert "pick and validate" not in text
        assert "Risk: no Aurora-compatible" not in text

    @pytest.mark.passes_on_main  # behaviour the fix must not change
    def test_it_names_no_engine_the_scope_leaves_out(self) -> None:
        text = _text(_wave_one(["dynamodb"]))
        assert "Aurora MySQL" not in text and "Aurora PostgreSQL" not in text

    def test_the_gate_names_what_the_plan_does_instead(self) -> None:
        gate = _wave_one(["dynamodb"])["gate"]
        assert "DynamoDB" in gate
        assert "instead" in gate
        # Partial coverage: queries no engine in the scope serves stay on the source.
        assert "stays on the source database" in gate
        assert "the plan moves the queries to" not in gate

    def test_it_names_every_engine_the_plan_moves_to(self) -> None:
        ranking = [
            {"target": "dynamodb", "assigned_queries": 3, "workload_percent": 75.0},
            {"target": "opensearch", "assigned_queries": 1, "workload_percent": 25.0},
        ]
        gate = _wave_one(["dynamodb", "opensearch"], ranking=ranking)["gate"]
        assert "DynamoDB and OpenSearch" in gate

    @pytest.mark.passes_on_main  # behaviour the fix must not change
    def test_the_wave_stays_wave_one_with_no_figures(self) -> None:
        wave = _wave_one(["dynamodb"])
        assert wave["wave"] == 1
        assert wave["moves_from"] == ["mysql"]
        assert wave["query_count"] == 0 and wave["workload_share_percent"] == 0.0

    def test_a_postgresql_source_reads_the_same_way(self) -> None:
        wave = _wave_one(["dynamodb"], source="postgresql")
        assert "PostgreSQL" in wave["rationale"]
        assert "heterogeneous" not in _text(wave).lower()
        assert "Aurora PostgreSQL" not in _text(wave)


class TestAScopeChoiceThatServesNoQuery:
    """#1327: the scope's Aurora engine is in scope but no query ends on it."""

    SCOPE = ["aurora_postgresql", "dynamodb"]

    def test_wave_one_says_the_choice_serves_no_query(self) -> None:
        ranking = [
            {"target": "aurora_postgresql", "assigned_queries": 0, "workload_percent": 0.0},
            {"target": "dynamodb", "assigned_queries": 1, "workload_percent": 100.0},
        ]
        wave = _wave_one(self.SCOPE, choice=SCOPE_CHOICE, ranking=ranking)
        assert wave["engines"] == []
        assert wave["title"] == "No move to Aurora: the scope's Aurora engine serves no query"
        assert "serves no query" in wave["rationale"]
        assert "not converted to Aurora in this plan" in wave["rationale"]
        assert "DynamoDB" in wave["gate"] and "instead" in wave["gate"]

    def test_it_is_not_called_heterogeneous_or_asks_for_a_pick(self) -> None:
        ranking = [
            {"target": "aurora_postgresql", "assigned_queries": 0, "workload_percent": 0.0},
            {"target": "dynamodb", "assigned_queries": 1, "workload_percent": 100.0},
        ]
        text = _text(_wave_one(self.SCOPE, choice=SCOPE_CHOICE, ranking=ranking))
        assert "heterogeneous" not in text.lower()
        assert "pick and validate" not in text
        assert "Aurora MySQL" not in text

    def test_an_in_scope_aurora_engine_with_no_analysis_is_the_same_case(self) -> None:
        # The scope names Aurora PostgreSQL, but no analysis ran for it: no choice is
        # recorded and no query ends on it. Not a heterogeneous source to pick for.
        wave = _wave_one(self.SCOPE, choice=None)
        assert wave["title"] == "No move to Aurora: the scope's Aurora engine serves no query"
        text = _text(wave)
        assert "heterogeneous" not in text.lower()
        assert "pick and validate" not in text
        assert "Aurora MySQL" not in text


class TestWhatDoesNotChange:
    @pytest.mark.passes_on_main  # behaviour the fix must not change
    def test_with_no_scope_a_mysql_source_moves_to_aurora_mysql(self) -> None:
        wave = _wave_one(None)
        assert wave["engines"] == ["aurora_mysql"]
        assert wave["title"] == "Move to Aurora MySQL"
        assert wave["homogeneity"] == "homogeneous"

    @pytest.mark.passes_on_main  # behaviour the fix must not change
    def test_a_scope_with_the_source_compatible_engine_is_unchanged(self) -> None:
        wave = _wave_one(["aurora_mysql", "dynamodb"])
        assert wave["engines"] == ["aurora_mysql"]
        assert wave["title"] == "Move to Aurora MySQL"

    @pytest.mark.passes_on_main  # behaviour the fix must not change
    def test_a_scope_choice_that_serves_queries_is_a_cross_engine_move(self) -> None:
        ranking = [
            {"target": "aurora_postgresql", "assigned_queries": 1, "workload_percent": 100.0}
        ]
        wave = _wave_one(["aurora_postgresql", "dynamodb"], choice=SCOPE_CHOICE, ranking=ranking)
        assert wave["homogeneity"] == "cross_engine"
        assert wave["engines"] == ["aurora_postgresql"]

    @pytest.mark.passes_on_main  # behaviour the fix must not change
    def test_a_genuinely_heterogeneous_source_with_no_scope_keeps_its_wording(self) -> None:
        # SQL Server has no Aurora dialect; with no scope, no resolver choice and no query
        # on an Aurora engine, wave 1 names no target and asks for a pick, as before.
        wave = _wave_one(None, source="sqlserver")
        assert wave["title"] == "Move off SQL Server (heterogeneous)"
        assert wave["homogeneity"] == "heterogeneous"
        assert "no Aurora-compatible target engine" in wave["gate"]
        assert "pick and validate" in wave["gate"]

    @pytest.mark.passes_on_main  # behaviour the fix must not change
    def test_a_heterogeneous_source_with_aurora_in_scope_but_no_choice_is_unchanged(self) -> None:
        wave = _wave_one(["aurora_mysql", "aurora_postgresql", "dynamodb"], source="sqlserver")
        assert wave["title"] == "Move off SQL Server (heterogeneous)"
        assert "pick and validate" in wave["gate"]
