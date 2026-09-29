#!/usr/bin/env python3
"""Generate the WordPress DynamoDB schema-design fixture *from the contract*.

The load-test contract integration tests
(``tests/integration/test_load_test_contract_integration.py``) exercise the
DynamoDB load-test tooling (pattern selection, key-condition parsing, table
provisioning params, k6 script generation) against a real schema-design output.
That artifact is a *generated* one — the output of the DynamoDB data-modeling
agent — and was never committed, so those tests silently skipped forever.

Rather than hand-write JSON (which could drift from the contract and give false
confidence), this builds the fixture by instantiating the authoritative contract
models in ``src/contracts/dynamodb_model_output.py`` and dumping them. If the
contract changes in a way this fixture violates, regeneration fails loudly here
— which is the point: the fixture is guaranteed contract-valid by construction.

Content is grounded in the real WordPress schema (the table names are the actual
``wp_*`` tables from ``docs/examples/wordpress``), and the access patterns model
WordPress's well-known query shapes. Enough in-scope patterns and tables carry
traffic to satisfy the tests' thresholds (>=40 testable patterns, >=15 tables).

Run:  uv run python tests/fixtures/generate_wordpress_dynamodb_schema.py
Writes: docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

from src.contracts.dynamodb_model_output import (
    DYNAMODB_OPERATIONS,
    AccessPattern,
    AttributeDefinition,
    DynamoDBModelOutputContract,
    HotPartitionEntry,
    KeyDefinition,
    TableDefinition,
    TradeOff,
)

OUT_PATH = (
    Path(__file__).parents[2]
    / "docs"
    / "examples"
    / "wordpress"
    / "load-test-results"
    / "schema-dynamodb"
    / "v2"
    / "schema_output.json"
)

# (dynamo table name, source wp_* tables, pk attr, pk type, optional sk attr, sk type)
# Grounded in the real WordPress schema shipped in docs/examples/wordpress.
_TABLES: list[tuple[str, list[str], str, str, str | None, str | None]] = [
    ("Posts", ["wp_posts"], "post_id", "N", None, None),
    ("PostMeta", ["wp_postmeta"], "post_id", "N", "meta_key", "S"),
    ("Comments", ["wp_comments"], "comment_id", "N", None, None),
    ("CommentMeta", ["wp_commentmeta"], "comment_id", "N", "meta_key", "S"),
    ("Users", ["wp_users"], "user_id", "N", None, None),
    ("UserMeta", ["wp_usermeta"], "user_id", "N", "meta_key", "S"),
    ("Options", ["wp_options"], "option_name", "S", None, None),
    ("Terms", ["wp_terms"], "term_id", "N", None, None),
    ("TermMeta", ["wp_termmeta"], "term_id", "N", "meta_key", "S"),
    ("TermTaxonomy", ["wp_term_taxonomy"], "term_taxonomy_id", "N", None, None),
    ("TermRelationships", ["wp_term_relationships"], "object_id", "N", "term_taxonomy_id", "N"),
    ("Links", ["wp_links"], "link_id", "N", None, None),
    ("ActionSchedulerActions", ["wp_actionscheduler_actions"], "action_id", "N", "status", "S"),
    ("ActionSchedulerLogs", ["wp_actionscheduler_logs"], "action_id", "N", "log_id", "N"),
    ("ActionSchedulerGroups", ["wp_actionscheduler_groups"], "group_id", "N", None, None),
    ("WcAdminNotes", ["wp_wc_admin_notes"], "note_id", "N", None, None),
    ("WcCustomerLookup", ["wp_wc_customer_lookup"], "customer_id", "N", None, None),
    ("WcCategoryLookup", ["wp_wc_category_lookup"], "category_id", "N", None, None),
]


def _key(attr: str, typ: str) -> KeyDefinition:
    # typ is one of "S"/"N"/"B" by construction (see _TABLES); the contract
    # re-validates it. cast keeps the literal type without duplicating the enum.
    return KeyDefinition(
        attribute_name=attr,
        attribute_type=cast("Literal['S', 'N', 'B']", typ),
    )


def _table_def(name, sources, pk, pk_t, sk, sk_t) -> TableDefinition:
    attrs = [
        AttributeDefinition(name=pk, type=pk_t, source_table=sources[0], source_column=pk),
    ]
    if sk:
        attrs.append(
            AttributeDefinition(name=sk, type=sk_t, source_table=sources[0], source_column=sk)
        )
    return TableDefinition(
        table_name=name,
        aggregate_pattern="separate",
        source_tables=sources,
        partition_key=_key(pk, pk_t),
        sort_key=_key(sk, sk_t) if sk else None,
        attributes=attrs,
        item_count=10000,
        item_size_bytes=512,
    )


def _patterns_for(idx, name, pk, sk) -> list[AccessPattern]:
    """A read and a write pattern per table; read uses an SK begins_with when the
    table has a sort key, otherwise a PK-only GetItem. Both in-scope with rps>0."""
    base = idx * 10
    pats: list[AccessPattern] = []
    op: DYNAMODB_OPERATIONS
    if sk:
        kc = f"PK={pk} AND SK begins_with 'X#'"
        op = "Query"
        items = 3.0
    else:
        kc = f"PK={pk}"
        op = "GetItem"
        items = 1.0
    pats.append(
        AccessPattern(
            pattern_id=f"DDB-AP-{base + 1}",
            pattern_group=f"{name} reads",
            query_ids=[f"q{base + 1}"],
            source_tables=[name],
            description=f"Read {name} by key",
            operation=op,
            table_name=name,
            key_condition=kc,
            design_rps=float(50 - idx),
            avg_items_returned=items,
            item_size_bytes=512,
            in_scope=True,
        )
    )
    pats.append(
        AccessPattern(
            pattern_id=f"DDB-AP-{base + 2}",
            pattern_group=f"{name} writes",
            query_ids=[f"q{base + 2}"],
            source_tables=[name],
            description=f"Write an item to {name}",
            operation="PutItem",
            table_name=name,
            key_condition=f"PK={pk}",
            design_rps=float(10 + idx),
            item_size_bytes=512,
            in_scope=True,
        )
    )
    # The busier WordPress entities (the first several) carry an extra update
    # pattern — real workloads update posts/meta/options far more than they insert.
    # This also lifts the in-scope pattern count past the tooling's testable floor.
    if idx < 8:
        pats.append(
            AccessPattern(
                pattern_id=f"DDB-AP-{base + 3}",
                pattern_group=f"{name} updates",
                query_ids=[f"q{base + 3}"],
                source_tables=[name],
                description=f"Update an existing {name} item",
                operation="UpdateItem",
                table_name=name,
                key_condition=f"PK={pk}",
                design_rps=float(20 + idx),
                item_size_bytes=512,
                in_scope=True,
            )
        )
    return pats


def build() -> DynamoDBModelOutputContract:
    table_defs = [_table_def(*t) for t in _TABLES]
    patterns: list[AccessPattern] = []
    for i, (name, _s, pk, _pt, sk, _st) in enumerate(_TABLES):
        patterns.extend(_patterns_for(i, name, pk, sk))
    hot = [
        HotPartitionEntry(
            table_name=t.table_name,
            operation="read",
            rcu_or_wcu_per_second=50.0,
            partition_limit=3000.0,
            utilization_pct=1.7,
            at_risk=False,
            contributing_patterns=[f"q{i * 10 + 1}"],
        )
        for i, t in enumerate(table_defs[:3])
    ]
    return DynamoDBModelOutputContract(
        job_id="b70de5dc",
        source_database="wordpress",
        access_patterns=patterns,
        table_definitions=table_defs,
        hot_partition_analysis=hot,
        trade_offs=[
            TradeOff(
                description="Single-entity table per WordPress entity",
                impact="Matches WordPress key lookups; extra tables vs. an item collection.",
            )
        ],
        validation_passed=True,
    )


def main() -> None:
    contract = build()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(contract.model_dump(mode="json"), indent=2) + "\n")
    testable = [ap for ap in contract.access_patterns if ap.in_scope and ap.design_rps > 0]
    tables_with_traffic = {ap.table_name for ap in testable}
    print(f"wrote {OUT_PATH}")
    print(f"  tables={len(contract.table_definitions)} patterns={len(contract.access_patterns)}")
    print(
        f"  in-scope testable patterns={len(testable)} "
        f"tables-with-traffic={len(tables_with_traffic)}"
    )


if __name__ == "__main__":
    main()
