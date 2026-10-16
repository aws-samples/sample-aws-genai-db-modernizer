"""The live collectors read bulk loads the same way the offline parser does (#777).

``COPY t FROM STDIN`` (PostgreSQL) and ``LOAD DATA ... INTO TABLE t``
(MySQL/MariaDB, also through the RDS Performance Insights path) are writes to
``t``: typed ``INSERT`` and attributed to ``t``, not to ``STDIN`` or ``TABLE``.
Since #795 they read it through the SQL facade, as the offline parser does.
"""

from __future__ import annotations

import pytest

from src.agents.referee.cache_overlay import is_write_query
from src.tools.aws import rds_tools
from src.tools.database import mysql_tools, postgres_tools
from src.tools.database.offline_parser import parse_offline_collection

COPY = "COPY wp_posts FROM STDIN WITH CSV"
LOAD = "LOAD DATA LOCAL INFILE ? INTO TABLE wp_posts FIELDS TERMINATED BY ?"


@pytest.mark.parametrize(
    "module,sql",
    [
        (postgres_tools, COPY),
        (mysql_tools, LOAD),
        (rds_tools, COPY),
        (rds_tools, LOAD),
    ],
)
def test_live_extractors_read_a_bulk_load_as_a_write_to_its_table(module, sql):
    assert module._extract_query_type(sql) == "INSERT"
    assert module._extract_tables(sql) == ["wp_posts"]


@pytest.mark.parametrize("module", [postgres_tools, rds_tools])
def test_a_live_copy_export_is_a_read_of_its_table(module):
    # The same answer as the offline parser: a read (OTHER) of wp_posts.
    assert module._extract_query_type("COPY wp_posts TO STDOUT") == "OTHER"
    assert module._extract_tables("COPY wp_posts TO STDOUT") == ["wp_posts"]


@pytest.mark.parametrize(
    "module,sql,table",
    [
        (postgres_tools, 'COPY "my table" FROM STDIN', "my table"),
        (mysql_tools, "LOAD DATA INFILE 'f' INTO TABLE `my-table`", "my-table"),
        (rds_tools, "LOAD DATA /*+ x */ INFILE 'f' INTO TABLE `my-table`", "my-table"),
    ],
)
def test_live_extractors_read_a_quoted_bulk_load_target_whole(module, sql, table):
    assert module._extract_query_type(sql) == "INSERT"
    assert module._extract_tables(sql) == [table]


@pytest.mark.parametrize("sql,engine", [(COPY, "postgresql"), (LOAD, "mysql")])
def test_the_write_gate_sees_a_bulk_load_on_both_dialects(sql, engine):
    collection = {
        "metadata": {"database_name": "shop", "engine": engine},
        "tables": [{"table_name": "wp_posts"}],
        "queries": [{"query_text": sql, "digest": "q1", "execution_count": 1}],
    }
    (query,) = parse_offline_collection(collection)["queries"]
    assert is_write_query(query) is True
