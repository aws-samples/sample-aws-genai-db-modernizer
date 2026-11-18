# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each table owner line's replica reads, counted once in the facts layer (#2321).

The results page's Table owners table has a Replica reads column for every mapped
table. ``facts.table_sources`` lists at most ``TABLE_SOURCES_SHOWN`` replicated tables
and no table nothing writes, so the count is each owner line's own
(``replica_read_count``): the in-scope reads the line's CDC engines (``also_read_by``)
serve, 0 for a table no other engine reads. It is computed once (ADR-032). A report
saved before the field gets it at read time from the same function over what the report
carries, or None when the report cannot give it; only then does a renderer show a dash.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from src.contracts.synthesis_output import SynthesisOutputContract
from src.report.facts import report_facts
from src.shared.report_facts import (
    TABLE_SOURCES_SHOWN,
    compute_report_facts,
    table_owner_fields,
)

REPO = Path(__file__).resolve().parents[3]
SAMPLE = REPO / "docs/examples/wordpress/sample-run/synthesis/v2/report.json"
PRE_212 = REPO / "tests/unit/report/fixtures/wordpress_sample_run_0_3_report.json"


def _mapping(table: str) -> dict[str, Any]:
    return {"source_table": table, "recommended_database": "dynamodb", "confidence_score": 50}


def _report(mappings: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "ranking": [
            {"target": "dynamodb", "confidence_score": 70, "workload_percent": 60.0},
            {"target": "aurora_mysql", "confidence_score": 70, "workload_percent": 40.0},
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 2}]},
        "schema_designs": {"dynamodb": {"status": "completed"}},
        "table_mappings": mappings,
        **extra,
    }


def _counts(facts: dict[str, Any]) -> dict[str, Any]:
    return {r["table"]: r["replica_read_count"] for r in facts["table_owner_lines"]["table_lines"]}


def test_each_owner_line_counts_the_reads_its_cdc_engines_serve() -> None:
    """A written table, a table nothing writes, a table only its owner reads and a
    table no query touches: each line has its own count, 0 meaning no replica read."""
    rows = table_owner_fields(
        [_mapping(t) for t in ("db.a", "db.b", "db.c", "db.d")],
        {
            "db.a": {
                "writes": {"dynamodb": 2},
                "queries": {"dynamodb": 2, "aurora_mysql": 3, "opensearch": 1},
                "cached_by": [],
            },
            "db.b": {"writes": {}, "queries": {"aurora_mysql": 1, "dynamodb": 2}, "cached_by": []},
            "db.c": {"writes": {"dynamodb": 1}, "queries": {"dynamodb": 4}, "cached_by": []},
        },
        retained_engine="aurora_mysql",
    )
    assert [(r["source_table"], r["replica_read_count"]) for r in rows] == [
        ("db.a", 4),
        ("db.b", 2),
        ("db.c", 0),
        ("db.d", 0),
    ]
    assert _counts(compute_report_facts(_report(rows))) == {
        "db.a": 4,
        "db.b": 2,
        "db.c": 0,
        "db.d": 0,
    }


def test_a_split_write_tables_count_leaves_out_the_other_engines_writes() -> None:
    rows = table_owner_fields(
        [_mapping("db.legacy")],
        {
            "db.legacy": {
                "writes": {"dynamodb": 1, "aurora_mysql": 1},
                "queries": {"dynamodb": 1, "aurora_mysql": 3},
                "cached_by": [],
            }
        },
        owners={"db.legacy": "dynamodb"},
    )
    assert rows[0]["also_read_by"] == ["aurora_mysql"]
    assert rows[0]["replica_read_count"] == 2


def test_every_owner_line_has_its_count_past_the_shown_cap() -> None:
    """facts.table_sources lists TABLE_SOURCES_SHOWN replicated tables; the owner
    lines count every mapped table."""
    n = TABLE_SOURCES_SHOWN + 5
    tables = [f"db.t{i:03d}" for i in range(n)]
    usage = {
        t: {"writes": {"dynamodb": 1}, "queries": {"dynamodb": 1, "aurora_mysql": 2}}
        for t in tables
    }
    sources = [
        {
            "table": t,
            "source_of_truth": "dynamodb",
            "write_query_ids": [f"{t}-w"],
            "replicas": [{"engine": "aurora_mysql", "read_query_ids": [f"{t}-r1", f"{t}-r2"]}],
        }
        for t in tables
    ]
    rows = table_owner_fields([_mapping(t) for t in tables], usage)
    facts = compute_report_facts(_report(rows, table_sources=sources))
    assert len(facts["table_sources"]["replicated_tables"]) == TABLE_SOURCES_SHOWN
    assert _counts(facts) == dict.fromkeys(tables, 2)


def _sample() -> dict[str, Any]:
    report: dict[str, Any] = json.loads(SAMPLE.read_text(encoding="utf-8"))
    return report


def test_an_old_block_gets_each_lines_count_from_the_report_at_read_time() -> None:
    """The shipped WordPress sample (1.69) stored its owner lines before the count. A
    written table's count is its table_sources reads; a table nothing writes that
    another engine reads (wp_users) gets the reads that engine serves, never a dash."""
    report = _sample()
    stored = report["facts"]["table_owner_lines"]["table_lines"]
    assert all("replica_read_count" not in r for r in stored)
    counts = _counts(report_facts(report))
    sources = {r["table"]: r for r in report["table_sources"]}
    for line in stored:
        table = line["table"]
        if table in sources and sources[table].get("source_of_truth") == line["owner"]:
            reads = {q for r in sources[table].get("replicas") or [] for q in r["read_query_ids"]}
            assert counts[table] == len(reads), table
        if not line["also_read_by"]:
            assert counts[table] == 0, table
    assert "wordpress.wp_users" not in sources
    assert counts["wordpress.wp_users"] and counts["wordpress.wp_users"] > 0
    assert all(isinstance(c, int) for c in counts.values())


def test_the_value_decides_not_the_key() -> None:
    """A block re-dumped through the contract has the field as None: filled in too."""
    report = _sample()
    for line in report["facts"]["table_owner_lines"]["table_lines"]:
        line["replica_read_count"] = None
    assert all(isinstance(c, int) for c in _counts(report_facts(report)).values())


def test_a_count_the_report_cannot_give_stays_absent() -> None:
    """A stored line whose CDC engine the report counts no read for keeps None, the
    one case a renderer shows a dash: never a 0 next to a CDC engine."""
    report = _sample()
    report.pop("query_pattern_groups")
    counts = _counts(report_facts(report))
    assert counts["wordpress.wp_users"] is None


def test_a_pre_212_report_gets_counts_with_its_owner_lines() -> None:
    report: dict[str, Any] = json.loads(PRE_212.read_text(encoding="utf-8"))
    facts = report_facts(report)
    counts = _counts(facts)
    assert all(isinstance(c, int) for c in counts.values())
    shown = {
        r["table"]: r["replica_read_count"] for r in facts["table_sources"]["replicated_tables"]
    }
    for table, n in shown.items():
        assert counts[table] == n, table


def test_a_stored_count_is_kept_and_the_block_validates() -> None:
    report = _sample()
    facts = report_facts(report)
    model = SynthesisOutputContract.model_validate({**report, "facts": facts})
    dumped = model.model_dump()["facts"]["table_owner_lines"]["table_lines"]
    # The contract carries the field, so a block re-dumped through it keeps the count.
    assert [r["replica_read_count"] for r in dumped] == list(_counts(facts).values())
    doctored = copy.deepcopy(report)
    for line in doctored["facts"]["table_owner_lines"]["table_lines"]:
        line["replica_read_count"] = 7
    assert set(_counts(report_facts(doctored)).values()) == {7}
