"""#381 review: the resolver's heterogeneous Aurora engine choice must reach the
executive summary, not only wave 1's rationale text (``build_summary``, using
``display_engine``/``display_source_database`` so the sentence names both
engines by their customer-facing display name).

#381 review round 2: the source name uses ``display_source_database`` (the same
helper every wave's "moves_from" text uses), not a hand-rolled
``SOURCE_ENGINE_DISPLAY_NAMES`` lookup that silently fell back to an empty string
for an unknown or missing ``source_engine``."""

from __future__ import annotations

from src.agents.referee.synthesis_data import SynthesisData
from src.agents.referee.synthesis_report import build_summary

RANKING = [
    {
        "target": "aurora_postgresql",
        "confidence_score": 70,
        "assigned_queries": 20,
        "workload_percent": 100.0,
    }
]
MAPPINGS = [{"source_table": "Sales.Orders", "recommended_database": "aurora_postgresql"}]
TCO = {"projected_monthly_cost": 0, "savings_percent": 0}
RISKS = {"risks": [], "overall_risk_level": "LOW"}
GROUPS: list[dict] = []


def _data(aurora_engine_choice: dict | None) -> SynthesisData:
    return SynthesisData(
        job_id="j",
        database_name="aw",
        collector={
            "metadata": {"source_database": {"engine": "sqlserver"}},
            "database_schema": {"tables": [{"table_id": "Sales.Orders"}]},
            "queries": {"query_patterns": [{"query_id": "q1"}]},
        },
        assignment={"aurora_engine_choice": aurora_engine_choice} if aurora_engine_choice else {},
    )


def test_heterogeneous_choice_is_named_with_display_names() -> None:
    choice = {
        "source_engine": "sqlserver",
        "engine": "aurora_postgresql",
        "totals": {"aurora_mysql": 60.0, "aurora_postgresql": 65.0},
        "margin": 5.0,
        "deciding_features": [],
        "reason": "higher total adjusted score wins (60.0 vs 65.0)",
    }
    text = build_summary(_data(choice), RANKING, MAPPINGS, TCO, RISKS, GROUPS)
    assert "the source SQL Server database has no Aurora engine of its own dialect" in text
    assert "Aurora PostgreSQL was selected to serve it" in text
    assert "cross-engine" in text


def test_no_sentence_without_a_recorded_choice() -> None:
    text = build_summary(_data(None), RANKING, MAPPINGS, TCO, RISKS, GROUPS)
    assert "has no Aurora engine of its own dialect" not in text


def test_unknown_source_engine_still_renders_with_a_title_cased_fallback() -> None:
    # #381 review round 2: an unmapped source_engine (not in
    # SOURCE_ENGINE_DISPLAY_NAMES) must still produce a readable name
    # (display_source_database falls back to display_engine's title-casing),
    # never the empty string the old hand-rolled lookup produced.
    choice = {
        "source_engine": "informix",
        "engine": "aurora_mysql",
        "totals": {},
        "margin": None,
        "deciding_features": [],
        "reason": "only one Aurora engine competed",
    }
    text = build_summary(_data(choice), RANKING, MAPPINGS, TCO, RISKS, GROUPS)
    assert "the source Informix database has no Aurora engine of its own dialect" in text


def test_empty_source_engine_skips_the_sentence_instead_of_naming_nothing() -> None:
    # #381 review round 2: a choice with no source_engine at all (e.g. a legacy or
    # malformed artifact) must not render "has no Aurora engine of its own dialect"
    # with an empty/blank name in front of it.
    choice = {
        "source_engine": "",
        "engine": "aurora_mysql",
        "totals": {},
        "margin": None,
        "deciding_features": [],
        "reason": "only one Aurora engine competed",
    }
    text = build_summary(_data(choice), RANKING, MAPPINGS, TCO, RISKS, GROUPS)
    assert "has no Aurora engine of its own dialect" not in text


def test_no_sentence_when_no_assignment_at_all() -> None:
    data = SynthesisData(
        job_id="j",
        database_name="aw",
        collector={
            "metadata": {"source_database": {"engine": "sqlserver"}},
            "database_schema": {"tables": [{"table_id": "Sales.Orders"}]},
            "queries": {"query_patterns": [{"query_id": "q1"}]},
        },
    )
    text = build_summary(data, RANKING, MAPPINGS, TCO, RISKS, GROUPS)
    assert "has no Aurora engine of its own dialect" not in text
