# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared builders for the step 1 tests (#1764)."""

from __future__ import annotations

import copy
from typing import Any

from src.contracts.collector_output import CollectorOutputContract
from src.shared.contract_versioning import current_version
from src.tools.data_movement.plan import JobInputs

FULL_DESIGN: list[dict[str, Any]] = [
    {
        "table_name": "posts",
        "columns": [
            {"name": "ID", "aurora_type": "BIGINT UNSIGNED"},
            {"name": "title", "aurora_type": "VARCHAR(20)"},
            {"name": "n", "aurora_type": "INT"},
            {"name": "flag", "aurora_type": "TINYINT(1)"},
        ],
    },
    {"table_name": "log", "columns": [{"name": "msg", "aurora_type": "TEXT"}]},
]


def col(name, data_type, pos=None, nullable=False, **extra):
    col = {"column_name": name, "data_type": data_type, "nullable": nullable, **extra}
    if pos is not None:
        col["ordinal_position"] = pos
    return col


def table(schema, name, columns, pk=()):
    return {
        "table_id": f"{schema}.{name}",
        "table_name": name,
        "schema_name": schema,
        "row_count": 5,
        "primary_key": list(pk),
        "columns": columns,
    }


def collector(engine="mysql", tables=None, views=None, version=None):
    return {
        "contract_version": version or current_version(CollectorOutputContract),
        "job_id": "j1",
        "metadata": {
            "collection_timestamp": "2024-01-15T10:00:00Z",
            "collector_version": "1.0.0",
            "source_database": {
                "engine": engine,
                "version": "8.0.32",
                "hostname": "db.local",
                "database_name": "wp",
            },
        },
        "database_schema": {"tables": tables or [], "views": views or []},
        "queries": {"query_patterns": []},
        "metrics": {"performance_metrics": {}},
    }


def mysql_tables():
    return [
        table(
            "wp",
            "posts",
            [
                col("title", "varchar(20)", 2, max_length=20),
                col("ID", "bigint unsigned", 1),
                col("flag", "tinyint(1)", 3),
                col("n", "int(11)", 4),
            ],
            pk=["ID"],
        ),
        table("wp", "log", [col("msg", "text", 1)]),
    ]


def step1_inputs(
    homogeneity="homogeneous",
    design=None,  # True: a full saved design; a list: that design; None: no design
    views=None,
    engine="mysql",
    tables=None,
    version=None,
    wave_tables=None,
):
    tables = tables if tables is not None else mysql_tables()
    coll = collector(engine, tables, views, version)
    ids = wave_tables or [t["table_id"] for t in tables] + [v["view_id"] for v in (views or [])]
    wave: dict[str, Any] = {
        "wave": 1,
        "title": "Move to Aurora",
        "engines": ["aurora_mysql"],
        "tables": ids,
    }
    if homogeneity is not None:
        wave["homogeneity"] = homogeneity
    report = {"assignment_summary": {"version": 2}, "migration_waves": [wave]}
    aurora = "aurora_postgresql" if engine == "postgresql" else "aurora_mysql"
    designs = {}
    if design is True:
        design = copy.deepcopy(FULL_DESIGN)
    if design:
        designs[aurora] = (f"schema-{aurora}/v2/schema_output.json", {"table_definitions": design})
    return JobInputs(
        job_id="j1",
        database_name="wp",
        report=report,
        assignment={},
        collector=coll,
        schema_designs=designs,
    )
