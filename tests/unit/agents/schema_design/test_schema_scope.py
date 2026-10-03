"""Schema design scope validation (issue #203).

A schema design may only reference the source tables and query IDs assigned
(in scope) to its engine in the effective assignment. ``validate_schema_scope``
reports every reference outside that scope; finalize and ``--merge`` turn those
reports into ``validation_passed=false`` plus a ``validation_failed`` status.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.schema_design.scope import validate_schema_scope

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


def _assignment(engine: str, other: str) -> dict:
    """``users`` (q-users) is in scope for ``engine``; ``orders`` (q-orders) is on
    ``other``; ``legacy`` (q-legacy) is assigned to ``engine`` but out of scope."""
    return {
        "version": 2,
        "query_assignments": [
            {
                "query_id": "q-users",
                "assigned_engine": engine,
                "source_tables": ["mydb.users"],
                "in_scope": True,
            },
            {
                "query_id": "q-orders",
                "assigned_engine": other,
                "source_tables": ["mydb.orders"],
                "in_scope": True,
            },
            {
                "query_id": "q-legacy",
                "assigned_engine": engine,
                "source_tables": ["mydb.legacy"],
                "in_scope": False,
            },
        ],
    }


def _mentions(messages: list[str], *needles: str) -> bool:
    return any(all(n in m for n in needles) for m in messages)


# ---------------------------------------------------------------------------
# Per-engine designs: (in_scope_design, out_of_scope_design)
# ---------------------------------------------------------------------------


def _dynamodb(table: str, query: str) -> dict:
    return {
        "table_definitions": [
            {
                "table_name": "Main",
                "source_tables": [table],
                "entities": [{"entity_type": "User", "source_table": table}],
            }
        ],
        "access_patterns": [
            {"pattern_id": "DDB-AP-1", "query_ids": [query], "source_tables": [table]}
        ],
        "unsupported_patterns": [{"query_ids": [query]}],
    }


def _documentdb(table: str, query: str) -> dict:
    return {
        "collections": [
            {
                "collection_name": "users",
                "source_tables": [table],
                "embedded_entities": [{"source_table": table}],
                "indexes": [{"index_name": "idx", "source_query_ids": [query]}],
            }
        ],
        "access_patterns": [
            {"pattern_id": "DOC-AP-1", "source_query_ids": [query], "source_tables": [table]}
        ],
        "unsupported_patterns": [{"source_query_ids": [query]}],
    }


def _opensearch(table: str, query: str) -> dict:
    return {
        "index_designs": [{"index_name": "users", "source_tables": [table]}],
        "data_stream_designs": [{"data_stream_name": "logs", "source_tables": [table]}],
        "access_patterns": [
            {"pattern_id": "OS-AP-1", "query_ids": [query], "source_tables": [table]}
        ],
        "unsupported_patterns": [{"query_ids": [query]}],
    }


def _elasticache(table: str, query: str) -> dict:
    return {
        "key_designs": [{"key_pattern": "user:{id}", "source_tables": [table]}],
        "access_patterns": [
            {"pattern_id": "EC-AP-1", "source_query_ids": [query], "source_tables": [table]}
        ],
        "unsupported_patterns": [{"source_query_ids": [query]}],
    }


def _aurora(table: str, _query: str) -> dict:
    # Aurora tables carry over 1:1, so table_name is the source table name.
    return {"table_definitions": [{"table_name": table.split(".")[-1]}]}


_ENGINES = {
    "dynamodb": (_dynamodb, "elasticache"),
    "documentdb": (_documentdb, "dynamodb"),
    "opensearch": (_opensearch, "aurora_mysql"),
    "elasticache": (_elasticache, "dynamodb"),
    "aurora_mysql": (_aurora, "dynamodb"),
    "aurora_postgresql": (_aurora, "dynamodb"),
}

_HAS_QUERY_IDS = {"dynamodb", "documentdb", "opensearch", "elasticache"}


@pytest.mark.parametrize("engine", sorted(_ENGINES))
def test_in_scope_design_passes(engine):
    build, other = _ENGINES[engine]
    assert (
        validate_schema_scope(engine, build("mydb.users", "q-users"), _assignment(engine, other))
        == []
    )


@pytest.mark.parametrize("engine", sorted(_ENGINES))
def test_table_assigned_to_another_engine_is_flagged(engine):
    build, other = _ENGINES[engine]
    messages = validate_schema_scope(
        engine, build("mydb.orders", "q-users"), _assignment(engine, other)
    )
    # Aurora designs spell the bare table name; the rest keep the `mydb.` prefix.
    assert _mentions(messages, "orders'", f"Out of scope for {engine}", other)
    assert all("q-users" not in m for m in messages)


@pytest.mark.parametrize("engine", sorted(_HAS_QUERY_IDS))
def test_query_assigned_to_another_engine_is_flagged(engine):
    build, other = _ENGINES[engine]
    messages = validate_schema_scope(
        engine, build("mydb.users", "q-orders"), _assignment(engine, other)
    )
    assert _mentions(messages, "q-orders", other)
    assert all("mydb.users" not in m for m in messages)


@pytest.mark.parametrize("engine", sorted(_HAS_QUERY_IDS))
def test_out_of_scope_query_for_same_engine_is_flagged(engine):
    build, other = _ENGINES[engine]
    messages = validate_schema_scope(
        engine, build("mydb.users", "q-legacy"), _assignment(engine, other)
    )
    assert _mentions(messages, "q-legacy", "out of scope")


@pytest.mark.parametrize("engine", sorted(_HAS_QUERY_IDS))
def test_unknown_query_is_flagged(engine):
    build, other = _ENGINES[engine]
    messages = validate_schema_scope(
        engine, build("mydb.users", "q-made-up"), _assignment(engine, other)
    )
    assert _mentions(messages, "q-made-up", "not in the assignment")


@pytest.mark.parametrize("engine", sorted(_ENGINES))
def test_table_names_match_with_or_without_db_prefix(engine):
    build, other = _ENGINES[engine]
    assignment = _assignment(engine, other)
    # Design omits the `<db>.` prefix the assignment uses.
    assert validate_schema_scope(engine, build("users", "q-users"), assignment) == []
    # Assignment omits the prefix the design uses.
    for qa in assignment["query_assignments"]:
        qa["source_tables"] = [t.split(".")[-1] for t in qa["source_tables"]]
    assert validate_schema_scope(engine, build("mydb.users", "q-users"), assignment) == []
    # Case and identifier quoting do not matter either.
    assert validate_schema_scope(engine, build("`MyDB`.`Users`", "q-users"), assignment) == []


def test_each_violation_is_reported_once_with_its_locations():
    design = _dynamodb("mydb.orders", "q-orders")
    messages = validate_schema_scope("dynamodb", design, _assignment("dynamodb", "elasticache"))
    assert len([m for m in messages if "'mydb.orders'" in m]) == 1
    assert len([m for m in messages if "'q-orders'" in m]) == 1
    table_msg = next(m for m in messages if "'mydb.orders'" in m)
    assert "table_definitions[Main]" in table_msg
    assert "access_patterns[DDB-AP-1]" in table_msg


def test_cross_engine_fields_are_not_checked():
    """Trade-offs, migration notes and cache-invalidation write queries legitimately
    name other engines' tables and queries; they are not design scope."""
    design = {
        "key_designs": [{"key_pattern": "user:{id}", "source_tables": ["mydb.users"]}],
        "cache_invalidation": [
            {"key_pattern": "user:{id}", "source_write_query_ids": ["q-orders"]}
        ],
        "migration_notes": [{"source_table": "mydb.orders"}],
        "trade_offs": [{"source_tables": ["mydb.orders"], "query_ids": ["q-orders"]}],
    }
    assert (
        validate_schema_scope("elasticache", design, _assignment("elasticache", "dynamodb")) == []
    )


def test_unknown_engine_or_malformed_output_does_not_crash():
    assignment = _assignment("dynamodb", "elasticache")
    assert validate_schema_scope("keyspaces", {"tables": [1]}, assignment) == []
    assert validate_schema_scope("dynamodb", {"table_definitions": "nope"}, assignment) == []
    assert (
        validate_schema_scope(
            "dynamodb", {"table_definitions": [None, {"source_tables": None}]}, assignment
        )
        == []
    )


def test_issue_203_wordpress_dynamodb_design_is_flagged():
    """The rubric judge's finding on the real wordpress run: WpOrderItems maps
    wp_woocommerce_order_items (ElastiCache) and WpPostContent includes
    wp_postmeta (Aurora MySQL)."""
    evidence = json.loads((FIXTURES / "issue_203_wordpress_scope.json").read_text())
    messages = validate_schema_scope(
        "dynamodb", evidence["dynamodb_design"], evidence["assignment"]
    )

    assert _mentions(
        messages, "wordpress.wp_woocommerce_order_items", "WpOrderItems", "elasticache"
    )
    assert _mentions(messages, "wordpress.wp_postmeta", "WpPostContent", "aurora_mysql")
    assert len(messages) == 2
