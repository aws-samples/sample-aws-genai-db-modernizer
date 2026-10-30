# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Expression keys are flagged on every source engine that has them (#295).

PostgreSQL is covered in ``test_expression_index_end_to_end.py``. The same
narrowing happens wherever a catalog reports an expression key without a
column: a MySQL 8 functional key part (``COLUMN_NAME`` NULL in
``information_schema.statistics``), an Oracle function-based key (reported as
its hidden ``SYS_NCnnnnn$`` column), and a functional key part in MySQL DDL
text. Each case starts from raw input and ends at the Aurora draft, which must
note the index rather than create a UNIQUE index on part of its keys.
SQL Server and MariaDB index only real columns, so their key rows are never
expression keys.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.agents.collector import mysql_collector, oracle_collector
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract, Index, Table
from src.contracts.schema_design_input import AgentTable
from src.tools.database.ddl_parser import parse_ddl
from src.tools.database.index_keys import add_index_key, is_expression_key
from src.tools.database.mariadb_tools import MariaDBRemoteCollector
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.schema.aurora_common.ddl_generator import generate_mysql_ddl, generate_pg_ddl

_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:t-AAAAAA"  # pragma: allowlist secret  # nosec B106 — test fixture ARN, not a credential

_VERSIONS = {
    "mysql": "8.0.36",
    "oracle": "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0",
    "sqlserver": "Microsoft SQL Server 2019 (RTM) - 15.0.2000.5 (X64)",
}


def _offline_table(engine: str, key_rows: list[dict[str, Any]]) -> Table:
    """One ``users`` table with one UNIQUE index, collected offline from ``engine``."""
    parsed = parse_offline_collection(
        {
            "metadata": {"database_name": "db", "version": _VERSIONS[engine]},
            "tables": [{"table_name": "users", "row_count": 10}],
            "columns": [
                {
                    "table_name": "users",
                    "column_name": name,
                    "column_type": data_type,
                    "data_type": data_type,
                    "is_nullable": "YES",
                }
                for name, data_type in (("tenant_id", "int"), ("name", "varchar"))
            ],
            "indexes": [
                {
                    "table_name": "users",
                    "index_name": "u_tenant_name",
                    "non_unique": 0,
                    "index_type": "btree",
                    **row,
                }
                for row in key_rows
            ],
            "primary_keys": [],
        }
    )
    contract = CollectorOutputContract.model_validate(
        build_offline_collector_output(
            parsed, job_id="job-295", database_name="db", s3_key="offline.json"
        )
    )
    [table] = contract.database_schema.tables
    return table


def _agent_table(table: Table) -> AgentTable:
    return AgentTable.model_validate(
        {
            "table_id": table.table_id,
            "table_name": table.table_name,
            "row_count": table.row_count,
            "primary_key": table.primary_key,
            "columns": [
                {"column_name": c.column_name, "data_type": c.data_type, "nullable": c.nullable}
                for c in table.columns
            ],
            "indexes": [i.model_dump() for i in table.indexes or []],
        }
    )


def _assert_noted_not_narrowed(table: Table, source_engine: str) -> None:
    """Neither Aurora draft creates the index; each notes it once."""
    agent_table = _agent_table(table)
    for generate in (generate_pg_ddl, generate_mysql_ddl):
        result = generate([agent_table], source_engine=source_engine)
        assert "u_tenant_name" not in result.full_ddl
        [note] = [n for n in result.index_notes if n["index"] == "u_tenant_name"]
        assert "expression keys" in note["reason"]
        assert "a UNIQUE index on part of its keys" in note["reason"]


# ---------------------------------------------------------------------------
# MySQL 8: a functional key part has a NULL COLUMN_NAME
# ---------------------------------------------------------------------------


def test_a_mysql_functional_key_part_flags_the_offline_index():
    # UNIQUE (tenant_id, (lower(name))) as collect-mysql.sql dumps it.
    table = _offline_table(
        "mysql",
        [
            {"column_name": "tenant_id", "seq_in_index": 1},
            {"column_name": None, "seq_in_index": 2},
        ],
    )

    [idx] = table.indexes
    assert (idx.columns, idx.is_unique, idx.has_expressions) == (["tenant_id"], True, True)
    _assert_noted_not_narrowed(table, "mysql")


def test_a_mysql_index_of_only_functional_key_parts_collects():
    table = _offline_table("mysql", [{"column_name": None, "seq_in_index": 1}])

    [idx] = table.indexes
    assert (idx.columns, idx.has_expressions) == ([], True)


def test_a_plain_mysql_index_is_not_flagged():
    table = _offline_table(
        "mysql",
        [
            {"column_name": "tenant_id", "seq_in_index": 1},
            {"column_name": "name", "seq_in_index": 2},
        ],
    )

    [idx] = table.indexes
    assert (idx.columns, idx.has_expressions, idx.index_expressions) == (
        ["tenant_id", "name"],
        False,
        None,
    )
    result = generate_pg_ddl([_agent_table(table)], source_engine="mysql")
    assert result.tables[0].index_sql == [
        'CREATE UNIQUE INDEX "u_tenant_name" ON "users" ("tenant_id", "name");'
    ]


def _mysql_live(version: str, cls: type[MySQLRemoteCollector] = MySQLRemoteCollector):
    ssm = MagicMock()

    def run_sql_json(*, sql: str, **_: Any) -> list[dict[str, Any]]:
        if "VERSION()" in sql:
            return [{"version": version}]
        base = {"index_name": "u_tenant_name", "non_unique": 0, "index_type": "BTREE"}
        return [
            {**base, "column_name": "tenant_id", "seq_in_index": 1, "expression": None},
            {**base, "column_name": None, "seq_in_index": 2, "expression": "lower(`name`)"},
        ]

    ssm.run_sql_json.side_effect = run_sql_json
    collector = cls(ssm=ssm, host="h", port=3306, database="db", secret_arn=_ARN)
    return collector, ssm


def _index_queries(ssm: MagicMock) -> list[str]:
    return [
        c.kwargs["sql"]
        for c in ssm.run_sql_json.call_args_list
        if "information_schema.statistics" in c.kwargs["sql"]
    ]


def test_the_live_mysql_collector_flags_a_functional_key_part_with_its_text():
    collector, ssm = _mysql_live("8.0.36")

    [raw] = collector.collect_indexes("users")
    collector.collect_indexes("users")

    assert raw["columns"] == ["tenant_id"]
    assert raw["has_expressions"] is True
    assert raw["index_expressions"] == "lower(`name`)"
    assert all(", expression" in sql for sql in _index_queries(ssm))
    # The server version is asked once, not once per table.
    versions = [c for c in ssm.run_sql_json.call_args_list if "VERSION()" in c.kwargs["sql"]]
    assert len(versions) == 1

    [table] = mysql_collector._build_tables(
        [
            {
                "table_name": "users",
                "columns": [
                    {"column_name": "tenant_id", "data_type": "int", "is_nullable": "NO"},
                    {"column_name": "name", "data_type": "varchar", "is_nullable": "YES"},
                ],
                "indexes": [raw],
            }
        ],
        "db",
    )
    [idx] = table.indexes
    assert (idx.columns, idx.has_expressions, idx.index_expressions) == (
        ["tenant_id"],
        True,
        "lower(`name`)",
    )
    result = generate_pg_ddl([_agent_table(table)], source_engine="mysql")
    [note] = result.index_notes
    assert "lower(`name`)" in note["reason"]


@pytest.mark.parametrize("version", ["5.7.44-log", "8.0.12"])
def test_the_live_mysql_collector_reads_no_expression_column_before_8_0_13(version):
    collector, ssm = _mysql_live(version)

    collector.collect_indexes("users")

    [sql] = _index_queries(ssm)
    assert "expression" not in sql


def test_the_live_mariadb_collector_never_reads_an_expression_column():
    collector, ssm = _mysql_live("10.11.6-MariaDB", MariaDBRemoteCollector)

    collector.collect_indexes("users")

    [sql] = _index_queries(ssm)
    assert "expression" not in sql
    assert not [c for c in ssm.run_sql_json.call_args_list if "VERSION()" in c.kwargs["sql"]]


# ---------------------------------------------------------------------------
# Oracle: a function-based key is reported as a hidden SYS_NCnnnnn$ column
# ---------------------------------------------------------------------------


def test_an_oracle_function_based_key_flags_the_offline_index():
    # CREATE UNIQUE INDEX u_tenant_name ON users (tenant_id, LOWER(name)), as
    # collect-oracle.sql dumps it (column names lower-cased).
    table = _offline_table(
        "oracle",
        [
            {"column_name": "tenant_id", "index_type": "other"},
            {"column_name": "sys_nc00003$", "index_type": "other"},
        ],
    )

    [idx] = table.indexes
    assert (idx.columns, idx.is_unique, idx.has_expressions) == (["tenant_id"], True, True)
    _assert_noted_not_narrowed(table, "oracle")


def test_the_live_oracle_collector_flags_a_function_based_key():
    ssm = MagicMock()
    base = {
        "index_name": "U_TENANT_NAME",
        "uniqueness": "UNIQUE",
        "index_type": "FUNCTION-BASED NORMAL",
        "is_primary": "NO",
    }
    ssm.run_sql_json.return_value = [
        {**base, "column_name": "TENANT_ID", "key_ordinal": 1},
        {**base, "column_name": "SYS_NC00003$", "key_ordinal": 2},
    ]
    collector = OracleRemoteCollector(
        ssm=ssm, host="h", port=1521, database="ORCLPDB1", secret_arn=_ARN
    )

    [raw] = collector.collect_indexes("APP.USERS")

    assert (raw["columns"], raw["has_expressions"]) == (["tenant_id"], True)
    [table] = oracle_collector._build_tables(
        {
            "tables": [
                {
                    "table_name": "users",
                    "schema_name": "app",
                    "columns": [
                        {"column_name": "tenant_id", "data_type": "number"},
                        {"column_name": "name", "data_type": "varchar2"},
                    ],
                    "indexes": [raw],
                }
            ]
        }
    )
    [idx] = table.indexes
    assert (idx.columns, idx.has_expressions) == (["tenant_id"], True)
    _assert_noted_not_narrowed(table, "oracle")


@pytest.mark.parametrize("oracle_kind", ["BITMAP", "FUNCTION-BASED NORMAL", "DOMAIN"])
def test_the_live_oracle_builder_takes_every_oracle_index_kind(oracle_kind):
    """The live query reports Oracle's own kinds, which the contract has no value for.
    They read as "other", as in the offline script, rather than failing the build."""
    ssm = MagicMock()
    ssm.run_sql_json.return_value = [
        {
            "index_name": "IDX_STATUS",
            "uniqueness": "NONUNIQUE",
            "index_type": oracle_kind,
            "is_primary": "NO",
            "column_name": "STATUS",
            "key_ordinal": 1,
        }
    ]
    collector = OracleRemoteCollector(
        ssm=ssm, host="h", port=1521, database="ORCLPDB1", secret_arn=_ARN
    )
    raw = collector.collect_indexes("APP.USERS")

    [table] = oracle_collector._build_tables(
        {
            "tables": [
                {
                    "table_name": "users",
                    "columns": [{"column_name": "status", "data_type": "varchar2"}],
                    "indexes": raw,
                }
            ]
        }
    )

    [idx] = table.indexes
    assert (idx.columns, idx.index_type.value, idx.has_expressions) == (
        ["status"],
        "other",
        False,
    )


# ---------------------------------------------------------------------------
# DDL text: a MySQL functional key part inside CREATE TABLE
# ---------------------------------------------------------------------------


def _ddl_index(key_list: str, unique: str = "UNIQUE KEY") -> dict[str, Any]:
    [table] = parse_ddl(
        "CREATE TABLE `users` (\n"
        "  `id` INT NOT NULL,\n"
        "  `tenant_id` INT NOT NULL,\n"
        "  `name` VARCHAR(100),\n"
        "  PRIMARY KEY (`id`),\n"
        f"  {unique} `u_tenant_name` {key_list}\n"
        ");",
        database_name="db",
    )
    [index] = [i for i in table["indexes"] if i["index_name"] == "u_tenant_name"]
    return dict(index)


@pytest.mark.parametrize(
    ("key_list", "columns", "expressions"),
    [
        ("(`tenant_id`, (lower(`name`)))", ["tenant_id"], "(lower(`name`))"),
        # The expression first: a list cut at its first ")" kept no key at all.
        ("((lower(`name`)), `tenant_id`)", ["tenant_id"], "(lower(`name`))"),
        ("((lower(`name`)))", [], "(lower(`name`))"),
    ],
)
def test_a_ddl_functional_key_part_is_flagged_not_named(key_list, columns, expressions):
    index = _ddl_index(key_list)

    assert index["columns"] == columns
    assert index["has_expressions"] is True
    assert index["index_expressions"] == expressions
    [table] = mysql_collector._build_tables_from_ddl(
        parse_ddl(
            "CREATE TABLE `users` (`tenant_id` INT, `name` VARCHAR(100), "
            f"UNIQUE KEY `u_tenant_name` {key_list});",
            database_name="db",
        ),
        "db",
    )
    [idx] = table.indexes
    assert (idx.columns, idx.has_expressions) == (columns, True)
    _assert_noted_not_narrowed(table, "mysql")


@pytest.mark.parametrize(
    ("key_list", "columns"),
    [
        ("(`tenant_id`, `name`)", ["tenant_id", "name"]),
        ("(`tenant_id`, `name`(20))", ["tenant_id", "name"]),
        ("(`tenant_id` ASC, `name` DESC)", ["tenant_id", "name"]),
    ],
)
def test_ddl_column_keys_stay_column_keys(key_list, columns):
    index = _ddl_index(key_list)

    assert index["columns"] == columns
    assert index["has_expressions"] is False
    assert index["index_expressions"] is None


def test_a_tsql_unique_constraint_names_its_columns():
    [table] = parse_ddl(
        "CREATE TABLE [Users] ([Tenant] INT, [Name] NVARCHAR(50), "
        "CONSTRAINT [UQ_Users] UNIQUE NONCLUSTERED ([Tenant], [Name]));",
        database_name="db",
        dialect="sqlserver",
    )
    [index] = table["indexes"]

    assert (index["columns"], index["has_expressions"]) == (["Tenant", "Name"], False)


# ---------------------------------------------------------------------------
# The rule itself, per engine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("engine", "column", "expected"),
    [
        ("postgresql", None, True),
        ("mysql", None, True),
        ("mysql", "", True),
        ("mysql", "tenant_id", False),
        ("oracle", "sys_nc00003$", True),
        ("oracle", "SYS_NC00012$", True),
        ("oracle", "tenant_id", False),
        # Only Oracle names a hidden column for an expression key.
        ("mysql", "sys_nc00003$", False),
        ("sqlserver", "sys_nc00003$", False),
        ("sqlserver", "TenantId", False),
        ("mariadb", "tenant_id", False),
    ],
)
def test_is_expression_key(engine, column, expected):
    assert is_expression_key(column, engine) is expected


def test_add_index_key_joins_several_expression_texts():
    index: dict[str, Any] = {"columns": [], "has_expressions": False, "index_expressions": None}

    for row in (
        {"column_name": "tenant_id"},
        {"column_name": None, "expression": "lower(`name`)"},
        {"column_name": None, "expression": "(`id` + 1)"},
    ):
        add_index_key(index, row, "mysql")

    assert index == {
        "columns": ["tenant_id"],
        "has_expressions": True,
        "index_expressions": "lower(`name`), (`id` + 1)",
    }


def test_the_contract_still_refuses_an_index_with_no_keys_at_all():
    with pytest.raises(ValueError, match="must name at least one column"):
        Index(index_name="i", columns=[], is_unique=True)
