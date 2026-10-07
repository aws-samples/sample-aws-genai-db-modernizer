"""Verify live collector column order against a disposable PostgreSQL database.

Set POSTGRES_INDEX_TEST_DSN to a local test database and run this module with
pytest -m integration. Each test creates and removes its own public table.
The SQL normally sent through SSM is executed directly on the test database.
"""

import os
from collections.abc import Iterator
from typing import Any, cast
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from src.tools.aws.ssm_executor import SSMExecutor
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

pytestmark = pytest.mark.integration


class LocalSqlExecutor:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def run_sql_json(self, **kwargs: Any) -> list[dict]:
        with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(kwargs["sql"])
            return [dict(row) for row in cursor.fetchall()]


@pytest.fixture
def indexed_table() -> Iterator[tuple[PostgreSQLRemoteCollector, str]]:
    dsn = os.environ.get("POSTGRES_INDEX_TEST_DSN")
    if not dsn:
        pytest.skip("Set POSTGRES_INDEX_TEST_DSN to a disposable PostgreSQL database")
    connection = psycopg2.connect(dsn)
    connection.autocommit = True
    table = "index_order_" + uuid4().hex[:12]
    qualified = sql.Identifier("public", table)
    try:
        with connection.cursor() as cursor:
            # Identifier quotes fixture-generated names; no SQLAlchemy or raw interpolation.
            cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                sql.SQL(
                    "CREATE TABLE {} (user_id integer, created_at timestamptz, PRIMARY KEY (created_at, user_id))"
                ).format(qualified)
            )
            cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                sql.SQL("CREATE INDEX {} ON {} (created_at, user_id)").format(
                    sql.Identifier(table + "_reverse"), qualified
                )
            )
        collector = PostgreSQLRemoteCollector(
            cast(SSMExecutor, LocalSqlExecutor(connection)), "localhost", 5432, "postgres", ""
        )
        yield collector, table
    finally:
        with connection.cursor() as cursor:
            # The same Identifier safely quotes the fixture table during cleanup.
            cursor.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                sql.SQL("DROP TABLE IF EXISTS {}").format(qualified)
            )
        connection.close()


def test_composite_index_preserves_index_order(
    indexed_table: tuple[PostgreSQLRemoteCollector, str],
) -> None:
    collector, table = indexed_table
    indexes = {index["index_name"]: index for index in collector.collect_indexes(table)}
    assert indexes[table + "_reverse"]["columns"] == ["created_at", "user_id"]
    assert indexes[table + "_pkey"]["columns"] == ["created_at", "user_id"]


def test_composite_primary_key_preserves_constraint_order(
    indexed_table: tuple[PostgreSQLRemoteCollector, str],
) -> None:
    collector, table = indexed_table
    assert collector.collect_primary_key(table) == ["created_at", "user_id"]
