"""Generate Aurora PostgreSQL and MySQL DDL from the normalized collector schema.

Pure, deterministic, and LLM-free. Every column the type map cannot resolve
confidently becomes a residual marker (surfaced to the LLM), never a silent
guess. Foreign keys and secondary indexes are emitted after all CREATE TABLE
statements so table ordering never breaks a reference.

Tables are emitted into a single flat namespace; `AgentTable.schema_name` is
intentionally not used, so source tables that share a name across schemas
would collide (out of scope for Phase 1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from src.contracts.aurora_design_delta import INDEX_METHODS, validate_aurora_type
from src.contracts.schema_design_input import AgentColumn, AgentIndex, AgentTable
from src.tools.schema.aurora_common.constraint_translator import (
    DefaultTranslation,
    fk_on_delete_clause,
    identity_clause,
    mysql_auto_increment_clause,
    not_null_clause,
    translate_default,
)
from src.tools.schema.aurora_common.source_family import classify_source_family
from src.tools.schema.aurora_common.source_types import resolve_source_type
from src.tools.schema.aurora_common.sql_safety import SqlFragmentError, render_source_predicate
from src.tools.schema.aurora_common.type_map import (
    TypeResolution,
    resolve_mysql_type,
    resolve_pg_type,
)


@dataclass(frozen=True)
class Dialect:
    """Per-engine DDL parametrization: identifier quoting, auto-increment clause, type resolver."""

    name: str
    quote_char: str
    auto_increment: Callable[[bool | None], str]
    resolve_type: Callable[..., TypeResolution]
    backslash_escapes: bool = False  # MySQL string literals treat \ as an escape

    def q(self, identifier: str) -> str:
        c = self.quote_char
        return c + identifier.replace(c, c + c) + c


POSTGRES = Dialect(
    name="aurora_postgresql",
    quote_char='"',
    auto_increment=identity_clause,
    resolve_type=resolve_pg_type,
)
MYSQL = Dialect(
    name="aurora_mysql",
    quote_char="`",
    auto_increment=mysql_auto_increment_clause,
    resolve_type=resolve_mysql_type,
    backslash_escapes=True,
)


@dataclass(frozen=True)
class TypeOverride:
    """A column type decided outside the type map (the model's delta, #273).

    Validated again here (the delta contract already did) so no caller can put
    an unchecked string into a column definition.
    """

    aurora_type: str
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "aurora_type", validate_aurora_type(self.aurora_type))


TypeOverrides = dict[tuple[str, str], TypeOverride]


@dataclass
class ColumnDDL:
    name: str
    aurora_type: str
    source_type: str | None
    script_derived: bool
    needs_judgment: bool
    judgment_reason: str
    fragment: str  # e.g. '"email" VARCHAR(255) NOT NULL'
    # What the draft made of the source default (#611): the DEFAULT clause it
    # emits (" DEFAULT ..." or ""), and whether the column is an identity
    # (AUTO_INCREMENT) column, from the source or from a sequence default.
    default_clause: str = ""
    identity: bool = False
    default_dropped: bool = False  # the draft dropped it; the reason is a column note


@dataclass
class TableDDL:
    table_name: str
    columns: list[ColumnDDL]
    create_sql: str
    index_sql: list[str] = field(default_factory=list)
    fk_sql: list[str] = field(default_factory=list)


@dataclass
class DdlResult:
    tables: list[TableDDL]
    full_ddl: str
    residuals: list[dict]
    # Source indexes the draft could not carry over as written: a partial-index
    # predicate Aurora cannot take, or an access method it cannot create or the
    # collector cannot describe (#293): {"table", "index", "reason"}.
    index_notes: list[dict] = field(default_factory=list)
    # Columns that carry over but need a look: {"table", "column", "kind", "reason"};
    # kind "default" (a source default the draft dropped, or turned into an
    # identity column, #156) or "time_zone" (a time-zone-aware type mapped to
    # one without, app-layer).
    column_notes: list[dict] = field(default_factory=list)


def excerpt(text: str, limit: int = 80) -> str:
    """Collector text echoed into notes: repr'd (no raw newlines/quotes) and truncated."""
    return repr(text[:limit]) + ("..." if len(text) > limit else "")


def _resolve(
    col: AgentColumn, dialect: Dialect, source_family: str, indexed: bool
) -> TypeResolution:
    has_default = col.default_value is not None and not col.is_auto_increment
    """The source ``data_type`` first (PostgreSQL/MySQL sources, #274), else the normalized type."""
    resolution = resolve_source_type(
        col.data_type,
        source_family=source_family,
        target=dialect.name,
        max_length=col.max_length,
        indexed=indexed,
        has_default=has_default,
        numeric_precision=col.numeric_precision,
        numeric_scale=col.numeric_scale,
    )
    if resolution is not None:
        return resolution
    return dialect.resolve_type(
        col.normalized_data_type,
        max_length=col.max_length,
        numeric_precision=col.numeric_precision,
        numeric_scale=col.numeric_scale,
    )


def _column_ddl(
    table_name: str,
    col: AgentColumn,
    residuals: list[dict],
    dialect: Dialect,
    override: TypeOverride | None = None,
    *,
    source_family: str = "other",
    indexed: bool = False,
    notes: list[dict] | None = None,
    source_engine: str = "",
    identity_ok: bool = False,
) -> ColumnDDL:
    source_type = col.normalized_data_type.value if col.normalized_data_type else None
    notes = notes if notes is not None else []
    if override is not None:
        # A model-decided type (#273): judged, so not script-derived and not a residual.
        resolution = TypeResolution(
            aurora_type=override.aurora_type, needs_judgment=True, reason=override.reason
        )
    else:
        resolution = _resolve(col, dialect, source_family, indexed)
    default = _default(
        table_name, col, resolution.aurora_type, dialect, notes, source_engine, identity_ok
    )
    if override is not None:
        return _column_from(col, resolution, source_type, dialect, default, script_derived=False)
    if (
        dialect.backslash_escapes
        and default.kind == "string"
        and isinstance(col.default_value, str)
        and "\\" in col.default_value
        and not resolution.needs_judgment
    ):
        # The doubled backslash is exact only without NO_BACKSLASH_ESCAPES; a
        # session SET in the DDL would change sql_mode for whatever runs next,
        # so the column is left for review instead (rare: none on the samples).
        resolution = TypeResolution(
            resolution.aurora_type,
            needs_judgment=True,
            reason=(
                f"DEFAULT {excerpt(col.default_value)} contains a backslash, written as \\\\ "
                "for MySQL's default sql_mode; confirm the server does not set "
                "NO_BACKSLASH_ESCAPES (where it would be two characters) or set the default "
                "by hand."
            ),
        )
    if resolution.note:
        notes.append(
            {
                "table": table_name,
                "column": col.column_name,
                "kind": "time_zone",
                "reason": resolution.note,
            }
        )
    if resolution.needs_judgment:
        residuals.append(
            {
                "table": table_name,
                "column": col.column_name,
                "source_type": source_type or col.data_type,
                "fallback_type": resolution.aurora_type,
                "reason": resolution.reason,
            }
        )
    return _column_from(
        col, resolution, source_type, dialect, default, script_derived=not resolution.needs_judgment
    )


def _default(
    table_name: str,
    col: AgentColumn,
    aurora_type: str,
    dialect: Dialect,
    notes: list[dict],
    source_engine: str,
    identity_ok: bool,
) -> DefaultTranslation:
    """The column's DEFAULT for the target, noting any default dropped or rewritten (#156)."""
    if col.is_auto_increment:
        # An identity/auto-increment column carries no DEFAULT clause.
        return DefaultTranslation()
    translated = translate_default(
        col.default_value,
        source_engine=source_engine,
        target=dialect.name,
        aurora_type=aurora_type,
        nullable=col.nullable,
        identity_ok=identity_ok,
    )
    if translated.reason:
        notes.append(
            {
                "table": table_name,
                "column": col.column_name,
                "kind": "default",
                # Collector text is echoed only through excerpt() (repr'd, truncated).
                "reason": f"DEFAULT {excerpt(str(col.default_value))} {translated.reason}",
            }
        )
    return translated


def _column_from(
    col: AgentColumn,
    resolution: TypeResolution,
    source_type: str | None,
    dialect: Dialect,
    default: DefaultTranslation,
    *,
    script_derived: bool,
) -> ColumnDDL:
    auto_increment = dialect.auto_increment(
        col.is_auto_increment or default.identity
    )  # nosemgrep: is-function-without-parentheses -- property, not a method
    # Identity/auto-increment and default are mutually exclusive — a column
    # cannot be both an identity/auto-increment column and carry a DEFAULT clause.
    fragment = (
        f"{dialect.q(col.column_name)} {resolution.aurora_type}"
        f"{auto_increment}"
        f"{not_null_clause(col.nullable)}"
        f"{'' if auto_increment else default.clause}"
    )
    return ColumnDDL(
        name=col.column_name,
        aurora_type=resolution.aurora_type,
        source_type=source_type,
        script_derived=script_derived,
        needs_judgment=resolution.needs_judgment,
        judgment_reason=resolution.reason,
        fragment=fragment,
        default_clause="" if auto_increment else default.clause,
        identity=bool(auto_increment),
        # Dropped means a reason and no clause. A translation that rewrites a default
        # to nothing always says why, so it reads as dropped too, by design.
        default_dropped=bool(default.reason) and not default.clause and not auto_increment,
    )


def _create_table_sql(table: AgentTable, columns: list[ColumnDDL], dialect: Dialect) -> str:
    lines = [f"  {c.fragment}" for c in columns]
    if table.primary_key:
        pk_cols = ", ".join(dialect.q(c) for c in table.primary_key)
        lines.append(f"  PRIMARY KEY ({pk_cols})")
    body = ",\n".join(lines)
    return f"CREATE TABLE {dialect.q(table.table_name)} (\n{body}\n);"


def is_primary_key_index(idx: AgentIndex, table: AgentTable) -> bool:
    """Whether ``idx`` is the index behind the table's PRIMARY KEY.

    Collectors do not always flag it (``is_primary``): PostgreSQL names it
    ``<table>_pkey`` and the offline parser only recognises MySQL's
    ``PRIMARY``. A unique, non-partial index on exactly the key columns (or a
    ``*_pkey`` one on the same column set) duplicates the PRIMARY KEY.

    Limitation: collectors report neither operator classes nor collations,
    so a unique index on the key columns with a different opclass or
    collation (e.g. ``text_pattern_ops``) is treated as a duplicate too.
    """
    if idx.is_primary:  # nosemgrep: is-function-without-parentheses -- property, not a method
        return True
    pk = [c.lower() for c in table.primary_key or []]
    cols = [c.lower() for c in idx.columns]
    if not pk or not idx.is_unique or idx.predicate:  # nosemgrep: is-function-without-parentheses
        return False
    return cols == pk or (idx.index_name.lower().endswith("_pkey") and set(cols) == set(pk))


def secondary_indexes(table: AgentTable) -> list[AgentIndex]:
    """The draft's secondary indexes, in ``TableDDL.index_sql`` order."""
    return [i for i in table.indexes or [] if not is_primary_key_index(i, table)]


_TEXT_TYPES = ("VARCHAR", "CHAR", "CHARACTER", "TEXT", "CITEXT")


def _text_columns(columns: list[ColumnDDL]) -> set[str]:
    """Columns whose draft type is a (non-array) character type."""
    return {
        c.name
        for c in columns
        if "[" not in c.aurora_type
        and c.aurora_type.split("(")[0].split()[0].upper() in _TEXT_TYPES
    }


# #293: the PostgreSQL access methods a source index can keep. ``btree`` needs no USING;
# ``fulltext`` and ``spatial`` (MySQL and SQL Server sources) and ``other`` have no
# PostgreSQL spelling, so they stay a plain B-tree index as before.
_PG_INDEX_METHODS = frozenset(INDEX_METHODS) - {"btree"}
# What PostgreSQL builds, which a source index from another engine can break: only a
# B-tree is UNIQUE, a hash or SP-GiST index takes one column, and a GIN or GiST index needs
# a default operator class for every column. These draft types have one (GIN also takes
# any array; GiST takes none); HSTORE, GEOMETRY and GEOGRAPHY bring theirs with the
# extension the column already needs. For any other column the source index used an
# operator class the collector does not record. Review of #1506.
_PG_ONE_COLUMN_METHODS = frozenset({"hash", "spgist"})
_PG_DEFAULT_OPCLASS_TYPES = {
    "gin": frozenset({"TSVECTOR", "JSONB", "HSTORE"}),
    "gist": frozenset(
        {
            "TSVECTOR",
            "TSQUERY",
            "HSTORE",
            "POINT",
            "BOX",
            "POLYGON",
            "CIRCLE",
            "INT4RANGE",
            "INT8RANGE",
            "NUMRANGE",
            "TSRANGE",
            "TSTZRANGE",
            "DATERANGE",
            "GEOMETRY",
            "GEOGRAPHY",
        }
    ),
}
# The methods Aurora MySQL cannot create and no B-tree stands in for: a B-tree on the
# same columns would not serve the full-text, containment or geometric queries the index
# exists for, and can fail to build on long values. The draft notes such an index instead
# of rendering one. A BRIN index is rendered as a B-tree, which serves the same range
# scans, with a note.
_MYSQL_DROPPED_METHODS = frozenset({"gin", "gist", "spgist"})


def index_method(index: AgentIndex) -> str | None:
    """The source index's access method, or None for an input written before #293."""
    return index.index_type.value if index.index_type else None


def _has_default_opclass(method: str, aurora_type: str) -> bool:
    if aurora_type.endswith("[]"):
        return method == "gin"
    base = aurora_type.split("(")[0].strip().upper()
    return base in _PG_DEFAULT_OPCLASS_TYPES[method]


def pg_method_conflict(
    method: str,
    unique: bool,
    index_columns: list[str],
    column_ddls: list[ColumnDDL],
    *,
    no_opclass_because: str,
) -> str:
    """Why Aurora PostgreSQL cannot build a ``method`` index on ``index_columns`` (UNIQUE when
    ``unique``) with these column types, or "" when it can. The draft checks a source index
    with it and the merge checks the model's delta, so both follow one rule;
    ``no_opclass_because`` says why a GIN or GiST index names no other operator class."""
    if method not in _PG_INDEX_METHODS:
        return ""  # a B-tree takes any columns and can be UNIQUE
    why = ""
    if unique:
        why = "only a B-tree index can be UNIQUE in PostgreSQL"
    elif method in _PG_ONE_COLUMN_METHODS and len(index_columns) > 1:
        why = f"a PostgreSQL {method} index takes one column, and this one has {len(index_columns)}"
    elif method in _PG_DEFAULT_OPCLASS_TYPES:
        types = {c.name.lower(): c.aurora_type for c in column_ddls}
        lacking = [
            f"{name} ({types.get(name.lower(), 'unknown type')})"
            for name in index_columns
            if not _has_default_opclass(method, types.get(name.lower(), ""))
        ]
        if lacking:
            why = (
                f"no default {method} operator class covers {', '.join(lacking)}, and "
                f"{no_opclass_because} (such as {method}_trgm_ops from pg_trgm on text, or "
                f"btree_{method} on a scalar)"
            )
    return why


def _pg_index_method(index: AgentIndex, columns: list[ColumnDDL]) -> tuple[str | None, str]:
    """The method an Aurora PostgreSQL draft keeps for ``index`` (None: a plain B-tree) and,
    when it cannot keep the source's, the index note that says why ("" otherwise)."""
    method = index_method(index)
    if method is None or method not in _PG_INDEX_METHODS:
        return None, ""
    why = pg_method_conflict(
        method,
        index.is_unique,
        index.columns,
        columns,
        no_opclass_because="the collector does not record the one the source index used",
    )
    if why:
        return None, f"{method.upper()} index carried over as a B-tree: {why}."
    return method, ""


def rendered_method(index: AgentIndex, dialect: Dialect, columns: list[ColumnDDL]) -> str | None:
    """The USING method the draft renders for ``index`` on ``dialect``, or None for a plain
    B-tree; Aurora MySQL takes no method."""
    return _pg_index_method(index, columns)[0] if dialect is POSTGRES else None


def carried_indexes(table: AgentTable, dialect: Dialect) -> list[AgentIndex]:
    """The secondary indexes the draft renders on ``dialect``, in ``TableDDL.index_sql``
    order: all of them on Aurora PostgreSQL; on Aurora MySQL, all but the GIN, GiST and
    SP-GiST indexes it cannot create (``_index_sql`` notes those instead)."""
    return [
        i
        for i in secondary_indexes(table)
        if not (dialect is MYSQL and index_method(i) in _MYSQL_DROPPED_METHODS)
    ]


def _index_sql(
    table: AgentTable, dialect: Dialect, notes: list[dict], column_ddls: list[ColumnDDL]
) -> list[str]:
    statements: list[str] = []
    columns = [c.column_name for c in table.columns]
    text_columns = _text_columns(column_ddls)
    if dialect is MYSQL:
        for idx in secondary_indexes(table):
            method = index_method(idx)
            if method in _MYSQL_DROPPED_METHODS:
                notes.append(
                    {
                        "table": table.table_name,
                        "index": idx.index_name,
                        "reason": (
                            f"{method.upper()} index not carried over: Aurora MySQL has no "
                            f"{method} access method, and a B-tree on the same columns would "
                            "not serve the queries it exists for. Use a FULLTEXT or SPATIAL "
                            "index where one fits the columns, or serve the search from the "
                            "application layer."
                        ),
                    }
                )
    for idx in carried_indexes(table, dialect):
        method = index_method(idx)
        using, why = _pg_index_method(idx, column_ddls) if dialect is POSTGRES else (None, "")
        if why:
            notes.append({"table": table.table_name, "index": idx.index_name, "reason": why})
        if dialect is MYSQL and method == "brin":
            notes.append(
                {
                    "table": table.table_name,
                    "index": idx.index_name,
                    "reason": (
                        "BRIN index carried over as a B-tree: Aurora MySQL has no brin access "
                        "method. A B-tree serves the same range scans but is larger."
                    ),
                }
            )
        unique = bool(idx.is_unique)  # nosemgrep: is-function-without-parentheses
        where_sql = None
        if idx.predicate:
            reason = ""
            if dialect is MYSQL:
                reason = "Aurora MySQL has no partial indexes"
            else:
                try:
                    where_sql = render_source_predicate(
                        idx.predicate, columns, dialect.q, text_columns
                    )
                except SqlFragmentError as exc:
                    reason = f"the predicate is outside the supported grammar ({exc})"
            if where_sql is None:
                notes.append(
                    {
                        "table": table.table_name,
                        "index": idx.index_name,
                        "reason": (
                            f"Partial index (WHERE {excerpt(idx.predicate)}) carried over as a "
                            "full "
                            f"{'non-unique ' if unique else ''}index: {reason}."
                            + (
                                " Uniqueness over the subset must be enforced another way."
                                if unique
                                else ""
                            )
                        ),
                    }
                )
                unique = False  # a full UNIQUE index would reject rows the source accepts
        statements.append(
            render_index(
                dialect,
                table.table_name,
                idx.index_name,
                idx.columns,
                unique=unique,
                method=using,
                where_sql=where_sql,
            )
        )
    return statements


def render_index(
    dialect: Dialect,
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
    method: str | None = None,
    include: list[str] | None = None,
    where_sql: str | None = None,
) -> str:
    """One CREATE INDEX statement with every identifier quoted by the dialect.

    ``method``, ``include`` and ``where_sql`` are PostgreSQL-only; the caller
    validates them (``where_sql`` comes from ``sql_safety.render_predicate``).
    ``method`` is checked here too, so no text outside ``INDEX_METHODS`` reaches
    ``USING`` whatever the caller.
    """
    if method and method not in INDEX_METHODS:
        raise SqlFragmentError(f"unknown index method '{method}'")
    cols = ", ".join(dialect.q(c) for c in columns)
    sql = (
        f"CREATE {'UNIQUE ' if unique else ''}INDEX {dialect.q(index_name)} "
        f"ON {dialect.q(table_name)}"
    )
    if method:
        sql += f" USING {method}"
    sql += f" ({cols})"
    if include:
        sql += " INCLUDE (" + ", ".join(dialect.q(c) for c in include) + ")"
    if where_sql:
        sql += f" WHERE {where_sql}"
    return sql + ";"


def _fk_sql(table: AgentTable, dialect: Dialect) -> list[str]:
    statements: list[str] = []
    for fk in table.foreign_keys or []:
        local = ", ".join(dialect.q(c) for c in fk.columns)
        ref = ", ".join(dialect.q(c) for c in fk.referenced_columns)
        statements.append(
            f"ALTER TABLE {dialect.q(table.table_name)} "
            f"ADD CONSTRAINT {dialect.q(fk.constraint_name)} "
            f"FOREIGN KEY ({local}) REFERENCES {dialect.q(fk.referenced_table)} ({ref})"
            f"{fk_on_delete_clause(fk.on_delete)};"
        )
    return statements


def _key_columns(table: AgentTable, dialect: Dialect) -> set[str]:
    """Columns in the primary key, an index the draft renders, or a foreign key."""
    cols = {c.lower() for c in table.primary_key or []}
    for idx in table.indexes or []:
        if dialect is MYSQL and index_method(idx) in _MYSQL_DROPPED_METHODS:
            continue  # #293: the draft renders no index on these columns
        cols.update(c.lower() for c in idx.columns)
    for fk in table.foreign_keys or []:
        cols.update(c.lower() for c in fk.columns)
    return cols


def _identity_ok(table: AgentTable, col: AgentColumn, dialect: Dialect, has_auto: bool) -> bool:
    """Whether a sequence default may make ``col`` an identity column (#156).

    Identity implies NOT NULL. MySQL also allows a single AUTO_INCREMENT
    column, which must be a key: here, the table's whole primary key.
    """
    if col.nullable:
        return False
    if dialect is MYSQL:
        pk = [c.lower() for c in table.primary_key or []]
        return not has_auto and pk == [col.column_name.lower()]
    return True


def _generate(
    tables: list[AgentTable],
    dialect: Dialect,
    type_overrides: TypeOverrides | None = None,
    source_engine: str = "",
) -> DdlResult:
    residuals: list[dict] = []
    notes: list[dict] = []
    column_notes: list[dict] = []
    table_ddls: list[TableDDL] = []
    overrides = type_overrides or {}
    family = classify_source_family(source_engine)

    for table in tables:
        keyed = _key_columns(table, dialect)
        has_auto = any(c.is_auto_increment for c in table.columns)
        columns = [
            _column_ddl(
                table.table_name,
                c,
                residuals,
                dialect,
                overrides.get((table.table_name, c.column_name)),
                source_family=family,
                indexed=c.column_name.lower() in keyed,
                notes=column_notes,
                source_engine=source_engine,
                identity_ok=_identity_ok(table, c, dialect, has_auto),
            )
            for c in table.columns
        ]
        table_ddls.append(
            TableDDL(
                table_name=table.table_name,
                columns=columns,
                create_sql=_create_table_sql(table, columns, dialect),
                index_sql=_index_sql(table, dialect, notes, columns),
                fk_sql=_fk_sql(table, dialect),
            )
        )

    return DdlResult(
        tables=table_ddls,
        full_ddl=assemble_full_ddl(table_ddls),
        residuals=residuals,
        index_notes=notes,
        column_notes=column_notes,
    )


def assemble_full_ddl(table_ddls: list[TableDDL]) -> str:
    """All CREATE TABLEs, then all indexes, then all FKs (so ordering never breaks a reference)."""
    parts: list[str] = [t.create_sql for t in table_ddls]
    for t in table_ddls:
        parts.extend(t.index_sql)
    for t in table_ddls:
        parts.extend(t.fk_sql)
    return "\n\n".join(parts)


def generate_pg_ddl(
    tables: list[AgentTable],
    type_overrides: TypeOverrides | None = None,
    source_engine: str = "",
) -> DdlResult:
    """Translate normalized source tables into Aurora PostgreSQL DDL.

    ``type_overrides`` maps ``(table_name, column_name)`` to a model-decided
    type (#273); those columns are emitted with it and are not residuals.
    ``source_engine`` is the collector's source engine: for a PostgreSQL or
    MySQL source each column's native ``data_type`` is mapped first (#274,
    ``source_types``), and the normalized type is the fallback.
    """
    return _generate(tables, POSTGRES, type_overrides, source_engine)


def generate_mysql_ddl(
    tables: list[AgentTable],
    type_overrides: TypeOverrides | None = None,
    source_engine: str = "",
) -> DdlResult:
    """Translate normalized source tables into Aurora MySQL DDL (see ``generate_pg_ddl``)."""
    return _generate(tables, MYSQL, type_overrides, source_engine)
