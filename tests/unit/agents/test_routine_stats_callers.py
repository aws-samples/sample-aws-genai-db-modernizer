# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each caller of ``attach_routine_stats`` names its own engine (#2268).

PostgreSQL schemas ``"Reporting"`` and ``reporting`` are two schemas, so the counts of
their same-named functions stay apart; a MySQL routine in ``WordPress`` takes the counts
the server filed under ``wordpress`` (``lower_case_table_names=2``). A caller that names
the wrong engine breaks one of the two, so these tests go through the callers.
"""

from unittest.mock import MagicMock

import pytest

from src.agents.collector import mysql_collector, postgres_collector
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.agents.collector.routine_rows import attach_routine_stats
from src.tools.database.offline_parser import parse_offline_collection

_PG_PROCS = [
    {"routine_name": "f", "schema_name": "Reporting", "routine_type": "FUNCTION"},
    {"routine_name": "f", "schema_name": "reporting", "routine_type": "FUNCTION"},
]
_PG_STATS = [
    {"routine_name": "f", "schema_name": "Reporting", "object_type": "FUNCTION",
     "execution_count": 1},
    {"routine_name": "f", "schema_name": "reporting", "object_type": "FUNCTION",
     "execution_count": 3},
]  # fmt: skip
_MY_PROCS = [
    {"routine_name": "p", "schema_name": "WordPress", "routine_type": "PROCEDURE"},
    {"routine_name": "f", "schema_name": "WordPress", "routine_type": "FUNCTION"},
]
_MY_STATS = [
    {"routine_name": "p", "schema_name": "wordpress", "object_type": "PROCEDURE",
     "execution_count": 2},
    {"routine_name": "f", "schema_name": "wordpress", "object_type": "FUNCTION",
     "execution_count": 1},
]  # fmt: skip


def _db(procs, stats):
    db = MagicMock()
    db.collect_tables.return_value = []
    db.collect_procedures.return_value = [dict(p) for p in procs]
    db.collect_triggers.return_value = []
    db.collect_routine_stats.return_value = (stats, {"status": "tracked"})
    return db


def _counts(rows):
    return [r.get("execution_count") for r in rows]


def test_the_postgresql_collector_keeps_case_different_schemas_apart():
    raw = postgres_collector._collect_schema_raw(_db(_PG_PROCS, _PG_STATS))
    assert _counts(raw["procedures"]) == [1, 3]


@pytest.mark.passes_on_main
def test_the_mysql_collector_matches_a_schema_whatever_its_case():
    raw = mysql_collector._collect_schema_raw(_db(_MY_PROCS, _MY_STATS))
    assert _counts(raw["procedures"]) == [2, 1]


def _offline(engine: str, procs, stats) -> list[dict]:
    raw = {
        "metadata": {"database_name": "app", "engine": engine},
        "tables": [{"table_name": "t", "row_count": 1}],
        "columns": [{"table_name": "t", "column_name": "id", "data_type": "int",
                     "is_nullable": "NO", "ordinal_position": 1}],
        "queries": [],
        "procedures": [dict(p, definition="x") for p in procs],
        "routine_stats": stats,
        "routine_stats_status": {"status": "tracked"},
    }  # fmt: skip
    out = build_offline_collector_output(
        parse_offline_collection(raw), job_id="j1", database_name="app", s3_key="c.json"
    )
    return [
        (p.get("execution_stats") or {}).get("total_executions")
        for p in out["database_schema"]["procedures"]
    ]


def test_the_offline_parser_keeps_postgresql_case_different_schemas_apart():
    assert _offline("postgresql", _PG_PROCS, _PG_STATS) == [1, 3]


@pytest.mark.passes_on_main
def test_the_offline_parser_matches_a_mysql_schema_whatever_its_case():
    assert _offline("mysql", _MY_PROCS, _MY_STATS) == [2, 1]


@pytest.mark.parametrize("spelling", ["postgres", "PostgreSQL", " postgresql "])
def test_every_spelling_of_postgresql_matches_schemas_exactly(spelling):
    procs = [dict(p) for p in _PG_PROCS]
    attach_routine_stats(procs, [], _PG_STATS, source_engine=spelling)
    assert _counts(procs) == [1, 3]
