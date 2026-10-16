"""ADR-031 Decision 2 step 1: every modelled engine is a candidate (#726).

Triage detects workload signals, but a signal is evidence, never a gate on which
query-owning engines are analysed and scored. Every active, query-owning engine in the
catalog is selected for analysis, minus the future placeholders (Decision 9, deferred)
and the Aurora flavour the source rules out (a homogeneous source keeps only its own; a
heterogeneous source analyses both and the resolver picks one, Decision 6).

ElastiCache is a cache overlay that owns no query, so it is not a routing candidate: it
is still selected only when one of its signals fires, exactly as before #726, so the
overlay is unchanged.
"""

from __future__ import annotations

import pytest

from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.aurora_choice import AURORA_ENGINES
from src.agents.referee.cache_overlay import CACHE_OVERLAY_ENGINES
from src.agents.referee.triage import (
    ANALYSIS_AGENTS,
    DEFERRED,
    UNNAMED_ENGINE_REASON,
    TriageSignal,
    _select_agents,
    triage,
)
from src.shared import catalog

AURORA = frozenset({"aurora_mysql", "aurora_postgresql"})
# The engines that can own a query, apart from Aurora: always selected.
OWNING = ANALYSIS_AGENTS - AURORA - CACHE_OVERLAY_ENGINES


def _collector(engine: str, queries: list[dict]) -> dict:
    return {
        "job_id": "test",
        "metadata": {"source_database": {"engine": engine}},
        "database_schema": {
            "tables": [
                {
                    "table_id": "app.orders",
                    "table_name": "orders",
                    "columns": [{"column_name": "id", "data_type": "int"}],
                    "foreign_keys": [],
                }
            ]
        },
        "queries": {"query_patterns": queries},
    }


def _signal_free_query(qid: str = "q1") -> dict:
    # A plain mid-traffic read with a mid-sized result: no key-value, range,
    # status, join, aggregation, scan, time-series, session, metadata, text-search,
    # leaderboard, subquery or high-frequency signal fires for it.
    return {
        "query_id": qid,
        "query_text": "SELECT id, total FROM orders WHERE customer_ref = ?",
        "query_type": "SELECT",
        "tables_accessed": ["app.orders"],
        "join_count": 0,
        "has_joins": False,
        "calls_per_second": 1.0,
        "rows_returned_avg": 50,
    }


class TestEveryModelledEngineIsACandidate:
    def test_the_candidates_are_the_catalogs_active_engines(self):
        assert ANALYSIS_AGENTS == catalog.engine_ids(status="active") - {"aurora"}

    def test_a_workload_with_no_signal_still_selects_every_engine(self):
        result = triage(_collector("mysql", [_signal_free_query()]))
        assert result.signals == []
        assert set(result.selected) == OWNING | {"aurora_mysql"}

    @pytest.mark.parametrize(
        "engine, kept, ruled_out",
        [
            ("mysql", "aurora_mysql", "aurora_postgresql"),
            ("mariadb", "aurora_mysql", "aurora_postgresql"),
            ("postgresql", "aurora_postgresql", "aurora_mysql"),
        ],
    )
    def test_a_homogeneous_source_skips_only_the_other_aurora_flavour(
        self, engine, kept, ruled_out
    ):
        result = triage(_collector(engine, [_signal_free_query()]))
        assert kept in result.selected
        assert set(result.skipped) == {ruled_out} | CACHE_OVERLAY_ENGINES
        assert result.skipped[ruled_out] == "Source engine does not match this Aurora variant"

    @pytest.mark.parametrize("engine", ["sqlserver", "oracle", "db2"])
    def test_a_heterogeneous_source_analyses_both_aurora_engines(self, engine):
        result = triage(_collector(engine, [_signal_free_query()]))
        assert set(result.selected) == ANALYSIS_AGENTS - CACHE_OVERLAY_ENGINES
        assert set(result.skipped) == CACHE_OVERLAY_ENGINES

    def test_an_empty_workload_still_selects_every_owning_engine(self):
        result = triage({"database_schema": {"tables": []}, "queries": {"query_patterns": []}})
        assert set(result.selected) == OWNING | AURORA

    def test_no_source_engine_analyses_both_aurora_engines(self):
        collector = _collector("mysql", [_signal_free_query()])
        del collector["metadata"]
        result = triage(collector)
        assert AURORA <= set(result.selected)
        assert "No source engine recorded" in result.selected["aurora_postgresql"][0]
        assert not AURORA & set(result.skipped)

    def test_an_unrecognised_source_engine_still_analyses_no_aurora_engine(self):
        result = triage(_collector("some_unknown_engine", [_signal_free_query()]))
        assert not AURORA & set(result.selected)
        assert AURORA <= set(result.skipped)

    def test_aurora_engines_are_aurora_choices_set(self):
        assert AURORA == AURORA_ENGINES

    def test_placeholders_stay_deferred_and_unselected(self):
        signals = [
            TriageSignal(signal="high_fk_density", targets=["neptune"], evidence="e"),
            TriageSignal(signal="write_heavy", targets=["keyspaces"], evidence="e"),
        ]
        result = _select_agents(signals, aurora_agents=["aurora_mysql"])
        assert DEFERRED <= set(result.deferred)
        assert not DEFERRED & set(result.selected)
        assert not DEFERRED & set(result.skipped)


class TestSignalsAreEvidenceOnly:
    def test_a_named_engine_carries_the_signal_as_its_reason(self):
        signals = [TriageSignal(signal="text_search", targets=["opensearch"], evidence="3 x")]
        result = _select_agents(signals, aurora_agents=["aurora_mysql"])
        assert "text_search: 3 x" in result.selected["opensearch"]

    def test_an_unnamed_engine_is_selected_with_a_reason_that_says_so(self):
        signals = [TriageSignal(signal="text_search", targets=["opensearch"], evidence="3 x")]
        result = _select_agents(signals, aurora_agents=["aurora_mysql"])
        for engine in ("dynamodb", "documentdb"):
            reasons = result.selected[engine]
            assert len(reasons) == 1
            assert "no triage signal names it" in reasons[0]

    def test_the_selected_set_does_not_depend_on_the_signals(self):
        none = _select_agents([], aurora_agents=["aurora_postgresql"])
        some = _select_agents(
            [
                TriageSignal(signal="text_search", targets=["opensearch"], evidence="e"),
                TriageSignal(signal="json_columns", targets=["documentdb"], evidence="e"),
            ],
            aurora_agents=["aurora_postgresql"],
        )
        assert set(none.selected) == set(some.selected)
        assert none.skipped == some.skipped


def _analysis(confidence: int) -> dict:
    return {
        "table_recommendations": [{"table_id": "app.orders", "confidence_score": confidence}],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


class TestAnUnnamedEngineCanWinOnScore:
    """An engine that no signal names is analysed, scored and can win the query."""

    def test_with_no_source_engine_the_resolver_picks_one_aurora_engine(self):
        collector = _collector("mysql", [_signal_free_query("q1")])
        del collector["metadata"]
        result = triage(collector)
        scores = {"aurora_mysql": 60, "aurora_postgresql": 80}
        analysis_outputs = {e: _analysis(scores.get(e, 10)) for e in result.selected}
        triage_doc = {
            "selected_agents": [{"agent_type": e} for e in result.selected],
            "signals": [],
            "query_capabilities": result.query_capabilities,
        }
        assignment = AssignmentResolver().resolve(triage_doc, analysis_outputs, collector)
        assert assignment.aurora_engine_choice is not None
        assert assignment.aurora_engine_choice.engine == "aurora_postgresql"
        (qa,) = assignment.query_assignments
        assert qa.assigned_engine == "aurora_postgresql"

    def test_dynamodb_wins_a_query_no_signal_names_it_for(self):
        collector = _collector("mysql", [_signal_free_query("q1")])
        result = triage(collector)
        assert not any("dynamodb" in s.targets for s in result.signals)

        # Only engines triage selects are analysed (the pipeline dispatches an
        # analysis per selected engine), so this is what the resolver sees.
        scores = {"aurora_mysql": 60, "dynamodb": 90, "documentdb": 50}
        analysis_outputs = {e: _analysis(scores.get(e, 10)) for e in result.selected}
        triage_doc = {
            "selected_agents": [{"agent_type": e} for e in result.selected],
            "signals": [],
            "query_capabilities": result.query_capabilities,
        }
        assignment = AssignmentResolver().resolve(triage_doc, analysis_outputs, collector)
        owner = {qa.query_id: qa.assigned_engine for qa in assignment.query_assignments}
        assert owner["q1"] == "dynamodb"


# ElastiCache's own signals (the ones on main that named it).
CACHE_SIGNALS = (
    "key_value_lookups",
    "high_frequency_reads",
    "session_store",
    "leaderboard_pattern",
)


class TestOwningEnginesAlwaysCacheOverlayOnItsSignals:
    """Both halves of the selection rule: an owning engine needs no signal; the
    cache overlay engine, which owns no query, is selected only by its signals."""

    def test_the_cache_overlay_engine_is_elasticache(self):
        assert CACHE_OVERLAY_ENGINES == frozenset({"elasticache"})

    def test_an_owning_engine_is_selected_without_a_signal(self):
        result = _select_agents([], aurora_agents=["aurora_mysql"])
        assert OWNING <= set(result.selected)
        for engine in OWNING:
            assert result.selected[engine] == [UNNAMED_ENGINE_REASON]

    def test_elasticache_without_a_signal_is_not_selected(self):
        result = triage(_collector("mysql", [_signal_free_query()]))
        assert "elasticache" not in result.selected
        assert result.skipped["elasticache"] == "No matching workload signals detected"

    @pytest.mark.parametrize("signal", CACHE_SIGNALS)
    def test_elasticache_is_selected_by_any_of_its_signals(self, signal):
        sig = TriageSignal(signal=signal, targets=["dynamodb", "elasticache"], evidence="e")
        result = _select_agents([sig], aurora_agents=["aurora_mysql"])
        assert result.selected["elasticache"] == [f"{signal}: e"]
        assert "elasticache" not in result.skipped

    def test_a_signal_free_hot_read_gets_no_cache_overlay(self):
        # On main ElastiCache is not analysed here, so there is no overlay; the
        # same holds now (byte-identical overlay behaviour).
        collector = _collector("mysql", [_signal_free_query("q1")])
        result = triage(collector)
        analysis_outputs = {e: _analysis(50) for e in result.selected}
        triage_doc = {
            "selected_agents": [{"agent_type": e} for e in result.selected],
            "signals": [],
            "query_capabilities": result.query_capabilities,
        }
        assignment = AssignmentResolver().resolve(triage_doc, analysis_outputs, collector)
        (qa,) = assignment.query_assignments
        assert qa.cache_engine is None
