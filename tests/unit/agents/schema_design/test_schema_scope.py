"""Schema design scope validation (issue #203).

A schema design may only reference the source tables and query IDs assigned
(in scope) to its engine in the effective assignment. ``validate_schema_scope``
reports every designed reference outside that scope; finalize, ``--merge`` and
the Bedrock path turn those into ``validation_passed=false`` plus a
``validation_failed`` status. ``unsupported_patterns`` IDs are warnings only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.schema_design.scope import (
    SCOPE_PREFIX,
    SCOPE_WARNING_PREFIX,
    apply_scope_violations,
    assess_schema_scope,
    validate_schema_scope,
)

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


def test_synthetic_table_owner_assignment_flags_cross_engine_tables():
    """The DynamoDB design from the issue #203 run against a SYNTHETIC assignment.

    The assignment is invented (one query per table, owned by that table's
    ``recommended_database`` in the run's ``table_mappings``), i.e. the per-table
    ownership the rubric judge assumed. It is not the run's real assignment: in
    that one, in-scope DynamoDB queries access both tables, so they are in scope
    (see ``test_real_wordpress_dynamodb_design_is_in_scope``). Under the assumed
    ownership, the two cross-engine tables are what gets flagged.
    """
    evidence = json.loads((FIXTURES / "issue_203_synthetic_table_owner_scope.json").read_text())
    messages = validate_schema_scope(
        "dynamodb", evidence["dynamodb_design"], evidence["assignment"]
    )

    assert _mentions(
        messages, "wordpress.wp_woocommerce_order_items", "WpOrderItems", "elasticache"
    )
    assert _mentions(messages, "wordpress.wp_postmeta", "WpPostContent", "aurora_mysql")
    assert len(messages) == 2


def test_real_wordpress_dynamodb_design_is_in_scope():
    """Regression: a real merged DynamoDB design and the real Reality Check v2
    assignment it was designed from (wordpress sample, trimmed) are in scope."""
    real = json.loads((FIXTURES / "wordpress_real_dynamodb_scope.json").read_text())
    report = assess_schema_scope("dynamodb", real["dynamodb_design"], real["assignment"])
    assert report.violations == []
    assert report.warnings == []


# ---------------------------------------------------------------------------
# unsupported_patterns are warnings, not violations
# ---------------------------------------------------------------------------

_UNSUPPORTED_FIELD = {
    "dynamodb": "query_ids",
    "documentdb": "source_query_ids",
    "opensearch": "query_ids",
    "elasticache": "source_query_ids",
}


@pytest.mark.parametrize("engine", sorted(_UNSUPPORTED_FIELD))
@pytest.mark.parametrize("qid,reason", [("q-orders", "assigned to"), ("q-ghost", "not in")])
def test_unsupported_pattern_ids_are_warnings_only(engine, qid, reason):
    build, other = _ENGINES[engine]
    design = build("mydb.users", "q-users")
    design["unsupported_patterns"] = [{_UNSUPPORTED_FIELD[engine]: [qid]}]

    report = assess_schema_scope(engine, design, _assignment(engine, other))

    assert report.violations == []
    assert len(report.warnings) == 1
    assert report.warnings[0].startswith(SCOPE_WARNING_PREFIX)
    assert qid in report.warnings[0] and reason in report.warnings[0]


# ---------------------------------------------------------------------------
# Exempt fields: legitimately name other engines' tables and queries
# ---------------------------------------------------------------------------


def test_dynamodb_attribute_source_table_is_not_checked():
    """A denormalized attribute copies a column from a table another engine owns."""
    design = _dynamodb("mydb.users", "q-users")
    design["table_definitions"][0]["attributes"] = [
        {"name": "order_total", "source_table": "mydb.orders", "denormalized": True}
    ]
    design["table_definitions"][0]["entities"][0]["attributes"] = [
        {"name": "last_order", "source_table": "mydb.orders"}
    ]
    assert validate_schema_scope("dynamodb", design, _assignment("dynamodb", "elasticache")) == []


@pytest.mark.parametrize("engine", ["documentdb", "opensearch"])
def test_migration_notes_and_trade_offs_are_not_checked(engine):
    build, other = _ENGINES[engine]
    design = build("mydb.users", "q-users")
    design["migration_notes"] = [{"source_table": "mydb.orders", "note": "orders stay put"}]
    design["trade_offs"] = [
        {"description": "d", "source_tables": ["mydb.orders"], "query_ids": ["q-orders"]}
    ]
    assert validate_schema_scope(engine, design, _assignment(engine, other)) == []


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
def test_aurora_foreign_key_to_table_owned_elsewhere_is_not_checked(engine):
    design = {
        "table_definitions": [
            {
                "table_name": "users",
                "foreign_keys": [
                    "ALTER TABLE users ADD FOREIGN KEY (order_id) REFERENCES orders(id)"
                ],
                "indexes": ["CREATE INDEX idx_orders ON orders (id)"],
            }
        ]
    }
    assert validate_schema_scope(engine, design, _assignment(engine, "dynamodb")) == []


# ---------------------------------------------------------------------------
# Robustness and re-checks
# ---------------------------------------------------------------------------


def test_non_string_query_ids_and_tables_in_assignment_do_not_crash():
    assignment = {
        "query_assignments": [
            {"query_id": 42, "assigned_engine": "dynamodb", "source_tables": ["mydb.users", 7]},
            {"query_id": None, "assigned_engine": "dynamodb", "source_tables": "mydb.x"},
            "garbage",
        ]
    }
    design = _dynamodb("mydb.users", "42")
    assert validate_schema_scope("dynamodb", design, assignment) == []


def test_scope_messages_carry_a_stable_prefix():
    messages = validate_schema_scope(
        "dynamodb", _dynamodb("mydb.orders", "q-orders"), _assignment("dynamodb", "elasticache")
    )
    assert messages and all(m.startswith(SCOPE_PREFIX) for m in messages)


def test_apply_replaces_stale_scope_messages_and_restores_passed():
    stale = f"{SCOPE_PREFIX}dynamodb: source table 'mydb.orders' ... Remove it from the design."
    output = {"validation_passed": False, "validation_failures": [stale]}

    cleared = apply_scope_violations(output, [])
    assert cleared["validation_passed"] is True
    assert cleared["validation_failures"] == []

    fresh = f"{SCOPE_PREFIX}dynamodb: query 'q-orders' ..."
    replaced = apply_scope_violations(output, [fresh])
    assert replaced["validation_passed"] is False
    assert replaced["validation_failures"] == [fresh]


def test_apply_keeps_other_failures_and_their_verdict():
    stale = f"{SCOPE_PREFIX}dynamodb: query 'q-orders' ..."
    output = {"validation_passed": False, "validation_failures": ["hot partition", stale]}
    result = apply_scope_violations(output, [])
    assert result["validation_passed"] is False
    assert result["validation_failures"] == ["hot partition"]


def test_apply_without_stale_messages_leaves_the_verdict_alone():
    output = {"validation_passed": False, "validation_failures": []}
    assert apply_scope_violations(output, []) == output
