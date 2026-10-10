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
from src.agents.referee.synthesis_report import (
    _short_group_labels,
    _table_rps_weights,
    build_summary,
)


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
    {
        "group_name": "Option lookups",
        "engines": ["dynamodb"],
        "access_patterns": [{"engine": "dynamodb", "query_ids": [f"q{i}" for i in range(40)]}],
    },
    {
        "group_name": "Post meta reads",
        "engines": ["dynamodb"],
        "access_patterns": [{"engine": "dynamodb", "query_ids": [f"q{i}" for i in range(40, 63)]}],
    },
    {
        "group_name": "ungrouped",
        "engines": ["elasticache"],
        "access_patterns": [
            {"engine": "elasticache", "query_ids": [f"q{i}" for i in range(63, 97)]}
        ],
    },
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
    """#478: queries and groups are counted on one footing (so "N
    access patterns across M query groups" can never read M > N once a
    relational engine's groups, which carry no access pattern, are
    included); the access-pattern total is called out separately as the
    non-relational engines' own figure."""
    text = _summary()
    assert (
        "Schema design produced 39 target objects: 97 queries in 3 query groups; "
        "65 in-scope access patterns on non-relational engines (dynamodb: 20 target "
        "tables, 52 in-scope access patterns; elasticache: 10 key designs, 13 "
        "in-scope access patterns; aurora_mysql: 9 target tables)."
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
    assert (
        "97 queries in 3 query groups; 60 in-scope access patterns (plus 5 out of scope) on" in text
    )
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


def test_top_query_groups_never_names_the_collector_blame_groups() -> None:
    """#478: neither "Utility and session statements" nor
    "Table not identified by the collector" is a real access pattern a
    reader should see ranked as a "busiest" group."""
    groups = [
        {"group_name": "Table not identified by the collector", "engines": ["aurora_mysql"]},
        {"group_name": "Utility and session statements", "engines": ["aurora_mysql"]},
        {"group_name": "Option lookups", "engines": ["dynamodb"]},
    ]
    text = build_summary(_data(), RANKING, MAPPINGS, TCO, RISKS, groups)
    assert "Top query groups by throughput: Option lookups." in text
    assert "Table not identified by the collector" not in text
    assert "Utility and session statements" not in text


def test_top_query_groups_omitted_when_only_collector_blame_groups_exist() -> None:
    groups = [
        {"group_name": "Table not identified by the collector", "engines": ["aurora_mysql"]},
    ]
    text = build_summary(_data(), RANKING, MAPPINGS, TCO, RISKS, groups)
    assert "Top query groups" not in text


def test_top_query_groups_shortens_a_multi_table_relational_group_name() -> None:
    """#478: a relational engine's group name is its
    (sorted, comma-joined) source tables -- joined into a comma-separated
    "top groups" list, an eight-table group reads like eight separate
    groups. Shortened to its busiest table (here, with no distinguishing
    ``total_design_rps`` given, every table ties and the alphabetical
    tiebreak picks "wp_postmeta" over "wp_posts" -- "m" sorts before "s")
    plus "+N more"."""
    groups = [
        {
            "group_name": "wordpress.wp_posts, wordpress.wp_postmeta, wordpress.wp_term_relationships",
            "engines": ["aurora_mysql"],
        },
        {"group_name": "Option lookups", "engines": ["dynamodb"]},
    ]
    text = build_summary(_data(), RANKING, MAPPINGS, TCO, RISKS, groups)
    assert (
        "Top query groups by throughput: wordpress.wp_postmeta (+2 more), Option lookups." in text
    )
    assert "wordpress.wp_posts (+2 more)" not in text
    assert "wordpress.wp_term_relationships" not in text


def test_short_group_labels_disambiguates_the_real_wordpress_term_tables_pair() -> None:
    """#478: on the wordpress sample, these two real groups
    both shortened to "wordpress.wp_term_relationships" -- "(+2 more)" for
    one, "(+3 more)" for the other -- which read as two different groups
    because the "+N more" counts differed, even though the *same single
    table* was shown for both. The weights below are the real per-table
    totals from that run (``_table_rps_weights`` over all six groups that
    touch a wp_term* table, not just these two): "wordpress.wp_term_taxonomy"
    is the busiest table for both, so the collision is real -- resolved by
    growing the later group to a second table."""
    all_wordpress_term_groups = [
        {
            "group_name": (
                "wordpress.wp_term_relationships, wordpress.wp_term_taxonomy, wordpress.wp_terms"
            ),
            "total_design_rps": 13.13314814814815,
        },
        {
            "group_name": (
                "wordpress.wp_term_relationships, wordpress.wp_term_taxonomy, "
                "wordpress.wp_termmeta, wordpress.wp_terms"
            ),
            "total_design_rps": 3.1870023148148148,
        },
        {
            "group_name": (
                "wordpress.wp_posts, wordpress.wp_term_relationships, "
                "wordpress.wp_term_taxonomy, wordpress.wp_terms"
            ),
            "total_design_rps": 0.4547337962962963,
        },
        {
            "group_name": "wordpress.wp_posts, wordpress.wp_term_relationships, wordpress.wp_term_taxonomy",
            "total_design_rps": 0.31877314814814817,
        },
        {"group_name": "wordpress.wp_termmeta", "total_design_rps": 0.0895949074074074},
        {
            "group_name": "wordpress.wp_term_taxonomy, wordpress.wp_terms",
            "total_design_rps": 0.043020833333333335,
        },
    ]
    weights = _table_rps_weights(all_wordpress_term_groups)
    the_colliding_pair = [
        "wordpress.wp_term_relationships, wordpress.wp_term_taxonomy, wordpress.wp_terms",
        "wordpress.wp_term_relationships, wordpress.wp_term_taxonomy, "
        "wordpress.wp_termmeta, wordpress.wp_terms",
    ]
    labels = _short_group_labels(the_colliding_pair, weights)
    assert labels == [
        "wordpress.wp_term_taxonomy (+2 more)",
        "wordpress.wp_term_taxonomy + wordpress.wp_term_relationships (+2 more)",
    ]
    assert labels[0] != labels[1]


def test_short_group_labels_shows_the_busiest_table_not_the_alphabetically_first() -> None:
    """#478: "wordpress.wp_term_relationships" sorts after
    both "wordpress.wp_posts" and "wordpress.wp_postmeta" alphabetically,
    but is by far the busiest table here -- it must lead, not the
    alphabetically first table."""
    weights = {
        "wordpress.wp_posts": 1.0,
        "wordpress.wp_postmeta": 1.0,
        "wordpress.wp_term_relationships": 50.0,
    }
    labels = _short_group_labels(
        ["wordpress.wp_posts, wordpress.wp_postmeta, wordpress.wp_term_relationships"],
        weights,
    )
    assert labels == ["wordpress.wp_term_relationships (+2 more)"]


def test_short_group_labels_falls_back_to_the_full_name_when_still_colliding() -> None:
    """Two identical group names (shouldn't happen, but never crash or loop
    forever): the first locks in its shortest form; the second, finding the
    same table shown, grows until it reaches its own full (already-unique,
    since it is now the complete name) form."""
    assert _short_group_labels(["a, b", "a, b"]) == ["a (+1 more)", "a, b"]


def test_short_group_labels_single_table_group_name_is_returned_unchanged() -> None:
    assert _short_group_labels(["Option lookups"]) == ["Option lookups"]


def test_table_rps_weights_sums_each_tables_group_rps_across_the_report() -> None:
    """#478: a table's weight is the sum of ``total_design_rps``
    over every group that touches it, not just the handful of names a
    caller is about to shorten -- a table's true "busiest" standing depends
    on its *whole* footprint, not only the top few groups."""
    groups = [
        {"group_name": "a, b", "total_design_rps": 5.0},
        {"group_name": "a, c", "total_design_rps": 2.0},
        {"group_name": "Utility and session statements", "total_design_rps": 999.0},
    ]
    weights = _table_rps_weights(groups)
    assert weights["a"] == 7.0
    assert weights["b"] == 5.0
    assert weights["c"] == 2.0
    assert weights["Utility and session statements"] == 999.0
