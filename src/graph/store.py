"""GraphStore — thin wrapper around LadybugDB for Cypher execution."""

from __future__ import annotations

import logging
from typing import Any, cast

import ladybug as lb

logger = logging.getLogger(__name__)

# Bounds applied to API reads (read-only handles opened by GraphStoreCache).
READ_QUERY_TIMEOUT_MS = 10_000
MAX_RESULT_ROWS = 1_000


class GraphStore:
    """Embedded graph database backed by LadybugDB."""

    def __init__(
        self,
        db_path: str,
        *,
        read_only: bool = False,
        query_timeout_ms: int | None = None,
    ):
        """Open the database at db_path.

        A read-write handle creates the database if it does not exist. A
        read-only handle requires an existing database and rejects every write
        at the engine level. ``query_timeout_ms`` interrupts any statement that
        runs longer than the given number of milliseconds.
        """
        self.read_only = read_only
        self._db = lb.Database(db_path, read_only=read_only)
        self._conn = lb.Connection(self._db)
        if query_timeout_ms is not None:
            self._conn.set_query_timeout(query_timeout_ms)

    def _execute_single(self, cypher: str, params: dict | None = None) -> lb.QueryResult:
        """Run one Cypher statement and return its single QueryResult.

        Connection.execute is typed as QueryResult | list[QueryResult]; the
        list form is only returned for multi-statement queries, which this
        wrapper never issues. Narrow it back to a single result.
        """
        if params is not None:
            result = self._conn.execute(cypher, parameters=params)
        else:
            result = self._conn.execute(cypher)
        if isinstance(result, list):
            return result[0]
        return result

    def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Run a Cypher query. Returns rows as list of dicts."""
        result = self._execute_single(cypher, params)
        # rows_as_dict() switches each row to a {column: value} dict.
        return cast("list[dict[Any, Any]]", list(result.rows_as_dict()))

    def query_bounded(
        self, cypher: str, params: dict | None = None, max_rows: int = MAX_RESULT_ROWS
    ) -> tuple[list[dict], bool]:
        """Run a Cypher query, returning at most ``max_rows`` rows.

        Returns ``(rows, truncated)`` where ``truncated`` is True when the
        result had more than ``max_rows`` rows.
        """
        result = self._execute_single(cypher, params).rows_as_dict()
        rows = cast("list[dict[Any, Any]]", result.get_n(max_rows))
        return rows, result.has_next()

    def execute(self, cypher: str, params: dict | None = None) -> None:
        """Run a Cypher statement that doesn't return results (DDL, inserts)."""
        self._execute_single(cypher, params)

    def _exec_schema_cypher(self, cypher: str) -> lb.QueryResult:  # nosec B608  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query  # fmt: skip
        """Execute Cypher built from internal schema catalog names (not user input)."""
        return self._execute_single(cypher)  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query  # fmt: skip

    def _list_tables(self) -> list[list[Any]]:
        """Return show_tables() rows as positional lists: [id, name, type, ...]."""
        rows = self._execute_single("CALL show_tables() RETURN *").get_all()
        # show_tables() yields positional rows (not dict-formatted), so each
        # row is a list. Cast for the type checker.
        return cast("list[list[Any]]", rows)

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
                    rows = cast("list[list[Any]]", self._exec_schema_cypher(cypher).get_all())
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
        self._conn.close()
        self._db.close()
