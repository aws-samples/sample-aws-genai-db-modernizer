"""GraphStore — thin wrapper around LadybugDB for Cypher execution."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

import ladybug as lb

logger = logging.getLogger(__name__)

# Bounds applied to API reads (read-only handles opened by GraphStoreCache).
READ_QUERY_TIMEOUT_MS = 10_000
MAX_RESULT_ROWS = 1_000

# Memory limits of a read-only handle (#998), each overridable from the
# environment. The buffer pool is shared by every statement on the handle and
# holds both the file's pages and each running statement's intermediate
# results. The engine runs a statement on every core unless told otherwise,
# and each of those threads takes its own buffers, so a statement's memory
# grows with the core count: capping the threads per statement and the
# statements running at once keeps the total inside the pool on any machine.
ENV_BUFFER_POOL_MB = "MODERNIZER_GRAPH_BUFFER_POOL_MB"
ENV_QUERY_THREADS = "MODERNIZER_GRAPH_QUERY_THREADS"
ENV_MAX_CONCURRENT_READS = "MODERNIZER_GRAPH_MAX_CONCURRENT_READS"
DEFAULT_READ_BUFFER_POOL_MB = 256
DEFAULT_READ_QUERY_THREADS = 2
DEFAULT_MAX_CONCURRENT_READS = 4
# How long a statement waits for one of the handle's read slots before the
# read is reported as busy (GraphBusyError) instead of queueing forever.
READ_SLOT_WAIT_S = 30.0


class GraphBusyError(RuntimeError):
    """Every read slot of the handle stayed taken for ``READ_SLOT_WAIT_S``."""


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using the default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%d is not positive; using the default %d", name, value, default)
        return default
    return value


@dataclass(frozen=True)
class ReadLimits:
    """The buffer pool, engine threads per statement and concurrent statements
    of a read-only handle."""

    buffer_pool_bytes: int
    query_threads: int
    max_concurrent_reads: int

    @classmethod
    def from_env(cls) -> ReadLimits:
        """The configured limits; an unset, non-integer or non-positive value
        falls back to its default."""
        pool_mb = _env_positive_int(ENV_BUFFER_POOL_MB, DEFAULT_READ_BUFFER_POOL_MB)
        return cls(
            buffer_pool_bytes=pool_mb * 1024 * 1024,
            query_threads=_env_positive_int(ENV_QUERY_THREADS, DEFAULT_READ_QUERY_THREADS),
            max_concurrent_reads=_env_positive_int(
                ENV_MAX_CONCURRENT_READS, DEFAULT_MAX_CONCURRENT_READS
            ),
        )


# Guards the one-time selection of ladybug's native backend (see
# _ensure_backend_loaded below).
_BACKEND_INIT_LOCK = threading.Lock()
_backend_loaded = False


def _ensure_backend_loaded() -> None:
    """Force ladybug's lazy backend selection to run once per process, under a lock.

    ``ladybug.Database`` picks its native backend (pybind, falling back to a
    ctypes C API) the first time any ``Database`` is constructed, caching the
    choice in module-level globals inside ``ladybug._backend`` that are set
    with a check-then-act pattern and no lock of their own. When two threads
    construct the first ``GraphStore`` of the process at the same time (for
    example ``GraphStoreCache`` opening several ``(db_name, job_id)`` keys in
    parallel), one thread can observe the other's "already attempted" flag
    before that other thread's import has actually finished. It then sees no
    backend selected yet and falls back to the C API backend, whose shared
    library this package does not ship -- raising "Could not find lbug C API
    shared library" (issue #470).

    Opening a disposable in-memory database under our own lock forces that
    one-time, process-wide backend selection to complete before any other
    thread even starts it. ``lazy_init=True`` skips the (comparatively
    expensive) full database init -- backend selection alone is all this
    warm-up needs. Every call after the first successful one returns
    immediately (the module-level flag below is set); a failed warm-up is
    not cached, so the next caller retries it.
    """
    global _backend_loaded
    if _backend_loaded:
        return
    with _BACKEND_INIT_LOCK:
        if _backend_loaded:
            return
        lb.Database(":memory:", lazy_init=True).close()
        _backend_loaded = True


class GraphStore:
    """Embedded graph database backed by LadybugDB.

    A read-write handle runs every statement on one connection, serialised by
    a lock. A read-only handle opens a short-lived connection per statement, so
    concurrent reads from several threads do not wait on each other; each of
    those connections carries ``query_timeout_ms``. A read-only handle is
    bounded by its ``ReadLimits``: at most ``max_concurrent_reads`` statements
    run at once (others wait for a slot), each on ``query_threads`` engine
    threads.
    """

    def __init__(
        self,
        db_path: str,
        *,
        read_only: bool = False,
        query_timeout_ms: int | None = None,
        buffer_pool_size: int = 0,
        read_limits: ReadLimits | None = None,
    ):
        """Open the database at db_path.

        A read-write handle creates the database if it does not exist. A
        read-only handle requires an existing database and rejects data and
        schema writes at the engine level. ``query_timeout_ms`` interrupts any
        statement that runs longer than the given number of milliseconds.
        ``buffer_pool_size`` (bytes, 0 = engine default) caps the page cache.
        A read-only handle uses ``read_limits`` (``ReadLimits.from_env()``
        when not given); its buffer pool applies when ``buffer_pool_size`` is
        0. A read-write handle has no read limits.
        """
        self.path = db_path
        self.read_only = read_only
        self._timeout_ms = query_timeout_ms
        self.read_limits = (read_limits or ReadLimits.from_env()) if read_only else None
        if self.read_limits is not None and not buffer_pool_size:
            buffer_pool_size = self.read_limits.buffer_pool_bytes
        self._read_slots = (
            threading.BoundedSemaphore(self.read_limits.max_concurrent_reads)
            if self.read_limits is not None
            else None
        )
        _ensure_backend_loaded()
        self._db = lb.Database(db_path, read_only=read_only, buffer_pool_size=buffer_pool_size)
        self._lock = threading.Lock()
        self._conn: lb.Connection | None = None
        if not read_only:
            self._conn = self._new_connection()

    def _new_connection(self) -> lb.Connection:
        threads = self.read_limits.query_threads if self.read_limits is not None else 0
        conn = lb.Connection(self._db, num_threads=threads)
        if self._timeout_ms is not None:
            conn.set_query_timeout(self._timeout_ms)
        return conn

    @contextmanager
    def _connection(self) -> Iterator[lb.Connection]:
        if self._conn is not None:
            with self._lock:
                yield self._conn
            return
        slots = self._read_slots
        if slots is not None and not slots.acquire(timeout=READ_SLOT_WAIT_S):
            raise GraphBusyError(f"no graph read slot freed within {READ_SLOT_WAIT_S:g}s")
        try:
            conn = self._new_connection()
            try:
                yield conn
            finally:
                conn.close()
        finally:
            if slots is not None:
                slots.release()

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

    def has_table(self, name: str) -> bool:
        """True when the graph has a node or rel table called ``name``."""
        try:
            return any(row[1] == name for row in self._list_tables())
        except Exception:  # noqa: BLE001
            return False

    def graph_meta(self) -> dict:
        """The GraphMeta stamp: ``schema_revision``, ``effective_version``, ``inputs_digest``.

        A graph saved before GraphMeta existed (or never stamped) reads as
        revision 0, so it is stale under any current revision.
        """
        empty = {"schema_revision": 0, "effective_version": 0, "inputs_digest": ""}
        if not self.has_table("GraphMeta"):
            return empty
        rows = self.query(
            "MATCH (m:GraphMeta) RETURN m.schema_revision AS schema_revision, "
            "m.effective_version AS effective_version, m.inputs_digest AS inputs_digest "
            "ORDER BY m.id LIMIT 1"
        )
        if not rows:
            return empty
        return {
            "schema_revision": int(rows[0].get("schema_revision") or 0),
            "effective_version": int(rows[0].get("effective_version") or 0),
            "inputs_digest": str(rows[0].get("inputs_digest") or ""),
        }

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
