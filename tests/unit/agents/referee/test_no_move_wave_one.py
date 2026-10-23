"""A wave 1 that moves nothing is marked once and every consumer reads the mark (#1383).

#1369 made wave 1 say "No move to Aurora" when the engine scope has no relational engine
(or its Aurora engine serves no query). The wave now carries ``no_move_reason``; the
facts, the next steps, the summary request and the summary check read it, so a wave that
moves nothing shows no engine line, no carried-table count and no latency comparison,
and the summary never calls a homogeneous source heterogeneous.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.migration_waves import build_migration_waves
from src.agents.referee.summary_input_slim import slim_summary_input
from src.agents.referee.synthesis_grounding import check_summary_no_move_wave
from src.agents.referee.synthesis_handler import apply_synthesis_llm_output
from src.agents.referee.synthesis_report import build_wave_checks
from src.contracts.synthesis_output import MigrationWave, WaveFacts
from src.shared.report_facts import wave_facts

RANKING = [{"target": "dynamodb", "assigned_queries": 1, "workload_percent": 100.0}]
QUERIES = [{"query_id": "r1", "assigned_engine": "dynamodb", "source_tables": ["app.orders"]}]


def _waves(scope: list[str] | None, source: str = "mysql") -> list[dict[str, Any]]:
    return build_migration_waves(
        ranking=RANKING,
        table_assignments=[
            {"table_id": "app.orders", "primary_engine": "dynamodb", "query_count": 1}
        ],
        query_assignments=QUERIES,
        co_dependency_groups=[],
        cache_overlay=None,
        source_engine=source,
        source_version="8.0.45",
        known_tables={"app.orders", "app.users"},
        engine_scope=scope,
    )


def _checks(waves: list[dict[str, Any]]) -> list[str]:
    return build_wave_checks(waves, [], QUERIES, None)


class TestTheWaveCarriesTheMark:
    def test_no_relational_engine_in_scope(self) -> None:
        assert _waves(["dynamodb"])[0]["no_move_reason"] == "no_relational_in_scope"

    def test_scope_choice_serves_nothing(self) -> None:
        waves = _waves(["aurora_postgresql", "dynamodb"])
        assert waves[0]["no_move_reason"] == "scope_choice_serves_nothing"

    @pytest.mark.passes_on_main  # behaviour the fix must not change
    def test_a_normal_wave_one_is_not_marked(self) -> None:
        assert not _waves(None)[0].get("no_move_reason")

    @pytest.mark.passes_on_main  # behaviour the fix must not change
    def test_a_heterogeneous_source_is_not_marked(self) -> None:
        wave = _waves(None, source="sqlserver")[0]
        assert not wave.get("no_move_reason")
        assert wave["homogeneity"] == "heterogeneous"

    def test_facts_say_no_move_once(self) -> None:
        assert wave_facts(_waves(["dynamodb"])[0])["no_move"] is True
        assert wave_facts(_waves(None)[0])["no_move"] is False

    def test_a_report_written_before_the_field_still_loads(self) -> None:
        wave = {k: v for k, v in _waves(None)[0].items() if k != "no_move_reason"}
        assert MigrationWave.model_validate(wave).no_move_reason is None
        facts = {k: v for k, v in wave_facts(wave).items() if k != "no_move"}
        assert WaveFacts.model_validate(facts).no_move is False


class TestNextSteps:
    def test_no_latency_check_after_a_wave_that_moves_nothing(self) -> None:
        checks = _checks(_waves(["dynamodb"]))
        assert not any("After wave 1" in c for c in checks)
        assert any("After wave 2" in c for c in checks)

    @pytest.mark.passes_on_main  # behaviour the fix must not change
    def test_a_normal_wave_one_keeps_its_latency_check(self) -> None:
        checks = _checks(_waves(None))
        assert any(c.startswith("After wave 1 (Aurora MySQL): compare the latency") for c in checks)


class TestTheSummaryRequest:
    def test_the_request_states_the_no_move_wave_and_hides_the_marker(self) -> None:
        payload = slim_summary_input({"migration_waves": _waves(["dynamodb"])})
        wave = payload["migration_waves"][0]
        assert "homogeneity" not in wave
        assert "moves nothing" in wave["no_move"]
        assert "heterogeneous" not in str(wave).lower()

    @pytest.mark.passes_on_main  # behaviour the fix must not change
    def test_a_heterogeneous_source_keeps_its_marker(self) -> None:
        wave = slim_summary_input({"migration_waves": _waves(None, source="sqlserver")})[
            "migration_waves"
        ][0]
        assert wave["homogeneity"] == "heterogeneous"
        assert "no_move" not in wave


class TestTheSummaryCheck:
    SUMMARY = "The heterogeneous MySQL source moves to DynamoDB."

    def test_a_homogeneous_source_called_heterogeneous_is_rejected(self) -> None:
        findings = check_summary_no_move_wave(self.SUMMARY, _waves(["dynamodb"]))
        assert len(findings) == 1 and findings[0]["high_confidence"]

    def test_the_word_alone_is_the_trigger(self) -> None:
        assert not check_summary_no_move_wave("MySQL moves to DynamoDB.", _waves(["dynamodb"]))

    def test_a_normal_wave_one_is_not_checked(self) -> None:
        assert not check_summary_no_move_wave(self.SUMMARY, _waves(None))

    def test_a_truly_heterogeneous_source_may_say_so(self) -> None:
        waves = _waves(["dynamodb"], source="sqlserver")
        assert not check_summary_no_move_wave(self.SUMMARY, waves)

    def test_apply_falls_back_to_the_deterministic_summary(self) -> None:
        result: dict[str, Any] = {
            "summary": "Deterministic text.",
            "migration_waves": _waves(["dynamodb"]),
            "effective_architecture": None,
            "known_tables": [],
        }
        out = apply_synthesis_llm_output(result, {"executive_summary": self.SUMMARY})
        assert out["summary_source"] == "deterministic_fallback"
        assert "heterogeneous" not in out["executive_summary"].lower()
