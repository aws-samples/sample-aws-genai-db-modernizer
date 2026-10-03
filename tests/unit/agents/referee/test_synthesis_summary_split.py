"""The deterministic summary describes the whole split, not ``ranking[0]`` (#219).

``build_ranking`` orders engines by an analysis weight, so ``ranking[0]`` is not the
engine carrying the most workload. With assignment data the summary must:

- report schema-design totals across every engine that carries queries (and per
  engine), not the first-ranked engine's numbers;
- list as "other targets evaluated" only engines that carry no workload (an engine
  the assignment left empty, or one the reality check eliminated), never a selected
  engine, and omit the sentence when there are none;
- state each fact once.

The numbers mirror the wordpress validation run that surfaced #219.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import SynthesisData
from src.agents.referee.synthesis_report import build_summary


def _rank(target: str, weight: float, aq: int, wp: float, tt: int, ap: int, pg: int) -> dict:
    return {
        "target": target,
        "confidence_score": 50,
        "weight": weight,
        "assigned_queries": aq,
        "workload_percent": wp,
        "schema_design_available": tt > 0,
        "target_tables": tt,
        "access_patterns": ap,
        "pattern_groups": pg,
    }


RANKING = [
    _rank("elasticache", 0.64, 34, 31.8, 10, 13, 1),
    _rank("dynamodb", 0.501, 63, 58.9, 20, 52, 28),
    _rank("aurora_mysql", 0.397, 10, 9.3, 9, 0, 0),
]
MAPPINGS = [
    {"source_table": f"wp.t{i}", "recommended_database": e}
    for i, e in enumerate(["dynamodb"] * 19 + ["elasticache"] * 2 + ["aurora_mysql"])
]
TCO = {"projected_monthly_cost": 582.76, "savings_percent": 0}
RISKS = {"risks": [{"severity": "HIGH"}], "overall_risk_level": "MEDIUM"}
GROUPS = [
    {"group_name": "Option lookups", "engines": ["dynamodb"]},
    {"group_name": "Post meta reads", "engines": ["dynamodb"]},
    {"group_name": "ungrouped", "engines": ["elasticache"]},
]


def _data() -> SynthesisData:
    return SynthesisData(
        job_id="j",
        database_name="wp",
        collector={
            "database_schema": {"tables": [{"table_id": f"wp.t{i}"} for i in range(50)]},
            "queries": {"query_patterns": [{"query_id": f"q{i}"} for i in range(107)]},
        },
    )


def _summary(ranking=RANKING, eliminated=None) -> str:
    return build_summary(_data(), ranking, MAPPINGS, TCO, RISKS, GROUPS, eliminated)


def test_schema_totals_cover_every_engine_with_workload() -> None:
    text = _summary()
    assert (
        "Schema design produced 39 target tables and 65 access patterns across 3 query "
        "groups (dynamodb: 20 tables, 52 access patterns; elasticache: 10 tables, 13 "
        "access patterns; aurora_mysql: 9 tables)."
    ) in text
    assert "10 target tables with 13 access patterns" not in text


def test_workload_split_is_ordered_by_assigned_queries() -> None:
    text = _summary()
    assert (
        "Workload split: dynamodb handles 63 queries (58.9%), elasticache handles 34 "
        "queries (31.8%), aurora_mysql handles 10 queries (9.3%)."
    ) in text


def test_selected_engines_are_not_other_targets() -> None:
    assert "Other targets evaluated" not in _summary()


def test_eliminated_and_empty_engines_are_the_other_targets() -> None:
    ranking = RANKING + [_rank("documentdb", 0.3, 0, 0.0, 0, 0, 0)]
    text = _summary(ranking, {"opensearch": "aurora_mysql", "neptune": None})
    assert text.endswith(
        "Other targets evaluated: documentdb (no queries assigned), opensearch "
        "(consolidated into aurora_mysql by the reality check), neptune (eliminated by "
        "the reality check)."
    )


def test_source_table_mapping_is_stated_once() -> None:
    text = _summary()
    assert text.count("source tables mapped") == 1
    assert "22 source tables mapped to aurora_mysql, dynamodb, elasticache." in text


def test_without_assignment_the_top_engine_summary_is_kept() -> None:
    ranking = [
        {k: v for k, v in r.items() if k not in ("assigned_queries", "workload_percent")}
        for r in RANKING
    ]
    text = _summary(ranking)
    assert "Top recommendation: elasticache with 50% average confidence." in text
    assert "Schema design produced 10 target tables with 13 access patterns" in text
    assert "Other targets evaluated: dynamodb (50%), aurora_mysql (50%)." in text
    assert text.count("source tables mapped") == 1
