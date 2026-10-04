"""Engine roles and waves from the REAL recommended_architecture (#281 follow-up).

``build_architecture_recommendation`` lists an engine with workload but no
schema design in ``recommended_architecture.databases`` only when NO engine has
a schema design (``llm_mode=none``). In a mixed run that engine is the retained
one: it must stay out of ``databases`` so ``renderers._engine_role`` calls it
"Retained" and ``pptx_report.derive`` puts it in the no-migration Wave 1,
rather than "Migration target" in a migration wave.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_handler import _build_schema_summaries
from src.agents.referee.synthesis_report import build_architecture_recommendation
from src.report import pptx_report, renderers


def _rank(target: str, conf: int, aq: int, wp: float, designed: bool) -> dict:
    return {
        "target": target,
        "confidence_score": conf,
        "assigned_queries": aq,
        "workload_percent": wp,
        "schema_design_available": designed,
        "target_tables": 3 if designed else 0,
        "tables_analyzed": 50,
        "patterns_detected": 0,
        "monthly_cost_usd": 0,
    }


def _report(ranking: list[dict], engines: dict[str, dict | None], mappings: list[dict]) -> dict:
    data = SynthesisData(
        job_id="job-1",
        database_name="wordpress",
        engines={e: EngineArtifacts(e, schema_design=s) for e, s in engines.items()},
    )
    return {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-10-04T00:00:00Z",
        "ranking": ranking,
        "recommended_architecture": build_architecture_recommendation(data, ranking, mappings),
        "schema_designs": _build_schema_summaries(data),
    }


def _roles(report: dict) -> dict[str, str]:
    return {e["engine"]: e["role"] for e in renderers._architecture_engines(report)}


def _wave_engines(report: dict) -> list[list[str]]:
    waves = pptx_report.derive(report, {})["waves"]
    return [[e["engine"] for e in w["engines"]] for w in waves]


def test_mixed_run_keeps_the_retained_engine_retained_in_wave_1() -> None:
    ranking = [
        _rank("elasticache", 48, 34, 31.8, True),
        _rank("dynamodb", 60, 63, 58.9, True),
        _rank("aurora_mysql", 50, 10, 9.3, False),  # workload, no design: retained
    ]
    designed = {"table_definitions": [{"table_name": "t"}], "key_designs": [{"key": "k"}]}
    report = _report(
        ranking,
        {"elasticache": designed, "dynamodb": designed, "aurora_mysql": None},
        [{"source_table": f"wp.t{i}", "recommended_database": "dynamodb"} for i in range(5)],
    )

    services = [d["service"] for d in report["recommended_architecture"]["databases"]]
    assert "aurora_mysql" not in services
    assert _roles(report) == {
        "elasticache": "Cache layer",
        "dynamodb": "Migration target",
        "aurora_mysql": "Retained",
    }
    waves = _wave_engines(report)
    assert "aurora_mysql" in waves[0]
    assert all("aurora_mysql" not in w for w in waves[1:])
    assert "Schema design was not run" not in report["recommended_architecture"]["rationale"]


def test_no_schema_design_lists_workload_engines_as_targets() -> None:
    # The wordpress --llm-mode none shape: no engine designed, no table mappings.
    ranking = [
        _rank("elasticache", 48, 34, 31.8, False),
        _rank("dynamodb", 60, 69, 64.5, False),
        _rank("opensearch", 40, 4, 3.7, False),
    ]
    report = _report(ranking, {e["target"]: None for e in ranking}, [])

    arch = report["recommended_architecture"]
    assert [d["service"] for d in arch["databases"]] == ["elasticache", "dynamodb", "opensearch"]
    assert all(d["table_count"] == 0 for d in arch["databases"])
    assert arch["rationale"] == (
        "Hybrid architecture: dynamodb for primary data storage and elasticache for "
        "caching hot data. Schema design was not run, so no tables are allocated yet."
    )
    assert _roles(report) == {
        "elasticache": "Cache layer",
        "dynamodb": "Migration target",
        "opensearch": "Migration target",
    }
    waves = _wave_engines(report)
    assert waves[0] == ["elasticache"]
    assert sorted(e for w in waves[1:] for e in w) == ["dynamodb", "opensearch"]
