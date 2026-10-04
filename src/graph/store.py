"""GraphStore — thin wrapper around LadybugDB for Cypher execution."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import ladybug as lb

logger = logging.getLogger(__name__)

# Bounds applied to API reads (read-only handles opened by GraphStoreCache).
READ_QUERY_TIMEOUT_MS = 10_000
MAX_RESULT_ROWS = 1_000
# Buffer pool for read-only handles. Assessment graphs are a few MB; this
# bounds the memory one handle can use for pages and intermediate results.
READ_BUFFER_POOL_BYTES = 256 * 1024 * 1024


class GraphStore:
    """Embedded graph database backed by LadybugDB.

    A read-write handle runs every statement on one connection, serialised by
    a lock. A read-only handle opens a short-lived connection per statement, so
    concurrent reads from several threads do not wait on each other; each of
    those connections carries ``query_timeout_ms``.
    """

    def __init__(
        self,
        db_path: str,
        *,
        read_only: bool = False,
        query_timeout_ms: int | None = None,
        buffer_pool_size: int = 0,
    ):
        """Open the database at db_path.

        A read-write handle creates the database if it does not exist. A
        read-only handle requires an existing database and rejects data and
        schema writes at the engine level. ``query_timeout_ms`` interrupts any
        statement that runs longer than the given number of milliseconds.
        ``buffer_pool_size`` (bytes, 0 = engine default) caps the page cache.
        """
        self.read_only = read_only
        self._timeout_ms = query_timeout_ms
        self._db = lb.Database(db_path, read_only=read_only, buffer_pool_size=buffer_pool_size)
        self._lock = threading.Lock()
        self._conn: lb.Connection | None = None
        if not read_only:
            self._conn = self._new_connection()

    def _new_connection(self) -> lb.Connection:
        conn = lb.Connection(self._db)
        if self._timeout_ms is not None:
            conn.set_query_timeout(self._timeout_ms)
        return conn

    @contextmanager
    def _connection(self) -> Iterator[lb.Connection]:
        if self._conn is not None:
            with self._lock:
                yield self._conn
            return
        conn = self._new_connection()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _execute_single(
        conn: lb.Connection, cypher: str, params: dict | None = None
    ) -> lb.QueryResult:
        """Run one Cypher statement and return its single QueryResult.

        Connection.execute is typed as QueryResult | list[QueryResult]; the
        list form is only returned for multi-statement queries, which this
        wrapper never issues. Narrow it back to a single result.
        """
        if params is not None:
            result = conn.execute(cypher, parameters=params)
        else:
            result = conn.execute(cypher)
        if isinstance(result, list):
            return result[0]
        return result

    def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Run a Cypher query. Returns rows as list of dicts."""
        with self._connection() as conn:
            result = self._execute_single(conn, cypher, params)
            # rows_as_dict() switches each row to a {column: value} dict.
            return cast("list[dict[Any, Any]]", list(result.rows_as_dict()))

    def query_bounded(
        self, cypher: str, params: dict | None = None, max_rows: int = MAX_RESULT_ROWS
    ) -> tuple[list[dict], bool]:
        """Run a Cypher query, returning at most ``max_rows`` rows.

        Returns ``(rows, truncated)`` where ``truncated`` is True when the
        result had more than ``max_rows`` rows. The statement itself should
        already carry a LIMIT (see ``cypher_guard.bound_result_rows``) so the
        engine stops early; this only trims the extra row.
        """
        with self._connection() as conn:
            result = self._execute_single(conn, cypher, params).rows_as_dict()
            rows = cast("list[dict[Any, Any]]", result.get_n(max_rows))
            return rows, result.has_next()

    def execute(self, cypher: str, params: dict | None = None) -> None:
        """Run a Cypher statement that doesn't return results (DDL, inserts)."""
        with self._connection() as conn:
            self._execute_single(conn, cypher, params)

    def _get_all(self, cypher: str) -> list[list[Any]]:
        with self._connection() as conn:
            return cast("list[list[Any]]", self._execute_single(conn, cypher).get_all())

    def _exec_schema_cypher(self, cypher: str) -> list[list[Any]]:  # nosec B608  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query  # fmt: skip
        """Execute Cypher built from internal schema catalog names (not user input)."""
        return self._get_all(cypher)  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query  # fmt: skip

    def _list_tables(self) -> list[list[Any]]:
        """Return show_tables() rows as positional lists: [id, name, type, ...]."""
        # show_tables() yields positional rows (not dict-formatted), so each
        # row is a list.
        return self._get_all("CALL show_tables() RETURN *")

    def is_populated(self) -> bool:
        """Check if the graph has any nodes."""
        try:
            tables = self._list_tables()
            if not tables:
                return False
            # Each row: [id, name, type, database, comment].
            # table_name comes from show_tables() — internal schema catalog, not user input.
            for table in tables:
                table_name = table[1]
                table_type = table[2]
                if table_type == "NODE":
                    cypher = f"MATCH (n:{table_name}) RETURN COUNT(n) AS c"  # nosec B608
                    rows = self._exec_schema_cypher(cypher)
                    if rows and rows[0][0] > 0:
                        return True
            return False
        except Exception:  # noqa: BLE001
            return False

    def clear(self) -> None:
        """Drop all data and schema."""
        try:
            tables = self._list_tables()
            # Drop rel tables first (they depend on node tables).
            # table names come from show_tables() — internal schema catalog, not user input.
            for table in tables:
                if table[2] == "REL":
                    self._exec_schema_cypher(f"DROP TABLE {table[1]}")  # nosec B608
            for table in tables:
                if table[2] == "NODE":
                    self._exec_schema_cypher(f"DROP TABLE {table[1]}")  # nosec B608
        except Exception as exc:  # nosec B110  # noqa: BLE001
            logger.warning("clear() failed: %s", exc)

    def close(self) -> None:
        """Close the database connection and release file locks."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._db.close()
