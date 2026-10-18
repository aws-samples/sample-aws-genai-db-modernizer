"""DocumentDB's variant-attribute fits test an access pattern, not a column count (#920).

``product-catalog``, ``polymorphic-data`` and ``content-management`` describe a real
document-model strength: an entity whose attribute set varies (by type or category, or
as attribute rows), read as one document by its key. They used to fire on schema shape
alone (more than 15 columns, more than 30% nullable columns, a column named ``type``),
which on discourse put them on 855 to 1029 of 1654 queries, deletes and unanchored
filters included. Nullable columns are ordinary relational design, not sparseness, and
the collector reports no null fraction, so nullability is never evidence.

Each pattern now needs a key-anchored read (the access-pattern class is a key lookup or
a key-scoped read, ADR-031 Decision 14 Amendment 5) of an entity with variable
attributes:

- ``polymorphic-data``: the read pins an attribute table's entity column (an
  entity-attribute-value table: entity key, attribute name, value);
- ``content-management``: the read joins a typed entity (a discriminator column) to its
  attribute rows on the entity key, the entity and its variable fields in one read;
- ``product-catalog``: the read fetches a typed entity whose variable attributes are a
  JSON column.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.tools.analysis.documentdb_analysis_tools import analyze_documentdb_use_cases

# #967: the three reads are one pattern, variant-attributes.
VARIANT_PATTERNS = ("variant-attributes",)


def _col(name: str, data_type: str = "varchar", nullable: bool = True) -> dict[str, Any]:
    normalized = {"varchar": "string", "text": "text", "bigint": "integer", "json": "json"}
    return {
        "column_name": name,
        "data_type": data_type,
        "normalized_data_type": normalized.get(data_type),
        "nullable": nullable,
    }


def _table(
    name: str, columns: list[dict], indexes: tuple[tuple[list[str], bool], ...] = ()
) -> dict[str, Any]:
    return {
        "table_id": f"public.{name}",
        "table_name": name,
        "schema_name": "public",
        "row_count": 500_000,
        "size_mb": 400.0,
        "columns": columns,
        "primary_key": ["id"],
        "foreign_keys": [],
        "indexes": [
            {"index_name": f"{name}_pkey", "columns": ["id"], "is_unique": True, "is_primary": True}
        ]
        + [
            {"index_name": f"{name}_{i}", "columns": cols, "is_unique": unique}
            for i, (cols, unique) in enumerate(indexes)
        ],
    }


# A typed content table: a discriminator, and wide and mostly nullable like most
# Rails and WordPress tables.
POSTS = _table(
    "posts",
    [_col("id", "bigint", False), _col("post_type"), _col("title"), _col("body", "text")]
    + [_col(f"extra_{i}") for i in range(16)],
)
# Its attribute rows: entity key, attribute name, value.
POST_FIELDS = _table(
    "post_custom_fields",
    [_col("id", "bigint", False), _col("post_id", "bigint"), _col("name"), _col("value", "text")],
    indexes=((["post_id"], False), (["name"], False)),
)
# A typed entity whose variable attributes are one JSON column.
PRODUCTS = _table(
    "products",
    [_col("id", "bigint", False), _col("category"), _col("title"), _col("attributes", "json")],
)
# Column shape only: wide, mostly nullable, a column named "type", no attribute rows and
# no JSON column. Every old detector fired on it.
THEMES = _table(
    "themes",
    [_col("id", "bigint", False), _col("type"), _col("component"), _col("started_at")]
    + [_col(f"setting_{i}") for i in range(20)],
)


def _query(qid: str, sql: str, tables: list[str], query_type: str = "SELECT") -> dict[str, Any]:
    joins = max(len(tables) - 1, 0)
    return {
        "query_id": qid,
        "query_text": sql,
        "query_type": query_type,
        "calls_per_second": 2.0,
        "frequency_per_hour": 7200,
        "tables_accessed": [f"public.{t}" for t in tables],
        "rows_returned_avg": 5.0,
        "has_joins": joins > 0,
        "join_count": joins,
    }


def _fired(*queries: dict) -> dict[str, set[str]]:
    collector = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [POSTS, POST_FIELDS, PRODUCTS, THEMES]},
        "queries": {"query_patterns": list(queries)},
    }
    analysis = analyze_documentdb_use_cases(collector)
    return {p.pattern_type: set(p.query_ids) for p in analysis.patterns_detected}


def _patterns_of(query: dict) -> set[str]:
    fired = _fired(query)
    return {p for p in VARIANT_PATTERNS if query["query_id"] in fired.get(p, set())}


POSITIVE = [
    (
        "variant-attributes",
        _query(
            "eav-read",
            "SELECT post_id, name, value FROM post_custom_fields WHERE post_id IN ($1, $2)",
            ["post_custom_fields"],
        ),
    ),
    (
        "variant-attributes",
        _query(
            "entity-with-fields",
            "SELECT p.id, p.title, f.name, f.value FROM posts p "
            "JOIN post_custom_fields f ON f.post_id = p.id WHERE p.id = $1",
            ["posts", "post_custom_fields"],
        ),
    ),
    (
        "variant-attributes",
        _query(
            "typed-document",
            "SELECT id, category, attributes FROM products WHERE id = $1",
            ["products"],
        ),
    ),
]

NEGATIVE = [
    (
        "variant-attributes",
        # One attribute across every entity: not one entity's attribute set.
        _query(
            "attribute-across-entities",
            "SELECT post_id, value FROM post_custom_fields WHERE name = $1",
            ["post_custom_fields"],
        ),
    ),
    (
        "variant-attributes",
        # The typed entity alone, without its variable fields.
        _query("entity-alone", "SELECT * FROM posts WHERE id = $1", ["posts"]),
    ),
    (
        "variant-attributes",
        # An aggregate over the catalog, not a read of one entity.
        _query(
            "catalog-aggregate",
            "SELECT category, COUNT(*) FROM products GROUP BY category",
            ["products"],
        ),
    ),
]


@pytest.mark.parametrize(("pattern", "query"), POSITIVE, ids=[q["query_id"] for _, q in POSITIVE])
def test_a_key_anchored_read_of_variable_attributes_fires_the_pattern(pattern, query):
    assert pattern in _patterns_of(query)


@pytest.mark.parametrize(("pattern", "query"), NEGATIVE, ids=[q["query_id"] for _, q in NEGATIVE])
def test_the_same_tables_without_the_access_pattern_do_not_fire_it(pattern, query):
    assert pattern not in _patterns_of(query)


@pytest.mark.parametrize(
    "query",
    [
        _query("theme-by-key", "SELECT * FROM themes WHERE id = $1", ["themes"]),
        _query(
            "theme-filter", 'SELECT "themes".* FROM "themes" WHERE "component" = $1', ["themes"]
        ),
        _query(
            "theme-delete",
            "DELETE FROM themes WHERE started_at < $1",
            ["themes"],
            query_type="DELETE",
        ),
    ],
    ids=lambda q: q["query_id"],
)
def test_a_column_shape_only_table_fires_no_variant_attribute_pattern(query):
    # More than 15 columns, nearly all nullable, a column named "type": the shape that
    # used to fire all three patterns, with no attribute rows and no JSON attributes.
    assert _patterns_of(query) == set()


def test_writes_and_unanchored_reads_of_attribute_rows_do_not_fire():
    for query in (
        _query(
            "eav-delete",
            "DELETE FROM post_custom_fields WHERE post_id = $1",
            ["post_custom_fields"],
            query_type="DELETE",
        ),
        _query(
            "eav-scan",
            "SELECT * FROM post_custom_fields ORDER BY id LIMIT 50",
            ["post_custom_fields"],
        ),
        _query(
            "eav-group",
            "SELECT name, COUNT(*) FROM post_custom_fields GROUP BY name",
            ["post_custom_fields"],
        ),
    ):
        assert _patterns_of(query) == set(), query["query_id"]


def test_the_patterns_cite_the_document_model_limits_they_depend_on():
    from src.shared.catalog.patterns import pattern

    for pattern_id in VARIANT_PATTERNS:
        why = pattern(pattern_id).engines["documentdb"].why
        assert "16 MB" in why, pattern_id
        assert "wildcard" in why.lower(), pattern_id
        assert "Developer Guide" in why, pattern_id


def test_the_trace_lists_the_tables_the_variant_facts_name():
    """#967: the decision trace's polymorphic_tables reads the same schema facts as the
    pattern (an attribute table, or a typed entity with a JSON column), never nullability
    or a column named type."""
    from src.tools.analysis.documentdb_analysis_tools import detect_polymorphic_tables

    collector = {"database_schema": {"tables": [POSTS, POST_FIELDS, PRODUCTS, THEMES]}}
    assert detect_polymorphic_tables(collector) == ["public.post_custom_fields", "public.products"]


LEGACY_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "documentdb_legacy_ids"


def test_an_analysis_saved_before_the_merge_routes_like_the_new_one():
    """#967 merged six pattern ids into two. ``analysis-old-ids.json`` is a saved
    DocumentDB analysis with the old ids (product-catalog/documentdb-02,
    polymorphic-data/documentdb-03, flexible-schema, write-time-aggregation/documentdb-08);
    ``analysis-new-ids.json`` is the same analysis with today's ids and the same table
    recommendations. Each loads as the analysis contract from disk, and the resolver's
    assignment for the old file equals the new file's, query by query."""
    from src.agents.referee.assignment_resolver import AssignmentResolver
    from src.contracts.analysis_output import AnalysisOutputContract

    collector = json.loads((LEGACY_DIR / "collection.json").read_text())
    triage = {
        "selected_agents": [{"agent_type": "documentdb"}, {"agent_type": "aurora_postgresql"}],
        "signals": [],
    }
    aurora = {
        "table_recommendations": [
            {"table_id": t["table_id"], "confidence_score": 60}
            for t in collector["database_schema"]["tables"]
        ],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }

    def routed(name: str) -> list[dict]:
        saved = json.loads((LEGACY_DIR / name).read_text())
        AnalysisOutputContract.model_validate(saved)  # the saved artifact still loads
        outputs = {"documentdb": saved, "aurora_postgresql": aurora}
        assignment = AssignmentResolver().resolve(triage, outputs, collector)
        return [qa.model_dump(mode="json") for qa in assignment.query_assignments]

    old_types = {
        p["pattern_type"]
        for p in json.loads((LEGACY_DIR / "analysis-old-ids.json").read_text())[
            "workload_analysis"
        ]["patterns_detected"]
    }
    assert old_types == {
        "product-catalog",
        "polymorphic-data",
        "flexible-schema",
        "write-time-aggregation",
    }
    old, new = routed("analysis-old-ids.json"), routed("analysis-new-ids.json")
    assert len(old) == 3
    assert old == new


@pytest.mark.parametrize(
    ("text", "fires"),
    [
        ("SELECT category, COUNT(*) FROM products GROUP BY category", True),
        ("SELECT COUNT(*) FROM products WHERE attributes IS NOT NULL", True),
        # One key's rows: an indexed count, not a $group pipeline (#967).
        ("SELECT COUNT(*) FROM products WHERE id = $1", False),
        # A GROUP BY that only de-duplicates is no aggregation.
        ("SELECT category FROM products GROUP BY category", False),
        # Review of #1093: a window function or FOUND_ROWS() alone is no aggregate.
        ("SELECT id, LAG(category) OVER (ORDER BY id) FROM products", False),
        ("SELECT id, ROW_NUMBER() OVER (PARTITION BY category ORDER BY id) FROM products", False),
        ("SELECT FOUND_ROWS()", False),
    ],
)
def test_aggregation_pipeline_needs_a_grouped_or_unanchored_aggregate(text, fires):
    fired = _fired(_query("agg", text, ["products"]))
    assert ("agg" in fired.get("aggregation-pipeline", set())) is fires
