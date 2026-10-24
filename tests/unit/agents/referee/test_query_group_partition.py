"""The owners' query groups are a partition of the in-scope queries (#226).

The cache layer owns no query: it fronts reads another engine serves. Its access
patterns used to land in the owner's group of the same name (a pattern with no
``pattern_group`` is grouped by its source tables), so that group listed the fronted
query a second time, as if it served it. The cache layer's patterns now make their own
groups (``cache_layer``), whose queries are labelled ``cached_read``.
"""

from __future__ import annotations

from collections import Counter

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_query_groups

QUERIES = [
    {
        "query_id": "q-ddb",
        "query_text": "SELECT * FROM wp_options WHERE option_name = ?",
        "query_type": "SELECT",
        "calls_per_second": 9.0,
        "tables_accessed": ["wp.wp_options"],
    },
    {
        "query_id": "q-aurora",
        "query_text": "SELECT * FROM wp_posts WHERE ID = ?",
        "query_type": "SELECT",
        "calls_per_second": 4.0,
        "tables_accessed": ["wp.wp_posts"],
    },
    {
        "query_id": "q-write",
        "query_text": "UPDATE wp_posts SET post_title = ? WHERE ID = ?",
        "query_type": "UPDATE",
        "calls_per_second": 1.0,
        "tables_accessed": ["wp.wp_posts"],
    },
]


def _data(aurora_design: dict | None = None, ddb_group: str | None = None) -> SynthesisData:
    ddb_pattern = {
        "pattern_id": "DDB-AP-1",
        "source_query_ids": ["q-ddb"],
        "source_tables": ["wp.wp_options"],
        "design_rps": 9.0,
    }
    if ddb_group:
        ddb_pattern["pattern_group"] = ddb_group
    engines = {
        "dynamodb": EngineArtifacts(
            engine="dynamodb", schema_design={"access_patterns": [ddb_pattern]}
        ),
        "elasticache": EngineArtifacts(
            engine="elasticache",
            schema_design={
                "access_patterns": [
                    {
                        "pattern_id": "EC-AP-1",
                        "source_query_ids": ["q-ddb"],
                        "source_tables": ["wp.wp_options"],
                        "design_rps": 9.0,
                    },
                    {
                        "pattern_id": "EC-AP-2",
                        "source_query_ids": ["q-aurora"],
                        "source_tables": ["wp.wp_posts"],
                        "design_rps": 4.0,
                    },
                ]
            },
        ),
    }
    if aurora_design is not None:
        engines["aurora_mysql"] = EngineArtifacts(
            engine="aurora_mysql", schema_design=aurora_design
        )
    return SynthesisData(
        job_id="test",
        database_name="wp",
        collector={"queries": {"query_patterns": QUERIES}},
        engines=engines,
        assignment={
            "query_assignments": [
                {
                    "query_id": "q-ddb",
                    "assigned_engine": "dynamodb",
                    "source_tables": ["wp.wp_options"],
                    "in_scope": True,
                },
                {
                    "query_id": "q-aurora",
                    "assigned_engine": "aurora_mysql",
                    "source_tables": ["wp.wp_posts"],
                    "assignment_reason": "relational core",
                    "in_scope": True,
                },
                {
                    "query_id": "q-write",
                    "assigned_engine": "aurora_mysql",
                    "source_tables": ["wp.wp_posts"],
                    "assignment_reason": "relational core",
                    "in_scope": True,
                },
            ]
        },
    )


def _served(groups: list[dict]) -> Counter[str]:
    """How many groups list each query as served (not as a cached read)."""
    return Counter(
        sq["query_id"] for g in groups for sq in g["source_queries"] if not sq.get("cached_read")
    )


def _aurora_designed() -> dict:
    return {
        "access_patterns": [
            {
                "pattern_id": "AUR-AP-1",
                "source_query_ids": ["q-aurora"],
                "source_tables": ["wp.wp_posts"],
                "design_rps": 4.0,
            },
            {
                "pattern_id": "AUR-AP-2",
                "source_query_ids": ["q-write"],
                "source_tables": ["wp.wp_posts"],
                "design_rps": 1.0,
            },
        ]
    }


def test_every_in_scope_query_is_served_by_exactly_one_group_undesigned_aurora() -> None:
    """DynamoDB names its group (``Option reads``), as a model's design does; the cache
    pattern on the same query is grouped by its table and must not list it as served."""
    groups = build_query_groups(_data(ddb_group="Option reads"))
    assert _served(groups) == {"q-ddb": 1, "q-aurora": 1, "q-write": 1}


def test_every_in_scope_query_is_served_by_exactly_one_group_designed_aurora() -> None:
    groups = build_query_groups(_data(aurora_design=_aurora_designed(), ddb_group="Option reads"))
    assert _served(groups) == {"q-ddb": 1, "q-aurora": 1, "q-write": 1}


def test_the_owner_group_lists_no_cache_pattern() -> None:
    for data in (_data(), _data(aurora_design=_aurora_designed())):
        for g in build_query_groups(data):
            if g.get("cache_layer"):
                continue
            assert "elasticache" not in g["engines"], g["group_name"]
            assert all(ap["engine"] != "elasticache" for ap in g["access_patterns"])


def test_the_cache_layer_has_its_own_groups_of_cached_reads() -> None:
    groups = build_query_groups(_data())
    cache = [g for g in groups if g.get("cache_layer")]
    assert sorted(g["group_name"] for g in cache) == ["wp.wp_options", "wp.wp_posts"]
    for g in cache:
        assert g["engines"] == ["elasticache"]
        assert g["source_queries"], g["group_name"]
        assert all(sq["cached_read"] is True for sq in g["source_queries"])
    by_name = {g["group_name"]: g for g in cache}
    assert [sq["query_id"] for sq in by_name["wp.wp_options"]["source_queries"]] == ["q-ddb"]
    assert by_name["wp.wp_options"]["total_design_rps"] == 9.0


def test_an_owner_group_of_the_same_name_keeps_only_its_own_figures() -> None:
    """The DynamoDB pattern has no pattern_group, so its group is named by its table, as
    the cache pattern's is: two groups, each with its own patterns and design RPS."""
    named = [g for g in build_query_groups(_data()) if g["group_name"] == "wp.wp_options"]
    assert len(named) == 2
    owner = next(g for g in named if not g.get("cache_layer"))
    assert owner["engines"] == ["dynamodb"]
    assert [ap["pattern_id"] for ap in owner["access_patterns"]] == ["DDB-AP-1"]
    assert owner["total_design_rps"] == 9.0
    assert "cache_layer" not in owner
    assert all("cached_read" not in sq for sq in owner["source_queries"])


def test_a_cache_pattern_group_name_never_merges_into_an_owner_group() -> None:
    """A cache pattern_group equal to an owner's (both ``Option reads``) stays apart."""
    data = _data(ddb_group="Option reads")
    data.engines["elasticache"].schema_design["access_patterns"][0][
        "pattern_group"
    ] = "Option reads"
    named = [g for g in build_query_groups(data) if g["group_name"] == "Option reads"]
    assert sorted(bool(g.get("cache_layer")) for g in named) == [False, True]
    assert _served(build_query_groups(data))["q-ddb"] == 1


@pytest.mark.passes_on_main  # guards what must not change
def test_no_cache_design_leaves_the_groups_as_they_were() -> None:
    data = _data()
    del data.engines["elasticache"]
    groups = build_query_groups(data)
    assert not any(g.get("cache_layer") for g in groups)
    assert not any(sq.get("cached_read") for g in groups for sq in g["source_queries"])
    assert _served(groups) == {"q-ddb": 1, "q-aurora": 1, "q-write": 1}
