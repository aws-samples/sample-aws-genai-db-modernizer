"""A table's row count and size: a number, a real zero, or unknown (#1636).

The collection scripts read every count from the source's own catalogue and statistics
views, never from the table (no scan, no ``COUNT(*)``, #1555), so a count is what the
source last recorded. Each source says "no statistics" its own way, and this module is
the one place that rule lives, for the collection scripts' output, the live collectors
and the collections and collector outputs written before #1636:

========== =============================== ============================================ ========
Source     Count read from                 Unknown when                                 Basis
========== =============================== ============================================ ========
PostgreSQL ``pg_class.reltuples``          -1 (14+); before 14, 0 with no pages and no  estimate
                                           analyse or vacuum on record
                                           (``pg_stat_user_tables``)
Oracle     ``ALL_TABLES.NUM_ROWS``         NULL (no statistics gathered)                estimate
MySQL,     ``information_schema.TABLES.    NULL; and a 0 on a table whose data is more  exact for
MariaDB    TABLE_ROWS``                    than one page (statistics not yet refreshed) MyISAM and
                                                                                        Aria, else
                                                                                        estimate
SQL Server ``sys.partitions.rows``         never (the engine keeps it)                  estimate
Db2        ``SYSCAT.TABLES.CARD``          -1 (no RUNSTATS)                             estimate
========== =============================== ============================================ ========

PostgreSQL: before 14 a table never vacuumed or analysed has reltuples 0 and relpages 0
(14 added -1 for it). The rule also reads a truly empty table as unknown when its vacuum
and analyse times are gone (``pg_stat_reset()``, a crash before 15, ``pg_upgrade``, or a
collection from a standby, which records none): the harmless direction, since a table of
no pages is assumed to hold 0 rows.

MySQL family: the MySQL Reference Manual (The INFORMATION_SCHEMA TABLES Table) documents
``TABLE_ROWS`` as NULL only for ``INFORMATION_SCHEMA`` tables and views, exact for MyISAM
and an estimate for InnoDB, so the older script's ``IFNULL(table_rows, 0)`` never turned
a base table's NULL into 0: an older collection's 0 is the server's own figure, read as a
new one is. An InnoDB 0 is ambiguous only when the table's data is bigger than one 16 KB
page (an empty InnoDB table's clustered index is one page): its statistics have not
caught up, so that 0 is unknown. The scripts round ``data_size_mb`` to 2 decimals, so one
page reads 0.02 (:data:`ONE_PAGE_MB`). With a larger ``innodb_page_size`` an empty table
reads as unknown, the harmless direction. A collector output written before #1636 holds
only the total size (data plus indexes), so there its 0 is kept as the server's figure.

SQL Server: Microsoft documents ``sys.partitions.rows`` as "the approximate number of rows
in this partition", so its basis is ``estimate``; but it is kept by the engine on every
change and never missing, so a 0 is an empty table in any collection.

A table's size follows the same three states (:func:`read_size_mb`): its data size plus
its index size, ``None`` when the source reports no data size (a DDL-only collection, an
Oracle table with no TABLE segment the script found, such as an index-organized table), a
real 0 otherwise. Before #1636 the Oracle script turned "no segment found" into 0, so an
older Oracle collection's 0 size is read as unknown. ``data_size_mb`` is kept on its own
(:func:`read_data_size_mb`), since the sizing assumption divides the data alone.

A collection written before #1636 carries no basis (``row_count_basis``). Its scripts
turned Oracle's NULL into 0 and passed PostgreSQL before 14's 0 for a table never
analysed, so a 0 from Oracle, from PostgreSQL before 14 (or of no known version) or from a
source this module does not know is read as unknown. PostgreSQL 14+'s 0 (its unknown was
already -1), SQL Server's, Db2's and the MySQL family's (by the page rule above) stay 0.
A negative count is unknown from any source. A collector output written before #1636 is
different for PostgreSQL: its collector clamped 14+'s -1 to 0 as well, so its adapter
passes no version and every PostgreSQL 0 in it reads as unknown.

``src/shared`` imports nothing from ``src/agents`` or ``src/report``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

EXACT = "exact"
ESTIMATE = "estimate"
RowCountBasis = Literal["exact", "estimate"]
ROW_COUNT_BASES: tuple[str, ...] = (EXACT, ESTIMATE)

# The key a collection written since #1636 carries on every table (null when the count
# is unknown); its absence marks an older collection.
BASIS_KEY = "row_count_basis"
ANALYZED_AT_KEY = "stats_analyzed_at"

#: The key a table read from DDL alone carries (``Table.from_ddl``): no catalogue was
#: read, so it has no row count and no size.
DDL_KEY = "from_ddl"
#: The rows a row-count threshold reads for a DDL-only table (no count, no size): 0, so
#: every small-table rule treats it as small, as before #1636 (ADR-031 Decision 14's
#: small reference-table read, Decision 5's substring scan threshold, the analyses'
#: small-table checks). Its row count stays unknown (None) in the collector output, the
#: facts and every deliverable, which state this rule. The interim rule the maintainer
#: chose on #1637 (2026-10-28); routing by the destination's need replaces it in #1646.
#: Any other table with neither a count nor a size is not known small.
DDL_TABLE_THRESHOLD_ROWS = 0

#: The stand-in for a size no source reported (the schema-design skills' long-standing
#: rule): 0.01 MB.
UNKNOWN_SIZE_MB = 0.01
#: One 16 KB InnoDB page as the scripts report it (MB, rounded to 2 decimals).
ONE_PAGE_MB = 0.02

# Sources whose 0 is an empty table in any collection: kept by the engine (SQL Server),
# or whose own "no statistics" is a value no empty table has (Db2's CARD -1).
_ZERO_IS_EMPTY_ENGINES = frozenset({"sqlserver", "db2"})
_POSTGRES = frozenset({"postgresql", "postgres"})
_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})
# MySQL-family storage engines whose TABLE_ROWS is the exact count (MySQL Reference
# Manual, The INFORMATION_SCHEMA TABLES Table: "For MyISAM tables, TABLE_ROWS is the
# exact row count").
_EXACT_STORAGE_ENGINES = frozenset({"myisam", "aria"})
_ESTIMATE_ENGINES = frozenset({"oracle", "db2", "sqlserver"}) | _POSTGRES | _MYSQL_FAMILY

# The one assumption every figure that needs a number for an unknown count states
# (schema-design sizing, the analysis cost bands and thresholds, the load test's
# document counts). It states the method only: a stored row can be wider than its
# declared width (a value over the 1 KB cap) and the size counts page overhead, so the
# figure is off in no known direction.
UNKNOWN_ROW_COUNT_ASSUMPTION = (
    "sizing takes each one's rows as its collected data size divided by its declared row "
    "width (each column at its declared size, each string at its full declared length up "
    "to 1 KB), not as 0"
)
NO_SIZE_ASSUMPTION = (
    f"sizing uses a fixed {UNKNOWN_SIZE_MB:g} MB stand-in for each, and no row-count rule "
    "treats one as small or large"
)
DDL_ASSUMPTION = (
    "routing treats each as a small reference table, and sizing uses a fixed "
    f"{UNKNOWN_SIZE_MB:g} MB stand-in for each"
)


def _engine(source_engine: str | None) -> str:
    return str(source_engine or "").strip().lower()


def _storage(storage_engine: object) -> str:
    return str(storage_engine or "").strip().lower()


def engine_major_version(version: object) -> int | None:
    """The major version a version text names (``"PostgreSQL 16.10 on x86_64"``,
    ``"13.4"``), or None."""
    found = re.search(r"(\d+)(?:\.\d+)?", str(version or ""))
    return int(found.group(1)) if found else None


def engine_basis(source_engine: str | None, storage_engine: object = None) -> str | None:
    """The basis a source's count has: ``exact``, ``estimate``, or None for a source this
    module does not know (a DDL-only collection has no count at all)."""
    engine = _engine(source_engine)
    if engine in _MYSQL_FAMILY and _storage(storage_engine) in _EXACT_STORAGE_ENGINES:
        return EXACT
    if engine in _ESTIMATE_ENGINES:
        return ESTIMATE
    return None


def _count(value: object) -> int | None:
    """A reported count as a whole number, or None for no count (null, negative, text)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    number = int(value)
    return number if number >= 0 else None


def _size(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _zero_is_empty(
    table: Mapping[str, Any], source_engine: str | None, engine_major: int | None
) -> bool:
    """Whether ``table``'s reported 0 is an empty table (see the module docstring)."""
    engine = _engine(source_engine)
    if engine in _ZERO_IS_EMPTY_ENGINES:
        return True
    if engine in _MYSQL_FAMILY:
        if _storage(table.get("storage_engine") or table.get("engine")) in _EXACT_STORAGE_ENGINES:
            return True
        data = _size(table.get("data_size_mb"))
        return data is None or data <= ONE_PAGE_MB
    if BASIS_KEY in table:  # the scripts since #1636 report a missing count as null
        return True
    return engine in _POSTGRES and engine_major is not None and engine_major >= 14


def parse_analyzed_at(value: object) -> datetime | None:
    """The statistics refresh time a source reported (ISO 8601 text), or None."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class RowCount:
    """One table's count as this code reads it (see the module docstring)."""

    row_count: int | None
    basis: str | None
    analyzed_at: datetime | None

    def fields(self) -> dict[str, Any]:
        """The collector output ``Table`` fields."""
        return {
            "row_count": self.row_count,
            BASIS_KEY: self.basis,
            ANALYZED_AT_KEY: self.analyzed_at,
        }


def read_row_count(
    table: Mapping[str, Any], source_engine: str | None, *, engine_major: int | None = None
) -> RowCount:
    """``table``'s count, from a collection table, a live collector's row or a stored
    collector output table, read by the per-engine rule above.

    ``table`` carries ``row_count`` and, from a collection written since #1636,
    ``row_count_basis`` (null when the count is unknown) and ``stats_analyzed_at``. The
    storage engine (the MySQL family) is ``engine`` in a collection and
    ``storage_engine`` in a collector output. ``engine_major`` is the source's major
    version, where known (PostgreSQL 14 changed how it says "no statistics").
    """
    analyzed_at = parse_analyzed_at(table.get(ANALYZED_AT_KEY))
    value = _count(table.get("row_count"))
    if value is None or (value == 0 and not _zero_is_empty(table, source_engine, engine_major)):
        return RowCount(None, None, analyzed_at)
    storage = table.get("storage_engine") or table.get("engine")
    # The engine's own basis (this module's table) wins; a source it does not know keeps
    # the basis the collection reported.
    reported = table.get(BASIS_KEY)
    basis = engine_basis(source_engine, storage)
    if basis is None and reported in ROW_COUNT_BASES:
        basis = str(reported)
    return RowCount(value, basis, analyzed_at)


def legacy_size_is_unknown(size_mb: float, source_engine: str | None) -> bool:
    """Whether a size of ``size_mb`` from a collection or collector output written before
    #1636 means "not found": Oracle's script wrote 0 for a table with no segment of type
    TABLE (a partitioned table's segments are partitions), and every Oracle table that
    has one has at least its first extent, so its 0 is unknown."""
    return size_mb == 0 and _engine(source_engine) == "oracle"


def read_data_size_mb(table: Mapping[str, Any], source_engine: str | None) -> float | None:
    """``table``'s data size in MB, or None when none is reported (see the docstring)."""
    data = _size(table.get("data_size_mb"))
    if data is None:
        return None
    if BASIS_KEY not in table and legacy_size_is_unknown(data, source_engine):
        return None
    return data


def read_size_mb(table: Mapping[str, Any], source_engine: str | None) -> float | None:
    """``table``'s size in MB: its data size plus ``index_size_mb`` (none reported adds
    nothing), or None when no data size is reported (see the module docstring)."""
    data = read_data_size_mb(table, source_engine)
    if data is None:
        return None
    return data + (_size(table.get("index_size_mb")) or 0.0)


def assumption_size_mb(table: Mapping[str, Any]) -> float | None:
    """The size the row-count assumption divides for a collector output table: its data
    size (``data_size_mb``), never its indexes; a table written before #1636 records
    only its total ``size_mb``, which is used then. None when neither is collected."""
    if "data_size_mb" in table:
        return _size(table.get("data_size_mb"))
    return _size(table.get("size_mb"))


def known_size_mb(table: Mapping[str, Any]) -> float | None:
    """A collector output table's size (``size_mb``), or None when none was collected."""
    return _size(table.get("size_mb"))


def collected_storage_mb(tables: Iterable[Mapping[str, Any]]) -> tuple[float, int]:
    """The tables' collected size summed (MB), and how many have none (#1636): storage is
    priced on the sizes collected, and every estimate states the tables it leaves out
    (:func:`unknown_size_assumption`), never reading a missing size as 0 unsaid."""
    total, unknown = 0.0, 0
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        size = known_size_mb(table)
        if size is None:
            unknown += 1
        else:
            total += size
    return total, unknown


def unknown_size_line(tables: Iterable[Mapping[str, Any]]) -> list[str]:
    """The pricing assumption line for the tables with no collected size, as a list
    (empty when every size is known), for an estimate's ``pricing_assumptions``."""
    line = unknown_size_assumption(collected_storage_mb(tables)[1])
    return [line] if line else []


def is_unmeasured_ddl_table(table: Mapping[str, Any]) -> bool:
    """Whether a collector output table was read from DDL alone and has neither a row
    count nor a size: the case :data:`DDL_TABLE_THRESHOLD_ROWS` covers."""
    return (
        table.get(DDL_KEY) is True
        and known_row_count(table) is None
        and assumption_size_mb(table) is None
    )


def unknown_size_assumption(unknown: int) -> str | None:
    """The TCO assumption line for the tables with no collected size (#1636), or None."""
    if not unknown:
        return None
    noun = "table has" if unknown == 1 else "tables have"
    them = "it" if unknown == 1 else "them"
    return f"{unknown} source {noun} no collected size, so no storage is priced for {them}"


def known_row_count(table: Mapping[str, Any]) -> int | None:
    """A collector output table's count: an int, or None when it is unknown."""
    value = table.get("row_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def rows_from_size(size_mb: float, row_bytes: int) -> int:
    """The stated assumption's row count: ``size_mb`` over a ``row_bytes`` row."""
    return max(round(float(size_mb) * 1_048_576 / max(int(row_bytes), 1)), 0)


def _how_many(count: int, total: int) -> str:
    if count < total:
        return f"{count} of {total} source {'table has' if total == 1 else 'tables have'}"
    return "The source table has" if total == 1 else f"All {total} source tables have"


def row_count_facts(tables: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """What the deliverables say about the collection's row counts (``RowCountFacts``).

    Counts the tables by basis, the unknown ones and, of those, the ones with no
    collected size either, and the range of statistics refresh times the source
    reported. ``statement`` is the one sentence every deliverable prints where a count is
    unknown (None when every count is known): how many there are and what sizing uses
    for them, the assumption from the data size, or the fixed stand-in when no size was
    collected (a DDL-only collection) and the thresholds then find nothing.
    """
    rows = [t for t in tables if isinstance(t, Mapping) and t.get("table_id")]
    unknown = [t for t in rows if known_row_count(t) is None]
    ddl = sum(1 for t in unknown if is_unmeasured_ddl_table(t))
    no_size = sum(1 for t in unknown if assumption_size_mb(t) is None) - ddl
    sized = len(unknown) - no_size - ddl
    exact = sum(1 for t in rows if known_row_count(t) is not None and t.get(BASIS_KEY) == EXACT)
    estimate = sum(
        1 for t in rows if known_row_count(t) is not None and t.get(BASIS_KEY) == ESTIMATE
    )
    times = sorted(
        found for t in rows if (found := parse_analyzed_at(t.get(ANALYZED_AT_KEY))) is not None
    )
    parts: list[str] = []
    if sized:
        parts.append(
            f"{_how_many(sized, len(rows))} no row count in the source's statistics; "
            f"{UNKNOWN_ROW_COUNT_ASSUMPTION}"
        )
    if ddl:
        parts.append(
            f"{_how_many(ddl, len(rows))} no row count and no size, since a DDL upload "
            f"collects neither; {DDL_ASSUMPTION}"
        )
    if no_size:
        parts.append(
            f"{_how_many(no_size, len(rows))} no row count and no collected size; "
            f"{NO_SIZE_ASSUMPTION}"
        )
    return {
        "table_count": len(rows),
        "unknown_count": len(unknown),
        "no_size_count": no_size + ddl,
        "ddl_table_count": ddl,
        "exact_count": exact,
        "estimate_count": estimate,
        "stats_oldest_at": times[0].isoformat() if times else None,
        "stats_newest_at": times[-1].isoformat() if times else None,
        "statement": ". ".join(parts) if parts else None,
    }
