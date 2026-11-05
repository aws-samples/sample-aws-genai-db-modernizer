# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 of an offline PostgreSQL job moves the tables' own schema (#1903).

The job starts from a collection ``collect-postgresql.sql`` wrote: the DMS task selects
schema ``public`` (where the tables are), never the database's name, and the DDL creates
the tables in ``public``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_step import (
    DDL_FILE,
    DMS_TEMPLATE,
    load_step1,
    render_files,
    table_mappings,
)
from src.tools.data_movement.plan import write_plans
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.transformation_contract.write import write_step1

TABLES = ("posts", "topics")


def _collection() -> dict:
    return {
        "metadata": {"version": "PostgreSQL 16.4 on x86_64", "database_name": "forum"},
        "tables": [
            {"table_name": t, "row_count": 10, "schema_name": "public"} for t in TABLES
        ],
        "columns": [
            {"table_name": t, "column_name": c, "data_type": d, "is_nullable": "NO",
             "ordinal_position": i}
            for t in TABLES for i, (c, d) in enumerate([("id", "bigint"), ("title", "text")], 1)
        ],  # fmt: skip
        "primary_keys": [{"table_name": t, "column_name": "id"} for t in TABLES],
        "queries": [
            {"digest": "d1", "query_text": "SELECT * FROM topics WHERE id = $1",
             "schema_name": "forum", "execution_count": 5},
            {"digest": "d2", "query_text": "UPDATE posts SET title = $1 WHERE id = $2",
             "schema_name": "forum", "execution_count": 5},
        ],  # fmt: skip
    }


def _job(root: Path) -> LocalArtifactStore:
    """A finished job ``forum/job1`` (assignment v2) whose collector output is the
    offline builder's, with every table in wave 1 to Aurora PostgreSQL."""
    collector = build_offline_collector_output(
        parse_offline_collection(_collection()), job_id="job1", database_name="forum", s3_key="k"
    )
    ids = [t["table_id"] for t in collector["database_schema"]["tables"]]
    wave = {"wave": 1, "title": "Move to Aurora PostgreSQL", "engines": ["aurora_postgresql"],
            "moves_from": ["postgresql"], "homogeneity": "homogeneous", "tables": ids,
            "gate": "Aurora PostgreSQL serves every query."}  # fmt: skip
    store = LocalArtifactStore(str(root))
    store.write_json("forum/job1/synthesis/v2/report.json",
                     {"assignment_summary": {"version": 2}, "migration_waves": [wave]})  # fmt: skip
    store.write_json(
        "forum/job1/assignment/v2/assignment.json", {"version": 2, "query_assignments": []}
    )
    store.write_json("forum/job1/collector/output.json", json.loads(json.dumps(collector)))
    write_step1(store, "forum", "job1")
    write_plans(store, "forum", "job1")
    return store


def test_the_dms_task_selects_the_tables_schema_and_the_ddl_creates_them_there(tmp_path):
    inputs = load_step1(_job(tmp_path), "forum", "job1")

    selections = [r for r in table_mappings(inputs)["rules"] if r["rule-type"] == "selection"]
    assert [
        (r["object-locator"]["schema-name"], r["object-locator"]["table-name"]) for r in selections
    ] == [("public", "posts"), ("public", "topics")]

    files = render_files(inputs)
    assert '"schema-name": "forum"' not in files[DMS_TEMPLATE]
    ddl = files[DDL_FILE]
    assert re.findall(r'CREATE SCHEMA IF NOT EXISTS "([^"]+)"', ddl) == ["public"]
    assert 'SCHEMA "forum"' not in ddl
