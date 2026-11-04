# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A composite primary key keeps the source's key order from a collection to the DDL (#1865).

``collect-mysql.sql`` and ``collect-postgresql.sql`` list a key's columns with their
``ordinal_position`` (the position within the key). ``JSON_ARRAYAGG`` does not keep the
subquery's ``ORDER BY``, so a collection can list them in column-name order; the
position field is the key order, not the row order.
"""

from __future__ import annotations

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.schema_design_input import project_schema_design_tables
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.schema.aurora_common.ddl_generator import generate_mysql_ddl, generate_pg_ddl
from src.tools.transformation_contract.step1 import build_step1
from tests.unit.tools.transformation_contract.conftest import step1_inputs

# The wordpress key (product_or_parent_id, term_id, product_id, taxonomy); the rows come
# in column-name order, with the key position as ordinal_position.
_KEY = {"product_or_parent_id": 1, "term_id": 2, "product_id": 3, "taxonomy": 4}
_KEY_ORDER = ["product_or_parent_id", "term_id", "product_id", "taxonomy"]
_COLUMN_ORDER = ["product_id", "product_or_parent_id", "taxonomy", "term_id"]


def _collection(engine: str) -> dict:
    data_type = "bigint" if engine == "mysql" else "int8"
    return {
        "metadata": {"database_name": "db", "engine": engine},
        "tables": [{"table_name": "lookup", "row_count": 10}],
        "columns": [
            {
                "table_name": "lookup",
                "column_name": name,
                "ordinal_position": i,
                "data_type": data_type,
                "column_type": data_type,
                "udt_name": data_type,
                "is_nullable": "NO",
            }
            for i, name in enumerate(_COLUMN_ORDER, start=1)
        ],
        "indexes": [],
        "primary_keys": [
            {"table_name": "lookup", "column_name": n, "ordinal_position": _KEY[n]}
            for n in sorted(_KEY)  # column-name order, not key order
        ],
    }


def _output(engine: str) -> dict:
    return build_offline_collector_output(
        parse_offline_collection(_collection(engine)),
        job_id="job-1865",
        database_name="db",
        s3_key="offline.json",
    )


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_the_offline_parser_orders_primary_key_columns_by_key_position(engine):
    [table] = parse_offline_collection(_collection(engine))["tables"]

    assert table["primary_key"] == _KEY_ORDER


@pytest.mark.passes_on_main  # guards the order main already keeps for old collections
def test_rows_without_a_position_keep_their_order():
    data = _collection("mysql")
    for row in data["primary_keys"]:
        del row["ordinal_position"]

    [table] = parse_offline_collection(data)["tables"]

    assert table["primary_key"] == sorted(_KEY)


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_the_projection_keeps_the_key_order(engine):
    contract = CollectorOutputContract.model_validate(_output(engine))

    [table] = project_schema_design_tables(contract)

    assert table.primary_key == _KEY_ORDER


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_the_aurora_ddl_primary_key_keeps_the_key_order(engine):
    contract = CollectorOutputContract.model_validate(_output(engine))
    tables = project_schema_design_tables(contract)

    ddl = (
        generate_mysql_ddl(tables, source_engine=engine)
        if engine == "mysql"
        else generate_pg_ddl(tables, source_engine=engine)
    ).full_ddl

    quote = "`" if engine == "mysql" else '"'
    key = ", ".join(f"{quote}{c}{quote}" for c in _KEY_ORDER)
    assert f"PRIMARY KEY ({key})" in ddl


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_the_step1_rule_primary_key_keeps_the_key_order(engine):
    output = _output(engine)
    tables = output["database_schema"]["tables"]

    contract = build_step1(step1_inputs(engine=engine, tables=tables))

    [rule] = contract.rules
    assert rule.primary_key == _KEY_ORDER
