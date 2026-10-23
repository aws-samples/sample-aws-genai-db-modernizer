"""Build one ``DataMovementPlan`` per migration wave from a finished job (#1413).

Pure and deterministic: :func:`build_plans` reads only the dicts it is given (the
synthesis report, the assignment, the collector output and the schema designs) and
the local price table; :func:`write_plans` loads them from the artifact store and
writes ``<db>/<job>/data-movement/wave-<n>/plan.json``. No AWS call, no clock.

Rules, per wave of the report's ``migration_waves`` (same count, same order):

- **The Aurora wave** (the wave with a ``homogeneity``, normally wave 1) is a DMS
  move from the legacy source: full load, then ongoing replication, straight into
  Aurora, so its ``hub`` is empty. A wave 1 with no Aurora engine names no target.
- **Every later wave** reads its source through Debezium: the engine it moves from
  (``moves_from``), the engine the cache fronts (``fronts``) or the owner a read
  model syncs from (``serves_from``). The connector class and the source settings
  come from that engine's dialect and, for the legacy source, the collector's
  ``deployment_type`` (RDS parameter groups and procedures, or a self-managed server).
  Each wave has its own ``topic_prefix`` and names a signal table. ``hub`` holds one
  compacted topic per source table the wave's targets read, keyed by its primary key,
  or by a unique index on NOT NULL columns for a table without one; a table with
  neither, or a view, has no topic and is listed under ``unsupported``.
- **Targets.** A wave engine owns a table its wave does not name in ``table_owners``
  (the deliverables' own owner rule) and replicates the rest from their owner. A
  cache engine is a ``cache`` target: a CDC-fed replica of the engine it fronts.
- **Gates.** Parity per table (a sampled key comparison for a table only a cache
  receives), a lag bound, and the reads that may lag: each replica target's reads of
  its tables, from the assignment (so a table nothing writes counts too) and the
  report's ``table_sources`` replicas, and the cache's reads.
- **Unsupported.** What no writer, connector or price can provide yet, each with
  its reason: a table with no key, a view, a key design placeholder or a mapped
  attribute that names no source column, a table its engine's design maps nowhere, an
  engine with no schema design, a replica or cache that would have to be fed from a
  non-relational owner once the wave cuts over, a dialect with no Debezium connector,
  and the cost when the price table has no price for a component.
- **Cost** (#1424), computed here once and read by the deliverables through the facts
  layer (ADR-032). Wave 1: one DMS replication instance and its storage for a month.
  A Debezium wave: topic storage on each of three MSK brokers (the hub tables' collected
  size), MSK Connect workers (one connector for the source and one per target), and,
  for the first Debezium wave only, the brokers' hours. Each component reads the dated
  price table; a missing price or an unmeasured table leaves the cost null and says why.
  The targets' own cost stays in the TCO.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.agents.referee.table_resolution import TableNameResolver
from src.contracts.data_movement_plan import (
    CdcSpec,
    DataMovementPlan,
    Gates,
    HubTopic,
    InitialLoad,
    LaggingRead,
    ParityGate,
    SourceSetting,
    SourceSpec,
    SourceTable,
    Stack,
    Target,
    TransformationRef,
    UnsupportedItem,
)
from src.shared import catalog
from src.shared.catalog.prices import HOURS_PER_MONTH, PriceTable
from src.shared.engine_names import display_engine
from src.shared.migration_wave_engines import CACHE_ENGINES, RELATIONAL_ENGINES, SEARCH_ENGINES

# The cost model's components (#1424); each is stated in the plan's cost_assumptions.
DMS_PRICE_SECTION = "dms.replication"
DMS_INSTANCE_CLASS = "dms.r6i.large"
DMS_STORAGE_GB = 100
MSK_PRICE_SECTION = "msk.provisioned"
MSK_BROKER_TYPE = "kafka.m5.large"
MSK_BROKERS = 3
MSK_CONNECT_PRICE_SECTION = "msk_connect.workers"
MSK_CONNECT_MCU_PER_CONNECTOR = 1


class _Default:
    """The default for ``build_plans(prices=...)``: the committed price table."""


_DEFAULT = _Default()

# Defaults the plan states in its notes; nothing measured them for the job.
HUB_PARTITIONS = 3
HEARTBEAT_INTERVAL_MS = 10_000
MAX_LAG_SECONDS = 5
# The incremental-snapshot signalling table each Debezium wave names on its source.
SIGNAL_TABLE = "debezium_signal"

# Source database engine -> SQL dialect. A target engine's dialect is that of the first
# source engine the catalog names as its own (``source_engines``, e.g. Aurora MySQL's).
SOURCE_DIALECTS: dict[str, str] = {
    "mysql": "mysql",
    "mariadb": "mariadb",
    "postgresql": "postgresql",
    "postgres": "postgresql",
    "sqlserver": "sqlserver",
    "oracle": "oracle",
    "db2": "db2",
}

DEBEZIUM_CONNECTORS: dict[str, str] = {
    "mysql": "io.debezium.connector.mysql.MySqlConnector",
    "mariadb": "io.debezium.connector.mariadb.MariaDbConnector",
    "postgresql": "io.debezium.connector.postgresql.PostgresConnector",
    "sqlserver": "io.debezium.connector.sqlserver.SqlServerConnector",
    "oracle": "io.debezium.connector.oracle.OracleConnector",
    "db2": "io.debezium.connector.db2.Db2Connector",
}

# Dialects whose initial load hands a captured log position to the connector (#1412:
# the bulk loader's position-capture handoff); the others snapshot through the connector.
POSITION_HANDOFF: dict[str, str] = {
    "mysql": "binlog_position",
    "mariadb": "binlog_position",
    "postgresql": "replication_slot_lsn",
}

_MYSQL_SETTINGS = (
    ("binlog_format", "ROW", "Row-based binary log, so each change carries the row"),
    ("binlog_row_image", "FULL", "Every column in each change, not only the changed ones"),
    (
        "binlog retention",
        "at least 24 hours",
        "Long enough to cover the initial load: binlog_expire_logs_seconds on a "
        "self-managed server",
    ),
)
_MYSQL_MANAGED_SETTINGS = (
    (
        "binlog_format",
        "ROW",
        "DB parameter group (cluster parameter group on Aurora): row-based binary log",
    ),
    ("binlog_row_image", "FULL", "Same parameter group: every column in each change"),
    (
        "binlog retention hours",
        "at least 24",
        "CALL mysql.rds_set_configuration('binlog retention hours', 24); automated "
        "backups must be on for the binary log to be kept",
    ),
)
_SETTINGS: dict[tuple[str, str], tuple[tuple[str, str, str], ...]] = {
    ("mysql", "managed"): _MYSQL_MANAGED_SETTINGS,
    ("mysql", "self_managed"): _MYSQL_SETTINGS,
    ("mariadb", "managed"): _MYSQL_MANAGED_SETTINGS,
    ("mariadb", "self_managed"): _MYSQL_SETTINGS,
    ("postgresql", "managed"): (
        (
            "rds.logical_replication",
            "1",
            "DB parameter group (cluster parameter group on Aurora): logical decoding on",
        ),
        ("plugin.name", "pgoutput", "The built-in logical decoding plugin"),
    ),
    ("postgresql", "self_managed"): (
        ("wal_level", "logical", "Logical decoding on"),
        ("max_replication_slots", "at least 1", "One slot per capture task"),
        ("max_wal_senders", "at least 1", "One sender per capture task"),
    ),
    ("sqlserver", "self_managed"): (
        ("recovery model", "FULL", "The transaction log keeps every change until captured"),
        (
            "change data capture",
            "enabled on the database and each table",
            "sys.sp_cdc_enable_db, then sys.sp_cdc_enable_table per table",
        ),
        ("SQL Server Agent", "running", "It runs the capture jobs"),
    ),
    ("sqlserver", "managed"): (
        ("automated backups", "on", "Keeps the database in the FULL recovery model"),
        (
            "change data capture",
            "enabled on the database and each table",
            "EXEC msdb.dbo.rds_cdc_enable_db, then sys.sp_cdc_enable_table per table",
        ),
    ),
    ("oracle", "managed"): (
        ("automated backups", "on", "Keeps the database in ARCHIVELOG mode"),
        (
            "supplemental logging",
            "database minimal, and primary-key or all columns per table",
            "rdsadmin.rdsadmin_util.alter_supplemental_logging",
        ),
    ),
    ("db2", "managed"): (
        ("automated backups", "on", "Keeps archive logging on, which capture reads"),
    ),
    ("oracle", "self_managed"): (
        ("log mode", "ARCHIVELOG", "Archived redo logs are what capture reads"),
        (
            "supplemental logging",
            "database minimal, and primary-key or all columns per table",
            "So each change identifies its row",
        ),
    ),
    ("db2", "self_managed"): (
        ("LOGARCHMETH1", "archive logging enabled", "Archived logs are what capture reads"),
    ),
}

# The design sections that name the source tables each key, table or index is built from.
_DESIGN_SECTIONS = (
    "key_designs",
    "table_definitions",
    "collections",
    "index_designs",
    "data_stream_designs",
)

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_TOPIC_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class JobInputs:
    """Everything a plan is built from, as read from the job's artifacts."""

    job_id: str
    database_name: str
    report: Mapping[str, Any]
    assignment: Mapping[str, Any]
    collector: Mapping[str, Any]
    # engine -> (path under the job, the design's payload)
    schema_designs: Mapping[str, tuple[str, Mapping[str, Any]]] = field(default_factory=dict)


@dataclass
class _Schema:
    """The collector's tables and views, with a resolver for parsed names."""

    tables: dict[str, Mapping[str, Any]]
    views: set[str]
    resolver: TableNameResolver | None

    @classmethod
    def from_collector(cls, collector: Mapping[str, Any]) -> _Schema:
        schema = collector.get("database_schema") or {}
        tables = {str(t["table_id"]): t for t in schema.get("tables") or [] if t.get("table_id")}
        views = {
            str(v.get("view_id") or v.get("view_name"))
            for v in schema.get("views") or []
            if v.get("view_id") or v.get("view_name")
        }
        return cls(tables, views, TableNameResolver.from_collector(collector))

    def resolve(self, name: str) -> str | None:
        if name in self.tables or name in self.views:
            return name
        return self.resolver.resolve(name) if self.resolver else None

    def columns(self, table_id: str) -> dict[str, str]:
        """Lower-cased column name -> the collector's spelling."""
        cols = self.tables.get(table_id, {}).get("columns") or []
        return {str(c["column_name"]).lower(): str(c["column_name"]) for c in cols}


def _sorted_unique(items: Iterable[str]) -> list[str]:
    return sorted({str(i) for i in items if i})


def _hub_key(table: Mapping[str, Any]) -> tuple[list[str], str] | None:
    """(key columns, key source) for a table's topic, or None when it has no key."""
    pk = [str(c) for c in table.get("primary_key") or []]
    if pk:
        return pk, "primary_key"
    not_null = {str(c["column_name"]) for c in table.get("columns") or [] if not c.get("nullable")}
    candidates = sorted(
        (
            (len(ix.get("columns") or []), str(ix.get("index_name") or ""), list(ix["columns"]))
            for ix in table.get("indexes") or []
            if ix.get("is_unique")
            and not ix.get("predicate")
            and ix.get("columns")
            and all(str(c) in not_null for c in ix["columns"])
        ),
    )
    if not candidates:
        return None
    return [str(c) for c in candidates[0][2]], "unique_index"


def topic_prefix(job_id: str, wave: int) -> str:
    """Debezium ``topic.prefix`` for a wave: unique per job and wave, so replication slots,
    publications and schema-history topics never collide across waves."""
    return _TOPIC_UNSAFE.sub("-", f"dm-{job_id}-w{wave}")


def _topic(prefix: str, table_id: str) -> str:
    return _TOPIC_UNSAFE.sub("-", f"{prefix}.{table_id}")


def _deployment(job: _JobView, engine: str) -> str | None:
    """The collector's deployment_type, which describes the legacy source only."""
    return job.deployment_type if engine == job.legacy else None


def dialect_of(engine: str) -> str | None:
    """The SQL dialect of a source engine or of a catalog engine with source engines."""
    if engine in SOURCE_DIALECTS:
        return SOURCE_DIALECTS[engine]
    entry = catalog.engine_entry(engine)
    if entry is not None and entry.source_engines:
        return SOURCE_DIALECTS.get(entry.source_engines[0])
    return None


# The collector's deployment_type of a source set through RDS parameter groups and procedures.
MANAGED_DEPLOYMENTS = frozenset({"rds_instance"})


def _source_flavour(engine: str, deployment_type: str | None) -> str:
    """``managed`` for a catalog engine (the retained Aurora engine) or a legacy source the
    collector saw on RDS (``deployment_type``), else ``self_managed``."""
    if catalog.engine_entry(engine) is not None or deployment_type in MANAGED_DEPLOYMENTS:
        return "managed"
    return "self_managed"


def _settings(engine: str, dialect: str | None, deployment_type: str | None) -> list[SourceSetting]:
    if dialect is None:
        return []
    rows = _SETTINGS.get((dialect, _source_flavour(engine, deployment_type)), ())
    return [SourceSetting(name=n, value=v, note=note) for n, v, note in rows]


def _wave_engines(wave: Mapping[str, Any]) -> list[str]:
    return [str(e) for e in wave.get("engines") or []]


def _cdc_source_engine(wave: Mapping[str, Any], legacy: str) -> str:
    """The engine a later wave reads its changes from."""
    for key in ("moves_from", "serves_from"):
        engines = sorted(str(e) for e in wave.get(key) or [])
        if engines:
            return engines[0]
    if wave.get("fronts"):
        return str(wave["fronts"])
    return legacy


def _assigned_tables(
    query_assignments: list[Mapping[str, Any]], schema: _Schema
) -> dict[str, set[str]]:
    """engine -> the tables its in-scope queries read; cache engine -> its cached reads'."""
    out: dict[str, set[str]] = {}
    for qa in query_assignments:
        names = [schema.resolve(str(t)) for t in qa.get("source_tables") or []]
        tables = {t for t in names if t}
        if qa.get("in_scope", True) and qa.get("assigned_engine"):
            out.setdefault(str(qa["assigned_engine"]), set()).update(tables)
        if qa.get("cache_engine") and not qa.get("cache_dropped"):
            out.setdefault(f"cache:{qa['cache_engine']}", set()).update(tables)
    return out


def _engine_reads(
    query_assignments: list[Mapping[str, Any]],
    schema: _Schema,
    sources: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, list[str]]]:
    """engine -> table -> the in-scope queries of the engine that read it, not its writes.

    From the assignment, so a table nothing writes (absent from ``table_sources``) still
    has its reads; a table's writes and locking reads are its ``write_query_ids``."""
    writes = {t: {str(q) for q in row.get("write_query_ids") or []} for t, row in sources.items()}
    out: dict[str, dict[str, set[str]]] = {}
    for qa in query_assignments:
        if not qa.get("in_scope", True) or not qa.get("assigned_engine"):
            continue
        qid = str(qa.get("query_id") or "")
        for name in qa.get("source_tables") or []:
            table = schema.resolve(str(name))
            if table and qid and qid not in writes.get(table, set()):
                out.setdefault(str(qa["assigned_engine"]), {}).setdefault(table, set()).add(qid)
    return {e: {t: sorted(ids) for t, ids in by.items()} for e, by in out.items()}


def _cached_reads(
    query_assignments: list[Mapping[str, Any]], schema: _Schema, cache_engine: str
) -> dict[str, list[str]]:
    """table -> the cached reads of it, sorted."""
    out: dict[str, set[str]] = {}
    for qa in query_assignments:
        if qa.get("cache_engine") != cache_engine or qa.get("cache_dropped"):
            continue
        for name in qa.get("source_tables") or []:
            table = schema.resolve(str(name))
            if table:
                out.setdefault(table, set()).add(str(qa["query_id"]))
    return {t: sorted(ids) for t, ids in out.items()}


def _transformation(inputs: JobInputs, engine: str) -> TransformationRef | None:
    found = inputs.schema_designs.get(engine)
    if found is None:
        return None
    path, payload = found
    version = payload.get("contract_version")
    return TransformationRef(artifact=path, design_version=str(version) if version else None)


def _attr_name(attr: Any) -> str:
    return str(attr.get("name")) if isinstance(attr, Mapping) else str(attr)


def _attribute_gap(
    engine: str, item: str, attr: Any, default_table: str | None, schema: _Schema
) -> list[UnsupportedItem]:
    """The attribute as unsupported when its direct source column is not a collected column."""
    if not isinstance(attr, Mapping):
        return []
    if attr.get("join") or attr.get("calculation") or attr.get("transformation"):
        return []  # derived on purpose; the design says how
    raw = attr.get("source_column")
    columns = [str(c) for c in (raw if isinstance(raw, list) else [raw]) if c]
    src = schema.resolve(str(attr["source_table"])) if attr.get("source_table") else default_table
    if columns and src:
        known = schema.columns(src)
        missing = [c for c in columns if c.lower() not in known]
        if not missing:
            return []
        reason = f"Source column {', '.join(missing)} is not a column of {src}"
    else:
        reason = (
            "It names no source column and no join, calculation or transformation, so a "
            "writer has nothing to build it from"
        )
    return [UnsupportedItem(kind="attribute", table=src, engine=engine, item=item, reason=reason)]


def _design_gaps(
    engine: str, payload: Mapping[str, Any], schema: _Schema, tables: set[str]
) -> list[UnsupportedItem]:
    """Key designs and attributes of ``engine``'s design that name no source column."""
    gaps: list[UnsupportedItem] = []

    def columns_of(names: Iterable[Any]) -> tuple[list[str], dict[str, str]]:
        resolved = _sorted_unique(schema.resolve(str(n)) or "" for n in names)
        cols: dict[str, str] = {}
        for t in resolved:
            cols.update(schema.columns(t))
        return resolved, cols

    if engine in CACHE_ENGINES:
        for kd in payload.get("key_designs") or []:
            pattern = str(kd.get("key_pattern") or "")
            resolved, cols = columns_of(kd.get("source_tables") or [])
            if tables and resolved and not set(resolved) & tables:
                continue
            table = resolved[0] if len(resolved) == 1 else None
            missing = [p for p in _PLACEHOLDER.findall(pattern) if p.lower() not in cols]
            if missing:
                gaps.append(
                    UnsupportedItem(
                        kind="key_design",
                        table=table,
                        engine=engine,
                        item=pattern,
                        reason=(
                            f"Placeholder {', '.join(sorted(missing))} is not a column of "
                            f"{', '.join(resolved) or 'any collected table'}, so a writer "
                            "cannot build the key from a row"
                        ),
                    )
                )
            for f in kd.get("fields_mapped") or []:
                if str(f).lower() not in cols:
                    gaps.append(
                        UnsupportedItem(
                            kind="attribute",
                            table=table,
                            engine=engine,
                            item=f"{pattern} {f}",
                            reason=(
                                f"Field {f} is not a column of "
                                f"{', '.join(resolved) or 'any collected table'}"
                            ),
                        )
                    )
    for td in payload.get("table_definitions") or []:
        name = str(td.get("table_name") or "")
        resolved, _ = columns_of(td.get("source_tables") or [])
        if tables and resolved and not set(resolved) & tables:
            continue
        for attr in td.get("attributes") or []:
            gaps.extend(_attribute_gap(engine, f"{name}.{_attr_name(attr)}", attr, None, schema))
        # An item collection keeps its attributes and key templates per entity (#1413).
        for entity in td.get("entities") or []:
            if not isinstance(entity, Mapping):
                continue
            src = schema.resolve(str(entity.get("source_table") or ""))
            label = f"{name}#{entity.get('entity_type')}"
            cols = schema.columns(src) if src else {}
            for key in ("pk_template", "sk_template"):
                template = str(entity.get(key) or "")
                missing = [p for p in _PLACEHOLDER.findall(template) if p.lower() not in cols]
                if missing:
                    gaps.append(
                        UnsupportedItem(
                            kind="key_design",
                            table=src,
                            engine=engine,
                            item=f"{label} {template}",
                            reason=(
                                f"Placeholder {', '.join(sorted(missing))} is not a column of "
                                f"{src or entity.get('source_table')}, so a writer cannot build "
                                "the key from a row"
                            ),
                        )
                    )
            for attr in entity.get("attributes") or []:
                gaps.extend(
                    _attribute_gap(engine, f"{label}.{_attr_name(attr)}", attr, src, schema)
                )
    mapped: set[str] = set()
    for section in _DESIGN_SECTIONS:
        for entry in payload.get(section) or []:
            if isinstance(entry, Mapping):
                names = [*(entry.get("source_tables") or [])]
                names += [
                    e.get("source_table")
                    for e in entry.get("entities") or []
                    if isinstance(e, Mapping)
                ]
                mapped.update(t for t in (schema.resolve(str(n)) for n in names if n) if t)
    if any(payload.get(section) for section in _DESIGN_SECTIONS):
        for t in sorted(tables - mapped):
            gaps.append(
                UnsupportedItem(
                    kind="unmapped_table",
                    table=t,
                    engine=engine,
                    reason=(
                        f"The {display_engine(engine)} design maps no key or table to it, so a "
                        "writer has nowhere to put its rows"
                    ),
                )
            )
    return gaps


def _section(sections: Mapping[str, Mapping[str, Mapping[str, Any]]], path: str) -> dict[str, Any]:
    engine, _, config = path.partition(".")
    return dict(sections.get(engine, {}).get(config, {}))


def _cost_gap(item: str, reason: str) -> UnsupportedItem:
    return UnsupportedItem(kind="cost", item=item, reason=reason)


def _no_price(what: str) -> str:
    return f"The price table (src/catalog/prices.toml) has no price for {what}, so the cost is not stated"


def _dms_cost(
    sections: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> tuple[float | None, list[UnsupportedItem]]:
    """One replication instance for a month, plus its allocated storage."""
    prices = _section(sections, DMS_PRICE_SECTION)
    hourly = (prices.get("instance_hourly") or {}).get(DMS_INSTANCE_CLASS)
    storage = prices.get("storage_price_per_gb_month")
    gaps = []
    if hourly is None:
        gaps.append(
            _cost_gap("dms", _no_price(f"the DMS replication instance {DMS_INSTANCE_CLASS}"))
        )
    if storage is None:
        gaps.append(_cost_gap("dms_storage", _no_price("DMS replication instance storage")))
    if gaps:
        return None, gaps
    # Both prices exist here: a missing one returned above.
    return float(hourly or 0) * HOURS_PER_MONTH + float(storage or 0) * DMS_STORAGE_GB, []


def _hub_gigabytes(job: _JobView, hub: list[HubTopic]) -> float | None:
    """The hub topics' size: each table's collected size (a compacted topic keeps each
    key's latest row, so about the table's size); None when a table's size is unknown."""
    total_mb = 0.0
    for topic in hub:
        size = job.schema.tables[topic.table].get("size_mb")
        if size is None:
            return None
        total_mb += float(size)
    return total_mb / 1024


def _debezium_cost(
    sections: Mapping[str, Mapping[str, Mapping[str, Any]]],
    job: _JobView,
    hub: list[HubTopic],
    targets: list[Target],
    carries_cluster: bool,
) -> tuple[float | None, list[UnsupportedItem]]:
    """Broker hours (the first Debezium wave only), topic storage and MSK Connect workers."""
    msk = _section(sections, MSK_PRICE_SECTION)
    connect = _section(sections, MSK_CONNECT_PRICE_SECTION)
    gaps: list[UnsupportedItem] = []
    hourly = (msk.get("instance_hourly") or {}).get(MSK_BROKER_TYPE)
    storage = msk.get("storage_price_per_gb_month")
    mcu = connect.get("mcu_price_per_hour")
    if carries_cluster and hourly is None:
        gaps.append(_cost_gap("msk", _no_price(f"MSK brokers of type {MSK_BROKER_TYPE}")))
    if storage is None:
        gaps.append(_cost_gap("msk_storage", _no_price("MSK broker storage")))
    if mcu is None:
        gaps.append(_cost_gap("msk_connect", _no_price("MSK Connect workers (per MCU-hour)")))
    gigabytes = _hub_gigabytes(job, hub)
    if gigabytes is None:
        gaps.append(
            _cost_gap(
                "hub_storage",
                "The collector did not measure the size of every hub table (size_mb), so the "
                "topic storage and the cost are not stated",
            )
        )
    if gaps:
        return None, gaps
    # Every price and the size exist here: a missing one returned above.
    # Each of the three brokers holds one copy of every partition, so every broker
    # provisions the topics' size, rounded up to a whole GiB (at least 1 GiB with a topic).
    per_broker = math.ceil(gigabytes or 0) if hub else 0
    total = float(storage or 0) * per_broker * MSK_BROKERS
    total += float(mcu or 0) * MSK_CONNECT_MCU_PER_CONNECTOR * (1 + len(targets)) * HOURS_PER_MONTH
    if carries_cluster:
        total += float(hourly or 0) * MSK_BROKERS * HOURS_PER_MONTH
    return total, []


def _monthly_cost(
    prices: PriceTable | None,
    job: _JobView,
    tool: str,
    hub: list[HubTopic],
    targets: list[Target],
    carries_cluster: bool,
) -> tuple[float | None, list[str], list[UnsupportedItem]]:
    """The wave's monthly cost, computed once here (ADR-032), its assumptions, and an
    unsupported entry for each component the price table cannot price."""
    sections: Mapping[str, Mapping[str, Mapping[str, Any]]] = prices.sections if prices else {}
    if tool == "dms":
        assumptions = [
            f"One DMS replication instance ({DMS_INSTANCE_CLASS}, Single-AZ) with "
            f"{DMS_STORAGE_GB} GB of allocated storage runs the full load and ongoing "
            "replication for the wave",
            f"The migration is billed as one month ({HOURS_PER_MONTH} hours); the instance is removed "
            "once the wave cuts over",
        ]
        cost, gaps = _dms_cost(sections)
    else:
        assumptions = [
            f"One provisioned MSK cluster of {MSK_BROKERS} {MSK_BROKER_TYPE} brokers, one per "
            "Availability Zone, holds the hub topics; later waves reuse it, so its broker "
            "hours are charged to the first Debezium wave only",
            "Topic storage is each hub table's collected size (a compacted topic keeps each "
            f"key's latest row), provisioned on each of the {MSK_BROKERS} brokers and rounded "
            "up to a whole GiB",
            f"MSK Connect runs one Debezium source connector and one writer per target, each "
            f"at {MSK_CONNECT_MCU_PER_CONNECTOR} MCU for {HOURS_PER_MONTH} hours",
        ]
        cost, gaps = _debezium_cost(sections, job, hub, targets, carries_cluster)
    assumptions.append("The targets' own cost is in the total cost of ownership, not repeated here")
    if prices is not None:
        assumptions.append(
            f"Prices: AWS Price List, {prices.region}, on-demand, retrieved on "
            f"{prices.retrieved_on.isoformat()}"
        )
    return (None if cost is None else round(cost, 2)), assumptions, gaps


def _owner_of(
    table: str,
    wave: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    readers: list[str],
    fallback: str,
) -> str:
    """The engine that holds ``table``'s writes once the wave is done."""
    for row in wave.get("table_owners") or []:
        if row.get("table") == table and row.get("owner") and row["owner"] not in readers:
            return str(row["owner"])
    owners_in_wave = sorted(
        str(r["owner"])
        for r in wave.get("table_owners") or []
        if r.get("table") == table and r.get("owner") in readers
    )
    if owners_in_wave:
        return owners_in_wave[0]
    sot = (sources.get(table) or {}).get("source_of_truth")
    if sot and sot in readers:
        return str(sot)
    owners = [e for e in readers if e not in SEARCH_ENGINES]
    if len(owners) == 1:
        return owners[0]
    if sot:
        return str(sot)
    return sorted(owners)[0] if owners else fallback


def _stacks(tool: str, targets: list[Target]) -> list[Stack]:
    if tool == "dms":
        rows = [
            (
                "dms-replication",
                "DMS replication instance, source and target endpoints, and the full-load "
                "and ongoing-replication task",
            )
        ]
    else:
        rows = [
            ("msk-hub", "MSK cluster and the compacted per-table topics"),
            ("debezium-source", "Debezium source connector on MSK Connect"),
        ]
        for engine in _sorted_unique(t.engine for t in targets):
            rows.append(
                (
                    f"{engine.replace('_', '-')}-writer",
                    f"Writer that applies the hub topics to {display_engine(engine)}",
                )
            )
    return [Stack(order=i, name=n, purpose=p) for i, (n, p) in enumerate(rows, start=1)]


def _unsupported_key(u: UnsupportedItem) -> tuple[str, str, str, str]:
    return (u.kind, u.table or "", u.engine or "", u.item or "")


@dataclass(frozen=True)
class _JobView:
    """What every wave's plan reads, resolved once per job."""

    inputs: JobInputs
    schema: _Schema
    legacy: str
    database: str
    assignment_version: int
    read_by: dict[str, set[str]]
    sources: dict[str, Mapping[str, Any]]
    cached_reads: dict[str, dict[str, list[str]]]
    # engine -> table -> the engine's in-scope reads of it (its writes left out), sorted
    reads: dict[str, dict[str, list[str]]]
    deployment_type: str | None
    prices: PriceTable | None

    @classmethod
    def build(cls, inputs: JobInputs, prices: PriceTable | None) -> _JobView:
        schema = _Schema.from_collector(inputs.collector)
        source_db = (inputs.collector.get("metadata") or {}).get("source_database") or {}
        query_assignments = [
            q for q in inputs.assignment.get("query_assignments") or [] if isinstance(q, Mapping)
        ]
        sources: dict[str, Mapping[str, Any]] = {}
        for row in inputs.report.get("table_sources") or []:
            name = str(row.get("table") or "")
            sources[schema.resolve(name) or name] = row
        deployment = source_db.get("deployment_type")
        return cls(
            inputs=inputs,
            schema=schema,
            legacy=str(source_db.get("engine") or "").lower(),
            database=str(source_db.get("database_name") or inputs.database_name),
            assignment_version=int(
                (inputs.report.get("assignment_summary") or {}).get("version")
                or inputs.assignment.get("version")
                or 0
            ),
            read_by=_assigned_tables(query_assignments, schema),
            sources=sources,
            cached_reads={
                e: _cached_reads(query_assignments, schema, e) for e in sorted(CACHE_ENGINES)
            },
            reads=_engine_reads(query_assignments, schema, sources),
            deployment_type=str(deployment) if deployment else None,
            prices=prices,
        )


def _wave_tables(
    job: _JobView, wave: Mapping[str, Any], engines: list[str], is_first: bool
) -> tuple[dict[str, set[str]], set[str], list[UnsupportedItem]]:
    """(engine -> the tables it receives, every table the wave reads, unknown names)."""
    wave_tables: set[str] = set()
    unknown: list[UnsupportedItem] = []
    for name in sorted(str(n) for n in wave.get("tables") or []):
        resolved = job.schema.resolve(name)
        if resolved:
            wave_tables.add(resolved)
        else:
            unknown.append(
                UnsupportedItem(
                    kind="unknown_table",
                    table=name,
                    reason="The wave names it, but the collector saw no such table or view",
                )
            )
    per_engine: dict[str, set[str]] = {}
    for engine in engines:
        if engine in CACHE_ENGINES:
            per_engine[engine] = set(job.read_by.get(f"cache:{engine}", set()))
        elif is_first or len(engines) == 1:
            per_engine[engine] = set(wave_tables)
        else:
            # The merged DynamoDB/DocumentDB wave: each engine receives what it reads.
            per_engine[engine] = job.read_by.get(engine, set()) & wave_tables
    read_tables = set(wave_tables) if is_first else set().union(*per_engine.values())
    return per_engine, read_tables, unknown


def _no_key(table: str, why: str) -> UnsupportedItem:
    return UnsupportedItem(
        kind="no_key",
        table=table,
        reason=f"No primary key and no unique index on NOT NULL columns, {why}",
    )


def _dms_capture(
    job: _JobView,
    wave: Mapping[str, Any],
    source_engine: str,
    dialect: str | None,
    base_tables: list[str],
) -> tuple[CdcSpec, InitialLoad, list[UnsupportedItem], list[str]]:
    cdc = CdcSpec(
        tool="dms",
        required_source_settings=_settings(source_engine, dialect, _deployment(job, source_engine)),
    )
    initial_load = InitialLoad(strategy="dms_full_load", position_capture="dms_managed")
    unsupported = [
        _no_key(t, "so ongoing replication cannot match its updates and deletes to a row")
        for t in base_tables
        if _hub_key(job.schema.tables[t]) is None
    ]
    notes = (
        ["Cross-engine move: the schema is converted to the target dialect before DMS loads it"]
        if wave.get("homogeneity") == "cross_engine"
        else []
    )
    return cdc, initial_load, unsupported, notes


def _debezium_capture(
    job: _JobView,
    wave_number: int,
    source_engine: str,
    dialect: str | None,
    base_tables: list[str],
    views: list[str],
) -> tuple[CdcSpec, InitialLoad, list[HubTopic], list[UnsupportedItem], list[str]]:
    connector = DEBEZIUM_CONNECTORS.get(dialect or "")
    handoff = POSITION_HANDOFF.get(dialect or "")
    prefix = topic_prefix(job.inputs.job_id, wave_number)
    signal = f"{job.database}.{SIGNAL_TABLE}"
    settings = _settings(source_engine, dialect, _deployment(job, source_engine))
    settings.append(
        SourceSetting(
            name="signal table",
            value=f"{signal} (id VARCHAR(42) PRIMARY KEY, type VARCHAR(32) NOT NULL, data TEXT)",
            note=(
                "Created before the connector starts; Debezium reads incremental-snapshot "
                "signals from it, and the connector's user may insert into it"
            ),
        )
    )
    cdc = CdcSpec(
        tool="debezium",
        connector_class=connector,
        topic_prefix=prefix,
        signal_table=signal,
        snapshot_mode="no_data" if handoff else "initial",
        heartbeat_interval_ms=HEARTBEAT_INTERVAL_MS,
        required_source_settings=settings,
    )
    if handoff:
        initial_load = InitialLoad.model_validate(
            {"strategy": "bulk_load_with_position_handoff", "position_capture": handoff}
        )
    else:
        initial_load = InitialLoad(
            strategy="connector_snapshot", position_capture="connector_offsets"
        )
    unsupported: list[UnsupportedItem] = []
    if connector is None:
        unsupported.append(
            UnsupportedItem(
                kind="source_dialect",
                engine=source_engine,
                reason=f"No Debezium source connector for {display_engine(source_engine)}",
            )
        )
    hub: list[HubTopic] = []
    for t in base_tables:
        key = _hub_key(job.schema.tables[t])
        if key is None:
            unsupported.append(_no_key(t, "so its topic has no message key to compact on"))
            continue
        hub.append(
            HubTopic.model_validate(
                {
                    "table": t,
                    "topic": _topic(prefix, t),
                    "key_columns": key[0],
                    "key_source": key[1],
                    "partitions": HUB_PARTITIONS,
                }
            )
        )
    unsupported.extend(
        UnsupportedItem(
            kind="view",
            table=v,
            reason="A view has no change stream; the topics of its base tables feed it",
        )
        for v in views
    )
    notes = [
        f"Hub topics have {HUB_PARTITIONS} partitions each, keyed by the message key so each "
        "row's changes stay in order; raise it for a hot table"
    ]
    return cdc, initial_load, hub, unsupported, notes


def _targets(
    job: _JobView,
    wave: Mapping[str, Any],
    per_engine: dict[str, set[str]],
    source_engine: str,
    is_first: bool,
) -> tuple[list[Target], list[UnsupportedItem]]:
    targets: list[Target] = []
    unsupported: list[UnsupportedItem] = []
    readers = [e for e in _wave_engines(wave) if e not in CACHE_ENGINES]
    for engine in sorted(per_engine):
        tables = per_engine[engine]
        if is_first:
            targets.append(
                Target(engine=engine, role="owner", tables=sorted(tables), source_of_truth=engine)
            )
            continue
        transformation = _transformation(job.inputs, engine)
        if engine in CACHE_ENGINES:
            fronted = str(wave.get("fronts") or source_engine)
            targets.append(
                Target(
                    engine=engine,
                    role="cache",
                    tables=sorted(tables),
                    source_of_truth=fronted,
                    transformation=transformation,
                )
            )
        else:
            by_owner: dict[str, set[str]] = {}
            for t in tables:
                if engine in SEARCH_ENGINES:
                    # A read model never owns: it syncs from the table's writer.
                    sot = (job.sources.get(t) or {}).get("source_of_truth")
                    holder = str(sot or source_engine)
                else:
                    holder = _owner_of(t, wave, job.sources, readers, source_engine)
                by_owner.setdefault(holder, set()).add(t)
            for holder in sorted(by_owner, key=lambda h: (h != engine, h)):
                targets.append(
                    Target(
                        engine=engine,
                        role="owner" if holder == engine else "replica",
                        tables=sorted(by_owner[holder]),
                        source_of_truth=holder,
                        transformation=transformation,
                    )
                )
        if transformation is None:
            unsupported.append(
                UnsupportedItem(
                    kind="no_schema_design",
                    engine=engine,
                    reason=(
                        f"The job has no schema design for {display_engine(engine)}, so its "
                        "writer has no transformation to apply"
                    ),
                )
            )
        else:
            design = job.inputs.schema_designs[engine][1]
            unsupported.extend(_design_gaps(engine, design, job.schema, tables))
    if is_first and not per_engine:
        unsupported.append(
            UnsupportedItem(
                kind="no_target",
                reason="Wave 1 names no Aurora engine, so DMS has no target to load",
            )
        )
    return targets, unsupported


def _reverse_feeds(job: _JobView, table: str, owner: str) -> list[UnsupportedItem]:
    """The replicas and cache of ``table`` that a non-relational ``owner`` would have to feed."""
    row = job.sources.get(table) or {}
    followers = {
        str(r.get("engine")): len(r.get("read_query_ids") or [])
        for r in row.get("replicas") or []
        if r.get("engine") and r.get("engine") != owner
    }
    for engine, reads in sorted(job.cached_reads.items()):
        if reads.get(table):
            followers.setdefault(engine, len(reads[table]))
    return [
        UnsupportedItem(
            kind="reverse_replication",
            table=table,
            engine=follower,
            reason=(
                f"Once this wave cuts over, its writes land on {display_engine(owner)} and "
                f"{display_engine(follower)} keeps {count} read{'s' if count != 1 else ''} of it "
                "as a replica, but the hub is fed only from a relational source"
            ),
        )
        for follower, count in sorted(followers.items())
    ]


def _replica_of_non_relational(table: str, engine: str, owner: str, reads: int) -> UnsupportedItem:
    return UnsupportedItem(
        kind="reverse_replication",
        table=table,
        engine=engine,
        reason=(
            f"{display_engine(engine)} keeps {reads} read{'s' if reads != 1 else ''} of it as a "
            f"replica of {display_engine(owner)}, which owns it once its wave cuts over, but the "
            "hub is fed only from a relational source"
        ),
    )


def _gates(
    job: _JobView,
    wave: Mapping[str, Any],
    source_engine: str,
    targets: list[Target],
    read_tables: set[str],
    base_tables: list[str],
    is_first: bool,
) -> tuple[Gates, list[UnsupportedItem]]:
    lagging: list[LaggingRead] = []
    unsupported: list[UnsupportedItem] = []
    cache_target = next((t for t in targets if t.role == "cache"), None)
    if cache_target is not None:
        reads = job.cached_reads.get(cache_target.engine, {})
        lagging.extend(
            LaggingRead(table=t, engine=cache_target.engine, read_query_ids=reads[t])
            for t in cache_target.tables
            if reads.get(t)
        )
    if not is_first:
        for tg in targets:
            if tg.role != "replica":
                continue
            for t in tg.tables:
                replicated = {
                    str(i)
                    for r in (job.sources.get(t) or {}).get("replicas") or []
                    if r.get("engine") == tg.engine
                    for i in r.get("read_query_ids") or []
                }
                ids = sorted(replicated | set(job.reads.get(tg.engine, {}).get(t, [])))
                if ids:
                    lagging.append(LaggingRead(table=t, engine=tg.engine, read_query_ids=ids))
                sot = tg.source_of_truth
                if sot and sot != source_engine and dialect_of(sot) is None:
                    unsupported.append(_replica_of_non_relational(t, tg.engine, sot, len(ids)))
        owners = {t: tg.engine for tg in targets if tg.role == "owner" for t in tg.tables}
        for t in sorted(read_tables):
            owner = owners.get(t)
            if owner and dialect_of(owner) is None:
                unsupported.extend(_reverse_feeds(job, t, owner))
    full_check = {t for tg in targets if tg.role != "cache" for t in tg.tables}
    parity = [
        ParityGate.model_validate(
            {
                "table": t,
                "check": (
                    "row_count_and_key_checksum" if t in full_check else "sampled_key_compare"
                ),
            }
        )
        for t in base_tables
    ]
    gates = Gates(
        wave_gate=str(wave.get("gate") or ""),
        parity=parity,
        max_lag_seconds=MAX_LAG_SECONDS,
        lagging_reads=sorted(lagging, key=lambda r: (r.table, r.engine)),
    )
    return gates, unsupported


def _plan_wave(
    job: _JobView,
    wave: Mapping[str, Any],
    is_first: bool,
    wave_count: int,
    carries_cluster: bool,
) -> DataMovementPlan:
    engines = _wave_engines(wave)
    tool = "dms" if is_first else "debezium"
    source_engine = job.legacy if is_first else _cdc_source_engine(wave, job.legacy)
    dialect = dialect_of(source_engine)
    per_engine, read_tables, unsupported = _wave_tables(job, wave, engines, is_first)
    base_tables = sorted(t for t in read_tables if t in job.schema.tables)
    views = sorted(t for t in read_tables if t not in job.schema.tables)
    source = SourceSpec(
        engine=source_engine,
        dialect=dialect,
        database=job.database,
        tables=[
            SourceTable(
                table=t,
                primary_key=[str(c) for c in job.schema.tables[t].get("primary_key") or []],
                estimated_rows=job.schema.tables[t].get("row_count"),
            )
            for t in base_tables
        ],
        views=views,
    )
    hub: list[HubTopic] = []
    if is_first:
        cdc, initial_load, gaps, notes = _dms_capture(
            job, wave, source_engine, dialect, base_tables
        )
    else:
        cdc, initial_load, hub, gaps, notes = _debezium_capture(
            job, int(wave["wave"]), source_engine, dialect, base_tables, views
        )
    unsupported.extend(gaps)
    targets, gaps = _targets(job, wave, per_engine, source_engine, is_first)
    unsupported.extend(gaps)
    gates, gaps = _gates(job, wave, source_engine, targets, read_tables, base_tables, is_first)
    unsupported.extend(gaps)
    notes.append(
        f"The lag gate is {MAX_LAG_SECONDS} seconds of capture lag, a default to tune against "
        "the lagging reads' tolerance"
    )
    cost, assumptions, gaps = _monthly_cost(job.prices, job, tool, hub, targets, carries_cluster)
    unsupported.extend(gaps)
    unique = {_unsupported_key(u): u for u in unsupported}
    return DataMovementPlan(
        job_id=job.inputs.job_id,
        database_name=job.inputs.database_name,
        assignment_version=job.assignment_version,
        wave=int(wave["wave"]),
        wave_count=wave_count,
        title=str(wave.get("title") or ""),
        source=source,
        cdc=cdc,
        hub=hub,
        initial_load=initial_load,
        targets=targets,
        gates=gates,
        stacks=_stacks(tool, targets),
        monthly_cost_usd=cost,
        cost_assumptions=assumptions,
        unsupported=[unique[k] for k in sorted(unique)],
        notes=notes,
    )


def build_plans(
    inputs: JobInputs, *, prices: PriceTable | None | _Default = _DEFAULT
) -> list[DataMovementPlan]:
    """One plan per wave of the report's ``migration_waves``, in wave order.

    ``prices`` is the dated price table (the committed one by default); ``None`` prices
    nothing, so every wave's cost is null with a reason."""
    if isinstance(prices, _Default):
        from src.shared.catalog.prices import price_table

        prices = price_table()
    waves = sorted(
        (w for w in inputs.report.get("migration_waves") or [] if isinstance(w, Mapping)),
        key=lambda w: int(w["wave"]),
    )
    if not waves:
        return []
    job = _JobView.build(inputs, prices)
    aurora = _aurora_wave(waves)
    first_hub = next((w for w in waves if w is not aurora), None)
    return [_plan_wave(job, w, w is aurora, len(waves), w is first_hub) for w in waves]


def _aurora_wave(waves: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The move to Aurora: the wave with a ``homogeneity`` (the marker
    ``migration_risks`` uses); for a report written before it, a first wave whose engines
    are all relational. None when the job has no such wave (an Aurora source)."""
    marked = [w for w in waves if w.get("homogeneity") is not None]
    if marked:
        return marked[0]
    first = waves[0]
    engines = _wave_engines(first)
    return first if engines and set(engines) <= RELATIONAL_ENGINES else None


def _schema_design_paths(store: Any, db: str, job_id: str, version: int) -> dict[str, str]:
    """engine -> the design for ``version`` (or the unversioned one), under the job."""
    prefix = f"{db}/{job_id}/"
    found: dict[str, tuple[int, str]] = {}
    for key in sorted(store.list_prefix(prefix)):
        rel = key[len(prefix) :] if key.startswith(prefix) else key
        m = re.fullmatch(r"schema-([^/]+)/(?:v(\d+)/)?schema_output\.json", rel)
        if not m:
            continue
        engine, v = m.group(1), int(m.group(2) or 0)
        if v not in (0, version):
            continue
        if engine not in found or v > found[engine][0]:
            found[engine] = (v, rel)
    return {engine: rel for engine, (_, rel) in sorted(found.items())}


def load_inputs(store: Any, db: str, job_id: str) -> JobInputs:
    """Read a finished job's artifacts (every read checks its contract version)."""
    from src.report.deliverables import synthesis_report_key

    report_key = synthesis_report_key(store, db, job_id, 0)
    report = store.read_json(report_key)
    version = int((report.get("assignment_summary") or {}).get("version") or 0)
    m = re.search(r"/synthesis/v(\d+)/report\.json$", report_key)
    if not version and m:
        version = int(m.group(1))
    assignment_key = f"{db}/{job_id}/assignment/v{version}/assignment.json"
    if not version or not store.exists(assignment_key):
        # Without it no plan knows the cache's reads or any engine's reads (#1413 review).
        raise FileNotFoundError(f"no assignment v{version} for {db}/{job_id}")
    assignment = store.read_json(assignment_key)
    collector = store.read_json(f"{db}/{job_id}/collector/output.json")
    designs = {
        engine: (rel, store.read_json(f"{db}/{job_id}/{rel}"))
        for engine, rel in _schema_design_paths(store, db, job_id, version).items()
    }
    return JobInputs(
        job_id=job_id,
        database_name=db,
        report=report,
        assignment=assignment,
        collector=collector,
        schema_designs=designs,
    )


def _write_plan(store: Any, db: str, job_id: str, plan: DataMovementPlan) -> str:
    path = f"{db}/{job_id}/data-movement/wave-{plan.wave}/plan.json"
    store.write_json(path, plan.model_dump(mode="json"))
    return path


def write_plans(store: Any, db: str, job_id: str) -> list[str]:
    """Build and write every wave's plan; the paths written, in wave order."""
    return [
        _write_plan(store, db, job_id, plan) for plan in build_plans(load_inputs(store, db, job_id))
    ]
