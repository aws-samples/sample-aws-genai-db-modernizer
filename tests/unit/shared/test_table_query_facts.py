# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2325: the queries each relational engine serves on each table, as a fact.

The table of a design's tables shows one figure per table. It is computed once here, from
the query groups, and the UI only formats it: a query on two tables is in both tables' rows
and once in the engine's own total.
"""

from __future__ import annotations

import pytest

from src.contracts.synthesis_output import QueryGroupFacts
from src.shared.report_facts import query_group_facts


def _group(name: str, engine: str, queries: dict[str, list[str]]) -> dict:
    return {
        "group_name": name,
        "engines": [engine],
        "access_patterns": [
            {"engine": engine, "query_ids": [qid], "design_rps": 1} for qid in queries
        ],
        "source_queries": [
            {"query_id": qid, "tables_accessed": tables} for qid, tables in queries.items()
        ],
    }


def _rows(report: dict) -> dict[tuple[str, str], tuple[int, list[str]]]:
    facts = query_group_facts(report)
    assert facts is not None
    QueryGroupFacts.model_validate(facts)
    return {
        (r["engine"], r["table"]): (r["query_count"], r["query_ids"])
        for r in facts["table_queries"]
    }


def test_a_join_query_is_in_each_tables_row_and_once_in_the_engine_total() -> None:
    report = {
        "query_groups": [
            _group("wp_posts", "aurora_mysql", {"q1": ["wp_posts"], "q2": ["wp_posts"]}),
            _group("wp_posts, wp_users", "aurora_mysql", {"q3": ["wp_posts", "wp_users"]}),
        ]
    }
    assert _rows(report) == {
        ("aurora_mysql", "wp_posts"): (3, ["q1", "q2", "q3"]),
        ("aurora_mysql", "wp_users"): (1, ["q3"]),
    }
    facts = query_group_facts(report)
    assert facts is not None
    assert facts["relational_query_counts"] == {"aurora_mysql": 3}


@pytest.mark.passes_on_main
def test_a_name_reads_the_same_with_a_schema_or_quotes_or_another_case() -> None:
    # A design of one schema (here: none in the report) keys tables by their bare name.
    report = {
        "query_groups": [
            _group("g", "aurora_mysql", {"q1": ["Shop.`WP_Posts`"], "q2": ["wp_posts"]}),
        ]
    }
    assert _rows(report) == {("aurora_mysql", "wp_posts"): (2, ["q1", "q2"])}


def test_only_the_engines_own_queries_count_and_placeholders_are_left_out() -> None:
    mixed = _group("t", "aurora_mysql", {"q1": ["t"]})
    mixed["access_patterns"].append({"engine": "dynamodb", "query_ids": ["q9"], "design_rps": 1})
    mixed["source_queries"].append({"query_id": "q9", "tables_accessed": ["t"]})
    mixed["source_queries"].append({"query_id": "q1", "tables_accessed": ["t", "unknown"]})
    assert _rows({"query_groups": [mixed]}) == {("aurora_mysql", "t"): (1, ["q1"])}


def test_a_report_with_no_relational_queries_has_no_rows() -> None:
    assert _rows({"query_groups": [_group("t", "dynamodb", {"q1": ["t"]})]}) == {}


def _designed(engine: str, tables: list[tuple[str, str | None]], groups: list[dict]) -> dict:
    """A report whose ``engine`` design summary lists ``tables`` as (table, schema)."""
    rows = [
        {"table_name": name, **({"schema_name": schema} if schema else {})}
        for name, schema in tables
    ]
    return {"query_groups": groups, "schema_designs": {engine: {"tables": rows}}}


def test_two_schemas_tables_of_one_name_are_two_rows_with_their_own_queries() -> None:
    """#2455: a design of several schemas keys each table ``schema.table``; a query names
    its table qualified, or bare when only one schema has that name."""
    report = _designed(
        "aurora_postgresql",
        [("orders", "sales"), ("orders", "archive"), ("customers", "sales")],
        [
            _group("g1", "aurora_postgresql", {"q1": ["sales.orders"], "q2": ['"Sales"."Orders"']}),
            _group("g2", "aurora_postgresql", {"q3": ["archive.orders"]}),
            _group("g3", "aurora_postgresql", {"q4": ["customers", "sales.orders"]}),
        ],
    )
    assert _rows(report) == {
        ("aurora_postgresql", "sales.orders"): (3, ["q1", "q2", "q4"]),
        ("aurora_postgresql", "archive.orders"): (1, ["q3"]),
        ("aurora_postgresql", "sales.customers"): (1, ["q4"]),
    }


def test_a_bare_name_two_schemas_share_counts_for_neither() -> None:
    report = _designed(
        "aurora_postgresql",
        [("orders", "sales"), ("orders", "archive")],
        [_group("g", "aurora_postgresql", {"q1": ["orders"], "q2": ["archive.orders"]})],
    )
    assert _rows(report) == {
        ("aurora_postgresql", "orders"): (1, ["q1"]),
        ("aurora_postgresql", "archive.orders"): (1, ["q2"]),
    }


def test_a_query_on_a_schema_the_design_lacks_counts_for_no_design_table() -> None:
    """#2479 review: ``crm.orders`` is not ``sales.orders``, even when ``sales`` is the
    only schema with an ``orders``; the source database's prefix is no schema."""
    report = _designed(
        "aurora_postgresql",
        [("orders", "sales"), ("customers", "archive")],
        [_group("g", "aurora_postgresql",
                {"q1": ["crm.orders"], "q2": ["sales.orders"], "q3": ["shop.orders"]})],
    )  # fmt: skip
    report["database_name"] = "shop"
    assert _rows(report) == {
        ("aurora_postgresql", "crm.orders"): (1, ["q1"]),
        ("aurora_postgresql", "sales.orders"): (2, ["q2", "q3"]),
    }


@pytest.mark.passes_on_main
def test_a_design_with_no_schema_keeps_bare_keys() -> None:
    """A design of one schema, and every design saved before #2455, lists no
    ``schema_name``: rows keep the bare key they always had."""
    report = _designed(
        "aurora_mysql",
        [("wp_posts", None)],
        [_group("g", "aurora_mysql", {"q1": ["wordpress.wp_posts"], "q2": ["wp_posts"]})],
    )
    assert _rows(report) == {("aurora_mysql", "wp_posts"): (2, ["q1", "q2"])}


def test_only_the_engines_own_design_decides_its_keys() -> None:
    report = _designed(
        "aurora_postgresql",
        [("orders", "sales"), ("orders", "archive")],
        [
            _group("g1", "aurora_postgresql", {"q1": ["sales.orders"]}),
            _group("g2", "aurora_mysql", {"q2": ["sales.orders"]}),
        ],
    )
    assert _rows(report) == {
        ("aurora_postgresql", "sales.orders"): (1, ["q1"]),
        ("aurora_mysql", "orders"): (1, ["q2"]),
    }


@pytest.mark.passes_on_main
def test_a_saved_report_from_before_2455_reads_its_rows_as_before() -> None:
    """The committed real-run reports carry no ``schema_name``: their rows are unchanged."""
    import gzip
    import json
    from pathlib import Path

    runs = Path(__file__).resolve().parents[3] / "tests/fixtures/real_runs"
    for path in (runs / "wordpress-both/report.json", runs / "discourse-both/report.json.gz"):
        raw = path.read_bytes()
        report = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
        facts = query_group_facts(report)
        assert facts is not None
        assert facts["table_queries"]
        assert all("." not in row["table"] for row in facts["table_queries"])


def test_a_query_on_a_left_out_system_table_keeps_its_own_row() -> None:
    """#2478 review: ``public`` plus ``pg_catalog`` is a design of one schema, so ``audit``
    is keyed bare. A query on ``pg_catalog.audit``, which the design left out with a
    ``system_schema`` note, counts under its own ``pg_catalog.audit`` row, never in the
    user table's; ``public.audit`` and ``shop.audit`` still count as ``audit``."""
    report = _designed(
        "aurora_postgresql",
        [("audit", None), ("users", None)],
        [_group("g", "aurora_postgresql",
                {"q1": ["public.audit"], "q2": ["pg_catalog.audit"], "q3": ["shop.audit"]})],
    )  # fmt: skip
    report["database_name"] = "shop"
    report["schema_designs"]["aurora_postgresql"]["app_layer_notes"] = [
        {"feature": "system_schema", "source_object": "pg_catalog.audit", "recommendation": "x"}
    ]
    assert _rows(report) == {
        ("aurora_postgresql", "audit"): (2, ["q1", "q3"]),
        ("aurora_postgresql", "pg_catalog.audit"): (1, ["q2"]),
    }


def test_a_three_part_name_of_a_schema_the_design_lacks_keeps_its_schema() -> None:
    """#2478 review: ``shop.shop.orders`` names the schema ``shop`` of the database
    ``shop``. The design has no such schema, so the row keeps ``shop.orders``, as the
    results page reads it (``sourceNameMapper``); only a two-part name's prefix can be the
    source database."""
    report = _designed(
        "aurora_postgresql",
        [("orders", "sales"), ("orders", "archive")],
        [
            _group(
                "g", "aurora_postgresql", {"q1": ["shop.shop.orders"], "q2": ["shop.sales.orders"]}
            )
        ],
    )
    report["database_name"] = "shop"
    assert _rows(report) == {
        ("aurora_postgresql", "shop.orders"): (1, ["q1"]),
        ("aurora_postgresql", "sales.orders"): (1, ["q2"]),
    }
