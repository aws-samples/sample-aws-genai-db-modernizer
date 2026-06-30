"""GraphStore — thin wrapper around LadybugDB for Cypher execution."""

from __future__ import annotations

import logging

import ladybug as lb

logger = logging.getLogger(__name__)


class GraphStore:
    """Embedded graph database backed by LadybugDB."""

    def __init__(self, db_path: str):
        """Open (or create) the database at db_path."""
        self._db = lb.Database(db_path)
        self._conn = lb.Connection(self._db)

    def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Run a Cypher query. Returns rows as list of dicts."""
        if params is not None:
            result = self._conn.execute(cypher, parameters=params)
        else:
            result = self._conn.execute(cypher)
        return list(result.rows_as_dict())

    def execute(self, cypher: str, params: dict | None = None) -> None:
        """Run a Cypher statement that doesn't return results (DDL, inserts)."""
        if params is not None:
            self._conn.execute(cypher, parameters=params)
        else:
            self._conn.execute(cypher)

    def _exec_schema_cypher(self, cypher: str) -> lb.QueryResult:  # nosec B608  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query  # fmt: skip
        """Execute Cypher built from internal schema catalog names (not user input)."""
        return self._conn.execute(cypher)  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query  # fmt: skip

    def is_populated(self) -> bool:
        """Check if the graph has any nodes."""
        try:
            result = self._conn.execute("CALL show_tables() RETURN *")
            tables = result.get_all()
            if not tables:
                return False
            # Each row: [id, name, type, database, comment].
            # table_name comes from show_tables() — internal schema catalog, not user input.
            for table in tables:
                table_name = table[1]
                table_type = table[2]
                if table_type == "NODE":
                    cypher = f"MATCH (n:{table_name}) RETURN COUNT(n) AS c"  # nosec B608
                    rows = self._exec_schema_cypher(cypher).get_all()
                    if rows and rows[0][0] > 0:
                        return True
            return False
        except Exception:  # noqa: BLE001
            return False

    def clear(self) -> None:
        """Drop all data and schema."""
        try:
            result = self._conn.execute("CALL show_tables() RETURN *")
            tables = result.get_all()
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
