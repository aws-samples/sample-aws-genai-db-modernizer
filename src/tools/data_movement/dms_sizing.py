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
from src.shared.catalog.dms_sizing import DmsSizingTable, LobRules, SizingRow, dms_sizing
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


UNKNOWN_TYPE_REASON = "type not known to map to a non-LOB DMS type (full LOB mode is the safe side)"


def lob_plan(
    collector: Mapping[str, Any],
    tables: Iterable[str],
    dialect: str | None,
    rules: LobRules | None = None,
) -> tuple[int, list[LobTable]]:
    """(the task's LOB size in KB, the listed tables' LOB mode); tables without LOBs are not listed.

    A dialect with a scalar list (PostgreSQL) treats a type in neither list as a LOB of unknown
    size. Raises ValueError for a table the collector does not have."""
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
        found.sort()
        names = [n for n, _ in found]
        sizes = [b for _, b in found]
        if unknown:
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
    limited = [t.max_size_kb for t in out if t.mode == "limited" and t.max_size_kb]
    return (max(limited) if limited else lob.default_max_kb), out


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
