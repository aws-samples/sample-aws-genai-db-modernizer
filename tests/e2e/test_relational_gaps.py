"""What the relational-move gap check finds on each bundled sample (#471).

The check is deterministic (no model), so a headless run of a sample finds the same
gaps as the deterministic pipeline. Each expectation below is a reviewed finding:

- wordpress (MySQL 8.0, InnoDB, utf8mb4): only the deprecated SQL_CALC_FOUND_ROWS /
  FOUND_ROWS() pair of WordPress's paginated queries, informational.
- discourse (PostgreSQL 16): only its pgvector halfvec embedding columns, informational
  (they need pgvector 0.7.0 or later on the target).
- trailgear (SQL Server 2019, moving to the chosen Aurora PostgreSQL): the T-SQL
  conversion list.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.e2e

EXPECTED = {
    "wordpress": ("aurora_mysql", {"mysql-sql-calc-found-rows": "informational"}),
    "discourse": ("aurora_postgresql", {"postgresql-pgvector-half-sparse": "informational"}),
    "trailgear": (
        "aurora_postgresql",
        {
            "sqlserver-apply-and-openjson": "needs_rewrite",
            "sqlserver-builtin-functions": "needs_rewrite",
            "sqlserver-date-functions": "needs_rewrite",
            "sqlserver-top-clause": "needs_rewrite",
            "sqlserver-tsql-routines": "needs_rewrite",
            "sqlserver-case-insensitive-comparison": "behaves_differently",
            "sqlserver-merge-to-postgresql": "behaves_differently",
            "sqlserver-table-and-query-hints": "behaves_differently",
            "sqlserver-types-behave-differently": "behaves_differently",
            "sqlserver-babelfish-option": "informational",
            "sqlserver-types-mapped": "informational",
        },
    ),
}


def _gaps(run: PipelineResult) -> dict[str, Any]:
    report = json.loads(run.report_json().read_text())
    gaps: dict[str, Any] | None = report["facts"]["relational_gaps"]
    assert gaps is not None
    return gaps


def test_each_sample_finds_its_reviewed_gaps(run: PipelineResult) -> None:
    gaps = _gaps(run)
    target, expected = EXPECTED[run.db]
    assert gaps["target_engine"] == target
    assert {g["rule_id"]: g["impact"] for g in gaps["gaps"]} == expected
    assert not gaps["gaps_by_impact"]["blocks_move"]
    if run.db == "wordpress":
        # A MySQL digest writes FOUND_ROWS() as `FOUND_ROWS` ( ): still a call.
        collector = json.loads((run.job_dir() / "collector" / "output.json").read_text())
        digests = {
            q["query_id"]
            for q in collector["queries"]["query_patterns"]
            if q["query_text"].strip() == "SELECT `FOUND_ROWS` ( )"
        }
        (gap,) = gaps["gaps"]
        assert digests and digests <= set(gap["queries"])


def test_every_found_gap_names_evidence_that_exists(run: PipelineResult) -> None:
    collector = json.loads((run.job_dir() / "collector" / "output.json").read_text())
    schema = collector["database_schema"]
    query_ids = {q["query_id"] for q in collector["queries"]["query_patterns"]}
    object_ids = {t["table_id"] for t in schema["tables"]}
    object_ids |= {
        f"{t['table_id']}.{c['column_name']}" for t in schema["tables"] for c in t["columns"]
    }
    for key, id_key in (
        ("views", "view_id"),
        ("procedures", "procedure_id"),
        ("triggers", "trigger_id"),
    ):
        object_ids |= {o[id_key] for o in schema.get(key) or []}
    table_ids = {t["table_id"] for t in schema["tables"]}
    for gap in _gaps(run)["gaps"]:
        assert set(gap["queries"]) <= query_ids, gap["rule_id"]
        # #1205: the collector's "unknown" placeholder is no table.
        assert set(gap["tables"]) <= table_ids, gap["rule_id"]
        assert {o["id"] for o in gap["objects"]} <= object_ids, gap["rule_id"]
