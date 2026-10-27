"""A DDL-only upload's routing is unchanged by #1636, while its row counts read as unknown.

A DDL upload holds no row counts and no sizes. Before #1636 every table it described
carried ``row_count`` 0, so a full read of one (``SELECT * FROM settings``) was a small
reference read, which DynamoDB rates 90. The maintainer kept that routing on #1637
(2026-10-28), as an interim rule until #1646: the count stays unknown (``row_count``
None) in the collector output, the facts and every deliverable, every small-table rule
treats a DDL-only table (``Table.from_ddl``) as small
(``row_counts.DDL_TABLE_THRESHOLD_ROWS``), and the deliverables say so in plain words. A
table from any other collection with neither a count nor a size is not known small.
"""

from __future__ import annotations

from typing import Any

from src.agents.collector.mysql_collector import _build_tables_from_ddl
from src.agents.referee.access_pattern import (
    KEY_LOOKUP,
    REFERENCE_READ,
    SchemaKeys,
    classify_query,
)
from src.shared.catalog.access_classes import class_engine_scores
from src.shared.prose_style import find_style_hits
from src.shared.row_counts import row_count_facts
from src.tools.database.ddl_parser import parse_ddl

DDL = """
CREATE TABLE settings (id INT PRIMARY KEY, name VARCHAR(64), value VARCHAR(255));
CREATE TABLE orders (id BIGINT PRIMARY KEY, customer_id BIGINT, total DECIMAL(10,2));
"""
QUERIES = {
    "q-settings-all": "SELECT id, name, value FROM settings",
    "q-orders-all": "SELECT * FROM orders",
    "q-settings-key": "SELECT value FROM settings WHERE id = ?",
}


def _ddl_collector() -> dict[str, Any]:
    tables = _build_tables_from_ddl(parse_ddl(DDL, "shop"), "shop")
    return {
        "metadata": {"source_database": {"engine": "mysql"}},
        "database_schema": {"tables": [t.model_dump(mode="json") for t in tables]},
    }


def _classified() -> dict[str, tuple[str, str]]:
    keys = SchemaKeys.from_collector(_ddl_collector())
    out = {}
    for qid, sql in QUERIES.items():
        found = classify_query(
            {"query_id": qid, "query_text": sql, "query_type": "SELECT"}, keys, "mysql"
        )
        out[qid] = (found.name, found.reason)
    return out


def test_a_ddl_upload_keeps_its_counts_unknown_and_has_no_sizes() -> None:
    tables = _ddl_collector()["database_schema"]["tables"]
    assert [(t["row_count"], t["size_mb"], t["from_ddl"]) for t in tables] == [
        (None, None, True),
        (None, None, True),
    ]


def test_full_reads_of_a_ddl_upload_stay_small_reference_reads() -> None:
    # The classes main gives these queries: no routing change.
    classes = {qid: name for qid, (name, _) in _classified().items()}
    assert classes == {
        "q-settings-all": REFERENCE_READ,
        "q-orders-all": REFERENCE_READ,
        "q-settings-key": KEY_LOOKUP,
    }
    # So DynamoDB keeps its 90 for each full read.
    assert class_engine_scores()[REFERENCE_READ]["dynamodb"] == 90


def test_the_reason_says_no_count_or_size_was_collected() -> None:
    _, reason = _classified()["q-settings-all"]
    assert (
        "a small table (no row count or size in a DDL upload, so routing treats it as small)"
        in (reason)
    )
    assert "0 rows" not in reason


def test_the_deliverables_state_the_rule_plainly() -> None:
    statement = str(row_count_facts(_ddl_collector()["database_schema"]["tables"])["statement"])
    assert statement == (
        "All 2 source tables have no row count and no size, since a DDL upload collects "
        "neither; routing treats each as a small reference table, and sizing uses a fixed "
        "0.01 MB stand-in for each"
    )
    assert find_style_hits(f"Row counts: {statement}.") == []


def test_a_ddl_tables_substring_scan_is_not_penalised_and_says_why() -> None:
    from src.agents.referee.substring_match import engine_reason, substring_matches

    collector = {
        **_ddl_collector(),
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": "SELECT id FROM settings WHERE name LIKE '%x@example.com'",
                    "query_type": "SELECT",
                    "tables_accessed": ["shop.settings"],
                    "calls_per_second": 50.0,
                }
            ]
        },
    }
    match = substring_matches(collector)["q1"]
    assert not match.scan_matters
    assert "no row count or size in a DDL upload, so routing treats it as small" in str(
        engine_reason("aurora_mysql", match)
    )


def test_a_table_with_neither_from_another_collection_is_not_known_small() -> None:
    collector = _ddl_collector()
    for table in collector["database_schema"]["tables"]:
        table["from_ddl"] = False
    keys = SchemaKeys.from_collector(collector)
    found = classify_query(
        {"query_id": "q", "query_text": QUERIES["q-settings-all"], "query_type": "SELECT"},
        keys,
        "mysql",
    )
    assert found.name != REFERENCE_READ
