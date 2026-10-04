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

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
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
        "Schema design produced 39 target objects and 65 in-scope access patterns across 3 "
        "query groups (dynamodb: 20 target tables, 52 in-scope access patterns; elasticache: "
        "10 key designs, 13 in-scope access patterns; aurora_mysql: 9 target tables)."
    ) in text
    assert "10 target tables with 13 access patterns" not in text


def test_only_in_scope_access_patterns_are_counted() -> None:
    data = _data()
    patterns = [{"pattern_id": f"p{i}", "in_scope": i >= 5} for i in range(52)]
    data.engines = {
        "dynamodb": EngineArtifacts("dynamodb", schema_design={"access_patterns": patterns}),
        "elasticache": EngineArtifacts("elasticache", schema_design={"access_patterns": [{}] * 13}),
        "aurora_mysql": EngineArtifacts("aurora_mysql", schema_design={"access_patterns": []}),
    }
    text = build_summary(data, RANKING, MAPPINGS, TCO, RISKS, GROUPS)
    assert "60 in-scope access patterns (plus 5 out of scope) across" in text
    assert (
        "(dynamodb: 20 target tables, 47 in-scope access patterns; elasticache: 10 key designs"
    ) in text


def test_no_out_of_scope_count_when_every_pattern_is_in_scope() -> None:
    assert "out of scope" not in _summary()


def test_resolved_risks_are_counted_in_the_risk_sentence() -> None:
    risks = {**RISKS, "resolved_risks": [{}, {}, {}]}
    text = build_summary(_data(), RANKING, MAPPINGS, TCO, risks, GROUPS)
    assert (
        "1 open migration risk (overall: MEDIUM); 3 more were resolved by the assignment."
    ) in text
    assert "risk(s)" not in text


def test_risk_sentence_singular_and_plural() -> None:
    one_resolved = {"risks": [{}, {}], "overall_risk_level": "LOW", "resolved_risks": [{}]}
    text = build_summary(_data(), RANKING, MAPPINGS, TCO, one_resolved, GROUPS)
    assert "2 open migration risks (overall: LOW); 1 more was resolved by the assignment." in text
    assert "1 open migration risk (overall: MEDIUM)." in _summary()


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
    assert "Schema design produced 10 target tables with 13 in-scope access patterns" in text
    assert "Other targets evaluated: dynamodb (50%), aurora_mysql (50%)." in text
    assert text.count("source tables mapped") == 1


def _rationale(data: SynthesisData, target: str) -> str:
    from src.agents.referee.synthesis_report import build_architecture_recommendation

    rank = {
        **next(r for r in RANKING if r["target"] == target),
        "tables_analyzed": 50,
        "patterns_detected": 0,
        "monthly_cost_usd": 0,
    }
    dbs = build_architecture_recommendation(data, [rank], MAPPINGS)["databases"]
    return str(dbs[0]["rationale"])


def test_engine_rationale_counts_in_scope_access_patterns() -> None:
    """The recommended-architecture rationale counts like the summary (#255)."""
    data = _data()
    patterns = [{"pattern_id": f"p{i}", "in_scope": i >= 5} for i in range(52)]
    data.engines = {
        "dynamodb": EngineArtifacts("dynamodb", schema_design={"access_patterns": patterns}),
        "elasticache": EngineArtifacts("elasticache", schema_design={"access_patterns": [{}] * 13}),
    }
    assert (
        "schema design: 20 target tables, 47 in-scope access patterns (plus 5 out of scope)."
        in _rationale(data, "dynamodb")
    )
    text = _rationale(data, "elasticache")
    assert "schema design: 10 target tables, 13 in-scope access patterns." in text
    assert "out of scope" not in text
