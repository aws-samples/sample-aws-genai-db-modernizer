# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An offline PostgreSQL collection names each table after its schema (#1903).

``collect-postgresql.sql`` reports each table's ``schema_name`` (``pg_namespace``); the
table id is ``<schema>.<table>`` (``public.topics``), as the live collector's is, never
``<database>.<table>``. A table the collection gives no schema (a hand-written or older
file) is in ``public``, the schema PostgreSQL's default ``search_path`` resolves a bare
name to. MySQL keeps ``<database>.<table>``: there the database is the schema.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract
from src.tools.database.offline_parser import parse_offline_collection, rederive_query_facts


def _table(name: str, schema: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"table_name": name, "row_count": 10}
    if schema is not None:
        row["schema_name"] = schema
    return row


def _raw(tables: list[dict[str, Any]], queries: list[str]) -> dict[str, Any]:
    return {
        "metadata": {"version": "PostgreSQL 16.4 on x86_64", "database_name": "forum"},
        "tables": tables,
        "columns": [
            {"table_name": t["table_name"], "column_name": "id", "data_type": "integer",
             "is_nullable": "NO", "ordinal_position": 1}
            for t in tables
        ],  # fmt: skip
        "primary_keys": [{"table_name": t["table_name"], "column_name": "id"} for t in tables],
        "queries": [
            {"digest": f"d{i}", "query_text": q, "schema_name": "forum", "execution_count": 5}
            for i, q in enumerate(queries)
        ],
    }


def _output(raw: dict[str, Any]) -> CollectorOutputContract:
    output = build_offline_collector_output(
        parse_offline_collection(raw), job_id="j1903", database_name="forum", s3_key="k"
    )
    return CollectorOutputContract.model_validate(output)


def _tables_accessed(contract: CollectorOutputContract) -> list[list[str]]:
    return [p.tables_accessed for p in contract.queries.query_patterns]


def test_a_postgresql_collection_names_each_table_after_its_schema():
    contract = _output(
        _raw(
            [_table("topics", "public"), _table("posts", "public")],
            [
                "SELECT * FROM topics WHERE id = $1",
                "SELECT * FROM public.posts p JOIN topics t ON t.id = p.id WHERE p.id = $1",
                'SELECT * FROM forum.public."topics" WHERE id = $1',
            ],
        )
    )

    assert sorted(t.table_id for t in contract.database_schema.tables) == [
        "public.posts",
        "public.topics",
    ]
    assert all(t.schema_name == "public" for t in contract.database_schema.tables)
    assert sorted(map(sorted, _tables_accessed(contract))) == [
        ["public.posts", "public.topics"],
        ["public.topics"],
        ["public.topics"],
    ]
    assert contract.metadata.source_database.database_name == "forum"


def test_a_table_outside_public_keeps_its_own_schema():
    contract = _output(
        _raw(
            [_table("topics", "public"), _table("events", "audit"), _table("log", "audit")],
            [
                "SELECT * FROM audit.events WHERE id = $1",
                # search_path may name audit: the one table of that name is the one read.
                "SELECT * FROM log WHERE id = $1",
                "SELECT * FROM archive.events WHERE id = $1",
            ],
        )
    )

    assert sorted(t.table_id for t in contract.database_schema.tables) == [
        "audit.events",
        "audit.log",
        "public.topics",
    ]
    by_id = {t.table_id: t for t in contract.database_schema.tables}
    assert by_id["audit.events"].schema_name == "audit"
    # archive.events is no collected table: kept as written, never audit.events.
    assert _tables_accessed(contract) == [["audit.events"], ["audit.log"], ["archive.events"]]


def test_one_name_in_two_schemas_gives_two_tables_and_a_bare_name_reads_public():
    contract = _output(
        _raw(
            [_table("events", "audit"), _table("events", "public")],
            [
                "SELECT * FROM events WHERE id = $1",
                "SELECT * FROM audit.events WHERE id = $1",
                "SELECT * FROM public.events WHERE id = $1",
            ],
        )
    )

    assert sorted(t.table_id for t in contract.database_schema.tables) == [
        "audit.events",
        "public.events",
    ]
    assert _tables_accessed(contract) == [["public.events"], ["audit.events"], ["public.events"]]


def test_a_bare_name_in_two_schemas_without_public_stays_as_written():
    contract = _output(
        _raw(
            [_table("events", "audit"), _table("events", "archive")],
            ["SELECT * FROM events WHERE id = $1"],
        )
    )

    assert _tables_accessed(contract) == [["events"]]


@pytest.mark.parametrize("schema_field", ["missing", "blank"])
def test_a_collection_without_the_schema_field_reads_public(schema_field):
    # A collection written by hand (or by an older script) may name no schema.
    tables = [_table("topics", None if schema_field == "missing" else "")]
    contract = _output(_raw(tables, ["SELECT * FROM topics WHERE id = $1"]))

    assert [t.table_id for t in contract.database_schema.tables] == ["public.topics"]
    assert _tables_accessed(contract) == [["public.topics"]]


@pytest.mark.passes_on_main  # MySQL's ids must not change with #1903
def test_mysql_keeps_the_database_as_the_qualifier():
    raw = _raw([_table("wp_posts", "wordpress")], ["SELECT * FROM wp_posts WHERE ID = 1"])
    raw["metadata"] = {"version": "8.0.36", "database_name": "wordpress"}
    for q in raw["queries"]:
        q["schema_name"] = "wordpress"
    output = build_offline_collector_output(
        parse_offline_collection(raw), job_id="j1903", database_name="wordpress", s3_key="k"
    )
    contract = CollectorOutputContract.model_validate(output)

    assert [t.table_id for t in contract.database_schema.tables] == ["wordpress.wp_posts"]
    assert _tables_accessed(contract) == [["wordpress.wp_posts"]]


@pytest.mark.passes_on_main  # a job saved before #1903 keeps its own ids
def test_a_saved_postgresql_output_keyed_by_the_database_keeps_its_ids():
    # A collector output saved before #1903 names its tables discourse.<table>: reading
    # its statements again resolves every reference to those saved ids.
    output = _output(
        _raw([_table("topics", "public")], ["SELECT * FROM topics WHERE id = $1"])
    ).model_dump(mode="json")
    for t in output["database_schema"]["tables"]:
        t["table_id"] = f"forum.{t['table_name']}"
    for p in output["queries"]["query_patterns"]:
        p["tables_accessed"] = ["forum.topics"]
    CollectorOutputContract.model_validate(output)

    again = rederive_query_facts(output, "postgresql")

    assert [p["tables_accessed"] for p in again["queries"]["query_patterns"]] == [["forum.topics"]]
