# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's replication instance, LOB settings and Aurora version line, from the collector (#1798).

Pure: reads the collector dict and the rule catalog (``src/catalog/dms_sizing.toml``,
``aurora_versions.toml``). The plan writes the results into its ``replication`` section, so
the deliverables show why a class was chosen; the DMS template reads them from the plan.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.contracts.data_movement_plan import DmsSizing, LobTable, UnboundedVarchar
from src.shared.catalog.dms_sizing import (
    DmsSizingTable,
    LobInstanceClass,
    LobRules,
    NotMovedRules,
    SizingRow,
    dms_sizing,
)
from src.shared.catalog.gaps import aurora_version_lines, parse_version_line

WRITE_QUERY_TYPES = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})
# A dialect that reads another's LOB types.
_LOB_FAMILY = {"mariadb": "mysql"}
_BASE_TYPE = re.compile(r"[^\s(]+")

# DMS's "Limit LOB size to" is in bytes, but the collector's max_length of a character type
# is a count of characters. DMS reads character LOBs as CLOB or NCLOB; an NCLOB stores two
# bytes per character and a 4-byte character takes two of them again, so a character LOB
# counts 4 bytes per character (the safe bound: limited mode truncates what exceeds it).
# Binary LOBs (BLOB) count their max_length as bytes.
_BYTES_PER_CHARACTER = 4
# MySQL spatial types are BLOBs in DMS, so their max_length (when the collector has one) is bytes.
_BINARY_LOBS = {
    "mysql": frozenset({
        "mediumblob", "longblob", "geometry", "point", "linestring", "polygon", "multipoint",
        "multilinestring", "multipolygon", "geometrycollection",
    }),
    "postgresql": frozenset({"bytea"}),
}  # fmt: skip
# The endpoint setting UnboundedVarcharMaxSize covers varchar only; a bpchar without a length
# is reported apart (``UnboundedVarchar.unsupported``).
_UNBOUNDED_VARCHAR = ("varchar", "character varying")
_SHOWN_COLUMNS = 5


def _source(collector: Mapping[str, Any]) -> Mapping[str, Any]:
    return (collector.get("metadata") or {}).get("source_database") or {}


def database_gb(collector: Mapping[str, Any]) -> float | None:
    """The database size in GB: the collector's, else the sum of its tables' ``size_mb``."""
    size = _source(collector).get("database_size_gb")
    if size is not None:
        return float(size)
    tables = (collector.get("database_schema") or {}).get("tables") or []
    sizes = [t.get("size_mb") for t in tables]
    if tables and all(s is not None for s in sizes):
        return round(sum(float(s) for s in sizes) / 1024, 3)
    return None


def write_rate(collector: Mapping[str, Any]) -> float | None:
    """The summed ``calls_per_second`` of the write statements; None when unknown."""
    patterns = (collector.get("queries") or {}).get("query_patterns") or []
    if not any(q.get("calls_per_second") is not None for q in patterns):
        return None
    writes = [q for q in patterns if str(q.get("query_type") or "").upper() in WRITE_QUERY_TYPES]
    rated = [float(q["calls_per_second"]) for q in writes if q.get("calls_per_second") is not None]
    if writes and not rated:
        return None
    return round(sum(rated), 3)


def _bounds(row: SizingRow) -> str:
    if row.max_database_gb is None:
        return "above every other row's bounds"
    return f"up to {row.max_database_gb:g} GB and {row.max_writes_per_second:g} writes per second"


def _sizing(row: SizingRow, gb: float | None, wps: float | None, reason: str) -> DmsSizing:
    return DmsSizing(row=row.id, instance_class=row.instance_class, allocated_storage_gb=row.allocated_storage_gb,
                     included_storage_gb=row.included_storage_gb,
                     database_gb=gb, writes_per_second=wps, reason=reason, note=row.note)  # fmt: skip


def unrated_writes(collector: Mapping[str, Any]) -> int:
    """How many write statements carry no ``calls_per_second``."""
    patterns = (collector.get("queries") or {}).get("query_patterns") or []
    return sum(
        1
        for q in patterns
        if str(q.get("query_type") or "").upper() in WRITE_QUERY_TYPES
        and q.get("calls_per_second") is None
    )


def size_instance(
    database_gb: float | None,
    writes_per_second: float | None,
    table: DmsSizingTable | None = None,
    unrated_write_statements: int = 0,
) -> DmsSizing:
    """The first row whose bounds hold (inclusive); the smallest row when an input is unknown."""
    rows = (table or dms_sizing()).rows
    missing = [
        w
        for w, v in (("database size", database_gb), ("write rate", writes_per_second))
        if v is None
    ]
    if missing:
        reason = (f"The collection has no {' or '.join(missing)}, so the smallest class is used; set the "
                  f"stack's InstanceClass parameter to size it yourself. {rows[0].why}")  # fmt: skip
        return _sizing(rows[0], database_gb, writes_per_second, reason)
    if database_gb is None or writes_per_second is None:  # handled above; narrows the types
        raise ValueError("database size and write rate are required past the fallback")
    for row in rows:
        if row.max_database_gb is None or (
            database_gb <= row.max_database_gb
            and writes_per_second <= (row.max_writes_per_second or 0)
        ):
            reason = (f"{database_gb:g} GB and {writes_per_second:g} writes per second: {_bounds(row)}. "
                      f"{row.why}")  # fmt: skip
            if unrated_write_statements:
                plural = "" if unrated_write_statements == 1 else "s"
                reason += (f" {unrated_write_statements} write statement{plural} had no rate and add nothing "
                           f"to the write rate.")  # fmt: skip
            return _sizing(row, database_gb, writes_per_second, reason)
    raise AssertionError("the catalog's last row has no bounds")  # DmsSizingTable guarantees it


def size_from_collector(
    collector: Mapping[str, Any], table: DmsSizingTable | None = None
) -> DmsSizing:
    """``size_instance`` on the collector's database size and write rate."""
    return size_instance(
        database_gb(collector), write_rate(collector), table, unrated_writes(collector)
    )


def _base_type(data_type: str, family: str) -> str:
    """The first word of the type, lower-cased, before a space or a parenthesis
    (``longtext character set utf8mb4`` is ``longtext``, ``double precision`` is ``double``);
    a PostgreSQL array (``_int4``, ``int4[]`` or ``ARRAY``) is ``array``."""
    text = data_type.strip().lower()
    if family == "postgresql" and (text.startswith("_") or text.endswith("]")):
        return "array"
    match = _BASE_TYPE.match(text)
    return match.group(0) if match else ""


def _bytes(column: Mapping[str, Any], base: str, family: str) -> int | None:
    """The column's declared maximum in bytes (see ``_BYTES_PER_CHARACTER``), None if unbounded."""
    declared = column.get("max_length")
    if declared is None:
        return None
    return int(declared) * (1 if base in _BINARY_LOBS.get(family, ()) else _BYTES_PER_CHARACTER)


def _unbounded(column: Mapping[str, Any], names: tuple[str, ...]) -> bool:
    text = str(column.get("data_type") or "").strip().lower()
    return column.get("max_length") is None and "(" not in text and text.startswith(names)


def _named(columns: list[str]) -> str:
    shown = ", ".join(columns[:_SHOWN_COLUMNS])
    more = len(columns) - _SHOWN_COLUMNS
    return shown + (f" and {more} more" if more > 0 else "")


def _table_columns(
    collector: Mapping[str, Any], tables: Iterable[str]
) -> list[tuple[str, list[Any]]]:
    """(table id, its columns) for each asked table, sorted; a table the collector lacks is an error."""
    by_id = {t["table_id"]: t for t in (collector.get("database_schema") or {}).get("tables") or []}
    out = []
    for table_id in sorted(set(tables)):
        if table_id not in by_id:
            raise ValueError(f"the collector has no table {table_id!r}")
        out.append((table_id, by_id[table_id].get("columns") or []))
    return out


# How a LobTable reason sized from the collection's measured values starts (#1918).
MEASURED = "Measured:"
UNKNOWN_TYPE_REASON = "type not known to map to a non-LOB DMS type (full LOB mode is the safe side)"
# DMS's largest LobMaxSize, in KB.
DMS_MAX_LOB_KB = 102400
# How to measure a PostgreSQL LOB column's longest value, in bytes (as the collection does).
MEASURE_LOB = "SELECT max(octet_length(<column>::text)) FROM <table>"


def _limited_only(
    table_id: str, names: list[str], unknown: list[str], known_kb: list[int], lob: LobRules
) -> LobTable:
    """A table of a dialect that never runs full LOB mode, whose LOB size is not declared (or is
    above the limit): limited mode at the catalog's size, or its declared size when larger."""
    size = min(max([lob.undeclared_max_kb, *known_kb]), DMS_MAX_LOB_KB)
    why = (f"{', '.join(sorted(unknown))} {'has' if len(unknown) == 1 else 'have'} a type not known to map "
           "to a non-LOB DMS type, read as a LOB" if unknown
           else "A LOB column has no declared maximum size (or one above the limit)")  # fmt: skip
    return LobTable(table=table_id, mode="limited", columns=names, max_size_kb=size, reason=(
        f"{why}; this source never runs full LOB mode (it writes a NULL LOB as an empty value, #1918), "
        f"so the table runs in limited LOB mode at {size} KB. The collection did not measure its values, and "
        f"a larger value is truncated: before the full load, measure {MEASURE_LOB} per column and raise "
        f"LobMaxSize if needed (2 bytes per byte of a character LOB), or collect again."))  # fmt: skip


def _lob_columns(
    columns: Iterable[Mapping[str, Any]], family: str, types: set[str]
) -> list[tuple[str, str, Any]]:
    """(name, base type, measured max_value_bytes) of each LOB-typed column, sorted by name."""
    out = []
    for c in columns:
        base = _base_type(str(c.get("data_type") or ""), family)
        if base in types:
            out.append((str(c["column_name"]), base, c.get("max_value_bytes")))
    return sorted(out)


def measured_lob_kb(value_bytes: int, base: str, family: str, rules: LobRules | None = None) -> int:
    """LobMaxSize in KB for a measured longest value (#1918): bytes DMS counts (a character LOB
    at ``measured_bytes_per_character`` per stored byte), times the headroom, rounded up to
    ``measured_round_kb``; never below ``undeclared_max_kb`` nor above DMS's maximum."""
    lob = rules or dms_sizing().lob
    per_byte = 1 if base in _BINARY_LOBS.get(family, ()) else lob.measured_bytes_per_character
    need = math.ceil(value_bytes * per_byte * lob.measured_headroom / 1024)
    if need <= lob.undeclared_max_kb:
        return lob.undeclared_max_kb
    return min(math.ceil(need / lob.measured_round_kb) * lob.measured_round_kb, DMS_MAX_LOB_KB)


def _measured_table(
    table_id: str, columns: list[Any], family: str, types: set[str], lob: LobRules
) -> LobTable | None:
    """Limited mode at the size the collection measured, when every LOB column was measured."""
    lobs = _lob_columns(columns, family, types)
    if not lobs or any(m is None for _, _, m in lobs):
        return None
    name, base, longest = max(lobs, key=lambda c: measured_lob_kb(int(c[2]), c[1], family, lob))
    size = measured_lob_kb(int(longest), base, family, lob)
    return LobTable(table=table_id, mode="limited", columns=[n for n, _, _ in lobs], max_size_kb=size, reason=(
        f"{MEASURED} the longest LOB value is {int(longest)} bytes ({name}); the table runs in limited "
        f"LOB mode at {size} KB. {lob.measured_why} A larger value is truncated, so measure again "
        "before the full load if the data grew."))  # fmt: skip


def unmeasured_lob_tables(lob_tables: Iterable[LobTable], dialect: str | None) -> list[str]:
    """The LOB tables of a ``limited_only`` dialect whose size the collection did not measure
    (an older collection, a table above ``lob_scan_max_mb``, or a column type it does not
    measure), sorted; none for other dialects."""
    if _LOB_FAMILY.get(dialect or "", dialect or "") not in dms_sizing().lob.limited_only:
        return []
    return sorted(t.table for t in lob_tables if not t.reason.startswith(MEASURED))


def lob_size_note(unmeasured: list[str], lob_max_size_kb: int) -> str | None:
    """The plan's finding when LOB sizes were not measured: the task's size is not checked
    against them, and a larger value is truncated."""
    if not unmeasured:
        return None
    n = len(unmeasured)
    return (f"{n} step 1 {'table has' if n == 1 else 'tables have'} a LOB column the collection did not "
            f"measure ({_named(unmeasured)}), so the task's {lob_max_size_kb} KB LobMaxSize is not checked "
            f"against {'it' if n == 1 else 'them'} and a larger value is truncated: collect again with "
            f"scripts/collect-postgresql.sql (it records each LOB column's longest value) and write the "
            f"plan again, or measure {MEASURE_LOB} per column.")  # fmt: skip


def size_for_lob(
    sizing: DmsSizing,
    lob_max_size_kb: int,
    dialect: str | None,
    rules: LobRules | None = None,
    *,
    table: str | None = None,
    task_kb: int | None = None,
) -> DmsSizing:
    """The sizing with the replication instance the largest LOB limit needs (#1918): the larger of
    the sizing row's class and the catalog's first class that holds it. ``lob_max_size_kb`` is
    the task's LobMaxSize, or, with ``table``, that table's own limit (its ``table-settings``
    rule; the task runs at ``task_kb``). Only for a ``limited_only`` dialect; others are returned
    as they are."""
    lob = rules or dms_sizing().lob
    if _LOB_FAMILY.get(dialect or "", dialect or "") not in lob.limited_only:
        return sizing
    classes = list(lob.instance_classes)

    def limit(c: LobInstanceClass) -> int:
        """The largest limit of this kind the class holds: a table's own, or the task's."""
        return c.max_table_lob_kb if table else c.max_lob_kb

    def holds(c: LobInstanceClass) -> bool:
        return limit(c) >= lob_max_size_kb and c.max_lob_kb >= (task_kb or 0)

    need = next((c for c in classes if holds(c)), classes[-1])
    order = [c.instance_class for c in classes]
    current = sizing.instance_class
    what = (f"The {lob_max_size_kb} KB LOB limit of {table} (its own table-settings rule; the task's "
            f"LobMaxSize is {task_kb} KB)" if table else f"The task's LobMaxSize of {lob_max_size_kb} KB")  # fmt: skip
    kind = "a table's own limit" if table else "a task-wide LobMaxSize"
    if current in order and order.index(current) >= order.index(need.instance_class):
        return sizing.model_copy(update={"reason": (
            f"{sizing.reason} {what} fits {current} "
            f"({kind} up to {limit(classes[order.index(current)])} KB).")})  # fmt: skip
    reason = (f"{sizing.reason} {what} needs {need.instance_class} "
              f"({kind} up to {limit(need)} KB): {need.why} So the class is raised from {current}.")  # fmt: skip
    note = sizing.note
    if not holds(need):
        reason = (f"{sizing.reason} {what} is above every class in "
                  f"the catalog; the largest, {need.instance_class}, holds {kind} of {limit(need)} KB.")  # fmt: skip
        note = (f"A LOB limit of {lob_max_size_kb} KB is above what the catalog's classes hold: size the "
                "replication instance with the customer's team, or move the table with the largest "
                "values apart, before the full load.")  # fmt: skip
    return sizing.model_copy(update={
        "instance_class": need.instance_class, "included_storage_gb": need.included_storage_gb,
        "allocated_storage_gb": max(sizing.allocated_storage_gb, need.included_storage_gb),
        "reason": reason, "note": note})  # fmt: skip


def not_moved_columns(
    columns: Iterable[Mapping[str, Any]], dialect: str | None, rules: NotMovedRules | None = None
) -> list[str]:
    """The columns of a type DMS does not migrate (``[not_moved]``, #1918), sorted: their table
    is created by step 1's DDL but not moved by its task."""
    names = set((rules or dms_sizing().not_moved).types(dialect))
    if not names:
        return []
    family = _LOB_FAMILY.get(dialect or "", dialect or "")
    out = []
    for c in columns:
        text = str(c.get("data_type") or "").strip().lower()
        base = text.lstrip("_").removesuffix("[]") if family == "postgresql" else text
        match = _BASE_TYPE.match(base)
        if match and match.group(0) in names:
            out.append(str(c["column_name"]))
    return sorted(out)


def lob_plan(
    collector: Mapping[str, Any],
    tables: Iterable[str],
    dialect: str | None,
    rules: LobRules | None = None,
) -> tuple[int, list[LobTable]]:
    """(the task's LOB size in KB, the listed tables' LOB mode); tables without LOBs are not listed.

    A dialect with a scalar list (PostgreSQL) treats a type in neither list as a LOB of unknown
    size. A ``limited_only`` dialect (PostgreSQL) never runs a table in full LOB mode: an unknown
    or larger size runs limited at ``undeclared_max_kb`` (#1918). Raises ValueError for a table
    the collector does not have."""
    lob = rules or dms_sizing().lob
    family = _LOB_FAMILY.get(dialect or "", dialect or "")
    types = set(lob.types.get(family, ()))
    scalars = set(lob.scalars.get(family, ()))
    out: list[LobTable] = []
    for table_id, columns in _table_columns(collector, tables):
        found, unknown = [], []
        for c in columns:
            base = _base_type(str(c.get("data_type") or ""), family)
            if base in types:
                found.append((str(c["column_name"]), _bytes(c, base, family)))
            elif scalars and base not in scalars:
                unknown.append(str(c["column_name"]))
                found.append((str(c["column_name"]), None))
        if not found:
            continue
        if family in lob.limited_only and not unknown:
            measured = _measured_table(table_id, columns, family, types, lob)
            if measured is not None:
                out.append(measured)
                continue
        found.sort()
        names = [n for n, _ in found]
        sizes = [b for _, b in found]
        known_kb = [math.ceil(int(b) / 1024) for b in sizes if b is not None]
        if family in lob.limited_only and (
            unknown or len(known_kb) < len(sizes) or max(known_kb) > lob.limited_max_kb
        ):
            out.append(_limited_only(table_id, names, unknown, known_kb, lob))
        elif unknown:
            out.append(LobTable(table=table_id, mode="full", columns=names, reason=(
                f"{', '.join(sorted(unknown))}: {UNKNOWN_TYPE_REASON}.")))  # fmt: skip
        elif any(b is None for b in sizes):
            out.append(LobTable(table=table_id, mode="full", columns=names, reason=(
                "A LOB column has no declared maximum size, so full LOB mode moves every value whole.")))  # fmt: skip
        else:
            largest_kb = max(math.ceil(int(b or 0) / 1024) for b in sizes)
            if largest_kb <= lob.limited_max_kb:
                out.append(LobTable(table=table_id, mode="limited", columns=names, max_size_kb=max(largest_kb, 1), reason=(
                    f"Every LOB column declares at most {largest_kb} KB (character columns counted at "
                    f"{_BYTES_PER_CHARACTER} bytes per character, as DMS counts bytes), within the "
                    f"{lob.limited_max_kb} KB limit.")))  # fmt: skip
            else:
                out.append(LobTable(table=table_id, mode="full", columns=names, reason=(
                    f"A LOB column declares up to {largest_kb} KB (character columns counted at "
                    f"{_BYTES_PER_CHARACTER} bytes per character), above the {lob.limited_max_kb} KB "
                    f"limit, so full LOB mode moves every value whole.")))  # fmt: skip
    by_id = {t["table_id"]: t for t in (collector.get("database_schema") or {}).get("tables") or []}
    unkeyed = {t.table for t in out if not by_id[t.table].get("primary_key")}
    return task_lob_kb(out, unkeyed, dialect, lob), out


def task_lob_kb(
    lob_tables: Iterable[LobTable],
    unkeyed: Iterable[str] = (),
    dialect: str | None = "postgresql",
    rules: LobRules | None = None,
) -> int:
    """The task's LobMaxSize in KB from its limited LOB tables' sizes.

    A ``limited_only`` dialect (PostgreSQL, #1918) runs the task at the largest table size up to
    ``undeclared_max_kb``, or, when every LOB table needs more, the smallest: limited LOB mode allocates
    LobMaxSize per LOB column of every buffered row, so a task-wide size taken from one table
    costs memory for all of them. A table above it gets its own limit (:func:`table_lob_limits`),
    except one without a primary key (DMS takes LOB settings only on a keyed table), whose size
    stays task-wide. Other dialects take the largest size. No LOB table: ``default_max_kb``."""
    lob = rules or dms_sizing().lob
    tables = [t for t in lob_tables if t.mode == "limited" and t.max_size_kb]
    sizes = [int(t.max_size_kb or 0) for t in tables]
    if not sizes:
        return lob.default_max_kb
    if _LOB_FAMILY.get(dialect or "", dialect or "") not in lob.limited_only:
        return max(sizes)
    within = [k for k in sizes if k <= lob.undeclared_max_kb]
    base = max(within) if within else min(sizes)
    keyless = set(unkeyed)
    return max([base, *(int(t.max_size_kb or 0) for t in tables if t.table in keyless)])


def table_lob_limits(lob_tables: Iterable[LobTable], task_kb: int) -> list[LobTable]:
    """The limited LOB tables whose size is above the task's LobMaxSize, sorted by table: each
    gets a ``table-settings`` rule with ``lob-settings`` (mode ``limited``, ``bulk-max-size`` its
    size in KB), which overrides the task's LOB settings for that table only (#1918)."""
    return sorted((t for t in lob_tables if t.mode == "limited" and (t.max_size_kb or 0) > task_kb),
                  key=lambda t: t.table)  # fmt: skip


def task_lob_mode(
    dialect: str | None,
    lob_tables: Iterable[LobTable],
    lob_max_size_kb: int,
    rules: LobRules | None = None,
) -> tuple[bool, int]:
    """(FullLobMode, LobMaxSize in KB) of step 1's task from its moved tables' LOB modes: full
    LOB mode when one of them is full, unless the dialect never runs it (``limited_only``,
    #1918); then a full table, as a plan written before #1918 has, runs limited at
    ``undeclared_max_kb``."""
    lob = rules or dms_sizing().lob
    full = any(t.mode == "full" for t in lob_tables)
    if _LOB_FAMILY.get(dialect or "", dialect or "") not in lob.limited_only:
        return full, lob_max_size_kb
    return False, max(lob_max_size_kb, lob.undeclared_max_kb) if full else lob_max_size_kb


def fails_on_lob_truncation(dialect: str | None, rules: LobRules | None = None) -> bool:
    """Whether the source endpoint stops the task on a LOB above LobMaxSize (``limited_only``)."""
    lob = rules or dms_sizing().lob
    return _LOB_FAMILY.get(dialect or "", dialect or "") in lob.limited_only


def unbounded_varchar(
    collector: Mapping[str, Any],
    tables: Iterable[str],
    dialect: str | None,
    rules: LobRules | None = None,
) -> UnboundedVarchar:
    """The source endpoint's UnboundedVarcharMaxSize: PostgreSQL reads a varchar without a length
    as at most 8000 bytes by default, so any such column raises it to the setting's maximum."""
    lob = rules or dms_sizing().lob
    varchars: list[str] = []
    bpchars: list[str] = []
    if dialect == "postgresql":
        for table_id, cols in _table_columns(collector, tables):
            for c in cols:
                name = f"{table_id}.{c['column_name']}"
                if _unbounded(c, _UNBOUNDED_VARCHAR):
                    varchars.append(name)
                elif _unbounded(c, ("bpchar",)):
                    bpchars.append(name)
    if not varchars and not bpchars:
        return UnboundedVarchar()
    limit = lob.unbounded_varchar_max_bytes
    parts = []
    if varchars:
        n = len(varchars)
        parts.append(
            f"{n} step 1 {'column is a varchar' if n == 1 else 'columns are varchars'} without a length "
            f"({_named(varchars)}), which the DMS PostgreSQL source reads as at most 8000 bytes by "
            f"default; the UnboundedVarcharMaxSize endpoint setting is raised to {limit} bytes, so values "
            f"up to {limit} bytes ({limit // 1048576} MB) move whole and larger values are truncated."
        )
    if bpchars:
        parts.append(
            f"{len(bpchars)} step 1 bpchar without a length ({_named(bpchars)}) is not covered by "
            f"UnboundedVarcharMaxSize, which applies to varchar only, so DMS may truncate its values: "
            f"review {'it' if len(bpchars) == 1 else 'them'} before the full load."
        )
    return UnboundedVarchar(
        max_bytes=limit if varchars else None, columns=len(varchars), column_names=varchars,
        unsupported=bpchars, reason=" ".join(parts),
    )  # fmt: skip


def target_engine_line(collector: Mapping[str, Any], target_engine: str) -> str | None:
    """The oldest Aurora line of ``target_engine`` at or above the source's; None if it has none."""
    entry = aurora_version_lines().get(target_engine)
    source = _source(collector)
    engine = str(source.get("engine") or "").lower()
    if entry is None or engine not in entry.source_engines:
        return None
    line = parse_version_line(engine, str(source.get("version") or ""))
    if line is None:
        return None
    for candidate in entry.lines:
        parsed = parse_version_line(engine, candidate)
        if parsed is not None and parsed >= line:
            return candidate
    return entry.lines[-1]
