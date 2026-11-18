# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2455: synthesis reads an Aurora design table of a design of several schemas as its own
table: its source table is ``schema.table`` and the report's design summary keeps its
schema, so the per-table query facts key it the way the design does."""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_handler import _build_aurora_schema_summary
from src.agents.referee.synthesis_report import schema_table_defs


def _design(*tables: dict) -> dict:
    return {"source_database": "shop", "table_definitions": list(tables)}


def test_a_schema_named_table_maps_to_its_own_source_table() -> None:
    design = _design(
        {"table_name": "orders", "schema_name": "sales", "columns": []},
        {"table_name": "orders", "schema_name": "archive", "columns": []},
    )
    defs = schema_table_defs("aurora_postgresql", design)
    assert [d["source_tables"] for d in defs] == [["sales.orders"], ["archive.orders"]]
    summary = _build_aurora_schema_summary("aurora_postgresql", design)
    assert [(t["table_name"], t["schema_name"]) for t in summary["tables"]] == [
        ("orders", "sales"),
        ("orders", "archive"),
    ]


@pytest.mark.passes_on_main
def test_a_table_with_no_schema_maps_as_before() -> None:
    design = _design({"table_name": "wp_posts", "columns": []})
    assert schema_table_defs("aurora_mysql", design)[0]["source_tables"] == ["shop.wp_posts"]
    summary = _build_aurora_schema_summary("aurora_mysql", design)
    assert "schema_name" not in summary["tables"][0]
