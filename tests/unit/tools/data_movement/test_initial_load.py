"""The initial-load-to-CDC handoff on fakes (#1427).

No database, broker or AWS call is made. The source is an in-memory fake with a
change log, transactions whose binlog event is written before their commit is
visible, publications and slots created at a log position, and an optional
collation; the real position captures run over fake connections that answer from
it. The SQL the module builds is checked as text plus its parameters.

Delivery is at least once with idempotent convergence: CDC may apply a change the
load already copied, and the tests check that the result is the same.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.contracts.data_movement_plan import (
    CdcSpec,
    DataMovementPlan,
    Gates,
    HubTopic,
    InitialLoad,
    ParityGate,
    SourceSetting,
    SourceSpec,
    SourceTable,
)
from src.tools.data_movement.debezium_connector import wave_connector_names
from src.tools.data_movement.initial_load import (
    Barrier,
    HandoffFailed,
    HandoffResult,
    HandoffSettings,
    InitialLoadError,
    KeyRange,
    LoadRow,
    MySqlPosition,
    MySqlPositionCapture,
    NoKeyError,
    OffsetSeed,
    PostgresPosition,
    PostgresSlotCapture,
    SqlSourceCatalog,
    SqlSourceReader,
    TargetRecord,
    UnsupportedDialectError,
    check_readiness,
    gtid_set_contains,
    handoff_names,
    identity_transform,
    integer_boundaries,
    load_tables,
    mysql_offset_seed,
    parse_gtid_set,
    parse_lsn,
    position_capture_for,
    position_capture_for_plan,
    position_reached,
    ranges_from_boundaries,
    read_barrier,
    run_handoff,
    sampled_boundaries,
    split_key_ranges,
    sql_dialect,
)

UUID = "3e11fa47-71ca-11e1-9e33-c80aa9429562"
TABLE = "shop.orders"


def _render_gtids(executed: set[int]) -> str:
    """``uuid:1-3:5`` for a set of transaction numbers."""
    if not executed:
        return ""
    parts: list[str] = []
    run: list[int] = []
    for n in sorted(executed):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            parts.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [n]
    parts.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return f"{UUID}:" + ":".join(parts)


# --------------------------------------------------------------------------- fakes


class RecordingConnection:
    """A source connection that records every statement and answers from a script.

    ``answers`` maps a predicate on the SQL text to the rows it returns; the first
    matching predicate wins. Unmatched statements return no rows.
    """

    def __init__(
        self,
        answers: Sequence[tuple[Callable[[str], bool], list[dict[str, Any]]]] = (),
        exact: Sequence[tuple[str, tuple[Any, ...], list[dict[str, Any]]]] = (),
    ):
        self.answers = list(answers)
        #: (SQL substring, exact parameters, rows): answered only for those parameters.
        self.exact = list(exact)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        self.calls.append((sql, tuple(params)))
        for fragment, want, rows in self.exact:
            if fragment in sql:
                return rows if tuple(params) == want else []
        for matches, rows in self.answers:
            if matches(sql):
                return rows
        return []


@dataclass
class Entry:
    """One change in the source's log (binlog / WAL), with its transaction number."""

    op: str
    table: str
    key: tuple[Any, ...]
    row: dict[str, Any] | None
    gtid: int


def _identity(v: Any) -> Any:
    return v


@dataclass
class FakeDatabase:
    """An in-memory source with a change log, read through the SourceReader shape.

    ``begin_write`` writes a change to the log (and so moves the binlog position)
    without making it visible or adding it to the executed GTID set; ``commit`` makes
    it visible. ``order`` stands in for the source collation.
    """

    key_columns: dict[str, tuple[str, ...]]
    order: Callable[[Any], Any] = _identity
    rows: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = field(default_factory=dict)
    log: list[Entry] = field(default_factory=list)
    executed: set[int] = field(default_factory=set)
    events: list[str] = field(default_factory=list)
    publications: dict[str, tuple[int, frozenset[str]]] = field(default_factory=dict)
    slots: dict[str, int] = field(default_factory=dict)
    schema_version: int = 0
    on_read: Callable[[FakeDatabase, int], None] | None = None
    fail_on_read: int | None = None
    reads: int = 0

    def begin_write(self, table: str, row: dict[str, Any] | None, key: Any = None) -> Entry:
        k = tuple(row[c] for c in self.key_columns[table]) if row is not None else key
        entry = Entry("delete" if row is None else "upsert", table, k, row, len(self.log) + 1)
        self.log.append(entry)
        return entry

    def commit(self, entry: Entry) -> None:
        if entry.row is None:
            self.rows.get(entry.table, {}).pop(entry.key, None)
        else:
            self.rows.setdefault(entry.table, {})[entry.key] = dict(entry.row)
        self.executed.add(entry.gtid)

    def write(self, table: str, row: dict[str, Any]) -> None:
        self.commit(self.begin_write(table, row))

    def delete(self, table: str, key: tuple[Any, ...]) -> None:
        self.commit(self.begin_write(table, None, key))

    def _sort(self, key: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(self.order(v) for v in key)

    def _sorted_keys(self, table: str) -> list[tuple[Any, ...]]:
        return sorted(self.rows.get(table, {}), key=self._sort)

    # SourceReader
    def count(self, table: str) -> int:
        return len(self.rows.get(table, {}))

    def key_bounds(self, table: str, column: str) -> tuple[Any, Any]:
        keys = self._sorted_keys(table)
        return (keys[0][0], keys[-1][0]) if keys else (None, None)

    def sample_boundaries(
        self, table: str, key_columns: Sequence[str], step: int, total: int
    ) -> list[tuple[Any, ...]]:
        keys = self._sorted_keys(table)
        return [k for i, k in enumerate(keys, start=1) if i % step == 0 and i < total]

    def read_range(
        self, table: str, key_columns: Sequence[str], key_range: KeyRange, limit: int
    ) -> list[dict[str, Any]]:
        self.events.append("read")
        self.reads += 1
        if self.fail_on_read == self.reads:
            raise ConnectionError("source connection lost")
        out = [
            dict(self.rows[table][k])
            for k in self._sorted_keys(table)
            if key_range.contains(k, order=self.order)
        ]
        if self.on_read is not None:
            self.on_read(self, self.reads)
        return out[:limit]


class MySqlFakeConnection:
    """Answers the MySQL capture's statements from the fake database's state."""

    def __init__(self, db: FakeDatabase, version: str = "8.0.39", gtid_mode: str = "ON"):
        self.db, self.version, self.gtid_mode = db, version, gtid_mode
        self.calls: list[str] = []

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        self.calls.append(sql)
        if sql.startswith("SELECT VERSION()"):
            return [{"version": self.version}]
        if sql.startswith("SELECT @@GLOBAL.gtid_mode"):
            return [{"gtid_mode": self.gtid_mode, "server_id": 7}]
        if sql.startswith("SHOW"):
            self.db.events.append("position")
            return [
                {
                    "File": "mysql-bin.000003",
                    "Position": len(self.db.log),
                    "Executed_Gtid_Set": _render_gtids(self.db.executed),
                }
            ]
        raise AssertionError(f"unexpected statement {sql!r}")


class PgFakeConnection:
    """Answers the PostgreSQL capture's statements from the fake database's state."""

    def __init__(self, db: FakeDatabase, fail_drop: bool = False):
        self.db = db
        self.fail_drop = fail_drop
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _lsn(self) -> str:
        return f"0/{len(self.db.log):X}"

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        self.calls.append((sql, tuple(params)))
        db = self.db
        if "FROM pg_replication_slots" in sql:
            return [{"slot_name": params[0]}] if params[0] in db.slots else []
        if "FROM pg_publication WHERE" in sql:
            if params[0] not in db.publications:
                return []
            return [{"pubinsert": True, "pubupdate": True, "pubdelete": True}]
        if "server_version_num" in sql:
            return [{"v": "160004"}]
        if "FROM pg_publication_rel" in sql:
            return []
        if "FROM pg_publication_tables" in sql:
            tables = db.publications[params[0]][1]
            return [
                {"schemaname": t.split(".")[0], "tablename": t.split(".")[1]}
                for t in sorted(tables)
            ]
        if sql.startswith("CREATE PUBLICATION"):
            m = re.match(r'CREATE PUBLICATION "(\w+)" FOR TABLE (.*)$', sql)
            assert m is not None
            tables = frozenset(t.replace('"', "") for t in m.group(2).split(", "))
            db.publications[m.group(1)] = (len(db.log), tables)
            db.events.append("publication")
            return []
        if "pg_create_logical_replication_slot" in sql:
            db.slots[params[0]] = len(db.log)
            db.events.append("slot")
            return [{"lsn": self._lsn()}]
        if "pg_current_wal_lsn" in sql:
            return [{"lsn": self._lsn()}]
        if "pg_drop_replication_slot" in sql:
            if self.fail_drop:
                raise ConnectionError("server closed the connection unexpectedly")
            del db.slots[params[0]]
            db.events.append("drop slot")
            return []
        if sql.startswith("DROP PUBLICATION"):
            del db.publications[sql.split('"')[1]]
            db.events.append("drop publication")
            return []
        raise AssertionError(f"unexpected statement {sql!r}")


@dataclass
class FakeCatalog:
    db: FakeDatabase
    unmet: list[str] = field(default_factory=list)

    def unmet_settings(self, settings: Sequence[SourceSetting]) -> list[str]:
        return list(self.unmet)

    def schema_fingerprint(self, tables: Sequence[str]) -> str:
        return f"v{self.db.schema_version}"


@dataclass
class FakeTarget:
    items: dict[tuple[str, tuple[Any, ...]], dict[str, Any]] = field(default_factory=dict)
    events: list[tuple[str, str, tuple[Any, ...]]] = field(default_factory=list)

    def upsert(self, records: Sequence[TargetRecord]) -> None:
        for r in records:
            self.items[(r.collection, r.key)] = dict(r.values)
            self.events.append(("load", r.collection, r.key))

    def apply_change(self, change: Entry) -> None:
        if change.row is None:
            self.items.pop((change.table, change.key), None)
        else:
            self.items[(change.table, change.key)] = dict(change.row)
        self.events.append(("cdc", change.table, change.key))

    def count(self, collection: str) -> int:
        return sum(1 for c, _ in self.items if c == collection)

    def rows_of(self, table: str) -> dict[tuple[Any, ...], dict[str, Any]]:
        return {k: v for (c, k), v in self.items.items() if c == table}


@dataclass
class FakeConnector:
    """Stands in for Debezium: on start, replays the source log from its start point.

    MySQL resumes from the seeded GTID set (``replay_by="gtid"``, what Debezium does
    when the offset has GTIDs) or, to show the hazard, from the binlog position. PostgreSQL
    resumes from the slot and publishes a change only if the publication existed, covering
    its table, at the change's position (pgoutput's catalog-as-of-the-change rule).
    """

    db: FakeDatabase
    target: FakeTarget
    dialect: str = "mysql"
    slot: str = "debezium_shop_wave_2"
    publication: str = "debezium_shop_wave_2_pub"
    replay_by: str = "gtid"
    autocreate_publication: bool = False
    running: bool = False
    fail_start: bool = False
    seeds: list[OffsetSeed] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    applied: list[Entry] = field(default_factory=list)
    lost: list[Entry] = field(default_factory=list)

    def is_running(self) -> bool:
        return self.running

    def seed_offset(self, seed: OffsetSeed) -> None:
        self.db.events.append("seed")
        self.seeds.append(seed)

    def _replay(self, entries: list[Entry]) -> None:
        for e in entries:
            self.target.apply_change(e)
            self.applied.append(e)

    def start(self, snapshot_mode: str) -> None:
        if self.fail_start:
            raise RuntimeError("connector creation failed")
        self.db.events.append("start")
        self.modes.append(snapshot_mode)
        self.running = True
        if self.dialect == "mysql":
            seed = self.seeds[-1]
            if self.replay_by == "gtid":
                done = str(seed.offset.get("gtids", ""))
                self._replay(
                    [e for e in self.db.log if not gtid_set_contains(done, f"{UUID}:{e.gtid}")]
                )
            else:
                self._replay(self.db.log[int(seed.offset["pos"]) :])
            return
        if self.autocreate_publication and self.publication not in self.db.publications:
            self.db.publications[self.publication] = (len(self.db.log), frozenset([TABLE]))
        start = self.db.slots[self.slot]
        for idx in range(start, len(self.db.log)):
            e = self.db.log[idx]
            pub = self.db.publications.get(self.publication)
            if pub is not None and pub[0] <= idx and e.table in pub[1]:
                self._replay([e])
            else:
                self.lost.append(e)


# ------------------------------------------------------------------------ helpers


def _plan(
    *,
    dialect: str | None = "mysql",
    tables: list[SourceTable] | None = None,
    hub: list[HubTopic] | None = None,
    strategy: str = "bulk_load_with_position_handoff",
    position: str | None = None,
    snapshot_mode: str | None = "no_data",
    parity: list[ParityGate] | None = None,
    max_lag_seconds: int = 60,
    settings: list[SourceSetting] | None = None,
) -> DataMovementPlan:
    tables = tables if tables is not None else [SourceTable(table=TABLE, primary_key=["id"])]
    if position is None:
        position = "replication_slot_lsn" if dialect == "postgresql" else "binlog_position"
    return DataMovementPlan(
        job_id="job-1",
        database_name="shop",
        assignment_version=1,
        wave=2,
        wave_count=3,
        title="Move orders",
        source=SourceSpec(engine="aurora-mysql", dialect=dialect, database="shop", tables=tables),
        cdc=CdcSpec(
            tool="debezium",
            connector_class="io.debezium.connector.mysql.MySqlConnector",
            topic_prefix="dm-job-1-w2",
            signal_table="shop.debezium_signal",
            snapshot_mode=snapshot_mode,
            heartbeat_interval_ms=10_000,
            required_source_settings=settings or [],
        ),
        hub=hub if hub is not None else [],
        initial_load=InitialLoad(strategy=strategy, position_capture=position),  # type: ignore[arg-type]
        gates=Gates(
            wave_gate="parity",
            parity=(
                parity
                if parity is not None
                else [ParityGate(table=t.table, check="row_count_and_key_checksum") for t in tables]
            ),
            max_lag_seconds=max_lag_seconds,
        ),
    )


def _settings(**kw: Any) -> HandoffSettings:
    base: dict[str, Any] = {"connector_name": "dm-job-1-w2-source", "chunks_per_table": 4}
    base.update(kw)
    return HandoffSettings(**base)


def _assert_partition(
    ranges: list[KeyRange], keys: list[tuple[Any, ...]], order: Callable[[Any], Any] | None = None
) -> None:
    """Every key falls in exactly one range (no gap, no overlap)."""
    for k in keys:
        hits = [r for r in ranges if r.contains(k, order=order)]
        assert len(hits) == 1, f"{k!r} is in {len(hits)} ranges"


def _sql_db(
    keys: list[tuple[Any, ...]], cols: tuple[str, ...], order: Callable[[Any], Any] = _identity
) -> FakeDatabase:
    db = FakeDatabase(key_columns={TABLE: cols}, order=order)
    for k in keys:
        db.rows.setdefault(TABLE, {})[k] = dict(zip(cols, k, strict=True))
    return db


def _seeded_db() -> FakeDatabase:
    db = FakeDatabase(key_columns={TABLE: ("id",)})
    for i in range(1, 101):
        db.write(TABLE, {"id": i, "status": "new", "total": i * 10})
    return db


def _mysql_run(
    db: FakeDatabase, connector: FakeConnector, target: FakeTarget, **kw: Any
) -> HandoffResult:
    return run_handoff(
        kw.pop("plan", _plan()),
        source=db,
        catalog=kw.pop("catalog", FakeCatalog(db)),
        capture=MySqlPositionCapture(MySqlFakeConnection(db)),
        connector=connector,
        target=target,
        settings=kw.pop("settings", _settings()),
        **kw,
    )


def _pg_capture(db: FakeDatabase) -> PostgresSlotCapture:
    return PostgresSlotCapture(
        PgFakeConnection(db), "debezium_shop_wave_2", "debezium_shop_wave_2_pub", [TABLE]
    )


# ------------------------------------------------------------- range splitting


def test_integer_boundaries_cover_the_span_evenly() -> None:
    assert integer_boundaries(1, 100, 4) == [(25,), (50,), (75,)]
    assert integer_boundaries(5, 5, 4) == []
    assert integer_boundaries(1, 3, 10) == [(1,), (2,)]


def test_ranges_from_boundaries_are_open_at_both_ends() -> None:
    ranges = ranges_from_boundaries([(10,), (20,)])
    assert ranges == [
        KeyRange(lower=None, upper=(10,)),
        KeyRange(lower=(10,), upper=(20,)),
        KeyRange(lower=(20,), upper=None),
    ]
    assert ranges_from_boundaries([]) == [KeyRange(lower=None, upper=None)]


def test_integer_key_split_has_no_gaps_or_overlaps() -> None:
    keys = [(k,) for k in random.Random(1).sample(range(-500, 50_000), 3000)]
    db = _sql_db(keys, ("id",))
    ranges = split_key_ranges(db, TABLE, ("id",), 8)
    assert len(ranges) == 8
    # Keys inserted after the split, below the minimum or above the maximum, still land once.
    _assert_partition(ranges, keys + [(-10_000,), (10**9,)])
    assert ranges == split_key_ranges(db, TABLE, ("id",), 8)


def test_composite_key_split_has_no_gaps_or_overlaps() -> None:
    rng = random.Random(2)
    keys = sorted({(rng.randint(1, 30), rng.randint(1, 500)) for _ in range(2000)})
    db = _sql_db(keys, ("tenant_id", "order_no"))
    ranges = split_key_ranges(db, TABLE, ("tenant_id", "order_no"), 6)
    assert 2 <= len(ranges) <= 6
    _assert_partition(ranges, keys + [(0, 0), (31, 1), (15, 10_000)])
    sizes = [sum(1 for k in keys if r.contains(k)) for r in ranges]
    assert max(sizes) - min(sizes) <= len(keys) // 6 + 1
    assert ranges == split_key_ranges(db, TABLE, ("tenant_id", "order_no"), 6)


def test_string_key_split_has_no_gaps_or_overlaps() -> None:
    rng = random.Random(3)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    keys = sorted({("".join(rng.choice(alphabet) for _ in range(8)),) for _ in range(1500)})
    db = _sql_db(keys, ("sku",))
    ranges = split_key_ranges(db, TABLE, ("sku",), 5)
    assert len(ranges) == 5
    _assert_partition(ranges, keys + [("",), ("zzzzzzzzz",), ("m",)])
    assert ranges == split_key_ranges(db, TABLE, ("sku",), 5)


def _fold(v: Any) -> Any:
    """A case-insensitive collation, as utf8mb4_0900_ai_ci compares letters."""
    return v.lower() if isinstance(v, str) else v


def test_string_key_split_follows_the_source_collation_not_python_order() -> None:
    rng = random.Random(4)
    words = sorted({"".join(rng.choice("abcdefghij") for _ in range(6)) for _ in range(800)})
    keys = [(w.capitalize() if rng.random() < 0.5 else w,) for w in words]
    db = _sql_db(keys, ("sku",), order=_fold)
    ranges = split_key_ranges(db, TABLE, ("sku",), 5)
    assert len(ranges) == 5
    _assert_partition(ranges, keys + [("A",), ("JJJJJJJ",), ("Ec",)], order=_fold)
    assert ranges == split_key_ranges(db, TABLE, ("sku",), 5)
    # The same ranges in Python order misplace keys: the collation is what the test exercises.
    assert any(sum(r.contains(k) for r in ranges) != 1 for k in keys)


def test_sampled_boundaries_are_deterministic_and_strictly_increasing() -> None:
    keys = [(f"k{i:04d}",) for i in range(100)]
    first = sampled_boundaries(keys, 4)
    assert first == sampled_boundaries(list(keys), 4)
    assert first == [("k0024",), ("k0049",), ("k0074",)]
    assert all(a < b for a, b in zip(first, first[1:], strict=False))
    assert sampled_boundaries([], 4) == []
    assert sampled_boundaries(keys[:1], 4) == []


def test_split_of_an_empty_table_is_one_open_range() -> None:
    assert split_key_ranges(_sql_db([], ("id",)), TABLE, ("id",), 8) == [KeyRange()]
    assert split_key_ranges(_sql_db([], ("a", "b")), TABLE, ("a", "b"), 8) == [KeyRange()]


def test_bool_bounds_are_not_treated_as_integers() -> None:
    db = _sql_db([(False,), (True,)], ("flag",))
    _assert_partition(split_key_ranges(db, TABLE, ("flag",), 2), [(False,), (True,)])


# --------------------------------------------------------------- SQL per dialect


def test_identifiers_are_quoted_per_dialect_and_validated() -> None:
    assert sql_dialect("mysql").table("shop.orders") == "`shop`.`orders`"
    assert sql_dialect("postgresql").table("public.orders") == '"public"."orders"'
    assert sql_dialect("postgresql").table("orders") == '"orders"'
    with pytest.raises(InitialLoadError):
        sql_dialect("mysql").table("shop.orders`; DROP TABLE x; --")
    with pytest.raises(UnsupportedDialectError):
        sql_dialect("sqlserver")
    with pytest.raises(UnsupportedDialectError):
        sql_dialect(None)


def test_range_read_sql_binds_boundary_values_as_parameters() -> None:
    conn = RecordingConnection()
    reader = SqlSourceReader(conn, sql_dialect("mysql"))
    reader.read_range(
        TABLE, ("tenant_id", "order_no"), KeyRange(lower=(3, "x'1"), upper=(9, "z")), 500
    )
    sql, params = conn.calls[0]
    assert sql == (
        "SELECT * FROM `shop`.`orders` WHERE "
        "((`tenant_id` > %s) OR (`tenant_id` = %s AND `order_no` > %s)) AND "
        "((`tenant_id` < %s) OR (`tenant_id` = %s AND `order_no` < %s) "
        "OR (`tenant_id` = %s AND `order_no` = %s)) "
        "ORDER BY `tenant_id`, `order_no` LIMIT %s"
    )
    assert params == (3, 3, "x'1", 9, 9, "z", 9, "z", 500)
    assert "x'1" not in sql


def test_range_read_sql_postgresql_open_range() -> None:
    conn = RecordingConnection()
    SqlSourceReader(conn, sql_dialect("postgresql")).read_range(
        "public.orders", ("id",), KeyRange(lower=None, upper=None), 1000
    )
    assert conn.calls == [('SELECT * FROM "public"."orders" ORDER BY "id" LIMIT %s', (1000,))]


def test_count_bounds_and_sample_sql() -> None:
    conn = RecordingConnection(
        [
            (lambda s: s.startswith("SELECT COUNT"), [{"n": 42}]),
            (lambda s: s.startswith("SELECT MIN"), [{"lo": 1, "hi": 9}]),
            (lambda s: "ROW_NUMBER" in s, [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]),
        ]
    )
    reader = SqlSourceReader(conn, sql_dialect("postgresql"))
    assert reader.count("public.orders") == 42
    assert reader.key_bounds("public.orders", "id") == (1, 9)
    assert reader.sample_boundaries("public.orders", ("a", "b"), 10, 42) == [(1, "x"), (2, "y")]
    assert conn.calls == [
        ('SELECT COUNT(*) AS n FROM "public"."orders"', ()),
        ('SELECT MIN("id") AS lo, MAX("id") AS hi FROM "public"."orders"', ()),
        (
            'SELECT "a", "b" FROM (SELECT "a", "b", ROW_NUMBER() OVER (ORDER BY "a", "b") '
            'AS dm_rn FROM "public"."orders") dm_sample WHERE MOD(dm_rn, %s) = 0 AND dm_rn < %s '
            'ORDER BY "a", "b"',
            (10, 42),
        ),
    ]


# ------------------------------------------------------------ position capture


def _mysql_conn(version: str, gtid_mode: str = "ON") -> RecordingConnection:
    status = [{"File": "mysql-bin.000003", "Position": 154, "Executed_Gtid_Set": f"{UUID}:1-77"}]
    return RecordingConnection(
        [
            (lambda s: s == "SELECT VERSION() AS version", [{"version": version}]),
            (
                lambda s: s.startswith("SELECT @@GLOBAL.gtid_mode"),
                [{"gtid_mode": gtid_mode, "server_id": 7}],
            ),
            (lambda s: s.startswith("SHOW"), status),
        ]
    )


def test_mysql_capture_uses_binary_log_status_on_8_4() -> None:
    conn = _mysql_conn("8.4.3")
    pos = MySqlPositionCapture(conn).capture()
    assert pos == MySqlPosition(
        file="mysql-bin.000003", pos=154, gtid_set=f"{UUID}:1-77", server_id=7
    )
    assert [c[0] for c in conn.calls] == [
        "SELECT VERSION() AS version",
        "SELECT @@GLOBAL.gtid_mode AS gtid_mode, @@server_id AS server_id",
        "SHOW BINARY LOG STATUS",
    ]


def test_mysql_capture_on_aurora_mysql_3() -> None:
    conn = _mysql_conn("8.0.mysql_aurora.3.05.2")
    assert MySqlPositionCapture(conn).capture().gtid_set == f"{UUID}:1-77"
    assert conn.calls[-1] == ("SHOW MASTER STATUS", ())


def test_mysql_capture_refuses_without_gtids() -> None:
    conn = _mysql_conn("8.0.39", gtid_mode="OFF_PERMISSIVE")
    with pytest.raises(InitialLoadError, match="gtid_mode is OFF_PERMISSIVE, not ON"):
        MySqlPositionCapture(conn).capture()
    assert not any(sql.startswith("SHOW") for sql, _ in conn.calls)


def test_mysql_capture_refuses_mysql_5_7() -> None:
    with pytest.raises(InitialLoadError, match="8.0 or later"):
        MySqlPositionCapture(_mysql_conn("5.7.44")).capture()


def test_mysql_capture_refuses_when_binary_logging_is_off() -> None:
    conn = RecordingConnection(
        [
            (lambda s: s.startswith("SELECT VERSION"), [{"version": "8.0.39"}]),
            (lambda s: s.startswith("SELECT @@"), [{"gtid_mode": "ON", "server_id": 1}]),
        ]
    )
    with pytest.raises(InitialLoadError, match="binary log"):
        MySqlPositionCapture(conn).capture()


def test_postgresql_capture_creates_the_publication_before_the_slot() -> None:
    conn = RecordingConnection(
        [(lambda s: "pg_create_logical_replication_slot" in s, [{"lsn": "0/16B3748"}])]
    )
    capture = PostgresSlotCapture(conn, "slot_w2", "pub_w2", ["public.orders", "public.items"])
    pos = capture.capture()
    assert pos == PostgresPosition(slot_name="slot_w2", lsn="0/16B3748")
    assert conn.calls == [
        ("SELECT slot_name FROM pg_replication_slots WHERE slot_name = %s", ("slot_w2",)),
        (
            "SELECT pubinsert, pubupdate, pubdelete FROM pg_publication WHERE pubname = %s",
            ("pub_w2",),
        ),
        ('CREATE PUBLICATION "pub_w2" FOR TABLE "public"."orders", "public"."items"', ()),
        (
            "SELECT lsn::text AS lsn FROM pg_create_logical_replication_slot(%s, %s)",
            ("slot_w2", "pgoutput"),
        ),
    ]
    assert all("slot_w2" not in sql for sql, _ in conn.calls)
    assert capture.created() == ["replication slot slot_w2", "publication pub_w2"]


ALL_ACTIONS = {"pubinsert": True, "pubupdate": True, "pubdelete": True}


def _existing_publication(
    *,
    actions: Mapping[str, bool] = ALL_ACTIONS,
    version: str = "160004",
    rels: list[dict[str, Any]] | None = None,
    tables: tuple[str, ...] = ("orders", "other"),
) -> RecordingConnection:
    return RecordingConnection(
        [
            (lambda s: "FROM pg_publication WHERE" in s, [dict(actions)]),
            (
                lambda s: "pg_publication_tables" in s,
                [{"schemaname": "public", "tablename": t} for t in tables],
            ),
            (lambda s: "server_version_num" in s, [{"v": version}]),
            (lambda s: "FROM pg_publication_rel" in s, rels or []),
            (lambda s: "pg_create_logical_replication_slot" in s, [{"lsn": "0/10"}]),
        ]
    )


def test_postgresql_capture_reuses_a_publication_that_covers_the_tables() -> None:
    conn = _existing_publication(
        rels=[
            {
                "schemaname": "public",
                "tablename": "orders",
                "row_filter": False,
                "column_list": False,
            }
        ]
    )
    capture = PostgresSlotCapture(conn, "slot_w2", "pub_w2", ["orders"])
    capture.capture()
    assert not any(sql.startswith("CREATE PUBLICATION") for sql, _ in conn.calls)
    assert conn.calls[1] == (
        "SELECT pubinsert, pubupdate, pubdelete FROM pg_publication WHERE pubname = %s",
        ("pub_w2",),
    )
    rel_sql, rel_params = next(c for c in conn.calls if "pg_publication_rel" in c[0])
    assert "r.prqual IS NOT NULL AS row_filter" in rel_sql
    assert "r.prattrs IS NOT NULL AS column_list" in rel_sql and rel_params == ("pub_w2",)
    # Only the slot is this run's: release never drops the publication it found.
    assert capture.release() == ["replication slot slot_w2"]
    assert conn.calls[-1] == ("SELECT pg_drop_replication_slot(%s)", ("slot_w2",))


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"actions": {**ALL_ACTIONS, "pubupdate": False}}, "does not publish update"),
        (
            {"actions": {"pubinsert": True, "pubupdate": False, "pubdelete": False}},
            "does not publish update, delete",
        ),
        (
            {"rels": [{"schemaname": "public", "tablename": "orders", "row_filter": True}]},
            "row filter or column list on public.orders",
        ),
        (
            {"rels": [{"schemaname": "public", "tablename": "orders", "column_list": True}]},
            "row filter or column list on public.orders",
        ),
    ],
)
def test_postgresql_capture_refuses_a_publication_that_narrows_the_changes(
    kw: dict[str, Any], message: str
) -> None:
    conn = _existing_publication(**kw)
    with pytest.raises(InitialLoadError, match=message):
        PostgresSlotCapture(conn, "slot_w2", "pub_w2", ["orders"]).capture()
    assert not any("pg_create_logical_replication_slot" in sql for sql, _ in conn.calls)


def test_a_filter_on_a_table_outside_the_wave_is_allowed() -> None:
    conn = _existing_publication(
        rels=[{"schemaname": "public", "tablename": "other", "row_filter": True}]
    )
    PostgresSlotCapture(conn, "slot_w2", "pub_w2", ["orders"]).capture()


def test_postgresql_14_has_no_row_filters_to_check() -> None:
    conn = _existing_publication(version="140011")
    PostgresSlotCapture(conn, "slot_w2", "pub_w2", ["orders"]).capture()
    assert not any("pg_publication_rel" in sql for sql, _ in conn.calls)


def test_postgresql_capture_refuses_a_publication_missing_a_table() -> None:
    conn = RecordingConnection(
        [
            (lambda s: "FROM pg_publication WHERE" in s, [dict(ALL_ACTIONS)]),
            (lambda s: "pg_publication_tables" in s, [{"schemaname": "public", "tablename": "a"}]),
        ]
    )
    capture = PostgresSlotCapture(conn, "slot_w2", "pub_w2", ["public.a", "public.b"])
    with pytest.raises(InitialLoadError, match="does not cover public.b"):
        capture.capture()
    assert not any("pg_create_logical_replication_slot" in sql for sql, _ in conn.calls)
    assert capture.release() == []


def test_postgresql_capture_refuses_an_existing_slot() -> None:
    conn = RecordingConnection(
        [(lambda s: "pg_replication_slots" in s, [{"slot_name": "debezium_shop_wave_2"}])]
    )
    capture = PostgresSlotCapture(conn, "debezium_shop_wave_2", "pub", [TABLE])
    with pytest.raises(InitialLoadError, match="already exists"):
        capture.capture()
    assert len(conn.calls) == 1
    assert capture.release() == [] and len(conn.calls) == 1


def test_position_capture_for_each_dialect() -> None:
    conn = RecordingConnection()
    assert isinstance(position_capture_for("mysql", conn), MySqlPositionCapture)
    pg = position_capture_for(
        "postgresql", conn, slot_name="s1", publication_name="p1", tables=["t"]
    )
    assert isinstance(pg, PostgresSlotCapture)
    with pytest.raises(InitialLoadError, match="slot and publication"):
        position_capture_for("postgresql", conn, slot_name="s1")
    for dialect in ("sqlserver", "oracle", "mariadb"):
        with pytest.raises(UnsupportedDialectError):
            position_capture_for(dialect, conn)


def test_handoff_names_are_the_connector_generators_names() -> None:
    names = handoff_names(_plan())
    assert names == wave_connector_names("dm-job-1-w2", 2)
    assert names.slot == "debezium_dm_job_1_w2_wave_2"
    assert names.publication == "debezium_dm_job_1_w2_wave_2_pub"
    assert names.connector == "debezium-dm-job-1-w2-wave-2"


def test_position_capture_for_plan_uses_the_generators_slot_and_publication() -> None:
    plan = _plan(dialect="postgresql")
    capture = position_capture_for_plan(plan, RecordingConnection())
    assert isinstance(capture, PostgresSlotCapture)
    assert capture.slot_name == "debezium_dm_job_1_w2_wave_2"
    assert capture.publication_name == "debezium_dm_job_1_w2_wave_2_pub"
    assert capture.tables == [TABLE]
    assert isinstance(
        position_capture_for_plan(_plan(), RecordingConnection()), MySqlPositionCapture
    )


def test_the_seed_is_keyed_on_the_generators_connector_name_by_default() -> None:
    db = _seeded_db()
    target = FakeTarget()
    connector = FakeConnector(db, target)
    _mysql_run(db, connector, target, settings=HandoffSettings())
    assert json.loads(connector.seeds[0].kafka_key()) == [
        "debezium-dm-job-1-w2-wave-2",
        {"server": "dm-job-1-w2"},
    ]


def test_mysql_offset_seed_is_debeziums_offset_record() -> None:
    seed = mysql_offset_seed(
        MySqlPosition(file="mysql-bin.000003", pos=154, gtid_set=f"{UUID}:1-77", server_id=7),
        connector_name="dm-job-1-w2-source",
        topic_prefix="dm-job-1-w2",
    )
    assert json.loads(seed.kafka_key()) == ["dm-job-1-w2-source", {"server": "dm-job-1-w2"}]
    assert json.loads(seed.kafka_value()) == {
        "file": "mysql-bin.000003",
        "pos": 154,
        "gtids": f"{UUID}:1-77",
    }


# ---------------------------------------------------------- positions compared


def test_gtid_sets_parse_merge_and_compare() -> None:
    other = "4e11fa47-71ca-11e1-9e33-c80aa9429562"
    parsed = parse_gtid_set(f"{UUID.upper()}:1-5:6-9:12,\n{other}:3")
    assert parsed == {(UUID, ""): [(1, 9), (12, 12)], (other, ""): [(3, 3)]}
    assert gtid_set_contains(f"{UUID}:1-20", f"{UUID}:3-7:9")
    assert not gtid_set_contains(f"{UUID}:1-5:7-20", f"{UUID}:3-7")
    assert not gtid_set_contains(f"{UUID}:1-20", f"{other}:1")
    assert gtid_set_contains(f"{UUID}:1-3", "")
    assert parse_gtid_set(f"{UUID}:1-3:audit:1-2") == {
        (UUID, ""): [(1, 3)],
        (UUID, "audit"): [(1, 2)],
    }


def test_positions_reached_by_gtid_set_and_lsn() -> None:
    def my(g: str) -> MySqlPosition:
        return MySqlPosition(file="f", pos=1, gtid_set=g, server_id=1)

    # A higher binlog position means nothing on its own: the GTID set decides.
    assert position_reached(my(f"{UUID}:1-10"), my(f"{UUID}:1-9"))
    assert not position_reached(my(f"{UUID}:1-8"), my(f"{UUID}:1-9"))
    assert parse_lsn("1/0") == 1 << 32
    assert position_reached(PostgresPosition("s", "0/1A"), PostgresPosition("s", "0/19"))
    assert not position_reached(PostgresPosition("s", "0/9"), PostgresPosition("s", "0/1A"))
    with pytest.raises(InitialLoadError):
        position_reached(my(""), PostgresPosition("s", "0/1"))
    with pytest.raises(InitialLoadError):
        parse_lsn("not-an-lsn")


# ---------------------------------------------------------- source settings


def _setting(name: str, value: str) -> SourceSetting:
    return SourceSetting(name=name, value=value)


SIGNAL = _setting("signal table", "shop.debezium_signal (id VARCHAR(42) PRIMARY KEY)")


def test_mysql_managed_settings_are_checked_read_only() -> None:
    conn = RecordingConnection(
        [
            (lambda s: "binlog_format" in s, [{"v": "ROW"}]),
            (lambda s: "binlog_row_image" in s, [{"v": "FULL"}]),
            (lambda s: "gtid_mode" in s, [{"v": "ON"}]),
            (lambda s: "enforce_gtid_consistency" in s, [{"v": "ON"}]),
            (
                lambda s: s.startswith("CALL"),
                [{"name": "binlog retention hours", "value": "48", "description": ""}],
            ),
        ],
        exact=[("information_schema.tables", ("shop", "debezium_signal"), [{"v": "x"}])],
    )
    catalog = SqlSourceCatalog(conn, sql_dialect("mysql"), "shop")
    settings = [
        _setting("binlog_format", "ROW"),
        _setting("binlog_row_image", "FULL"),
        _setting("gtid_mode", "ON"),
        _setting("enforce_gtid_consistency", "ON"),
        _setting("binlog retention hours", "at least 24"),
        SIGNAL,
    ]
    assert catalog.unmet_settings(settings) == []
    assert all(
        sql.startswith("SELECT") or sql == "CALL mysql.rds_show_configuration"
        for sql, _ in conn.calls
    )
    assert (
        "SELECT table_name AS v FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        ("shop", "debezium_signal"),
    ) in conn.calls


def test_mysql_gtid_settings_off_are_unmet() -> None:
    conn = RecordingConnection(
        [
            (lambda s: "gtid_mode" in s, [{"v": "OFF"}]),
            (lambda s: "enforce_gtid_consistency" in s, [{"v": "WARN"}]),
        ]
    )
    unmet = SqlSourceCatalog(conn, sql_dialect("mysql"), "shop").unmet_settings(
        [_setting("gtid_mode", "ON"), _setting("enforce_gtid_consistency", "ON")]
    )
    assert unmet == ["gtid_mode: is OFF, needs ON", "enforce_gtid_consistency: is WARN, needs ON"]


@pytest.mark.parametrize(
    ("value", "location"),
    [
        ("wordpress.debezium_signal (id VARCHAR(42))", ("public", "debezium_signal")),
        ("audit.debezium_signal", ("audit", "debezium_signal")),
        ("wordpress.audit.debezium_signal", ("audit", "debezium_signal")),
        ("debezium_signal", ("public", "debezium_signal")),
    ],
)
def test_postgresql_signal_table_is_looked_up_in_its_schema(
    value: str, location: tuple[str, str]
) -> None:
    """The plan names ``<database>.debezium_signal``; on PostgreSQL that table is in
    ``public`` of the connection's database (where #1430 creates it)."""
    conn = RecordingConnection(exact=[("information_schema.tables", location, [{"v": "x"}])])
    catalog = SqlSourceCatalog(conn, sql_dialect("postgresql"), "wordpress")
    assert catalog.unmet_settings([_setting("signal table", value)]) == []
    assert catalog.signal_table_location(value.split(" ")[0]) == location


def test_postgresql_signal_table_in_the_wrong_schema_is_unmet() -> None:
    conn = RecordingConnection(
        exact=[("information_schema.tables", ("wordpress", "debezium_signal"), [{"v": "x"}])]
    )
    catalog = SqlSourceCatalog(conn, sql_dialect("postgresql"), "wordpress")
    unmet = catalog.unmet_settings([_setting("signal table", "wordpress.debezium_signal (id)")])
    assert unmet == ["signal table: wordpress.debezium_signal does not exist"]
    assert conn.calls[-1][1] == ("public", "debezium_signal")


def test_mysql_unmet_settings_are_named() -> None:
    conn = RecordingConnection(
        [
            (lambda s: "binlog_format" in s, [{"v": "MIXED"}]),
            (lambda s: "binlog_expire_logs_seconds" in s, [{"v": 3600}]),
            (
                lambda s: s.startswith("CALL"),
                [{"name": "binlog retention hours", "value": None, "description": ""}],
            ),
        ]
    )
    unmet = SqlSourceCatalog(conn, sql_dialect("mysql"), "shop").unmet_settings(
        [
            _setting("binlog_format", "ROW"),
            _setting("binlog retention", "at least 24 hours"),
            _setting("binlog retention hours", "at least 24"),
            SIGNAL,
            _setting("something new", "on"),
        ]
    )
    assert unmet == [
        "binlog_format: is MIXED, needs ROW",
        "binlog retention: binlog_expire_logs_seconds is 3600, needs at least 86400",
        "binlog retention hours: is NULL, needs at least 24: binlogs may be purged during the load",
        "signal table: shop.debezium_signal does not exist",
        "something new: cannot be checked read-only by the handoff",
    ]


def test_mysql_binlogs_that_never_expire_meet_retention() -> None:
    conn = RecordingConnection([(lambda s: "binlog_expire_logs_seconds" in s, [{"v": 0}])])
    catalog = SqlSourceCatalog(conn, sql_dialect("mysql"), "shop")
    assert catalog.unmet_settings([_setting("binlog retention", "at least 24 hours")]) == []


def test_postgresql_settings_are_checked_from_pg_settings() -> None:
    values = {"rds.logical_replication": "on", "wal_level": "replica", "max_replication_slots": "0"}

    class Conn(RecordingConnection):
        def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
            self.calls.append((sql, tuple(params)))
            if "pg_settings" in sql:
                return [{"v": values[params[0]]}]
            return []

    conn = Conn()
    unmet = SqlSourceCatalog(conn, sql_dialect("postgresql"), "shop").unmet_settings(
        [
            _setting("rds.logical_replication", "1"),
            _setting("plugin.name", "pgoutput"),
            _setting("wal_level", "logical"),
            _setting("max_replication_slots", "at least 1"),
        ]
    )
    assert unmet == [
        "wal_level: is replica, needs logical",
        "max_replication_slots: is 0, needs at least 1",
    ]
    assert all(
        c == ("SELECT setting AS v FROM pg_settings WHERE name = %s", (p,))
        for c, p in zip(
            conn.calls,
            ["rds.logical_replication", "wal_level", "max_replication_slots"],
            strict=True,
        )
    )


def test_mysql_fingerprint_sees_enum_members_unsigned_and_charset() -> None:
    def fingerprint(**changed: Any) -> str:
        row = {
            "s": "shop",
            "t": "orders",
            "c": "status",
            "p": 2,
            "d": "enum",
            "ct": "enum('new','paid')",
            "cs": "utf8mb4",
            "co": "utf8mb4_0900_ai_ci",
            "n": "NO",
            "dp": None,
        }
        row.update(changed)
        conn = RecordingConnection([(lambda s: "information_schema.columns" in s, [row])])
        return SqlSourceCatalog(conn, sql_dialect("mysql"), "shop").schema_fingerprint([TABLE])

    base = fingerprint()
    assert fingerprint(ct="enum('new','paid','refunded')") != base
    assert fingerprint(ct="int unsigned", d="int") != fingerprint(ct="int", d="int")
    assert fingerprint(cs="latin1") != base
    assert fingerprint(co="utf8mb4_bin") != base
    assert fingerprint() == base


def test_fingerprint_columns_per_dialect() -> None:
    mysql = RecordingConnection()
    SqlSourceCatalog(mysql, sql_dialect("mysql"), "shop").schema_fingerprint([TABLE])
    sql = mysql.calls[0][0]
    for column in ("column_type", "character_set_name", "collation_name", "datetime_precision"):
        assert column in sql
    pg = RecordingConnection()
    SqlSourceCatalog(pg, sql_dialect("postgresql"), "shop").schema_fingerprint(["public.orders"])
    pg_sql = pg.calls[0][0]
    assert "udt_name" in pg_sql and "collation_name" in pg_sql and "column_type" not in pg_sql


def test_schema_fingerprint_reads_information_schema_with_parameters() -> None:
    rows = [{"s": "shop", "t": "orders", "c": "id", "p": 1, "d": "int", "n": "NO"}]
    conn = RecordingConnection([(lambda s: "information_schema.columns" in s, rows)])
    catalog = SqlSourceCatalog(conn, sql_dialect("mysql"), "shop")
    before = catalog.schema_fingerprint([TABLE, "items"])
    sql, params = conn.calls[0]
    assert params == ("shop", "orders", "shop", "items")
    assert "WHERE (table_schema = %s AND table_name = %s) OR (table_schema = %s AND" in sql
    assert "orders" not in sql
    rows.append({"s": "shop", "t": "orders", "c": "note", "p": 2, "d": "text", "n": "YES"})
    assert catalog.schema_fingerprint([TABLE, "items"]) != before


# --------------------------------------------------------------- plan and keys


def test_load_tables_take_the_hub_key_then_the_primary_key() -> None:
    plan = _plan(
        tables=[
            SourceTable(table="shop.a", primary_key=["id"]),
            SourceTable(table="shop.b", primary_key=[]),
        ],
        hub=[
            HubTopic(
                table="shop.b",
                topic="dm-job-1-w2.shop.b",
                key_columns=["code"],
                key_source="unique_index",
                partitions=1,
            )
        ],
    )
    assert [(t.table, t.key_names) for t in load_tables(plan)] == [
        ("shop.a", ("id",)),
        ("shop.b", ("code",)),
    ]


def test_refuses_a_table_without_a_key_before_touching_the_source() -> None:
    db = FakeDatabase(key_columns={})
    target = FakeTarget()
    connector = FakeConnector(db, target)
    with pytest.raises(NoKeyError, match="shop.logs"):
        _mysql_run(db, connector, target, plan=_plan(tables=[SourceTable(table="shop.logs")]))
    assert db.events == [] and connector.seeds == []


@pytest.mark.parametrize(
    "kw",
    [
        {"strategy": "connector_snapshot", "position": "connector_offsets"},
        {"snapshot_mode": "initial"},
        {"position": "replication_slot_lsn"},
        {"dialect": "sqlserver"},
    ],
)
def test_refuses_a_plan_that_is_not_a_position_handoff(kw: dict[str, Any]) -> None:
    db = FakeDatabase(key_columns={TABLE: ("id",)})
    target = FakeTarget()
    with pytest.raises(InitialLoadError):
        _mysql_run(db, FakeConnector(db, target), target, plan=_plan(**kw))
    assert db.events == []


def test_refuses_while_the_connector_is_running() -> None:
    db = FakeDatabase(key_columns={TABLE: ("id",)})
    target = FakeTarget()
    with pytest.raises(InitialLoadError, match="running"):
        _mysql_run(db, FakeConnector(db, target, running=True), target)
    assert db.events == []


def test_refuses_unmet_source_settings_before_the_capture() -> None:
    db = _seeded_db()
    target = FakeTarget()
    catalog = FakeCatalog(db, unmet=["binlog retention hours: is NULL, needs at least 24"])
    with pytest.raises(InitialLoadError, match="binlog retention hours: is NULL"):
        run_handoff(
            _plan(dialect="postgresql"),
            source=db,
            catalog=catalog,
            capture=_pg_capture(db),
            connector=FakeConnector(db, target, dialect="postgresql"),
            target=target,
            settings=_settings(),
        )
    assert db.events == [] and db.slots == {} and db.publications == {}


# ------------------------------------------------------------------- handoff


def test_a_write_during_the_load_converges_after_at_least_once_replay() -> None:
    db = _seeded_db()
    history = len(db.log)

    def live_writes(d: FakeDatabase, read_no: int) -> None:
        if read_no == 1:  # lands while the load is in flight
            d.write(TABLE, {"id": 80, "status": "paid", "total": 800})
            d.write(TABLE, {"id": 101, "status": "new", "total": 1010})
            d.delete(TABLE, (3,))

    db.on_read = live_writes
    target = FakeTarget()
    connector = FakeConnector(db, target)
    result = _mysql_run(
        db,
        connector,
        target,
        settings=_settings(batch_size=10),
        clock=iter(range(1000, 2000, 100)).__next__,
    )

    # The source and the target agree once CDC has replayed the load window.
    assert target.rows_of(TABLE) == db.rows[TABLE]
    # CDC replays the changes since P0 (and nothing before it); id 80 and id 101 are
    # also copied by the load, so they are written twice with the same final value.
    assert connector.applied == db.log[history:]
    loaded = {k for kind, _, k in target.events if kind == "load"}
    assert {(80,), (101,), (3,)} <= loaded
    # Every CDC write lands after every load write.
    last_load = max(i for i, e in enumerate(target.events) if e[0] == "load")
    first_cdc = min(i for i, e in enumerate(target.events) if e[0] == "cdc")
    assert last_load < first_cdc
    # Capture before the first read, seed before the first read, start last.
    assert db.events[0] == "position"
    assert db.events.index("seed") < db.events.index("read")
    assert db.events[-2:] == ["position", "start"]  # the load-end position, then the start
    assert connector.modes == ["recovery"] and result.start_snapshot_mode == "recovery"
    assert result.position == MySqlPosition(
        file="mysql-bin.000003", pos=history, gtid_set=f"{UUID}:1-{history}", server_id=7
    )
    assert result.load_end_position.gtid_set == f"{UUID}:1-{history + 3}"  # type: ignore[union-attr]
    assert connector.seeds == [result.offset_seed]
    assert result.load_started_ms < result.load_finished_ms
    (table_result,) = result.tables
    assert table_result.table == TABLE and table_result.chunks == 4
    assert table_result.rows_read == table_result.records_written


def _in_flight_db() -> tuple[FakeDatabase, int]:
    """id 5's update is in the binlog before the capture but commits after the first read."""
    db = _seeded_db()
    pending = db.begin_write(TABLE, {"id": 5, "status": "refunded", "total": 0})

    def commit_late(d: FakeDatabase, read_no: int) -> None:
        if read_no == 1:
            d.commit(pending)

    db.on_read = commit_late
    return db, pending.gtid


def test_a_commit_not_visible_at_the_capture_is_replayed_through_the_gtid_set() -> None:
    db, gtid = _in_flight_db()
    target = FakeTarget()
    connector = FakeConnector(db, target)
    result = _mysql_run(db, connector, target)
    assert isinstance(result.position, MySqlPosition)
    assert result.position.pos == len(db.log)  # the binlog position is past the transaction
    assert not gtid_set_contains(result.position.gtid_set, f"{UUID}:{gtid}")
    assert target.rows_of(TABLE)[(5,)]["status"] == "refunded"
    assert [e.gtid for e in connector.applied] == [gtid]


def test_resuming_from_the_binlog_position_would_lose_that_commit() -> None:
    """Why GTIDs are required: the fake shows the commit-visibility window."""
    db, _ = _in_flight_db()
    target = FakeTarget()
    _mysql_run(db, FakeConnector(db, target, replay_by="position"), target)
    assert db.rows[TABLE][(5,)]["status"] == "refunded"
    assert target.rows_of(TABLE)[(5,)]["status"] == "new"


def test_postgresql_handoff_publishes_writes_made_during_the_load() -> None:
    db = _seeded_db()
    target = FakeTarget()
    connector = FakeConnector(db, target, dialect="postgresql")

    def live_write(d: FakeDatabase, read_no: int) -> None:
        if read_no == 2:
            d.write(TABLE, {"id": 50, "status": "shipped", "total": 0})

    db.on_read = live_write
    result = run_handoff(
        _plan(dialect="postgresql"),
        source=db,
        catalog=FakeCatalog(db),
        capture=_pg_capture(db),
        connector=connector,
        target=target,
        settings=_settings(),
    )
    assert db.events[:2] == ["publication", "slot"] and db.events[2] == "read"
    assert result.offset_seed is None and connector.seeds == []
    assert connector.modes == ["no_data"]
    assert result.position == PostgresPosition(slot_name="debezium_shop_wave_2", lsn="0/64")
    assert result.load_end_position == PostgresPosition(
        slot_name="debezium_shop_wave_2", lsn="0/65"
    )
    assert connector.lost == []
    assert [(e.table, e.key) for e in connector.applied] == [(TABLE, (50,))]
    assert target.rows_of(TABLE) == db.rows[TABLE]


def test_a_publication_created_after_the_slot_loses_writes_made_during_the_load() -> None:
    """Why the publication comes first: the fake shows the publication window."""
    db = _seeded_db()
    target = FakeTarget()
    connector = FakeConnector(db, target, dialect="postgresql", autocreate_publication=True)

    @dataclass
    class SlotOnly:  # what the handoff would do if it left the publication to the connector
        conn: PgFakeConnection

        def capture(self) -> PostgresPosition:
            rows = self.conn.query(
                "SELECT lsn::text AS lsn FROM pg_create_logical_replication_slot(%s, %s)",
                ("debezium_shop_wave_2", "pgoutput"),
            )
            return PostgresPosition("debezium_shop_wave_2", rows[0]["lsn"])

        def current(self) -> PostgresPosition:
            rows = self.conn.query("SELECT pg_current_wal_lsn()::text AS lsn", ())
            return PostgresPosition("debezium_shop_wave_2", rows[0]["lsn"])

        def created(self) -> list[str]:
            return []

        def release(self) -> list[str]:
            return []

    def live_write(d: FakeDatabase, read_no: int) -> None:
        if read_no == 2:
            d.write(TABLE, {"id": 50, "status": "shipped", "total": 0})

    db.on_read = live_write
    run_handoff(
        _plan(dialect="postgresql"),
        source=db,
        catalog=FakeCatalog(db),
        capture=SlotOnly(PgFakeConnection(db)),
        connector=connector,
        target=target,
        settings=_settings(),
    )
    assert [e.key for e in connector.lost] == [(50,)]
    assert target.rows_of(TABLE)[(50,)]["status"] == "new"


def test_ddl_during_the_load_fails_and_drops_what_the_run_created() -> None:
    db = _seeded_db()
    target = FakeTarget()
    connector = FakeConnector(db, target, dialect="postgresql")

    def alter(d: FakeDatabase, read_no: int) -> None:
        if read_no == 1:
            d.schema_version += 1

    db.on_read = alter
    with pytest.raises(HandoffFailed) as err:
        run_handoff(
            _plan(dialect="postgresql"),
            source=db,
            catalog=FakeCatalog(db),
            capture=_pg_capture(db),
            connector=connector,
            target=target,
            settings=_settings(),
        )
    message = str(err.value)
    assert "schema changed during the load" in message
    assert (
        "dropped replication slot debezium_shop_wave_2, publication debezium_shop_wave_2_pub "
        "(created by this run)"
    ) in message
    assert db.slots == {} and db.publications == {}
    assert not connector.running and "start" not in db.events


def test_a_load_failure_drops_only_the_slot_when_the_publication_was_there() -> None:
    db = _seeded_db()
    db.publications["debezium_shop_wave_2_pub"] = (0, frozenset([TABLE]))
    db.fail_on_read = 2
    target = FakeTarget()
    with pytest.raises(HandoffFailed, match="source connection lost; dropped replication slot"):
        run_handoff(
            _plan(dialect="postgresql"),
            source=db,
            catalog=FakeCatalog(db),
            capture=_pg_capture(db),
            connector=FakeConnector(db, target, dialect="postgresql"),
            target=target,
            settings=_settings(),
        )
    assert db.slots == {} and "debezium_shop_wave_2_pub" in db.publications
    assert db.events[-1] == "drop slot"


def test_a_failed_cleanup_keeps_the_original_error_and_names_the_slot() -> None:
    db = _seeded_db()
    db.fail_on_read = 2
    target = FakeTarget()
    capture = PostgresSlotCapture(
        PgFakeConnection(db, fail_drop=True),
        "debezium_shop_wave_2",
        "debezium_shop_wave_2_pub",
        [TABLE],
    )
    with pytest.raises(HandoffFailed) as err:
        run_handoff(
            _plan(dialect="postgresql"),
            source=db,
            catalog=FakeCatalog(db),
            capture=capture,
            connector=FakeConnector(db, target, dialect="postgresql"),
            target=target,
            settings=_settings(),
        )
    message = str(err.value)
    assert "source connection lost" in message
    assert isinstance(err.value.__cause__, ConnectionError)
    assert "cleanup also failed (server closed the connection unexpectedly)" in message
    assert "replication slot debezium_shop_wave_2" in message and "may still exist" in message
    assert "holds WAL" in message
    assert "debezium_shop_wave_2" in db.slots


def test_a_mysql_load_failure_has_nothing_to_clean_up() -> None:
    db = _seeded_db()
    db.fail_on_read = 1
    target = FakeTarget()
    with pytest.raises(HandoffFailed, match="nothing to clean up"):
        _mysql_run(db, FakeConnector(db, target), target)


def test_a_connector_that_fails_to_start_keeps_the_slot_for_a_retry() -> None:
    db = _seeded_db()
    target = FakeTarget()
    with pytest.raises(HandoffFailed, match="kept replication slot debezium_shop_wave_2"):
        run_handoff(
            _plan(dialect="postgresql"),
            source=db,
            catalog=FakeCatalog(db),
            capture=_pg_capture(db),
            connector=FakeConnector(db, target, dialect="postgresql", fail_start=True),
            target=target,
            settings=_settings(),
        )
    assert "debezium_shop_wave_2" in db.slots


def test_transform_hook_builds_extra_records_during_the_load() -> None:
    db = _seeded_db()
    target = FakeTarget()

    def with_reverse_index(row: LoadRow) -> list[TargetRecord]:
        idx = TargetRecord(
            collection="orders_by_status",
            key=(row.values["status"], row.values["id"]),
            values={"id": row.values["id"]},
        )
        return [*identity_transform(row), idx]

    _mysql_run(db, FakeConnector(db, target), target, transform=with_reverse_index)
    assert target.count(TABLE) == 100
    assert target.count("orders_by_status") == 100


def test_identity_transform_copies_the_row_under_its_key() -> None:
    row = LoadRow(table=TABLE, key_names=("a", "b"), values={"a": 1, "b": "x", "c": None})
    assert identity_transform(row) == [
        TargetRecord(collection=TABLE, key=(1, "x"), values={"a": 1, "b": "x", "c": None})
    ]


def test_a_rerun_of_the_load_is_idempotent() -> None:
    db = _seeded_db()
    target = FakeTarget()
    for _ in range(2):
        _mysql_run(db, FakeConnector(db, target), target)
    assert target.count(TABLE) == 100
    assert target.rows_of(TABLE) == db.rows[TABLE]


# ------------------------------------------------------------------ readiness


def _my(gtids: str) -> MySqlPosition:
    return MySqlPosition(file="mysql-bin.000003", pos=1, gtid_set=gtids, server_id=7)


def _handoff(load_end: MySqlPosition | PostgresPosition | None = None) -> HandoffResult:
    return HandoffResult(
        position=_my(f"{UUID}:1-100"),
        offset_seed=None,
        start_snapshot_mode="recovery",
        tables=[],
        load_started_ms=1_000,
        load_finished_ms=5_000,
        load_end_position=load_end or _my(f"{UUID}:1-110"),
        schema_fingerprint="x",
    )


@dataclass
class FakeOffsets:
    committed: MySqlPosition | PostgresPosition | None

    def committed_position(self) -> MySqlPosition | PostgresPosition | None:
        return self.committed


class CountingTarget:
    def __init__(self, counts: Mapping[str, int]):
        self.counts = dict(counts)

    def count(self, collection: str) -> int:
        return self.counts.get(collection, 0)


def _ready(
    committed: MySqlPosition | PostgresPosition | None,
    barrier: MySqlPosition | PostgresPosition,
    *,
    now_ms: int = 25_000,
    target_rows: int = 100,
    handoff: HandoffResult | None = None,
    plan: DataMovementPlan | None = None,
) -> Any:
    return check_readiness(
        plan or _plan(max_lag_seconds=30),
        source=_seeded_db(),
        offsets=FakeOffsets(committed),
        target=CountingTarget({TABLE: target_rows}),
        handoff=handoff or _handoff(),
        barrier=Barrier(position=barrier, read_at_ms=20_000),
        now_ms=now_ms,
    )


def test_ready_once_the_connector_passes_the_barrier_and_counts_match() -> None:
    r = _ready(_my(f"{UUID}:1-130"), _my(f"{UUID}:1-125"))
    assert r.ready, r.reasons
    assert r.lag_seconds == 5.0
    assert [(c.table, c.source_rows, c.target_rows, c.compared) for c in r.counts] == [
        (TABLE, 100, 100, True)
    ]


def test_fresh_heartbeats_do_not_make_a_lagging_connector_ready() -> None:
    """The connector keeps committing (heartbeats) while replaying a backlog: its
    position, not the heartbeat's emit time, decides."""
    r = _ready(_my(f"{UUID}:1-112"), _my(f"{UUID}:1-125"))
    assert not r.ready and r.lag_seconds is None
    assert "the connector is behind the barrier position" in r.reasons


def test_not_ready_before_the_load_window_is_replayed() -> None:
    r = _ready(_my(f"{UUID}:1-105"), _my(f"{UUID}:1-100"))
    assert not r.ready
    assert "the connector has not replayed the load window yet" in r.reasons


def test_an_old_barrier_bounds_the_lag_over_the_gate() -> None:
    r = _ready(_my(f"{UUID}:1-130"), _my(f"{UUID}:1-125"), now_ms=60_000)
    assert not r.ready and r.lag_seconds == 40.0
    assert any("read a new barrier" in x for x in r.reasons)


def test_not_ready_without_a_committed_offset_or_on_a_count_mismatch() -> None:
    assert not _ready(None, _my(f"{UUID}:1-125")).ready
    short = _ready(_my(f"{UUID}:1-130"), _my(f"{UUID}:1-125"), target_rows=97)
    assert not short.ready
    assert f"{TABLE}: source has 100 rows, target 97" in short.reasons


def test_postgresql_readiness_compares_lsns() -> None:
    plan = _plan(dialect="postgresql", max_lag_seconds=30)
    handoff = _handoff(load_end=PostgresPosition("s", "0/200"))
    ok = _ready(
        PostgresPosition("s", "0/300"), PostgresPosition("s", "0/280"), handoff=handoff, plan=plan
    )
    assert ok.ready, ok.reasons
    behind = _ready(
        PostgresPosition("s", "0/250"), PostgresPosition("s", "0/280"), handoff=handoff, plan=plan
    )
    assert not behind.ready


def test_cache_tables_report_counts_without_comparing_them() -> None:
    r = _ready(
        _my(f"{UUID}:1-130"),
        _my(f"{UUID}:1-125"),
        target_rows=12,
        plan=_plan(
            parity=[ParityGate(table=TABLE, check="sampled_key_compare")], max_lag_seconds=30
        ),
    )
    assert r.ready, r.reasons
    assert r.counts[0].compared is False and r.counts[0].source_rows == 100


def test_readiness_never_writes_into_the_source() -> None:
    conn = _mysql_conn("8.0.39")
    conn.answers.insert(0, (lambda s: s.startswith("SELECT COUNT"), [{"n": 100}]))
    barrier = read_barrier(MySqlPositionCapture(conn), now_ms=9_000)
    assert barrier == Barrier(
        position=MySqlPosition(
            file="mysql-bin.000003", pos=154, gtid_set=f"{UUID}:1-77", server_id=7
        ),
        read_at_ms=9_000,
    )
    r = check_readiness(
        _plan(),
        source=SqlSourceReader(conn, sql_dialect("mysql")),
        offsets=FakeOffsets(_my(f"{UUID}:1-200")),
        target=CountingTarget({TABLE: 100}),
        handoff=_handoff(),
        barrier=barrier,
        now_ms=10_000,
    )
    assert r.ready, r.reasons
    assert ("SELECT COUNT(*) AS n FROM `shop`.`orders`", ()) in conn.calls
    assert all(sql.startswith(("SELECT", "SHOW")) for sql, _ in conn.calls)
