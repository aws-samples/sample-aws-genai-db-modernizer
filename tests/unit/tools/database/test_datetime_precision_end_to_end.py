# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL datetime precision survives from a collection file to the draft (#292).

An offline collection (the output of ``scripts/collect-postgresql.sql``) is built by
the shared ``_build_columns`` in ``mysql_collector.py``, not by the live
``postgres_collector._build_tables``; the first round of this fix mapped only the
live builder, so ``run_collect.py`` wrote ``None``. These tests start from the raw
file and end at the generated DDL for both Aurora engines.
"""

from __future__ import annotations

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.analysis_output import AnalysisOutputContract
from src.contracts.collector_output import CollectorOutputContract, Table
from src.contracts.schema_design_input import AgentTable, project_schema_design_input
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.schema.aurora_common.ddl_generator import generate_mysql_ddl, generate_pg_ddl
from tests.unit.agents.test_group_input import _analysis

# column name -> (udt_name, datetime_precision) as collect-postgresql.sql reports it
_COLUMNS = {
    "a": ("timestamp", 3),
    "b": ("timestamptz", 2),
    "c": ("timestamp", 6),  # a bare timestamp: PostgreSQL reports its default
    "d": ("timestamp", None),
    "e": ("timestamp", 9),  # beyond PostgreSQL's 0 to 6: unknown
    "f": ("interval", 3),
    "g": ("date", 0),
}


def _offline_output(*, with_field: bool = True) -> dict:
    parsed = parse_offline_collection(
        {
            "metadata": {"database_name": "db", "engine": "postgresql"},
            "tables": [{"table_name": "events", "row_count": 10}],
            "columns": [
                {
                    "table_name": "events",
                    "column_name": name,
                    "column_type": data_type,
                    "data_type": data_type,
                    "is_nullable": "YES",
                    **({"datetime_precision": prec} if with_field else {}),
                }
                for name, (data_type, prec) in _COLUMNS.items()
            ],
            "indexes": [],
            "primary_keys": [],
        }
    )
    return build_offline_collector_output(
        parsed, job_id="job-292", database_name="db", s3_key="offline.json"
    )


def _offline_table(*, with_field: bool = True) -> Table:
    contract = CollectorOutputContract.model_validate(_offline_output(with_field=with_field))
    [table] = contract.database_schema.tables
    return table


def _agent_table() -> AgentTable:
    """The table the schema design step receives: the real projection of the output."""
    output = _offline_output()
    agent, _, _ = project_schema_design_input(
        CollectorOutputContract.model_validate(output),
        AnalysisOutputContract.model_validate(_analysis(output)),
    )
    [table] = agent.tables
    return table


def _types(result) -> dict[str, str]:
    return {c.name: c.aurora_type for c in result.tables[0].columns}


def test_the_offline_builder_carries_datetime_precision_onto_the_contract():
    precisions = {c.column_name: c.datetime_precision for c in _offline_table().columns}

    assert precisions == {n: p for n, (_, p) in _COLUMNS.items()}


def test_an_old_collection_without_the_field_loads_with_none():
    assert {c.datetime_precision for c in _offline_table(with_field=False).columns} == {None}


def test_the_offline_collection_reaches_aurora_postgresql():
    result = generate_pg_ddl([_agent_table()], source_engine="postgresql")

    assert _types(result) == {
        "a": "TIMESTAMP(3)",
        "b": "TIMESTAMPTZ(2)",
        "c": "TIMESTAMP",
        "d": "TIMESTAMP",
        "e": "TIMESTAMP",
        "f": "INTERVAL(3)",
        "g": "DATE",
    }


def test_the_offline_collection_reaches_aurora_mysql():
    result = generate_mysql_ddl([_agent_table()], source_engine="postgresql")

    types = _types(result)
    assert types["a"] == "DATETIME(3)"
    assert types["b"] == "DATETIME(2)"
    assert types["c"] == types["d"] == types["e"] == "DATETIME(6)"
    assert types["g"] == "DATE"
    # An interval has no Aurora MySQL type; the precision does not change that.
    assert types["f"] != "INTERVAL(3)"
    [residual] = [r for r in result.residuals if r["column"] == "f"]
    assert residual["fallback_type"] == "TEXT"
    assert residual["source_type"] == "interval"


def test_the_projection_carries_datetime_precision_to_the_schema_design_input():
    precisions = {c.column_name: c.datetime_precision for c in _agent_table().columns}

    assert precisions == {n: p for n, (_, p) in _COLUMNS.items()}
