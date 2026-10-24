"""
Initial load to change-data-capture handoff (#1427, part of #1412).

Loads a wave's existing rows into its target while the source keeps taking writes,
then hands over to Debezium. Delivery is at least once: a change the load already
saw may be applied again by CDC, and upserts by key make the result the same
(idempotent convergence), so nothing is lost and a replay is harmless. It is driven
by a ``DataMovementPlan`` (``src.contracts.data_movement_plan``): ``initial_load``
must be ``bulk_load_with_position_handoff`` and ``cdc`` a Debezium connector with
``snapshot_mode=no_data``. The algorithm follows the CDC prototype by Norbert Funke
and Dinesh Kumar (contrib/cdc-elasticache-prototype); nothing here imports from it.

The handoff, step by step:

1. **Check** the plan and the tables before touching the source: every table needs
   a key (the hub topic's ``key_columns``, else its primary key), the source dialect
   needs a position capture, and the connector must not be running.
2. **Check the source settings read-only** (``cdc.required_source_settings``):
   ``binlog_format``, ``binlog_row_image`` and binlog retention on MySQL (on RDS and
   Aurora, ``CALL mysql.rds_show_configuration``: a NULL ``binlog retention hours``
   lets binlogs be purged during a long load); ``rds.logical_replication`` or
   ``wal_level``, ``max_replication_slots`` and ``max_wal_senders`` on PostgreSQL;
   the signal table on both. Any unmet or uncheckable setting refuses the run.
3. **Fingerprint the wave's table schemas** (``information_schema.columns``).
4. **Capture the position P0**, before any row is read.

   - MySQL needs 8.0 or later and ``gtid_mode=ON``. ``SHOW BINARY LOG STATUS``
     (8.2 and later) or ``SHOW MASTER STATUS`` (8.0, Aurora MySQL 3) gives the binlog
     file, position and executed GTID set. GTIDs are required because, with group
     commit, a transaction's binlog event is written before its commit is visible:
     the binlog position can already be past a transaction the load cannot see yet,
     and resuming from that position would lose it. The executed GTID set only
     includes a transaction once it commits, and Debezium resumes from the GTID
     set, so such a transaction is replayed.
   - PostgreSQL: first create the wave's publication for its tables (or check that
     an existing one covers them), then create the connector's logical replication
     slot (``pg_create_logical_replication_slot(<slot>, 'pgoutput')``). A publication
     the run did not create must cover the tables, publish insert, update and delete,
     and have no row filter or column list (PostgreSQL 15+). The order
     matters: pgoutput decides what to publish from the catalog as of each change,
     so a change made before the publication exists is never published. From the
     slot's consistent point (P0) the database keeps every change for the slot.
     An existing slot is refused: it would replay from an unknown older point.
     The connection must be in autocommit mode (a slot cannot be created in a
     transaction that has written).

5. **Seed the connector's start offset to P0.**

   - MySQL: produce one record to the Kafka Connect offset topic the connector's
     worker configuration names (``offset.storage.topic``), before the connector
     is created. Its key is ``["<connector name>", {"server": "<topic.prefix>"}]``
     and its value Debezium's MySQL offset, ``{"file": ..., "pos": ..., "gtids":
     ...}`` (``OffsetSeed``).
   - PostgreSQL: nothing to seed. The connector is configured with the slot and
     the publication from step 4 (``slot.name``, ``publication.name``, and
     ``publication.autocreate.mode=disabled``; not ``filtered``, which rewrites an
     existing publication with ``ALTER PUBLICATION ... SET TABLE``). With no stored
     offset it streams from the slot's position.

6. **Load** each table by key range: split the key space (integer keys
   arithmetically, composite and string keys by ordered boundary sampling in the
   source's collation), read each range in key order in batches (keyset
   pagination), pass every row through the transform hook, and upsert the records
   by key. Every load write must complete before the connector starts (a parallel
   runner keeps this: see ``load_table``).
7. **Check the schemas again** and refuse if a wave table changed during the load,
   then read the source position at the end of the load (``load_end_position``).
8. **Start the connector** (``ConnectorControl.start``). MySQL: the first start
   uses ``snapshot.mode=recovery``: a seeded offset comes with an empty schema
   history topic, and recovery rebuilds the history from the current table
   structure without reading rows. That structure is the one at P0 only if no DDL
   ran on the wave's tables since P0, which step 7 checks; the adapter sets the
   mode back to the plan's ``no_data`` once the history is rebuilt, so a restart
   never rebuilds it again. DDL between step 7 and the recovery snapshot is not seen
   here; the adapter checks the fingerprint again once the history is rebuilt (#1436). PostgreSQL starts in ``no_data``; its change stream
   carries each relation's schema. The connector replays every change from P0, so
   those writes land after the load's and the newest value wins; a delete removes
   a row the load copied.
9. **Readiness** (``check_readiness``), without writing into the source: the
   connector's committed source position (advanced by its heartbeats) must have
   passed the load-end position and a barrier position read from the source
   (``read_barrier``) within the plan's ``max_lag_seconds``, and the source's row
   count per table (``SELECT COUNT(*)``) must match the target's where the parity
   gate compares counts. No sentinel row is ever written. On PostgreSQL the barrier
   is ``pg_current_wal_lsn()``, which is server-wide, while Debezium commits offsets
   only for captured changes: with quiet wave tables next to busy ones the gate can
   stay not ready (safe, but it stalls); tracked on #1436. On a table that never
   stops taking writes the counts may never match at one instant: the gate then
   needs a quiet period.

If anything fails after the capture and before the connector starts, the
replication slot and publication this run created are dropped (never ones it did
not create) and the error says so; if dropping them fails too, the original error is
kept and the message names what may still exist. If the connector itself fails to start, they are
kept so the start can be retried, and the error says that too.

Every SQL statement is built for the plan's source dialect; identifiers are checked
against an allowlist and quoted, and every value is a bound parameter. Table and
column names must be the catalog's names (on PostgreSQL, as stored, usually lower
case): they are quoted as given.

The source connection, the connector control, the offset reader and the target
writer are interfaces; their AWS and database implementations are #1436.
MariaDB, SQL Server and Oracle add a ``PositionCapture`` (and an offset seed) behind
the same interface (#1435); until then they are refused.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from src.contracts.data_movement_plan import DataMovementPlan, SourceSetting
from src.tools.data_movement.debezium_connector import ConnectorNames, wave_connector_names

Key = tuple[Any, ...]


class InitialLoadError(ValueError):
    """The plan, the source or the connector cannot run the handoff."""


class NoKeyError(InitialLoadError):
    """A table has no key to split, upsert or compact on."""


class UnsupportedDialectError(InitialLoadError):
    """No SQL dialect or position capture exists for the source yet."""


# --------------------------------------------------------------------------- SQL

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


@dataclass(frozen=True)
class SqlDialect:
    """Identifier quoting and the parameter placeholder of one source dialect."""

    name: Literal["mysql", "postgresql"]
    quote_char: str
    placeholder: str = "%s"

    def quote(self, identifier: str) -> str:
        if not _IDENTIFIER.match(identifier):
            raise InitialLoadError(f"identifier {identifier!r} is not a plain SQL identifier")
        return f"{self.quote_char}{identifier}{self.quote_char}"

    def table(self, table_id: str) -> str:
        """``schema.table`` (or ``table``) as a quoted, qualified name."""
        parts = table_id.split(".")
        if not 1 <= len(parts) <= 2:
            raise InitialLoadError(f"table id {table_id!r} is not schema.table")
        return ".".join(self.quote(p) for p in parts)

    def columns(self, columns: Sequence[str]) -> str:
        return ", ".join(self.quote(c) for c in columns)


_DIALECTS: dict[str, SqlDialect] = {
    "mysql": SqlDialect(name="mysql", quote_char="`"),
    "postgresql": SqlDialect(name="postgresql", quote_char='"'),
}


def sql_dialect(name: str | None) -> SqlDialect:
    """The SQL dialect for a plan's ``source.dialect``; refuses the others."""
    if name is None or name not in _DIALECTS:
        raise UnsupportedDialectError(
            f"source dialect {name!r} has no initial-load SQL; supported: {sorted(_DIALECTS)}"
        )
    return _DIALECTS[name]


Condition = tuple[int, str, Any]  # (key column position, "=" | "<" | ">", value)
Clause = list[list[Condition]]  # OR of ANDs


def _after(bound: Key) -> Clause:
    """``key > bound`` in lexicographic order, without row-value comparison."""
    return [
        [*((j, "=", bound[j]) for j in range(i)), (i, ">", bound[i])] for i in range(len(bound))
    ]


def _up_to(bound: Key) -> Clause:
    """``key <= bound`` in lexicographic order, without row-value comparison."""
    less = [
        [*((j, "=", bound[j]) for j in range(i)), (i, "<", bound[i])] for i in range(len(bound))
    ]
    return [*less, [(j, "=", bound[j]) for j in range(len(bound))]]


_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "=": lambda a, b: bool(a == b),
    "<": lambda a, b: bool(a < b),
    ">": lambda a, b: bool(a > b),
}


@dataclass(frozen=True)
class KeyRange:
    """Keys ``k`` with ``lower < k <= upper`` in the key's column order.

    ``None`` leaves that end open, so a split's first and last ranges also cover keys
    written below the minimum or above the maximum after the split.
    """

    lower: Key | None = None
    upper: Key | None = None

    def clauses(self) -> list[Clause]:
        """The range as AND-ed clauses: what the SQL renders and ``contains`` evaluates."""
        out: list[Clause] = []
        if self.lower is not None:
            out.append(_after(self.lower))
        if self.upper is not None:
            out.append(_up_to(self.upper))
        return out

    def contains(self, key: Key, order: Callable[[Any], Any] | None = None) -> bool:
        """Evaluates the same clauses the SQL predicate renders, for tests and fakes.

        Values compare in Python order, or through ``order`` (a sort key standing in
        for a collation). Never use it to classify real string keys: on a source, the
        database evaluates the predicate in its own collation, as it sampled the
        boundaries.
        """
        o = order or (lambda v: v)
        return all(
            any(all(_OPS[op](o(key[i]), o(v)) for i, op, v in conj) for conj in clause)
            for clause in self.clauses()
        )


def _render(dialect: SqlDialect, columns: Sequence[str], clause: Clause) -> tuple[str, list[Any]]:
    params: list[Any] = []
    terms: list[str] = []
    for conj in clause:
        parts = []
        for i, op, value in conj:
            parts.append(f"{dialect.quote(columns[i])} {op} {dialect.placeholder}")
            params.append(value)
        terms.append("(" + " AND ".join(parts) + ")")
    return "(" + " OR ".join(terms) + ")", params


def _check_bound(columns: Sequence[str], bound: Key | None) -> None:
    if bound is not None and len(bound) != len(columns):
        raise InitialLoadError(f"bound {bound!r} does not match key columns {list(columns)}")


# ------------------------------------------------------------------ source side


class SourceConnection(Protocol):
    """A DB-API style connection in autocommit mode: runs one statement with bound
    parameters and returns its rows as dicts (none for DDL)."""

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]: ...


class SourceReader(Protocol):
    """The reads the load and the readiness check make on the source."""

    def count(self, table: str) -> int: ...

    def key_bounds(self, table: str, column: str) -> tuple[Any, Any]: ...

    def sample_boundaries(
        self, table: str, key_columns: Sequence[str], step: int, total: int
    ) -> list[Key]: ...

    def read_range(
        self, table: str, key_columns: Sequence[str], key_range: KeyRange, limit: int
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class SqlSourceReader:
    """``SourceReader`` over a connection, with SQL built for the source dialect."""

    conn: SourceConnection
    dialect: SqlDialect

    def count(self, table: str) -> int:
        sql = f"SELECT COUNT(*) AS n FROM {self.dialect.table(table)}"  # nosec B608
        rows = self.conn.query(sql, ())
        return int(rows[0]["n"]) if rows else 0

    def key_bounds(self, table: str, column: str) -> tuple[Any, Any]:
        col = self.dialect.quote(column)
        sql = f"SELECT MIN({col}) AS lo, MAX({col}) AS hi FROM {self.dialect.table(table)}"  # nosec B608
        rows = self.conn.query(sql, ())
        return (rows[0]["lo"], rows[0]["hi"]) if rows else (None, None)

    def sample_boundaries(
        self, table: str, key_columns: Sequence[str], step: int, total: int
    ) -> list[Key]:
        """Every ``step``-th key in key order, below the ``total``-th (needs MySQL 8.0)."""
        cols = self.dialect.columns(key_columns)
        ph = self.dialect.placeholder
        # Identifiers are allowlisted and quoted; step and total are bound parameters.
        sql = (
            f"SELECT {cols} FROM (SELECT {cols}, ROW_NUMBER() OVER (ORDER BY {cols}) "  # nosec B608
            f"AS dm_rn FROM {self.dialect.table(table)}) dm_sample "
            f"WHERE MOD(dm_rn, {ph}) = 0 AND dm_rn < {ph} ORDER BY {cols}"
        )
        rows = self.conn.query(sql, (step, total))
        return [tuple(r[c] for c in key_columns) for r in rows]

    def read_range(
        self, table: str, key_columns: Sequence[str], key_range: KeyRange, limit: int
    ) -> list[dict[str, Any]]:
        _check_bound(key_columns, key_range.lower)
        _check_bound(key_columns, key_range.upper)
        where: list[str] = []
        params: list[Any] = []
        for clause in key_range.clauses():
            text, p = _render(self.dialect, key_columns, clause)
            where.append(text)
            params.extend(p)
        sql = f"SELECT * FROM {self.dialect.table(table)}"  # nosec B608
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {self.dialect.columns(key_columns)} LIMIT {self.dialect.placeholder}"
        params.append(limit)
        return self.conn.query(sql, tuple(params))


# --------------------------------------------------------------- range splitting


def integer_boundaries(lo: int, hi: int, chunks: int) -> list[Key]:
    """Upper bounds splitting ``[lo, hi]`` into up to ``chunks`` equal integer spans."""
    size = max(1, math.ceil((hi - lo + 1) / max(1, chunks)))
    out: list[Key] = []
    bound = lo + size - 1
    while bound < hi and len(out) < chunks - 1:
        out.append((bound,))
        bound += size
    return out


def sampled_boundaries(sorted_keys: Sequence[Key], chunks: int) -> list[Key]:
    """Every ``ceil(n / chunks)``-th key, below the last: what the sampling SQL returns."""
    n = len(sorted_keys)
    if n == 0 or chunks <= 1:
        return []
    step = math.ceil(n / chunks)
    return [sorted_keys[i - 1] for i in range(step, n, step)]


def ranges_from_boundaries(boundaries: Sequence[Key]) -> list[KeyRange]:
    """Contiguous ranges between ordered boundaries, open at both ends: no gap, no overlap."""
    edges: list[Key | None] = [None, *boundaries, None]
    return [KeyRange(lower=a, upper=b) for a, b in zip(edges, edges[1:], strict=False)]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def split_key_ranges(
    source: SourceReader, table: str, key_columns: Sequence[str], chunks: int
) -> list[KeyRange]:
    """Split a table's key space into at most ``chunks`` ranges, deterministically.

    A single integer key is split arithmetically between its minimum and maximum.
    Any other key (composite, string, date, ...) is split at every ``ceil(n/chunks)``-th
    key in the source's own key order, so the boundaries follow the source collation.
    """
    if not key_columns:
        raise NoKeyError(f"{table}: no key columns to split on")
    if chunks <= 1:
        return [KeyRange()]
    if len(key_columns) == 1:
        lo, hi = source.key_bounds(table, key_columns[0])
        if lo is None or hi is None:
            return [KeyRange()]
        if _is_int(lo) and _is_int(hi):
            return ranges_from_boundaries(integer_boundaries(lo, hi, chunks))
    total = source.count(table)
    if total == 0:
        return [KeyRange()]
    step = math.ceil(total / chunks)
    sampled = source.sample_boundaries(table, key_columns, step, total)
    boundaries = [k for i, k in enumerate(sampled) if i == 0 or k != sampled[i - 1]]
    return ranges_from_boundaries(boundaries)


# ------------------------------------------------------------ source positions


@dataclass(frozen=True)
class MySqlPosition:
    """A MySQL binlog coordinate and the executed GTID set at that point."""

    file: str
    pos: int
    gtid_set: str
    server_id: int


@dataclass(frozen=True)
class PostgresPosition:
    """A WAL position (LSN) of the connector's logical replication slot."""

    slot_name: str
    lsn: str


SourcePosition = MySqlPosition | PostgresPosition

GtidSet = dict[tuple[str, str], list[tuple[int, int]]]


def parse_gtid_set(text: str) -> GtidSet:
    """``uuid[:tag]:1-5:7,uuid2:1-3`` as merged intervals per (source uuid, tag)."""
    out: GtidSet = {}
    for part in re.sub(r"\s+", "", text).split(","):
        if not part:
            continue
        fields = part.split(":")
        uuid, tag = fields[0].lower(), ""
        for item in fields[1:]:
            match = re.fullmatch(r"(\d+)(?:-(\d+))?", item)
            if match is None:
                tag = item.lower()
                continue
            lo = int(match.group(1))
            hi = int(match.group(2) or lo)
            out.setdefault((uuid, tag), []).append((lo, hi))
    for key, intervals in out.items():
        merged: list[tuple[int, int]] = []
        for lo, hi in sorted(intervals):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
            else:
                merged.append((lo, hi))
        out[key] = merged
    return out


def gtid_set_contains(outer: str, inner: str) -> bool:
    """Whether every transaction of ``inner`` is in ``outer``."""
    have = parse_gtid_set(outer)
    for key, intervals in parse_gtid_set(inner).items():
        for lo, hi in intervals:
            if not any(a <= lo and hi <= b for a, b in have.get(key, [])):
                return False
    return True


def parse_lsn(text: str) -> int:
    """A PostgreSQL LSN, ``X/Y`` in hexadecimal, as one integer."""
    match = re.fullmatch(r"([0-9A-Fa-f]+)/([0-9A-Fa-f]+)", text.strip())
    if match is None:
        raise InitialLoadError(f"{text!r} is not a PostgreSQL LSN")
    return (int(match.group(1), 16) << 32) + int(match.group(2), 16)


def position_reached(committed: SourcePosition, target: SourcePosition) -> bool:
    """Whether a connector at ``committed`` has processed everything up to ``target``.

    MySQL compares GTID sets (the binlog file and position are only a hint); PostgreSQL
    compares LSNs.
    """
    if isinstance(committed, MySqlPosition) and isinstance(target, MySqlPosition):
        return gtid_set_contains(committed.gtid_set, target.gtid_set)
    if isinstance(committed, PostgresPosition) and isinstance(target, PostgresPosition):
        return parse_lsn(committed.lsn) >= parse_lsn(target.lsn)
    raise InitialLoadError(
        f"cannot compare {type(committed).__name__} with {type(target).__name__}"
    )


# ------------------------------------------------------------ position capture


class PositionReader(Protocol):
    """Reads the source's current change-log position, read-only."""

    def current(self) -> SourcePosition: ...


class PositionCapture(PositionReader, Protocol):
    """Captures the position the connector starts from (one per dialect).

    ``capture`` may create source objects (the PostgreSQL publication and slot);
    ``created`` names them and ``release`` drops exactly those, never others.
    """

    def capture(self) -> SourcePosition: ...

    def created(self) -> list[str]: ...

    def release(self) -> list[str]: ...


def _version(text: str) -> tuple[int, int]:
    match = re.match(r"^(\d+)\.(\d+)", text)
    if not match:
        raise InitialLoadError(f"cannot read the MySQL version from {text!r}")
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class MySqlPositionCapture:
    """Reads the binlog coordinate and GTID set from MySQL 8.0+ or Aurora MySQL 3."""

    conn: SourceConnection

    def _status_sql(self) -> str:
        version = self.conn.query("SELECT VERSION() AS version", ())
        major_minor = _version(str(version[0]["version"])) if version else (0, 0)
        if major_minor < (8, 0):
            raise InitialLoadError(
                f"MySQL {major_minor[0]}.{major_minor[1]} is not supported: the load needs "
                "8.0 or later (ROW_NUMBER() for key sampling)"
            )
        # SHOW MASTER STATUS was replaced by SHOW BINARY LOG STATUS in 8.2; it is
        # MySQL's own statement name.
        return "SHOW BINARY LOG STATUS" if major_minor >= (8, 2) else "SHOW MASTER STATUS"

    def _server_id(self) -> int:
        server = self.conn.query(
            "SELECT @@GLOBAL.gtid_mode AS gtid_mode, @@server_id AS server_id", ()
        )
        mode = str(server[0].get("gtid_mode", "")).upper() if server else ""
        if mode != "ON":
            raise InitialLoadError(
                f"gtid_mode is {mode or 'unknown'}, not ON: without GTIDs a transaction whose "
                "binlog event is written but whose commit is not yet visible could be lost "
                "between the captured position and the load's reads. Turn on gtid_mode (and "
                "enforce_gtid_consistency) in the parameter group; Aurora MySQL 3 supports it"
            )
        return int(server[0]["server_id"])

    def _read(self, status_sql: str, server_id: int) -> MySqlPosition:
        status = self.conn.query(status_sql, ())
        if not status or not status[0].get("File"):
            raise InitialLoadError(
                "the source returned no binary log position: binary logging is off"
            )
        row = status[0]
        return MySqlPosition(
            file=str(row["File"]),
            pos=int(row["Position"]),
            gtid_set=re.sub(r"\s+", "", str(row.get("Executed_Gtid_Set") or "")),
            server_id=server_id,
        )

    def current(self) -> MySqlPosition:
        status_sql = self._status_sql()
        return self._read(status_sql, self._server_id())

    def capture(self) -> MySqlPosition:
        return self.current()

    def created(self) -> list[str]:
        return []

    def release(self) -> list[str]:
        return []


def _schema_and_table(table_id: str, default_schema: str) -> tuple[str, str]:
    parts = table_id.split(".")
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return default_schema, parts[0]
    raise InitialLoadError(f"table id {table_id!r} is not schema.table")


class PostgresSlotCapture:
    """Creates the wave's publication, then the connector's slot, before the load."""

    def __init__(
        self,
        conn: SourceConnection,
        slot_name: str,
        publication_name: str,
        tables: Sequence[str],
        plugin: str = "pgoutput",
    ):
        self.conn = conn
        self.slot_name = slot_name
        self.publication_name = publication_name
        self.tables = list(tables)
        self.plugin = plugin
        self._dialect = sql_dialect("postgresql")
        self._created_slot = False
        self._created_publication = False

    def _ensure_publication(self) -> None:
        pub = self._dialect.quote(self.publication_name)
        wanted = {_schema_and_table(t, "public") for t in self.tables}
        existing = self.conn.query(
            "SELECT pubinsert, pubupdate, pubdelete FROM pg_publication WHERE pubname = %s",
            (self.publication_name,),
        )
        if existing:
            self._check_existing(existing[0], wanted)
            return
        tables = ", ".join(self._dialect.table(t) for t in self.tables)
        # DDL takes no parameters: the names are allowlisted and quoted.
        self.conn.query(f"CREATE PUBLICATION {pub} FOR TABLE {tables}", ())  # nosec B608
        self._created_publication = True

    def _check_existing(self, pub_row: Mapping[str, Any], wanted: set[tuple[str, str]]) -> None:
        """A publication the run did not create must publish every insert, update and
        delete of every wave table, with no row filter and no column list."""
        name = self.publication_name
        skipped = [a for a in ("insert", "update", "delete") if not pub_row.get(f"pub{a}")]
        if skipped:
            raise InitialLoadError(
                f"publication {name!r} does not publish {', '.join(skipped)}; changes made "
                "during the load would be dropped. Use publish = 'insert, update, delete' or "
                "drop it before the load"
            )
        rows = self.conn.query(
            "SELECT schemaname, tablename FROM pg_publication_tables WHERE pubname = %s",
            (self.publication_name,),
        )
        covered = {(str(r["schemaname"]), str(r["tablename"])) for r in rows}
        missing = sorted(f"{s}.{t}" for s, t in wanted - covered)
        if missing:
            raise InitialLoadError(
                f"publication {name!r} exists but does not cover "
                f"{', '.join(missing)}; add them or drop it before the load"
            )
        version = self.conn.query("SELECT current_setting('server_version_num') AS v", ())
        if version and int(version[0]["v"]) >= 150000:  # row filters and column lists: 15+
            rels = self.conn.query(
                "SELECT n.nspname AS schemaname, c.relname AS tablename, "
                "r.prqual IS NOT NULL AS row_filter, r.prattrs IS NOT NULL AS column_list "
                "FROM pg_publication_rel r JOIN pg_publication p ON p.oid = r.prpubid "
                "JOIN pg_class c ON c.oid = r.prrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE p.pubname = %s",
                (name,),
            )
            narrowed = sorted(
                f"{r['schemaname']}.{r['tablename']}"
                for r in rels
                if (str(r["schemaname"]), str(r["tablename"])) in wanted
                and (r.get("row_filter") or r.get("column_list"))
            )
            if narrowed:
                raise InitialLoadError(
                    f"publication {name!r} has a row filter or column list on "
                    f"{', '.join(narrowed)}; the connector would miss rows or columns"
                )

    def capture(self) -> PostgresPosition:
        if not self.tables:
            raise InitialLoadError("PostgreSQL handoff needs at least one table to publish")
        existing = self.conn.query(
            "SELECT slot_name FROM pg_replication_slots WHERE slot_name = %s", (self.slot_name,)
        )
        if existing:
            raise InitialLoadError(
                f"replication slot {self.slot_name!r} already exists; drop it "
                "(pg_drop_replication_slot) or pick another slot before the load"
            )
        self._ensure_publication()
        rows = self.conn.query(
            "SELECT lsn::text AS lsn FROM pg_create_logical_replication_slot(%s, %s)",
            (self.slot_name, self.plugin),
        )
        if not rows:
            raise InitialLoadError(f"creating replication slot {self.slot_name!r} returned no LSN")
        self._created_slot = True
        return PostgresPosition(slot_name=self.slot_name, lsn=str(rows[0]["lsn"]))

    def current(self) -> PostgresPosition:
        rows = self.conn.query("SELECT pg_current_wal_lsn()::text AS lsn", ())
        return PostgresPosition(slot_name=self.slot_name, lsn=str(rows[0]["lsn"]))

    def created(self) -> list[str]:
        out = []
        if self._created_slot:
            out.append(f"replication slot {self.slot_name}")
        if self._created_publication:
            out.append(f"publication {self.publication_name}")
        return out

    def release(self) -> list[str]:
        dropped = []
        if self._created_slot:
            self.conn.query("SELECT pg_drop_replication_slot(%s)", (self.slot_name,))
            self._created_slot = False
            dropped.append(f"replication slot {self.slot_name}")
        if self._created_publication:
            pub = self._dialect.quote(self.publication_name)
            self.conn.query(f"DROP PUBLICATION {pub}", ())  # nosec B608
            self._created_publication = False
            dropped.append(f"publication {self.publication_name}")
        return dropped


def position_capture_for(
    dialect: str | None,
    conn: SourceConnection,
    *,
    slot_name: str | None = None,
    publication_name: str | None = None,
    tables: Sequence[str] = (),
) -> PositionCapture:
    """The position capture for a source dialect; MariaDB, SQL Server and Oracle are refused.

    PostgreSQL takes the slot and publication names the wave's connector configuration
    uses (``slot.name``, ``publication.name``) and the wave's tables.
    """
    # A plan's handoff takes them from the connector generator: position_capture_for_plan.
    if dialect == "mysql":
        return MySqlPositionCapture(conn)
    if dialect == "postgresql":
        if not slot_name or not publication_name:
            raise InitialLoadError(
                "PostgreSQL position capture needs the connector's slot and publication names"
            )
        return PostgresSlotCapture(conn, slot_name, publication_name, tables)
    raise UnsupportedDialectError(f"no position capture for source dialect {dialect!r} yet")


def handoff_names(plan: DataMovementPlan) -> ConnectorNames:
    """The wave's connector, slot and publication names, as the Debezium connector
    generator renders them (``wave_connector_names``), so the two never drift apart."""
    if not plan.cdc.topic_prefix:
        raise InitialLoadError("the handoff needs a Debezium connector with a topic prefix")
    return wave_connector_names(plan.cdc.topic_prefix, plan.wave)


def position_capture_for_plan(plan: DataMovementPlan, conn: SourceConnection) -> PositionCapture:
    """The position capture for a plan's source, with the generator's slot and publication
    names and the plan's tables."""
    names = handoff_names(plan)
    return position_capture_for(
        plan.source.dialect,
        conn,
        slot_name=names.slot,
        publication_name=names.publication,
        tables=[t.table for t in load_tables(plan)],
    )


@dataclass(frozen=True)
class OffsetSeed:
    """One Kafka Connect source offset record: what Debezium resumes from."""

    connector_name: str
    partition: Mapping[str, Any]
    offset: Mapping[str, Any]

    def kafka_key(self) -> str:
        """The record key in the offset topic (JSON converter, no schemas)."""
        return json.dumps([self.connector_name, dict(self.partition)], separators=(",", ":"))

    def kafka_value(self) -> str:
        return json.dumps(dict(self.offset), separators=(",", ":"), sort_keys=True)


def mysql_offset_seed(
    position: MySqlPosition, *, connector_name: str, topic_prefix: str
) -> OffsetSeed:
    """Debezium's MySQL offset at ``position``, keyed on the connector's ``topic.prefix``."""
    offset: dict[str, Any] = {"file": position.file, "pos": position.pos}
    if position.gtid_set:
        offset["gtids"] = position.gtid_set
    return OffsetSeed(
        connector_name=connector_name, partition={"server": topic_prefix}, offset=offset
    )


# --------------------------------------------------------------- source catalog


class SourceCatalog(Protocol):
    """Read-only checks on the source: settings and table schemas."""

    def unmet_settings(self, settings: Sequence[SourceSetting]) -> list[str]: ...

    def schema_fingerprint(self, tables: Sequence[str]) -> str: ...


def _first_int(text: str) -> int | None:
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SqlSourceCatalog:
    """``SourceCatalog`` over a connection; every statement is a read."""

    conn: SourceConnection
    dialect: SqlDialect
    database: str

    @property
    def _default_schema(self) -> str:
        return self.database if self.dialect.name == "mysql" else "public"

    def _one(self, sql: str, params: Sequence[Any] = ()) -> Any:
        rows = self.conn.query(sql, params)
        return rows[0]["v"] if rows else None

    def _mysql_variable(self, name: str) -> Any:
        # Variable names cannot be bound; only these fixed names are ever read.
        sql = {
            "binlog_format": "SELECT @@GLOBAL.binlog_format AS v",
            "binlog_row_image": "SELECT @@GLOBAL.binlog_row_image AS v",
            "binlog_expire_logs_seconds": "SELECT @@GLOBAL.binlog_expire_logs_seconds AS v",
            "gtid_mode": "SELECT @@GLOBAL.gtid_mode AS v",
            "enforce_gtid_consistency": "SELECT @@GLOBAL.enforce_gtid_consistency AS v",
        }[name]
        return self._one(sql)

    def _pg_setting(self, name: str) -> Any:
        return self._one("SELECT setting AS v FROM pg_settings WHERE name = %s", (name,))

    def signal_table_location(self, table_id: str) -> tuple[str, str]:
        """Where the plan's signal table lives, as (schema, table).

        The plan names it ``<database>.debezium_signal``. On MySQL a database is a
        schema. On PostgreSQL the connection is already in that database, so a leading
        database name is dropped and the table is in ``public``, unless the plan names a
        schema (``schema.table`` with another first part, or ``database.schema.table``).
        """
        parts = table_id.split(".")
        if self.dialect.name == "mysql" or len(parts) < 2:
            return _schema_and_table(table_id, self._default_schema)
        if len(parts) == 3 and parts[0] == self.database:
            return parts[1], parts[2]
        if len(parts) == 2:
            return ("public", parts[1]) if parts[0] == self.database else (parts[0], parts[1])
        raise InitialLoadError(f"signal table {table_id!r} is not [database.][schema.]table")

    def _signal_table_exists(self, table_id: str) -> bool:
        schema, table = self.signal_table_location(table_id)
        rows = self.conn.query(
            "SELECT table_name AS v FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        return bool(rows)

    def _check(self, setting: SourceSetting) -> str | None:
        """None when met, else why not."""
        name, want = setting.name, setting.value
        mysql = self.dialect.name == "mysql"
        if name == "signal table":
            table_id = want.split(" ", 1)[0]
            return None if self._signal_table_exists(table_id) else f"{table_id} does not exist"
        if mysql and name in (
            "binlog_format",
            "binlog_row_image",
            "gtid_mode",
            "enforce_gtid_consistency",
        ):
            got = self._mysql_variable(name)
            return None if str(got).upper() == want.upper() else f"is {got}, needs {want}"
        if mysql and name == "binlog retention":
            got = _as_int(self._mysql_variable("binlog_expire_logs_seconds"))
            hours = _first_int(want) or 0
            if got is None:
                return "binlog_expire_logs_seconds is unknown"
            if got == 0 or got >= hours * 3600:  # 0: binlogs are never purged
                return None
            return f"binlog_expire_logs_seconds is {got}, needs at least {hours * 3600}"
        if mysql and name == "binlog retention hours":
            rows = self.conn.query("CALL mysql.rds_show_configuration", ())
            got = next((r.get("value") for r in rows if r.get("name") == name), None)
            hours = _first_int(want) or 0
            if _as_int(got) is None or (_as_int(got) or 0) < hours:
                return (
                    f"is {got if got is not None else 'NULL'}, needs at least {hours}: "
                    "binlogs may be purged during the load"
                )
            return None
        if not mysql and name == "plugin.name":
            return None if want == "pgoutput" else f"plugin {want} is not supported"
        if not mysql and name in ("rds.logical_replication", "wal_level"):
            got = str(self._pg_setting(name))
            ok = got.lower() in ("on", "1") if name == "rds.logical_replication" else got == want
            return None if ok else f"is {got}, needs {want}"
        if not mysql and name in ("max_replication_slots", "max_wal_senders"):
            got_int = _as_int(self._pg_setting(name))
            need = _first_int(want) or 1
            return (
                None if got_int is not None and got_int >= need else f"is {got_int}, needs {want}"
            )
        return "cannot be checked read-only by the handoff"

    def unmet_settings(self, settings: Sequence[SourceSetting]) -> list[str]:
        out = []
        for s in settings:
            why = self._check(s)
            if why is not None:
                out.append(f"{s.name}: {why}")
        return out

    def schema_fingerprint(self, tables: Sequence[str]) -> str:
        """A hash of the tables' columns: name, position, full type, nullability, size,
        precision, collation, and on MySQL the column type with its ENUM/SET members and
        UNSIGNED and the character set (binlog rows are decoded with the rebuilt schema,
        ENUMs by index), on PostgreSQL the underlying type name."""
        if not tables:
            return hashlib.sha256(b"[]").hexdigest()
        where: list[str] = []
        params: list[Any] = []
        for t in tables:
            schema, table = _schema_and_table(t, self._default_schema)
            where.append("(table_schema = %s AND table_name = %s)")
            params.extend([schema, table])
        # Only placeholders are joined in; schema and table names are bound parameters.
        extra = (
            "column_type AS ct, character_set_name AS cs, "
            if self.dialect.name == "mysql"
            else "udt_name AS ut, "
        )
        sql = (
            "SELECT table_schema AS s, table_name AS t, column_name AS c, "  # nosec B608
            "ordinal_position AS p, data_type AS d, " + extra + "is_nullable AS n, "
            "character_maximum_length AS l, numeric_precision AS np, numeric_scale AS ns, "
            "datetime_precision AS dp, collation_name AS co "
            "FROM information_schema.columns WHERE "
            + " OR ".join(where)
            + " ORDER BY table_schema, table_name, ordinal_position"
        )
        rows = self.conn.query(sql, tuple(params))
        blob = json.dumps(rows, default=str, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()


# ------------------------------------------------------------ transform and target


@dataclass(frozen=True)
class LoadRow:
    """One source row, as read by the load."""

    table: str
    key_names: tuple[str, ...]
    values: Mapping[str, Any]

    @property
    def key(self) -> Key:
        try:
            return tuple(self.values[c] for c in self.key_names)
        except KeyError as exc:
            raise InitialLoadError(f"{self.table}: row has no key column {exc.args[0]!r}") from exc


@dataclass(frozen=True)
class TargetRecord:
    """One item the target writer upserts by ``(collection, key)``."""

    collection: str
    key: Key
    values: Mapping[str, Any]


#: The per-row hook: the compiled transformation contract plugs in here, so embedded
#: fields and reverse indexes are built during the load. One row may give several
#: records. A record keyed on a non-key column (a reverse index entry) is only
#: idempotent if the CDC writer removes the stale entry from the change's before-image
#: when that column changes; the CDC writer must do so (#1436, #1428).
RowTransform = Callable[[LoadRow], Sequence[TargetRecord]]


def identity_transform(row: LoadRow) -> list[TargetRecord]:
    """Copy the row unchanged, under its table and key."""
    return [TargetRecord(collection=row.table, key=row.key, values=dict(row.values))]


class TargetCounter(Protocol):
    def count(self, collection: str) -> int: ...


class TargetWriter(TargetCounter, Protocol):
    """Writes records idempotently: a second upsert of a key replaces the first."""

    def upsert(self, records: Sequence[TargetRecord]) -> None: ...


class ConnectorControl(Protocol):
    """The Debezium connector's lifecycle, as the handoff needs it."""

    def is_running(self) -> bool: ...

    def seed_offset(self, seed: OffsetSeed) -> None: ...

    def start(self, snapshot_mode: str) -> None: ...


# ------------------------------------------------------------------- handoff


class HandoffFailed(InitialLoadError):
    """The handoff stopped after the capture; the message says what was cleaned up."""


@dataclass(frozen=True)
class LoadTable:
    table: str
    key_names: tuple[str, ...]


@dataclass(frozen=True)
class HandoffSettings:
    """Run settings the plan does not carry."""

    #: The deployed connector's name; None for the generator's default
    #: (``debezium-<prefix>-wave-<n>``). The MySQL offset seed is keyed on it.
    connector_name: str | None = None
    chunks_per_table: int = 16
    batch_size: int = 1000


@dataclass(frozen=True)
class TableLoadResult:
    table: str
    chunks: int
    rows_read: int
    records_written: int


@dataclass(frozen=True)
class HandoffResult:
    position: SourcePosition
    offset_seed: OffsetSeed | None
    start_snapshot_mode: str
    tables: list[TableLoadResult]
    load_started_ms: int
    load_finished_ms: int
    load_end_position: SourcePosition
    schema_fingerprint: str


_CAPTURE_KIND = {"mysql": "binlog_position", "postgresql": "replication_slot_lsn"}

#: The first start after the handoff: MySQL rebuilds its empty schema history from the
#: current table structure (Debezium's ``recovery`` mode); PostgreSQL has no history.
FIRST_START_MODE = {"mysql": "recovery", "postgresql": "no_data"}


def load_tables(plan: DataMovementPlan) -> list[LoadTable]:
    """The plan's source tables with their keys: the hub topic's key, else the primary key."""
    hub_keys = {t.table: t.key_columns for t in plan.hub}
    out: list[LoadTable] = []
    for t in plan.source.tables:
        key = hub_keys.get(t.table) or t.primary_key
        if not key:
            raise NoKeyError(
                f"{t.table} has no primary key and no hub key: the load cannot split it and "
                "CDC replay cannot upsert it"
            )
        out.append(LoadTable(table=t.table, key_names=tuple(key)))
    return out


def _check_plan(plan: DataMovementPlan) -> SqlDialect:
    dialect = sql_dialect(plan.source.dialect)
    load = plan.initial_load
    if load.strategy != "bulk_load_with_position_handoff":
        raise InitialLoadError(
            f"initial_load.strategy is {load.strategy!r}, not a position handoff"
        )
    if load.position_capture != _CAPTURE_KIND[dialect.name]:
        raise InitialLoadError(
            f"initial_load.position_capture {load.position_capture!r} does not match "
            f"dialect {dialect.name!r}"
        )
    if plan.cdc.tool != "debezium" or not plan.cdc.topic_prefix:
        raise InitialLoadError("the handoff needs a Debezium connector with a topic prefix")
    if plan.cdc.snapshot_mode != "no_data":
        raise InitialLoadError(
            f"cdc.snapshot_mode is {plan.cdc.snapshot_mode!r}; the handoff needs no_data, or "
            "the connector would snapshot the tables again"
        )
    return dialect


def load_table(
    source: SourceReader,
    target: TargetWriter,
    table: LoadTable,
    ranges: Sequence[KeyRange],
    *,
    transform: RowTransform = identity_transform,
    batch_size: int = 1000,
) -> TableLoadResult:
    """Copy every range of one table, in key order, batch by batch.

    Each range is a unit a runtime may run in parallel. Contract for such a runner:
    every call must have returned (all its upserts acknowledged) before the connector
    starts, and nothing else writes to the target before then. A load write landing
    after a CDC write of the same key could bring back an older value or a deleted row.
    """
    rows_read = written = 0
    for key_range in ranges:
        lower = key_range.lower
        while True:
            rows = source.read_range(
                table.table,
                table.key_names,
                KeyRange(lower=lower, upper=key_range.upper),
                batch_size,
            )
            records: list[TargetRecord] = []
            last: Key | None = None
            for values in rows:
                row = LoadRow(table=table.table, key_names=table.key_names, values=values)
                records.extend(transform(row))
                last = row.key
            if records:
                target.upsert(records)
            rows_read += len(rows)
            written += len(records)
            if len(rows) < batch_size or last is None:
                break
            lower = last
    return TableLoadResult(
        table=table.table, chunks=len(ranges), rows_read=rows_read, records_written=written
    )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def run_handoff(
    plan: DataMovementPlan,
    *,
    source: SourceReader,
    catalog: SourceCatalog,
    capture: PositionCapture,
    connector: ConnectorControl,
    target: TargetWriter,
    settings: HandoffSettings,
    transform: RowTransform = identity_transform,
    clock: Callable[[], int] = _now_ms,
) -> HandoffResult:
    """Check, capture P0, seed the connector, load every table, then start the connector."""
    dialect = _check_plan(plan)
    tables = load_tables(plan)
    if connector.is_running():
        raise InitialLoadError(
            "the connector is running; the handoff starts it after the load from the captured "
            "position"
        )
    unmet = catalog.unmet_settings(plan.cdc.required_source_settings)
    if unmet:
        raise InitialLoadError("source settings not met: " + "; ".join(unmet))
    table_ids = [t.table for t in tables]
    fingerprint = catalog.schema_fingerprint(table_ids)
    try:
        position = capture.capture()
        seed: OffsetSeed | None = None
        if isinstance(position, MySqlPosition):
            seed = mysql_offset_seed(
                position,
                connector_name=settings.connector_name or handoff_names(plan).connector,
                topic_prefix=plan.cdc.topic_prefix or "",  # never empty: checked by _check_plan
            )
            connector.seed_offset(seed)
        started = clock()
        results = [
            load_table(
                source,
                target,
                t,
                split_key_ranges(source, t.table, t.key_names, settings.chunks_per_table),
                transform=transform,
                batch_size=settings.batch_size,
            )
            for t in tables
        ]
        finished = clock()
        if catalog.schema_fingerprint(table_ids) != fingerprint:
            raise InitialLoadError(
                "a wave table's schema changed during the load; the connector would decode "
                "the changes since the captured position with the new schema. Re-run the "
                "handoff when no DDL is running on the wave's tables"
            )
        load_end = capture.current()
    except Exception as exc:
        try:
            dropped = capture.release()
            cleanup = (
                "dropped " + ", ".join(dropped) + " (created by this run)"
                if dropped
                else "nothing to clean up"
            )
        except Exception as release_exc:  # keep the original error; say what is left
            left = ", ".join(capture.created()) or "objects this run created"
            cleanup = (
                f"cleanup also failed ({release_exc}): {left} may still exist; a replication "
                "slot holds WAL on the source until it is dropped (pg_drop_replication_slot)"
            )
        raise HandoffFailed(
            f"handoff failed before the connector started: {exc}; {cleanup}"
        ) from exc
    mode = FIRST_START_MODE[dialect.name]
    try:
        connector.start(snapshot_mode=mode)
    except Exception as exc:
        kept = capture.created()
        note = (
            "kept " + ", ".join(kept) + " so the start can be retried; drop them if you "
            "abandon the handoff"
            if kept
            else "nothing was created on the source"
        )
        raise HandoffFailed(
            f"the load finished but the connector did not start: {exc}; {note}"
        ) from exc
    return HandoffResult(
        position=position,
        offset_seed=seed,
        start_snapshot_mode=mode,
        tables=results,
        load_started_ms=started,
        load_finished_ms=finished,
        load_end_position=load_end,
        schema_fingerprint=fingerprint,
    )


# ------------------------------------------------------------------ readiness


class OffsetReader(Protocol):
    """The connector's committed source position, read from its offset topic.

    Debezium's heartbeats keep committing this offset while the captured tables are
    quiet, so it advances even with no changes to deliver.
    """

    def committed_position(self) -> SourcePosition | None: ...


@dataclass(frozen=True)
class Barrier:
    """A source position read at a known time: the connector passing it bounds the lag."""

    position: SourcePosition
    read_at_ms: int


def read_barrier(reader: PositionReader, *, now_ms: int) -> Barrier:
    """Read the source's current position, read-only, as a lag barrier."""
    return Barrier(position=reader.current(), read_at_ms=now_ms)


@dataclass(frozen=True)
class TableCount:
    table: str
    source_rows: int
    target_rows: int
    compared: bool  # False for a cache, whose parity gate samples keys instead


@dataclass(frozen=True)
class Readiness:
    ready: bool
    #: Upper bound on the connector's lag, once it has passed the barrier
    #: (now minus the barrier's read time); None while it has not.
    lag_seconds: float | None
    counts: list[TableCount] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def check_readiness(
    plan: DataMovementPlan,
    *,
    source: SourceReader,
    offsets: OffsetReader,
    target: TargetCounter,
    handoff: HandoffResult,
    barrier: Barrier,
    now_ms: int,
) -> Readiness:
    """Whether CDC has caught up after the load, from source positions and counts only.

    The caller reads a barrier (``read_barrier``) and polls this check. While the
    connector has not passed the barrier it is not ready; once it has, its lag was at
    most ``now - barrier.read_at_ms``. A barrier older than ``max_lag_seconds`` can no
    longer pass: read a new one. Reads the source (``SELECT COUNT(*)`` per table) and
    never writes into it.
    """
    reasons: list[str] = []
    committed = offsets.committed_position()
    lag: float | None = None
    if committed is None:
        reasons.append("the connector has committed no source offset yet")
    else:
        if not position_reached(committed, handoff.load_end_position):
            reasons.append("the connector has not replayed the load window yet")
        if position_reached(committed, barrier.position):
            lag = max(0, now_ms - barrier.read_at_ms) / 1000
            if lag > plan.gates.max_lag_seconds:
                reasons.append(
                    f"lag bound {lag:.1f}s is over the {plan.gates.max_lag_seconds}s gate; "
                    "read a new barrier"
                )
        else:
            reasons.append("the connector is behind the barrier position")
    count_check = {p.table for p in plan.gates.parity if p.check == "row_count_and_key_checksum"}
    counts: list[TableCount] = []
    for t in plan.source.tables:
        src = source.count(t.table)
        tgt = target.count(t.table)
        compared = t.table in count_check
        counts.append(
            TableCount(table=t.table, source_rows=src, target_rows=tgt, compared=compared)
        )
        if compared and src != tgt:
            reasons.append(f"{t.table}: source has {src} rows, target {tgt}")
    return Readiness(ready=not reasons, lag_seconds=lag, counts=counts, reasons=reasons)
