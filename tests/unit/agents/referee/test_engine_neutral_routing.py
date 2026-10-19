"""#1081: the engine-specific routing branches read catalog data, not engine names.

Each test changes the catalog fact a branch reads (the module constant built from the
engine files) and checks the branch follows it, so the rule lives in the catalog and the
code names no engine. The shipped values are pinned too, so the move changes no routing.
"""

from __future__ import annotations

import pytest

from src.agents.referee import (
    aurora_choice,
    reality_check_floors,
    reality_check_patterns,
    reality_check_rules,
)
from src.agents.referee.aurora_choice import EXACT_TIE_RULE_REASON, choose_heterogeneous_engine
from src.agents.referee.reality_check_rules import _query_eligible_engines
from src.agents.referee.table_assignments import derive_table_assignments
from src.agents.referee.triage import BASELINE
from src.contracts.move_ledger import ReasonCode
from src.shared import catalog
from src.shared.cache_engines import CACHE_ENGINES, DEFAULT_CACHE_ENGINE

BOTH = ["aurora_mysql", "aurora_postgresql"]


class TestTheShippedCatalogKeepsTheRules:
    """The values the code used to name, now read from the catalog."""

    def test_the_heterogeneous_aurora_rules(self) -> None:
        assert catalog.exact_tie_engine() == "aurora_postgresql"
        assert aurora_choice.FEATURE_FAVOURS == {
            "sequences": "aurora_postgresql",
            "merge_statements": "aurora_postgresql",
            "json_usage": "aurora_postgresql",
            "multiple_schemas": "aurora_postgresql",
            "babelfish_compatible": "aurora_postgresql",
        }
        assert aurora_choice.PRIMARY_FEATURE_SIGNALS == (
            "sequences",
            "merge_statements",
            "json_usage",
            "multiple_schemas",
        )
        assert aurora_choice.DIALECT_FEATURE_SIGNALS == ("babelfish_compatible",)
        assert EXACT_TIE_RULE_REASON == (
            "tie between Aurora MySQL and Aurora PostgreSQL; "
            "PostgreSQL preferred on a tie by maintainer rule, ADR-031 Decision 6"
        )

    def test_every_reported_feature_has_its_catalog_direction(self) -> None:
        reported = aurora_choice.detect_heterogeneous_features({}, "sqlserver")
        assert set(reported) == set(aurora_choice.FEATURE_FAVOURS)

    def test_the_search_depth_floor(self) -> None:
        assert set(reality_check_floors.FLOOR_RULES) == {"opensearch"}
        assert reality_check_floors.search_floor_engines() == {"opensearch"}
        assert reality_check_floors._floor_reason_code("opensearch") is ReasonCode.FLOOR_OPENSEARCH

    def test_every_other_engine_records_the_cost_share_floor(self) -> None:
        for engine in catalog.engine_ids(status=catalog.ACTIVE) - {"opensearch"}:
            assert reality_check_floors._floor_reason_code(engine) is ReasonCode.FLOOR_COST_SHARE

    def test_the_join_block_engines(self) -> None:
        assert catalog.denormalized_join_engines() == {"dynamodb"}
        assert reality_check_rules.DENORMALIZED_JOIN_ENGINES == {"dynamodb"}

    def test_the_architectural_pattern_roles(self) -> None:
        assert reality_check_patterns.CQRS_READ_ENGINES == {"opensearch", "elasticache", "dynamodb"}
        assert reality_check_patterns.VIEW_ENGINES == {"opensearch"}
        assert reality_check_patterns.NO_EVENT_SYNC_PRIMARY_ENGINES == {"dynamodb"}

    def test_the_default_cache_engine_and_the_generic_relational_id(self) -> None:
        assert CACHE_ENGINES == {"elasticache"} and DEFAULT_CACHE_ENGINE == "elasticache"
        assert BASELINE == "aurora"


class TestTheAuroraChoiceFollowsTheCatalog:
    def _tie(self, monkeypatch, engine: str) -> None:
        monkeypatch.setattr(aurora_choice, "EXACT_TIE_ENGINE", engine)

    def _favour(self, monkeypatch, **favours: str) -> None:
        monkeypatch.setattr(
            aurora_choice, "FEATURE_FAVOURS", {**aurora_choice.FEATURE_FAVOURS, **favours}
        )

    def test_an_exact_tie_goes_to_the_catalogs_preferred_engine(self, monkeypatch) -> None:
        self._tie(monkeypatch, "aurora_mysql")
        winner, trace = choose_heterogeneous_engine(
            BOTH, {"aurora_mysql": 10.0, "aurora_postgresql": 10.0}, features={}
        )
        assert winner == "aurora_mysql"
        assert trace["deciding_features"] == [] and trace["margin"] == 0.0

    def test_a_feature_tips_toward_the_engine_its_catalog_entry_favours(self, monkeypatch) -> None:
        self._favour(monkeypatch, sequences="aurora_mysql")
        close = {"aurora_mysql": 980.0, "aurora_postgresql": 1000.0}
        winner, trace = choose_heterogeneous_engine(BOTH, close, features={"sequences": True})
        assert (winner, trace["deciding_features"]) == ("aurora_mysql", ["sequences"])
        tie = {"aurora_mysql": 5.0, "aurora_postgresql": 5.0}
        assert choose_heterogeneous_engine(BOTH, tie, features={"sequences": True})[0] == (
            "aurora_mysql"
        )

    def test_the_choice_counts_features_per_side(self, monkeypatch) -> None:
        self._favour(monkeypatch, sequences="aurora_mysql", json_usage="aurora_mysql")
        close = {"aurora_mysql": 980.0, "aurora_postgresql": 1000.0}
        two_to_one = {"sequences": True, "json_usage": True, "merge_statements": True}
        winner, trace = choose_heterogeneous_engine(BOTH, close, features=two_to_one)
        assert (winner, trace["deciding_features"]) == (
            "aurora_mysql",
            ["json_usage", "sequences"],
        )

    def test_an_even_split_of_workload_features_falls_to_the_dialect_feature(
        self, monkeypatch
    ) -> None:
        self._favour(monkeypatch, sequences="aurora_mysql", babelfish_compatible="aurora_mysql")
        close = {"aurora_mysql": 980.0, "aurora_postgresql": 1000.0}
        split = {"sequences": True, "merge_statements": True, "babelfish_compatible": True}
        winner, trace = choose_heterogeneous_engine(BOTH, close, features=split)
        assert (winner, trace["deciding_features"]) == ("aurora_mysql", ["babelfish_compatible"])

    def test_an_even_split_and_no_dialect_feature_decides_nothing(self, monkeypatch) -> None:
        self._favour(monkeypatch, sequences="aurora_mysql")
        close = {"aurora_mysql": 980.0, "aurora_postgresql": 1000.0}
        split = {"sequences": True, "merge_statements": True}
        winner, trace = choose_heterogeneous_engine(BOTH, close, features=split)
        assert (winner, trace["deciding_features"]) == ("aurora_postgresql", [])
        assert "no deciding feature" in trace["reason"]

    def test_a_workload_feature_outranks_the_dialect_feature(self, monkeypatch) -> None:
        self._favour(monkeypatch, sequences="aurora_mysql")
        close = {"aurora_mysql": 980.0, "aurora_postgresql": 1000.0}
        both = {"sequences": True, "babelfish_compatible": True}
        assert choose_heterogeneous_engine(BOTH, close, features=both)[0] == "aurora_mysql"

    def test_the_margin_is_the_preferred_engines_total_minus_the_other(self, monkeypatch) -> None:
        totals = {"aurora_mysql": 100.0, "aurora_postgresql": 40.0}
        assert choose_heterogeneous_engine(BOTH, totals)[1]["margin"] == -60.0
        self._tie(monkeypatch, "aurora_mysql")
        assert choose_heterogeneous_engine(BOTH, totals)[1]["margin"] == 60.0

    def test_the_higher_total_wins_whatever_the_catalog_prefers(self, monkeypatch) -> None:
        for preferred in BOTH:
            self._tie(monkeypatch, preferred)
            self._favour(monkeypatch, sequences=preferred)
            totals = {"aurora_mysql": 100.0, "aurora_postgresql": 40.0}
            assert choose_heterogeneous_engine(BOTH, totals, features={"sequences": True})[0] == (
                "aurora_mysql"
            )

    def test_a_pool_without_the_rule_engine_is_an_assertion(self, monkeypatch) -> None:
        self._tie(monkeypatch, "newdb")
        with pytest.raises(AssertionError, match="exact_tie_preferred"):
            choose_heterogeneous_engine(BOTH, {})


SECOND = "documentdb"


@pytest.fixture
def second_floor_engine(monkeypatch):
    """A second engine carrying a search_depth floor of its own (#1081 review)."""
    rules = dict(reality_check_floors.FLOOR_RULES)
    rules[SECOND] = reality_check_floors.FloorRule(
        kind="search_depth",
        reason_code=ReasonCode.FLOOR_COST_SHARE,
        min_calls_per_second=5.0,
        native_override_min_calls_per_second=50.0,
        min_corpus_rows_avg=10.0,
    )
    monkeypatch.setattr(reality_check_floors, "FLOOR_RULES", rules)
    return rules[SECOND]


DEEP = "SELECT id, ts_rank(v, q) AS r FROM docs WHERE v @@ plainto_tsquery($1) ORDER BY r"
SHALLOW = "SELECT * FROM docs WHERE id = $1"


class TestAnyEngineCanCarryAFloor:
    def test_the_floor_reads_the_engines_own_thresholds(self, second_floor_engine) -> None:
        qas = [{"query_id": "q1"}]
        rare = {"q1": {"query_text": DEEP, "calls_per_second": 2.0, "rows_returned_avg": 1}}
        # 2 calls/s clears OpenSearch's 1.0 floor but not the second engine's 5.0.
        assert reality_check_floors.justification_verdicts("opensearch", qas, rare, None)["q1"][0]
        verdict = reality_check_floors.justification_verdicts(SECOND, qas, rare, None)["q1"]
        assert verdict[0] is False and "below the 5 calls/s floor" in verdict[1]
        big = {"q1": {**rare["q1"], "rows_returned_avg": 20}}
        assert reality_check_floors.justification_verdicts(SECOND, qas, big, None)["q1"][0]

    def test_each_floor_engine_records_its_own_reason_and_judges_its_workload(
        self, second_floor_engine
    ) -> None:
        assert reality_check_floors._floor_reason_code(SECOND) is ReasonCode.FLOOR_COST_SHARE
        assert reality_check_floors.search_floor_engines() == {"opensearch", SECOND}
        qmap = {"q1": {"query_text": SHALLOW, "calls_per_second": 100.0}}
        assert (
            reality_check_floors._workload_ever_justified(SECOND, [{"query_id": "q1"}], qmap, None)
            is False
        )

    def test_a_pin_closes_every_pinned_floor_engine_to_shallow_queries(
        self, second_floor_engine
    ) -> None:
        qas = [
            {"query_id": "q1", "assigned_engine": "aurora_postgresql"},
            {"query_id": "q2", "assigned_engine": "aurora_postgresql"},
        ]
        qmap = {"q1": {"query_text": SHALLOW}, "q2": {"query_text": DEEP}}
        closed = reality_check_floors.closed_to_pinned_search(qas, qmap, ["opensearch", SECOND])
        assert closed == {"opensearch": {"q1"}, SECOND: {"q1"}}
        assert reality_check_floors.closed_to_pinned_search(qas, qmap, ["aurora_mysql"]) == {}

    def test_a_bad_reason_code_fails_when_the_floors_are_built(self, monkeypatch) -> None:
        floor = catalog.justification_floors()["opensearch"].model_copy(
            update={"reason_code": "floor.nope"}
        )
        monkeypatch.setattr(catalog, "justification_floors", lambda: {"opensearch": floor})
        with pytest.raises(catalog.CatalogError, match="not a move-ledger ReasonCode"):
            reality_check_floors._floor_rules()


def _trace(qid: str, *engines: str) -> dict[str, dict]:
    return {qid: {"engines": {e: 70.0 for e in engines}}}


class TestTheJoinBlockFollowsTheCatalog:
    """Review of #375 finding B2: a join leaving a non-relational engine never lands on an
    engine whose file says joins need a denormalised design."""

    QUERY = {"query_id": "q1", "query_text": "SELECT 1", "has_joins": True, "join_count": 1}

    def _eligible(self, current: str) -> list[str]:
        return _query_eligible_engines(
            "q1",
            {"query_id": "q1"},
            current,
            ["documentdb", "dynamodb"],
            _trace("q1", "documentdb", "dynamodb", current),
            {},
            {"q1": self.QUERY},
            {},
            "mysql",
        )

    def test_the_shipped_block_is_on_dynamodb(self) -> None:
        assert self._eligible("opensearch") == ["documentdb"]
        assert self._eligible("aurora_mysql") == ["documentdb", "dynamodb"]

    def test_the_block_moves_with_the_catalog_flag(self, monkeypatch) -> None:
        monkeypatch.setattr(reality_check_rules, "DENORMALIZED_JOIN_ENGINES", {"documentdb"})
        assert self._eligible("opensearch") == ["dynamodb"]
        # A join already on a flagged engine is not leaving one, so nothing is blocked.
        assert self._eligible("documentdb") == ["documentdb", "dynamodb"]


class TestThePatternsFollowTheRoles:
    @staticmethod
    def _qa(qid: str, engine: str) -> dict:
        return {"query_id": qid, "assigned_engine": engine}

    def test_the_shipped_roles(self) -> None:
        assignments = [self._qa("a", "aurora_mysql"), self._qa("b", "aurora_mysql")]
        assignments.append(self._qa("c", "opensearch"))
        names = [p["name"] for p in reality_check_patterns.patterns_for_assignment(assignments)]
        assert names == [
            "Command Query Responsibility Segregation (CQRS)",
            "Materialized View Pattern",
            "Event-Driven Sync",
        ]

    def test_the_view_engine_is_the_search_role(self, monkeypatch) -> None:
        monkeypatch.setattr(reality_check_patterns, "VIEW_ENGINES", frozenset({"documentdb"}))
        assignments = [self._qa("a", "aurora_mysql"), self._qa("b", "aurora_mysql")]
        assignments.append(self._qa("c", "documentdb"))
        views = [
            p["applies_to"]
            for p in reality_check_patterns.patterns_for_assignment(assignments)
            if p["name"] == "Materialized View Pattern"
        ]
        assert views == [{"source_engine": "aurora_mysql", "view_engine": "documentdb"}]

    def test_no_event_sync_for_a_key_value_primary(self, monkeypatch) -> None:
        assignments = [self._qa("a", "documentdb"), self._qa("b", "documentdb")]
        assignments.append(self._qa("c", "aurora_mysql"))
        names = lambda: [  # noqa: E731
            p["name"] for p in reality_check_patterns.patterns_for_assignment(assignments)
        ]
        assert "Event-Driven Sync" in names()
        monkeypatch.setattr(
            reality_check_patterns, "NO_EVENT_SYNC_PRIMARY_ENGINES", frozenset({"documentdb"})
        )
        assert "Event-Driven Sync" not in names()


def test_a_table_with_no_owner_anywhere_falls_back_to_the_generic_relational_id() -> None:
    from src.contracts.assignment_models import QueryAssignment

    qa = QueryAssignment(
        query_id="q1",
        assigned_engine="elasticache",
        confidence=50,
        source_tables=["db.t"],
        assignment_reason="test",
    )
    tables, _ = derive_table_assignments([qa])
    assert [t.primary_engine for t in tables] == [BASELINE]


class TestOneJoinBlockDefinition:
    """#1081 review: the eligibility check and the floor's absorber read one helper."""

    JOIN = {"has_joins": True, "join_count": 1}

    def test_the_helper(self) -> None:
        blocked = reality_check_rules.join_blocked_on
        assert blocked("dynamodb", ["opensearch"], self.JOIN)
        assert not blocked("dynamodb", ["aurora_mysql"], self.JOIN)
        assert not blocked("dynamodb", ["opensearch"], {"has_joins": False})
        assert not blocked("documentdb", ["opensearch"], self.JOIN)
        # A tied set holding a relational engine lifts the block.
        assert not blocked("dynamodb", ["opensearch", "aurora_postgresql"], self.JOIN)

    def test_two_flagged_engines_read_the_same_way_on_both_paths(self, monkeypatch) -> None:
        monkeypatch.setattr(
            reality_check_rules, "DENORMALIZED_JOIN_ENGINES", frozenset({"dynamodb", "documentdb"})
        )
        # Leaving a flagged engine for another flagged one is not blocked; leaving a
        # search engine for either is.
        assert not reality_check_rules.join_blocked_on("dynamodb", ["documentdb"], self.JOIN)
        assert reality_check_rules.join_blocked_on("documentdb", ["opensearch"], self.JOIN)

    def test_the_floor_absorber_has_no_copy_of_its_own(self) -> None:
        import inspect

        source = inspect.getsource(reality_check_floors)
        assert "DENORMALIZED_JOIN_ENGINES" not in source
        assert "join_blocked_on(" in source
