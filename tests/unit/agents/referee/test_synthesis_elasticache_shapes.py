"""ElastiCache designs must survive synthesis (issue #130).

Each schema designer names its output after its target's own vocabulary, so
synthesis cannot ask every engine for ``table_definitions``. It used to, with
inline rescues for OpenSearch and DocumentDB and none for ElastiCache, and the
consequence was not a cosmetic count: ``schema_design_available`` came back False
for a cache holding a complete set of key designs, the cache fell out of
``engines_with_workload``, and the report recommended an architecture with no
cache in it while the same report's ``schema_summaries`` said the Redis design was
``completed``.

The two preconditions behave differently and both are covered here:

- zero assigned queries (the common case for a cache layer — the assignment
  routes queries to the store that owns the data) degraded ``architecture_type``
  all the way to SINGLE_DATABASE;
- non-zero assigned queries kept ``architecture_type`` correct but still dropped
  the cache from ``recommended_architecture.databases``, because that list is
  gated on table mappings, which had no ElastiCache branch either.
"""

from __future__ import annotations

import pytest

from src.agents.referee.schema_shapes import design_count, design_table_defs, is_cache_engine
from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import (
    build_architecture_recommendation,
    build_ranking,
    build_table_mappings,
)

# Shaped after the validated contract in src/contracts/elasticache_model_output.py:
# key_designs (min_length=1) and access_patterns, never table_definitions.
_EC_SCHEMA = {
    "key_designs": [
        {
            "key_pattern": "session:{session_id}",
            "data_type": "hash",
            "source_tables": ["wp_usermeta"],
            "ttl_seconds": 300,
        },
        {
            "key_pattern": "post:{post_id}",
            "data_type": "string",
            "source_tables": ["wp_posts"],
            "ttl_seconds": 600,
        },
    ],
    "access_patterns": [
        {"pattern_id": "EC-AP-1", "pattern_group": "session_lookup"},
        {"pattern_id": "EC-AP-2", "pattern_group": "post_read"},
    ],
    "validation_passed": True,
}

_DDB_SCHEMA = {
    "table_definitions": [
        {
            "table_name": "Posts",
            "source_tables": ["wp_posts"],
            "aggregate_pattern": "single_table",
        },
    ],
    "access_patterns": [{"pattern_id": "DDB-AP-1", "pattern_group": "post_read"}],
    "validation_passed": True,
}

_DDB_ANALYSIS = {
    "table_recommendations": [{"table_id": "wp_posts", "confidence_score": 82}],
    "workload_analysis": {"patterns_detected": ["key_value"]},
    "cost_estimate": {"monthly_cost_usd": 120.0},
}

# Redis analysis scores the tables it would cache, and those scores can beat the
# store's — 95 > 82 here on purpose, to pin that the cache still does not become
# wp_posts' recommended_database.
_EC_ANALYSIS = {
    "table_recommendations": [
        {"table_id": "wp_posts", "confidence_score": 95},
        {"table_id": "wp_usermeta", "confidence_score": 90},
    ],
    "workload_analysis": {"patterns_detected": ["cacheable_reads"]},
    "cost_estimate": {"monthly_cost_usd": 45.0},
}

# (engine, artifact, number of target objects it defines) — one row per engine
# the map knows, so a new engine added without a row shows up as a gap here.
_DESIGN_FIELDS_BY_ENGINE: list[tuple[str, dict, int]] = [
    ("dynamodb", {"table_definitions": [{}, {}]}, 2),
    ("aurora_postgresql", {"table_definitions": [{}]}, 1),
    ("aurora_mysql", {"table_definitions": [{}]}, 1),
    ("documentdb", {"collections": [{}, {}, {}]}, 3),
    ("elasticache", {"key_designs": [{}]}, 1),
    ("opensearch", {"index_designs": [{}], "data_stream_designs": [{}]}, 2),
]

_ASSIGNMENT_WITH_CACHE_QUERIES = {
    "query_assignments": [
        {"assigned_engine": "dynamodb", "assignment_reason": "key-value access", "in_scope": True},
        {"assigned_engine": "elasticache", "assignment_reason": "hot read", "in_scope": True},
    ],
    "table_assignments": [{"primary_engine": "dynamodb"}],
}


def _data(assignment=None) -> SynthesisData:
    return SynthesisData(
        job_id="job-130",
        database_name="wordpress",
        assignment=assignment,
        engines={
            "dynamodb": EngineArtifacts(
                engine="dynamodb", analysis=_DDB_ANALYSIS, schema_design=_DDB_SCHEMA
            ),
            "elasticache": EngineArtifacts(
                engine="elasticache", analysis=_EC_ANALYSIS, schema_design=_EC_SCHEMA
            ),
        },
    )


def _entry(ranking: list[dict], engine: str) -> dict:
    return next(r for r in ranking if r["target"] == engine)


class TestRanking:
    def test_key_designs_count_as_a_schema_design(self) -> None:
        ranking = build_ranking(_data())
        elasticache = _entry(ranking, "elasticache")

        assert elasticache["schema_design_available"] is True
        assert elasticache["target_tables"] == 2

    def test_other_engines_still_counted_from_their_own_field(self) -> None:
        ranking = build_ranking(_data())

        assert _entry(ranking, "dynamodb")["target_tables"] == 1


class TestArchitectureWithZeroAssignedQueries:
    """A cache layer normally wins no queries — the assignment routes them to the
    store that owns the data. That is precisely when the old code collapsed the
    architecture to a single database."""

    def test_cache_is_recognised_without_any_assignment(self) -> None:
        data = _data()
        ranking = build_ranking(data)
        mappings = build_table_mappings(data)

        architecture = build_architecture_recommendation(data, ranking, mappings)

        assert architecture["architecture_type"] == "HYBRID_WITH_CACHE"
        assert "elasticache" in {d["service"] for d in architecture["databases"]}

    def test_rationale_names_the_cache(self) -> None:
        """HYBRID_WITH_CACHE whose rationale never mentions a cache is the same
        defect one layer down: the type came from engines_with_workload while the
        sentence came from the databases list the cache had been dropped from."""
        data = _data()
        ranking = build_ranking(data)
        mappings = build_table_mappings(data)

        rationale = build_architecture_recommendation(data, ranking, mappings)["rationale"]

        assert "elasticache" in rationale
        assert "dynamodb" in rationale


class TestArchitectureWithAssignedQueries:
    """Assigned queries alone put the cache into engines_with_workload, so
    architecture_type was already right here — but databases is gated on table
    mappings, which had no ElastiCache branch, so the cache was still missing."""

    def test_cache_reaches_the_databases_list(self) -> None:
        data = _data(_ASSIGNMENT_WITH_CACHE_QUERIES)
        ranking = build_ranking(data)
        mappings = build_table_mappings(data)

        architecture = build_architecture_recommendation(data, ranking, mappings)
        cache = next(d for d in architecture["databases"] if d["service"] == "elasticache")

        assert architecture["architecture_type"] == "HYBRID_WITH_CACHE"
        assert sorted(cache["tables"]) == ["wp_posts", "wp_usermeta"]
        assert cache["table_count"] == 2


class TestCacheIsNeverAMigrationTarget:
    def test_store_keeps_the_primary_slot_despite_a_lower_score(self) -> None:
        """A cache fronts a store; it is not a table's destination. Letting it win
        the primary slot would print "migrate wp_posts to elasticache"."""
        mappings = build_table_mappings(_data())
        wp_posts = next(m for m in mappings if m["source_table"] == "wp_posts")

        assert wp_posts["recommended_database"] == "dynamodb"
        assert [alt["database"] for alt in wp_posts["alternatives"]] == ["elasticache"]

    def test_cache_wins_when_no_store_designed_the_table(self) -> None:
        """In a cache-only assessment it is the only answer there is, and saying
        nothing about the table would be worse than naming the cache."""
        data = SynthesisData(
            job_id="job-130",
            database_name="wordpress",
            engines={
                "elasticache": EngineArtifacts(
                    engine="elasticache", analysis=_EC_ANALYSIS, schema_design=_EC_SCHEMA
                )
            },
        )

        mappings = build_table_mappings(data)

        assert {m["recommended_database"] for m in mappings} == {"elasticache"}

    def test_mapping_carries_the_key_pattern_as_the_target(self) -> None:
        mappings = build_table_mappings(_data())
        usermeta = next(m for m in mappings if m["source_table"] == "wp_usermeta")

        assert usermeta["target_table"] == "session:{session_id}"
        assert usermeta["aggregate_pattern"] == "cache_key"


class TestSchemaShapes:
    @pytest.mark.parametrize("engine,schema,expected", _DESIGN_FIELDS_BY_ENGINE)
    def test_every_engine_design_field_is_counted(self, engine, schema, expected) -> None:
        """One map, so adding an engine cannot silently skip a caller. Counts are
        of the engine's own field, never of table_definitions."""
        assert design_count(engine, schema) == expected

    def test_missing_and_empty_designs_read_as_no_design(self) -> None:
        assert design_count("elasticache", {}) == 0
        assert design_count("elasticache", {"key_designs": []}) == 0
        assert design_count("elasticache", {"key_designs": None}) == 0

    def test_unknown_engine_falls_back_rather_than_raising(self) -> None:
        """Synthesis must not fail a phase over an engine the map has not met."""
        assert design_count("neptune", {"table_definitions": [{}]}) == 1
        assert design_count("neptune", {"vertex_designs": [{}]}) == 0

    def test_normalisation_uses_each_engines_own_name_key(self) -> None:
        opensearch = design_table_defs(
            "opensearch",
            {
                "index_designs": [{"index_name": "posts-idx", "source_tables": ["wp_posts"]}],
                "data_stream_designs": [{"data_stream_name": "logs-ds", "source_tables": ["logs"]}],
            },
        )

        assert [(d["table_name"], d["aggregate_pattern"]) for d in opensearch] == [
            ("posts-idx", "search_index"),
            ("logs-ds", "data_stream"),
        ]

    def test_record_keeps_its_own_aggregate_pattern_when_it_has_one(self) -> None:
        defs = design_table_defs(
            "dynamodb",
            {"table_definitions": [{"table_name": "Posts", "aggregate_pattern": "single_table"}]},
        )

        assert defs[0]["aggregate_pattern"] == "single_table"
        assert defs[0]["source_tables"] == []

    def test_cache_engines_are_matched_by_name_not_substring_alone(self) -> None:
        """memorydb is a cache without "cache" in its name; dynamodb is not one."""
        assert is_cache_engine("elasticache")
        assert is_cache_engine("ElastiCache")
        assert is_cache_engine("memorydb")
        assert not is_cache_engine("dynamodb")
        assert not is_cache_engine("aurora_postgresql")
