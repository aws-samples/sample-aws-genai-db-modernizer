# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generate Aurora PostgreSQL and MySQL DDL from the normalized collector schema.

Pure, deterministic, and LLM-free. Every column the type map cannot resolve
confidently becomes a residual marker (surfaced to the LLM), never a silent
guess. Foreign keys and secondary indexes are emitted after all CREATE TABLE
statements so table ordering never breaks a reference.

Tables are emitted into a single flat namespace when they all come from one schema. When
they come from more than one (``qualified``, #2455), each table is created in its source
schema (``"sales"."orders"``), the script opens with one ``CREATE SCHEMA IF NOT EXISTS``
per schema, and the draft names the table ``<schema>.<table>`` in its notes and keys
(``design_table_name``), so two schemas' tables of one name are two tables.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from src.contracts.aurora_design_delta import INDEX_METHODS, validate_aurora_type
from src.contracts.schema_design_input import (
    AgentColumn,
    AgentForeignKey,
    AgentIndex,
    AgentTable,
    ForeignKeyAction,
)
from src.shared.engine_names import display_engine
from src.shared.sql_identifiers import (
    UNSAFE_NAME_REASON,
    UnsafeIdentifierError,
    has_unprintable,
    quote_identifier,
)
from src.shared.table_keys import colliding_keys, is_system_schema, table_key
from src.tools.schema.aurora_common.constraint_translator import (
    DefaultTranslation,
    fk_on_delete_clause,
    identity_clause,
    mysql_auto_increment_clause,
    not_null_clause,
    translate_default,
)
from src.tools.schema.aurora_common.source_family import classify_type_family
from src.tools.schema.aurora_common.source_types import resolve_source_type
from src.tools.schema.aurora_common.sql_safety import (
    SqlFragmentError,
    comment_text,
    index_definition_is_only,
    render_index_expressions,
    render_source_predicate,
)
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
        """``identifier`` quoted, or ``UnsafeIdentifierError`` for a name holding a control,
        format, surrogate or line separator character (#2381 review 2): ``_generate`` then
        leaves its object out with ``UNSAFE_NAME_REASON``."""
        return quote_identifier(identifier, self.quote_char)


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
    # The engine to check the type for; None checks it for either (a model's delta). Step 1
    # names Aurora PostgreSQL to give an enum column its "schema"."type" (#1834).
    engine: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "aurora_type", validate_aurora_type(self.aurora_type, self.engine))


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
    nullable: bool
    # What the draft made of the source default (#611): the DEFAULT clause it
    # emits (" DEFAULT ..." or ""), and whether the column is an identity
    # (AUTO_INCREMENT) column, from the source or from a sequence default.
    default_clause: str = ""
    identity: bool = False
    default_dropped: bool = False  # the draft dropped it; the reason is a column note


@dataclass
class TableDDL:
    table_name: str  # the design's name for the table (``design_table_name``)
    columns: list[ColumnDDL]
    create_sql: str
    index_sql: list[str] = field(default_factory=list)
    fk_sql: list[str] = field(default_factory=list)
    # The schema the table is created in, None in a design of one schema (#2455), and the
    # CREATE SCHEMA statement the script runs first for it.
    schema_name: str | None = None
    schema_sql: str | None = None


def design_table_name(table: AgentTable, qualified: bool) -> str:
    """The design's name for ``table``: ``<schema>.<table>`` in a design whose tables come
    from more than one schema (#2455), else the table name. Notes, keys and type overrides
    use it; the SQL quotes the two parts apart (``quote_table``)."""
    if qualified and table.schema_name:
        return f"{table.schema_name}.{table.table_name}"
    return table.table_name


def quote_table(dialect: Dialect, table_name: str, schema: str | None = None) -> str:
    """The table as the SQL names it: ``"schema"."table"`` with a schema, else ``"table"``."""
    return f"{dialect.q(schema)}.{dialect.q(table_name)}" if schema else dialect.q(table_name)


def create_schema_sql(dialect: Dialect, schema: str) -> str:
    """The statement that creates a qualified design's schema, run before its tables."""
    return f"CREATE SCHEMA IF NOT EXISTS {dialect.q(schema)};"


@dataclass
class DdlResult:
    tables: list[TableDDL]
    full_ddl: str
    residuals: list[dict]
    # Source indexes the draft could not carry over as written: a partial-index
    # predicate Aurora cannot take, an access method it cannot create or the
    # collector cannot describe (#293), or an expression key the collector cannot
    # name (#295): {"table", "index", "reason"}.
    index_notes: list[dict] = field(default_factory=list)
    # Columns that carry over but need a look: {"table", "column", "kind", "reason"};
    # kind "default" (a source default the draft dropped, or turned into an
    # identity column, #156) or "time_zone" (a time-zone-aware type mapped to
    # one without, app-layer).
    column_notes: list[dict] = field(default_factory=list)
    # Source objects the draft does not create because their name cannot be quoted into SQL
    # (#2381 review 2, ``sql_identifiers``): {"table", "kind", "name", "reason"}, ``kind`` one
    # of table, column, primary key, index, foreign key. Never written into the DDL.
    left_out: list[dict] = field(default_factory=list)


def excerpt(text: str, limit: int = 80) -> str:
    """Collector text echoed into notes: repr'd (no raw newlines/quotes) and truncated."""
    return repr(text[:limit]) + ("..." if len(text) > limit else "")


def _resolve(
    col: AgentColumn, dialect: Dialect, source_family: str, indexed: bool
) -> TypeResolution:
    has_default = col.default_value is not None and not col.is_auto_increment
    """The source ``data_type`` first (PostgreSQL/MySQL sources, #274; Oracle/SQL Server
    date/time and LOB types, #2219), else the normalized type."""
    resolution = resolve_source_type(
        col.data_type,
        source_family=source_family,
        target=dialect.name,
        max_length=col.max_length,
        indexed=indexed,
        has_default=has_default,
        numeric_precision=col.numeric_precision,
        numeric_scale=col.numeric_scale,
        datetime_precision=col.datetime_precision,
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
        return _column_from(
            col, resolution, source_type, dialect, default, script_derived=False, table=table_name
        )
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
        col,
        resolution,
        source_type,
        dialect,
        default,
        script_derived=not resolution.needs_judgment,
        table=table_name,
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
    table: str,
) -> ColumnDDL:
    # The one place a column type is written (#1860): whatever produced it (the
    # source-type map, a normalized-type map, a residual's fallback or the model's
    # override), it passes the allowlist for this engine or nothing is written.
    try:
        aurora_type = validate_aurora_type(resolution.aurora_type, dialect.name)
    except ValueError as exc:
        raise ValueError(f"column {table}.{col.column_name}: {exc}") from exc
    auto_increment = dialect.auto_increment(
        col.is_auto_increment or default.identity
    )  # nosemgrep: is-function-without-parentheses -- property, not a method
    # Identity/auto-increment and default are mutually exclusive — a column
    # cannot be both an identity/auto-increment column and carry a DEFAULT clause.
    fragment = (
        f"{dialect.q(col.column_name)} {aurora_type}"
        f"{auto_increment}"
        f"{not_null_clause(col.nullable)}"
        f"{'' if auto_increment else default.clause}"
    )
    return ColumnDDL(
        name=col.column_name,
        aurora_type=aurora_type,
        source_type=source_type,
        script_derived=script_derived,
        needs_judgment=resolution.needs_judgment,
        judgment_reason=resolution.reason,
        fragment=fragment,
        nullable=col.nullable,
        default_clause="" if auto_increment else default.clause,
        identity=bool(auto_increment),
        # Dropped means a reason and no clause. A translation that rewrites a default
        # to nothing always says why, so it reads as dropped too, by design.
        default_dropped=bool(default.reason) and not default.clause and not auto_increment,
    )


def _mysql_primary_key_prefixes(
    table: AgentTable, columns: list[ColumnDDL], notes: list[dict]
) -> dict[str, int]:
    """Key prefix lengths for an Aurora MySQL primary key (#1623), by lower-cased column name.

    Only prefixes the source recorded (``PRIMARY KEY (url(255))``) are used, cut to fit the
    key limit with the other key parts. A primary key never gets a default prefix, since a
    prefix makes it stricter than the source's: a TEXT or BLOB key column with no recorded
    length, a shortened prefix and a key over the limit each add a note instead.
    """
    names = list(table.primary_key or [])
    lower = [c.lower() for c in names]
    known: dict[str, int | None] = {}
    for idx in table.indexes or []:
        if idx.prefix_lengths and is_primary_key_index(idx, table):
            known = {c.lower(): n for c, n in zip(idx.columns, idx.prefix_lengths, strict=False)}
            break
    plan = mysql_key_plan(names, [known.get(c) for c in lower], columns)

    def note(reason: str) -> None:
        notes.append(
            {"table": table.table_name, "index": "PRIMARY", "kind": "key_prefix", "reason": reason}
        )

    prefixes: dict[str, int] = {}
    missing: list[str] = []
    for pos, (name, key) in enumerate(zip(names, lower, strict=True)):
        chosen = plan.prefixes[pos] if plan.prefixes else None
        if chosen is None:
            continue
        if known.get(key) is None:
            missing.append(name)
        else:
            prefixes[key] = chosen
    if missing:
        note(
            f"Primary key column(s) {', '.join(missing)} are TEXT or BLOB and the source records "
            "no key length: Aurora MySQL needs one (error 1170). Choose a prefix length or a "
            "VARCHAR(n) type. The draft adds none, since a prefix would make the key stricter "
            "than the source's."
        )
    cut = [c for c in plan.shrunk if known.get(c.lower()) is not None]
    if cut:
        note(
            f"Primary key prefix shortened on {', '.join(cut)} to keep the key within "
            f"{MYSQL_MAX_KEY_BYTES} bytes (4 bytes per character on utf8mb4). The key is "
            "stricter than the source's, so the data load can fail on duplicate keys: check "
            "the values before loading."
        )
    if not plan.fits:
        note(_over_limit_reason("Primary key"))
    elif plan.prefixes is None and plan.key_bytes > MYSQL_MAX_KEY_BYTES:
        note(_over_limit_reason("Primary key", plan.key_bytes))
    return prefixes


def _create_table_sql(
    table: AgentTable,
    columns: list[ColumnDDL],
    dialect: Dialect,
    notes: list[dict] | None = None,
    ref: tuple[str, str | None] | None = None,
) -> str:
    """``ref`` is the (table, schema) the SQL names, when ``table`` carries the design's
    qualified name (``_generate``); else the table's own name, with no schema."""
    prefixes: dict[str, int] = {}
    if table.primary_key and dialect is MYSQL:
        prefixes = _mysql_primary_key_prefixes(table, columns, notes if notes is not None else [])
    name, schema = ref or (table.table_name, None)
    return render_create_table(
        dialect,
        name,
        [c.fragment for c in columns],
        [(c, prefixes.get(c.lower())) for c in table.primary_key or []],
        schema=schema,
    )


def render_create_table(
    dialect: Dialect,
    table_name: str,
    fragments: list[str],
    primary_key: list[tuple[str, int | None]],
    *,
    schema: str | None = None,
) -> str:
    """One CREATE TABLE statement: each column fragment on its own line, then the PRIMARY
    KEY (column, Aurora MySQL key prefix or None) when there is one; the table in
    ``schema`` when given (#2455). A saved design's ``generated_ddl`` is rebuilt through
    this on every read (#2199)."""
    lines = [f"  {f}" for f in fragments]
    if primary_key:
        pk_cols = ", ".join(
            dialect.q(c) + _prefix_suffix([n for _, n in primary_key], pos)
            for pos, (c, _) in enumerate(primary_key)
        )
        lines.append(f"  PRIMARY KEY ({pk_cols})")
    body = ",\n".join(lines)
    return f"CREATE TABLE {quote_table(dialect, table_name, schema)} (\n{body}\n);"


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
    # nosemgrep: is-function-without-parentheses -- field, not a method
    if not pk or not idx.is_unique or idx.predicate:
        return False
    return cols == pk or (idx.index_name.lower().endswith("_pkey") and set(cols) == set(pk))


def expression_indexes(table: AgentTable) -> list[AgentIndex]:
    """Source indexes with an expression key, which the draft cannot carry over.

    The collector names only the plain column keys (#295), so the key list is
    incomplete and anything the draft built from it would be a different index.
    They leave the draft through ``DdlResult.index_notes`` instead: a design's index
    statements are re-rendered from column lists wherever a saved design is read. Step
    1's DDL, which moves the source schema as is, recreates the ones it can check
    (``recreated_expression_index``, #2381).
    """
    return [i for i in table.indexes or [] if i.has_expressions]


def secondary_indexes(table: AgentTable) -> list[AgentIndex]:
    """The draft's secondary indexes, in ``TableDDL.index_sql`` order."""
    return [
        i
        for i in table.indexes or []
        if not i.has_expressions and not is_primary_key_index(i, table)
    ]


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
# ``other`` has no PostgreSQL spelling, so it stays a plain B-tree index as before.
# ``fulltext`` and ``spatial`` (MySQL and SQL Server sources) are noted instead
# (``_DROPPED_METHODS``).
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
# The methods a target cannot create and no B-tree stands in for: a B-tree on the same
# columns would not serve the full-text, containment or geometric queries the index exists
# for, and can fail to build on long values. The draft notes such an index, with what to use
# on that target, instead of rendering one: GIN, GiST and SP-GiST on Aurora MySQL; FULLTEXT
# and SPATIAL on Aurora PostgreSQL, whose equivalents are the ones the mysql-fulltext-search
# and mysql-spatial-types gap rules name. On Aurora MySQL a BRIN index is rendered as a
# B-tree, which serves the same range scans, with a note.
# #2227: a SQL Server columnstore index stores columns for analytic scans and has no key; a
# B-tree over every column would serve none of its queries and fails the key limit.
_COLUMNSTORE_ALTERNATIVE = (
    "Serve the analytic scans it exists for from an analytics store, such as Amazon Redshift "
    "through a zero-ETL integration, or drop it."
)
_DROPPED_METHODS: dict[str, dict[str, str]] = {
    MYSQL.name: {
        **dict.fromkeys(
            ("gin", "gist", "spgist"),
            "Use a FULLTEXT or SPATIAL index where one fits the columns, or serve the search "
            "from the application layer.",
        ),
        "columnstore": _COLUMNSTORE_ALTERNATIVE,
    },
    POSTGRES.name: {
        "columnstore": _COLUMNSTORE_ALTERNATIVE,
        "fulltext": (
            "Use PostgreSQL full-text search: a tsvector column or expression with a GIN "
            "index, queried with @@ and to_tsquery."
        ),
        "spatial": "Use the PostGIS extension: a geometry or geography column with a GiST index.",
    },
}
# #1596: the source methods Aurora MySQL keeps, as the index kind (CREATE FULLTEXT INDEX)
# rather than USING, where the draft's columns take it: FULLTEXT on character columns,
# SPATIAL on one NOT NULL geometry column, neither UNIQUE. A plain index in their place
# fails on TEXT (error 1170); where the draft cannot keep the kind, it renders one with a note.
_MYSQL_INDEX_KINDS = frozenset({"fulltext", "spatial"})
_MYSQL_SPATIAL_TYPES = ("GEOMETRY", "POINT", "LINESTRING", "POLYGON", "COLLECTION")  # MULTI*


def index_method(index: AgentIndex) -> str | None:
    """The source index's access method, or None for an input written before #293."""
    return index.index_type.value if index.index_type else None


def _dropped(index: AgentIndex, dialect: Dialect) -> bool:
    """Whether the draft notes ``index`` instead of rendering it on ``dialect``."""
    return index_method(index) in _DROPPED_METHODS[dialect.name]


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


def _mysql_index_kind(index: AgentIndex, columns: list[ColumnDDL]) -> tuple[str | None, str]:
    """The kind an Aurora MySQL draft keeps for ``index`` (FULLTEXT or SPATIAL; None: a plain
    B-tree) and, when it cannot keep the source's, the index note that says why ("" otherwise)."""
    method = index_method(index)
    if method not in _MYSQL_INDEX_KINDS:
        return None, ""
    types = {c.name.lower(): c.aurora_type for c in columns}
    nullable = {c.name.lower() for c in columns if c.nullable} if method == "spatial" else set()
    keys = [name.lower() for name in index.columns]
    kinds = [types.get(key, "").split("(")[0].upper() for key in keys]
    on = ", ".join(
        f"{name} ({types.get(key, 'unknown type')}{', nullable' if key in nullable else ''})"
        for name, key in zip(index.columns, keys, strict=True)
    )
    why = ""
    if index.is_unique:
        why = f"Aurora MySQL has no UNIQUE {method} index"
    elif method == "fulltext" and not all(k.endswith(("CHAR", "TEXT")) for k in kinds):
        why = f"a fulltext index takes only CHAR, VARCHAR and TEXT columns, and this one is on {on}"
    elif method == "spatial" and (
        len(keys) != 1 or not kinds[0].endswith(_MYSQL_SPATIAL_TYPES) or keys[0] in nullable
    ):
        why = f"a spatial index takes one NOT NULL geometry column, and this one is on {on}"
    if why:
        return None, f"{method.upper()} index carried over as a B-tree: {why}."
    return method, ""


def rendered_method(index: AgentIndex, dialect: Dialect, columns: list[ColumnDDL]) -> str | None:
    """The method the draft renders for ``index`` on ``dialect``, or None for a plain
    B-tree: a USING method on Aurora PostgreSQL, FULLTEXT or SPATIAL on Aurora MySQL."""
    if dialect is POSTGRES:
        return _pg_index_method(index, columns)[0]
    return _mysql_index_kind(index, columns)[0]


def rendered_key(
    index: AgentIndex, dialect: Dialect, columns: list[ColumnDDL]
) -> tuple[bool, list[int | None] | None]:
    """Whether the draft keeps ``index`` UNIQUE, and its Aurora MySQL key prefixes (#1623)."""
    # nosemgrep: is-function-without-parentheses -- field, not a method
    unique = bool(index.is_unique)
    if dialect is not MYSQL:
        return unique, None
    kind = rendered_method(index, MYSQL, columns)  # #1596: FULLTEXT/SPATIAL take no prefix
    plan = mysql_key_plan(index.columns, index.prefix_lengths, columns, kind)
    return unique and not plan.weakens_unique, plan.prefixes


def carried_indexes(table: AgentTable, dialect: Dialect) -> list[AgentIndex]:
    """The secondary indexes the draft renders on ``dialect``, in ``TableDDL.index_sql``
    order: all but the ones ``_index_sql`` notes instead (``_DROPPED_METHODS``: GIN, GiST
    and SP-GiST on Aurora MySQL, FULLTEXT and SPATIAL on Aurora PostgreSQL)."""
    return [i for i in secondary_indexes(table) if not _dropped(i, dialect)]


# #1623: Aurora MySQL (InnoDB) rejects an index on a TEXT or BLOB column without a key
# prefix (error 1170), and a key longer than 3072 bytes (error 1071). The draft costs a
# character key part at 4 bytes per character (utf8mb4, the Aurora MySQL 3 default) and a
# binary key part at its length. 191 is the default prefix when the source records none: it
# fits one column in 767 bytes, the oldest InnoDB limit.
MYSQL_DEFAULT_KEY_PREFIX = 191
MYSQL_MAX_KEY_BYTES = 3072
_MIN_USEFUL_PREFIX = 16
_MYSQL_TEXT_TYPES = frozenset({"TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT"})
_MYSQL_BLOB_TYPES = frozenset({"TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB"})
_MYSQL_CHAR_TYPES = frozenset({"VARCHAR", "CHAR"})
_MYSQL_BINARY_TYPES = frozenset({"VARBINARY", "BINARY"})
_MYSQL_OTHER_PART_BYTES = 8  # an int, date or similar key part; small next to a string
# FULLTEXT and SPATIAL keys take no prefix; a prefix there would make them a B-tree.
_MYSQL_NO_PREFIX_METHODS = frozenset({"fulltext", "spatial"})
_TYPE_RE = re.compile(r"^\s*([A-Za-z]+)\s*(?:\(\s*(\d+))?")


@dataclass
class KeyPlan:
    """How the draft renders one Aurora MySQL index key (#1623)."""

    prefixes: list[int | None] | None = None
    defaulted: list[str] = field(default_factory=list)  # TEXT/BLOB columns given the default
    shrunk: list[str] = field(default_factory=list)  # prefixes cut to fit 3072 bytes
    fits: bool = True
    key_bytes: int = 0

    @property
    def weakens_unique(self) -> bool:
        """A prefix the source did not have: it would reject rows the source accepts."""
        return bool(self.defaulted or self.shrunk)


def _key_part(aurora_type: str) -> tuple[str, int | None]:
    m = _TYPE_RE.match(aurora_type)
    if not m:
        return "", None
    return m.group(1).upper(), int(m.group(2)) if m.group(2) else None


def mysql_key_plan(
    index_columns: list[str],
    known: list[int | None] | None,
    column_ddls: list[ColumnDDL],
    method: str | None = None,
) -> KeyPlan:
    """The key prefixes of an Aurora MySQL index and whether the key fits 3072 bytes.

    A source prefix is kept on a character or binary column, unless the column is no
    longer than the prefix (error 1089). A TEXT or BLOB column with no known prefix gets
    ``MYSQL_DEFAULT_KEY_PREFIX``. When the key is over the byte limit the prefixes are cut
    to share what the unprefixed parts leave; ``fits`` is False when they cannot be cut
    to a useful length. ``method`` is the kind the draft renders (``rendered_method``):
    a FULLTEXT or SPATIAL index gets no prefix.
    """
    if method in _MYSQL_NO_PREFIX_METHODS:
        return KeyPlan()
    types = {c.name.lower(): _key_part(c.aurora_type) for c in column_ddls}
    fixed = 0
    # (position, requested chars or bytes, bytes per unit, defaulted) of each shrinkable part
    flex: list[tuple[int, int, int, bool]] = []
    for pos, name in enumerate(index_columns):
        base, length = types.get(name.lower(), ("", None))
        source = known[pos] if known and pos < len(known) else None
        if base in _MYSQL_TEXT_TYPES | _MYSQL_BLOB_TYPES:
            unit = 4 if base in _MYSQL_TEXT_TYPES else 1
            flex.append((pos, source or MYSQL_DEFAULT_KEY_PREFIX, unit, source is None))
        elif base in _MYSQL_CHAR_TYPES | _MYSQL_BINARY_TYPES:
            unit = 4 if base in _MYSQL_CHAR_TYPES else 1
            width = length if length is not None else (1 if base in ("CHAR", "BINARY") else 255)
            if source is not None and source < width:
                flex.append((pos, source, unit, False))
            else:
                fixed += width * unit
        else:
            fixed += _MYSQL_OTHER_PART_BYTES
    if not flex:
        return KeyPlan(key_bytes=fixed)
    prefixes: list[int | None] = [None] * len(index_columns)
    plan = KeyPlan(prefixes=prefixes)
    budget = MYSQL_MAX_KEY_BYTES - fixed
    remaining = len(flex)
    # Smallest request first: a short prefix keeps its length, the long ones share the rest.
    for pos, wanted, unit, defaulted in sorted(flex, key=lambda f: f[1] * f[2]):
        share = budget // remaining
        take = min(wanted * unit, share)
        chars = take // unit
        if chars < min(wanted, _MIN_USEFUL_PREFIX):
            plan.fits = False
            chars = max(chars, 1)
        if chars < wanted:
            plan.shrunk.append(index_columns[pos])
        elif defaulted:
            plan.defaulted.append(index_columns[pos])
        prefixes[pos] = chars
        budget -= chars * unit
        remaining -= 1
    plan.key_bytes = MYSQL_MAX_KEY_BYTES - budget
    if budget < 0:
        plan.fits = False
    return plan


# #2227: the most a PostgreSQL B-tree index row holds on 8 KB pages; a longer row fails the
# INSERT or UPDATE that writes it ("index row size exceeds btree maximum").
PG_MAX_INDEX_ROW_BYTES = 2704
_PG_UNBOUNDED_TYPES = frozenset({"TEXT", "BYTEA", "JSON", "JSONB", "XML", "CITEXT"})
_PG_SIZED_TEXT_TYPES = frozenset({"VARCHAR", "CHAR", "CHARACTER"})


def _pg_part_bytes(aurora_type: str) -> int | None:
    """The most bytes one value of ``aurora_type`` adds to a PostgreSQL index row (4 per
    character), or None when it has no bound."""
    base, length = _key_part(aurora_type)
    if base in _PG_UNBOUNDED_TYPES or (base in _PG_SIZED_TEXT_TYPES and length is None):
        return None
    if base in _PG_SIZED_TEXT_TYPES and length is not None:
        return length * 4
    return _MYSQL_OTHER_PART_BYTES


def _include_notes(
    table: AgentTable, idx: AgentIndex, dialect: Dialect, column_ddls: list[ColumnDDL]
) -> list[dict]:
    """What the draft says about a source index's INCLUDE columns (#2227).

    Aurora MySQL has no INCLUDE: the columns it drops are named, except the primary key's,
    which every InnoDB secondary index stores anyway. Aurora PostgreSQL keeps INCLUDE, and
    its B-tree rows hold at most ``PG_MAX_INDEX_ROW_BYTES``: unbounded or long INCLUDE
    columns are named.
    """
    if not idx.included_columns:
        return []
    note = {"table": table.table_name, "index": idx.index_name}
    if dialect is MYSQL:
        pk = {c.lower() for c in table.primary_key or []}
        dropped = [c for c in idx.included_columns if c.lower() not in pk]
        if not dropped:
            return []
        return [
            {
                **note,
                "reason": (
                    f"Non-key columns {', '.join(dropped)} not carried over: the source index "
                    "stores them without keying them (INCLUDE, or the partitioning column SQL "
                    "Server adds) and Aurora MySQL has no INCLUDE clause, so the index no longer "
                    "covers the queries that read them and each such query looks up the row. "
                    "Add them as trailing key "
                    f"columns where the key stays within {MYSQL_MAX_KEY_BYTES} bytes (not on a "
                    "UNIQUE index, whose uniqueness would then cover them too), or accept the "
                    "extra lookups."
                ),
            }
        ]
    types = {c.name.lower(): c.aurora_type for c in column_ddls}
    sizes = {c: _pg_part_bytes(types.get(c.lower(), "")) for c in idx.included_columns}
    unbounded = [c for c, n in sizes.items() if n is None]
    keys = [_pg_part_bytes(types.get(c.lower(), "")) for c in idx.columns]
    total = sum(n or 0 for n in [*keys, *sizes.values()])
    if not unbounded and total <= PG_MAX_INDEX_ROW_BYTES:
        return []
    named = unbounded or [c for c, n in sizes.items() if n and n > _MYSQL_OTHER_PART_BYTES]
    return [
        {
            **note,
            "reason": (
                f"INCLUDE columns {', '.join(named)} can make an index row longer than the "
                f"{PG_MAX_INDEX_ROW_BYTES} bytes an Aurora PostgreSQL B-tree row holds, and "
                "an INSERT or UPDATE writing such a row fails. Confirm the longest values fit, "
                "or leave the long columns out of INCLUDE."
            ),
        }
    ]


def _pk_copy_include_notes(table: AgentTable) -> list[dict]:
    """Aurora PostgreSQL: a source index the draft drops as a copy of the primary key, but
    whose INCLUDE columns the primary key does not store (#2227)."""
    return [
        {
            "table": table.table_name,
            "index": idx.index_name,
            "reason": (
                f"Index left out as a copy of the primary key, but it also stores INCLUDE "
                f"columns {', '.join(idx.included_columns)}, which the primary key index does "
                "not cover on Aurora PostgreSQL. Create it as a UNIQUE index on the key with "
                "INCLUDE if the queries it serves read them."
            ),
        }
        for idx in table.indexes or []
        if idx.included_columns and not idx.has_expressions and is_primary_key_index(idx, table)
    ]


def _key_prefix_note(table: str, index: str, plan: KeyPlan, unique: bool) -> dict:
    """The index note for a prefix the source did not have (``kind`` key_prefix)."""
    parts = []
    if plan.defaulted:
        parts.append(
            f"Key prefix of {MYSQL_DEFAULT_KEY_PREFIX} characters added on "
            f"{', '.join(plan.defaulted)}: Aurora MySQL cannot index a TEXT or BLOB column "
            "without a prefix length and the source records none."
        )
    if plan.shrunk:
        parts.append(
            f"Key prefix shortened on {', '.join(plan.shrunk)} to keep the key within "
            f"{MYSQL_MAX_KEY_BYTES} bytes (4 bytes per character on utf8mb4)."
        )
    parts.append("Confirm the lengths against the values and the queries.")
    if unique:
        parts.append(
            "The index is carried over as non-unique, since a prefix would reject rows the "
            "source accepts; enforce uniqueness another way."
        )
    return {"table": table, "index": index, "kind": "key_prefix", "reason": " ".join(parts)}


# Why an index is not created, the one sentence the draft has written since it first left
# one out (#1623); a saved design's skipped-index comment must end with exactly this (#2199).
NOT_CREATED_REASON = (
    f"its key parts cannot share {MYSQL_MAX_KEY_BYTES} bytes on Aurora MySQL (4 bytes per "
    "character on utf8mb4) with a useful prefix on its TEXT or BLOB columns. Index fewer or "
    "shorter columns, or serve the lookups another way."
)


def _not_created_reason(plan: KeyPlan) -> str:
    return NOT_CREATED_REASON


def _over_limit_reason(what: str, key_bytes: int | None = None) -> str:
    size = f" is {key_bytes} bytes and" if key_bytes else ""
    return (
        f"{what}{size} is over the {MYSQL_MAX_KEY_BYTES}-byte Aurora MySQL key limit "
        "(4 bytes per character on utf8mb4), which MySQL rejects (error 1071). Shorten the "
        "column or key a prefix of it."
    )


SKIPPED_INDEX_PREFIX = "-- Index "
SKIPPED_INDEX_SUFFIX = f" not created: {NOT_CREATED_REASON}"
# Characters a ``--`` comment may not carry: every C0 and C1 control character (``\r``
# among them, which some viewers read as a line break) and the Unicode line separators.
_COMMENT_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def comment_name(table_name: str, index_name: str) -> str:
    """``table.index`` as a skipped-index comment writes it: each control character or line
    separator replaced by a space, then the whitespace collapsed, so the comment is one
    line and nothing after it is part of it. A saved design's comment is checked against
    this (#2199)."""
    return comment_text(_COMMENT_UNSAFE.sub(" ", f"{table_name}.{index_name}"))


def skipped_index_comment(table_name: str, index_name: str) -> str:
    """The SQL comment standing in for an index the key limit rules out."""
    return f"{SKIPPED_INDEX_PREFIX}{comment_name(table_name, index_name)}{SKIPPED_INDEX_SUFFIX}"


def _skipped_index(table_name: str, idx: AgentIndex, plan: KeyPlan) -> str:
    """A SQL comment standing in for an index the key limit rules out, so the draft keeps
    one statement per source index and the DDL still applies."""
    return skipped_index_comment(table_name, idx.index_name)


def _index_sql(
    table: AgentTable,
    dialect: Dialect,
    notes: list[dict],
    column_ddls: list[ColumnDDL],
    ref: tuple[str, str | None] | None = None,
) -> list[str]:
    """``ref`` as for ``_create_table_sql``."""
    statements: list[str] = []
    ref_name, ref_schema = ref or (table.table_name, None)
    columns = [c.column_name for c in table.columns]
    for idx in expression_indexes(table):
        # The collector names only the plain keys, so an index built from them
        # would be narrower than the source -- and a UNIQUE one would reject
        # rows the source accepts. Leave it out and say why.
        unique = bool(idx.is_unique)  # nosemgrep: is-function-without-parentheses
        # The expressions alone, not the whole CREATE statement: a real
        # pg_get_indexdef runs well past excerpt()'s cut, and what the
        # customer needs in order to recreate the index is the part the draft
        # could not carry over.
        keys = idx.index_expressions or idx.index_def
        notes.append(
            {
                "table": table.table_name,
                "index": idx.index_name,
                "reason": (
                    "Source index has expression keys"
                    + (f" ({excerpt(keys)})" if keys else "")
                    + ": the draft leaves it out rather than create "
                    + ("a UNIQUE index" if unique else "an index")
                    + " on part of its keys."
                    + (
                        " Recreate it on the target to keep the uniqueness it enforces."
                        if unique
                        else " Recreate it on the target."
                    )
                ),
            }
        )
    text_columns = _text_columns(column_ddls)
    dropped = _DROPPED_METHODS[dialect.name]
    target = display_engine(dialect.name)
    for idx in secondary_indexes(table):
        method = index_method(idx)
        if method in dropped:
            notes.append(
                {
                    "table": table.table_name,
                    "index": idx.index_name,
                    "reason": (
                        f"{method.upper()} index not carried over: {target} has no {method} "
                        "access method, and a B-tree on the same columns would not serve the "
                        f"queries it exists for. {dropped[method]}"
                    ),
                }
            )
    if dialect is POSTGRES:
        notes.extend(_pk_copy_include_notes(table))
    for idx in carried_indexes(table, dialect):
        method = index_method(idx)
        if dialect is POSTGRES:
            using, why = _pg_index_method(idx, column_ddls)
        else:
            using, why = _mysql_index_kind(idx, column_ddls)
        if why:
            notes.append({"table": table.table_name, "index": idx.index_name, "reason": why})
        notes.extend(_include_notes(table, idx, dialect, column_ddls))
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
        # nosemgrep: is-function-without-parentheses -- field, not a method
        unique = bool(idx.is_unique)
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
        prefixes = None
        if dialect is MYSQL:
            # #1596: the kind the draft keeps; a FULLTEXT or SPATIAL index it renders as a
            # B-tree instead takes a prefix like any other
            plan = mysql_key_plan(idx.columns, idx.prefix_lengths, column_ddls, using)
            if not plan.fits:
                statements.append(_skipped_index(table.table_name, idx, plan))
                notes.append(
                    {
                        "table": table.table_name,
                        "index": idx.index_name,
                        "kind": "key_prefix",
                        "reason": f"Index not created: {_not_created_reason(plan)}",
                    }
                )
                continue
            prefixes = plan.prefixes
            if plan.prefixes is None and plan.key_bytes > MYSQL_MAX_KEY_BYTES:
                notes.append(
                    {
                        "table": table.table_name,
                        "index": idx.index_name,
                        "kind": "key_prefix",
                        "reason": _over_limit_reason("Index", plan.key_bytes),
                    }
                )
            if plan.defaulted or plan.shrunk:
                notes.append(_key_prefix_note(table.table_name, idx.index_name, plan, unique))
                unique = False  # a UNIQUE prefix index would reject rows the source accepts
        try:
            statement = render_index(
                dialect,
                ref_name,
                idx.index_name,
                idx.columns,
                unique=unique,
                method=using,
                # #2227: a SQL Server INCLUDE column stays a stored column on Aurora
                # PostgreSQL; Aurora MySQL has no INCLUDE and keys only the key columns.
                include=idx.included_columns if dialect is POSTGRES else None,
                where_sql=where_sql,
                prefix_lengths=prefixes,
                schema=ref_schema,
            )
        except UnsafeIdentifierError:
            # One statement per carried index still (``delta_merge`` zips them by name).
            statement = _unsafe_index_comment(table.table_name, idx.index_name)
            notes.append(
                {"table": table.table_name, "index": idx.index_name,
                 "kind": "unsafe_name", "reason": UNSAFE_NAME_REASON}
            )  # fmt: skip
        statements.append(statement)
    return statements


def _unsafe(table: str, kind: str, name: str) -> dict:
    """A left-out object's record: the names as collected (``DdlResult.left_out`` readers
    clean them before writing a comment), the fixed reason."""
    return {"table": table, "kind": kind, "name": name, "reason": UNSAFE_NAME_REASON}


def _unsafe_index_comment(table_name: str, index_name: str) -> str:
    return f"-- Index {comment_name(table_name, index_name)} not created: {UNSAFE_NAME_REASON}."


def _prefix_suffix(prefix_lengths: list[int | None] | None, pos: int) -> str:
    n = prefix_lengths[pos] if prefix_lengths and pos < len(prefix_lengths) else None
    if n is None:
        return ""
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise SqlFragmentError(f"index key prefix length must be a positive integer, got {n!r}")
    return f"({n})"


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
    prefix_lengths: list[int | None] | None = None,
    schema: str | None = None,
) -> str:
    """One CREATE INDEX statement with every identifier quoted by the dialect, on the table
    in ``schema`` when given (#2455).

    ``method``, ``include`` and ``where_sql`` are PostgreSQL-only, except that Aurora MySQL
    renders a ``fulltext`` or ``spatial`` method as the kind of a non-unique index (#1596);
    the caller validates them (``where_sql`` comes from ``sql_safety.render_predicate``).
    ``prefix_lengths`` is Aurora MySQL-only: one entry per column, rendered ``col(N)``; a
    FULLTEXT or SPATIAL index takes none (#1623).
    ``method`` is checked here too, so no text outside ``INDEX_METHODS`` reaches
    ``USING`` whatever the caller.
    """
    kind = "UNIQUE " if unique else ""
    if dialect is MYSQL and method in _MYSQL_INDEX_KINDS and not unique:
        kind, method = f"{method.upper()} ", None
    if method and method not in INDEX_METHODS:
        raise SqlFragmentError(f"unknown index method '{method}'")
    if kind not in ("", "UNIQUE ") and any(n is not None for n in prefix_lengths or []):
        raise SqlFragmentError(f"a {kind.strip()} index takes no key prefix")
    cols = ", ".join(
        dialect.q(c) + _prefix_suffix(prefix_lengths, pos) for pos, c in enumerate(columns)
    )
    sql = (
        f"CREATE {kind}INDEX {dialect.q(index_name)} ON {quote_table(dialect, table_name, schema)}"
    )
    if method:
        sql += f" USING {method}"
    sql += f" ({cols})"
    if include:
        sql += " INCLUDE (" + ", ".join(dialect.q(c) for c in include) + ")"
    if where_sql:
        sql += f" WHERE {where_sql}"
    return sql + ";"


def recreated_expression_index(
    table_name: str, idx: AgentIndex, column_ddls: list[ColumnDDL], schema: str | None = None
) -> tuple[str | None, str | None]:
    """An Aurora PostgreSQL ``CREATE INDEX`` for a PostgreSQL source's expression index
    (#2381), as ``(statement, None)``, or ``(None, why)`` when it cannot be created.

    Only the key text the strict grammar accepts is used (``render_index_expressions``),
    re-rendered key by key with the identifiers quoted. ``why`` is a fixed sentence that
    quotes none of the collector's text. Left out: an index mixing column and expression
    keys, one with INCLUDE columns or an access method other than B-tree, one whose
    expression or predicate is outside the grammar, and one whose source definition says
    more than the statement would (``sql_safety.index_definition_is_only``): an operator class, a sort
    order, a collation, NULLS NOT DISTINCT or storage parameters, which the expression
    text does not carry (#2381 review).
    """
    if has_unprintable(idx.index_name) or has_unprintable(table_name):
        return None, UNSAFE_NAME_REASON
    if idx.columns or idx.included_columns:
        return None, "this release does not recreate indexes that mix columns and expressions"
    if index_method(idx) not in (None, "btree"):
        return None, "this release recreates B-tree expression indexes only"
    columns = [c.name for c in column_ddls]
    text_columns = _text_columns(column_ddls)
    try:
        keys = render_index_expressions(
            idx.index_expressions or "", columns, POSTGRES.q, text_columns
        )
    except SqlFragmentError:
        return None, (
            "its expression is outside what this release can check: calls of lower, upper, "
            "md5, left or COALESCE on the table's columns"
        )
    where = ""
    if idx.predicate:
        try:
            where = " WHERE " + render_source_predicate(
                idx.predicate, columns, POSTGRES.q, text_columns
            )
        except SqlFragmentError:
            return None, "its predicate is outside the supported grammar"
    unique = bool(
        idx.is_unique
    )  # nosemgrep: is-function-without-parentheses -- field, not a method
    if not index_definition_is_only(idx.index_def, idx.index_expressions, idx.predicate, unique):
        return None, (
            "its source definition has more than its expression and predicate (an operator "
            "class, a sort order, a collation, NULLS NOT DISTINCT, INCLUDE or storage "
            "parameters), which this release does not recreate"
        )
    keys_sql = ", ".join(f"({k})" for k in keys)
    try:
        name, table = POSTGRES.q(idx.index_name), quote_table(POSTGRES, table_name, schema)
    except UnsafeIdentifierError:
        return None, UNSAFE_NAME_REASON
    return f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON {table} ({keys_sql}){where};", None


def _fk_sql(
    table: AgentTable,
    dialect: Dialect,
    left_out: list[dict] | None = None,
    ref: tuple[str, str | None] | None = None,
    schemas_of: dict[str, list[str]] | None = None,
) -> list[str]:
    """``ref`` as for ``_create_table_sql``; ``schemas_of`` maps each bare table name of a
    qualified design (lower case) to the schemas that have it, for ``referenced_table``."""
    statements: list[str] = []
    name, schema = ref or (table.table_name, None)
    for fk in table.foreign_keys or []:
        resolved = _referenced(fk.referenced_table, schema, schemas_of or {})
        if resolved is None:
            # quotable_tables leaves such a key out of the base; never guess a schema.
            if left_out is not None:
                found = (schemas_of or {}).get(fk.referenced_table.lower(), [])
                left_out.append(_ambiguous_reference(table.table_name, fk, found))
            continue
        referenced, referenced_schema = resolved
        try:
            statements.append(
                render_foreign_key(
                    dialect,
                    name,
                    fk.constraint_name,
                    fk.columns,
                    referenced,
                    fk.referenced_columns,
                    fk.on_delete,
                    schema=schema,
                    referenced_schema=referenced_schema,
                )
            )
        except UnsafeIdentifierError:
            if left_out is not None:
                left_out.append(_unsafe(table.table_name, "foreign key", fk.constraint_name))
    return statements


def _referenced(
    referenced: str, schema: str | None, schemas_of: dict[str, list[str]]
) -> tuple[str, str | None] | None:
    """The (table, schema) a foreign key of a table in ``schema`` references (#2455). The
    collectors report the referenced table by its bare name, so it is the table of that
    name in the key's own schema (as spelled), else the one other schema of the design
    that has it, else the key's own schema when no schema has it. None when two other
    schemas have it and the key's own does not: the reference is ambiguous (#2479 review),
    and the key is left out rather than guessed. With no schema (one-schema design) it is
    the name as is."""
    if schema is None:
        return referenced, None
    found = schemas_of.get(referenced.lower(), [])
    if not found or schema in found:
        return referenced, schema
    if len(found) == 1:
        return referenced, found[0]
    return None


def _schemas_of(tables: list[AgentTable]) -> dict[str, list[str]]:
    """Each bare table name (lower case) of a qualified design to the schemas that have it,
    as spelled, in table order."""
    schemas_of: dict[str, list[str]] = {}
    for t in tables:
        if t.schema_name:
            found = schemas_of.setdefault(t.table_name.lower(), [])
            if t.schema_name not in found:
                found.append(t.schema_name)
    return schemas_of


def _ambiguous_reference(table: str, fk: AgentForeignKey, schemas: list[str]) -> dict:
    """A foreign key whose bare referenced table is in two other schemas (#2479 review)."""
    reason = (
        f"its referenced table {fk.referenced_table} is in more than one other schema "
        f"({', '.join(sorted(schemas))}) and not in this table's, so the design cannot tell "
        "which one it references; add it by hand once you know"
    )
    return {"table": table, "kind": "foreign key", "name": fk.constraint_name,
            "reason": reason, "feature": "ambiguous_reference"}  # fmt: skip


def _colliding_table(label: str, qualified: bool = True) -> dict:
    """A table whose key another table of the design has (#2479 review): in a design of one
    schema, its bare name folded (#2478, ``Orders`` and ``orders``)."""
    what = "its schema and name match" if qualified else "its name matches"
    reason = (
        f"{what} another table of the design once case and quote marks "
        "are ignored, so neither is designed; rename one, or design them by hand"
    )
    return {"table": label, "kind": "table", "name": label, "reason": reason,
            "feature": "colliding_table_name"}  # fmt: skip


def _system_table(table: AgentTable) -> dict:
    """A table of a system schema (#2478): the database creates and owns it."""
    label = f"{table.schema_name}.{table.table_name}"
    reason = (
        f"it is in the system schema {table.schema_name}, which the database creates and "
        "owns, so the design does not create it"
    )
    return {"table": label, "kind": "table", "name": label, "reason": reason,
            "feature": "system_schema"}  # fmt: skip


def render_foreign_key(
    dialect: Dialect,
    table_name: str,
    constraint_name: str,
    columns: list[str],
    referenced_table: str,
    referenced_columns: list[str],
    on_delete: ForeignKeyAction | None,
    *,
    schema: str | None = None,
    referenced_schema: str | None = None,
) -> str:
    """One ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY statement, every identifier quoted,
    each table in its schema when given (#2455). A saved design's ``foreign_keys`` are
    re-rendered through this on every read (#2199)."""
    local = ", ".join(dialect.q(c) for c in columns)
    ref = ", ".join(dialect.q(c) for c in referenced_columns)
    target = quote_table(dialect, referenced_table, referenced_schema)
    return (
        f"ALTER TABLE {quote_table(dialect, table_name, schema)} "
        f"ADD CONSTRAINT {dialect.q(constraint_name)} "
        f"FOREIGN KEY ({local}) REFERENCES {target} ({ref})"
        f"{fk_on_delete_clause(on_delete)};"
    )


def _key_columns(table: AgentTable, dialect: Dialect) -> set[str]:
    """Columns in the primary key, an index the draft renders, or a foreign key."""
    cols = {c.lower() for c in table.primary_key or []}
    for idx in table.indexes or []:
        if _dropped(idx, dialect):
            continue  # #293: the draft renders no index on these columns
        cols.update(c.lower() for c in idx.columns)
    for fk in table.foreign_keys or []:
        cols.update(c.lower() for c in fk.columns)
    return cols


# #2219: an Oracle or SQL Server LOB in a secondary index keeps its type and takes the
# key prefix the index renders with (#1623); SQL Server also lists an index's INCLUDE
# columns (``nvarchar(max)`` ones among them) with its keys, and narrowing those to a
# VARCHAR would cut the data. Only a primary or foreign key, which takes no prefix here,
# makes the column a residual.
_NATIVE_KEY_FAMILIES = frozenset({"oracle", "sqlserver"})


def _constraint_columns(table: AgentTable) -> set[str]:
    """Columns in the primary key or a foreign key."""
    cols = {c.lower() for c in table.primary_key or []}
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
    qualified: bool = False,
) -> DdlResult:
    """``qualified``: create each table in its source schema (#2455, see the module
    docstring); the type overrides are then keyed on ``design_table_name``."""
    residuals: list[dict] = []
    notes: list[dict] = []
    column_notes: list[dict] = []
    table_ddls: list[TableDDL] = []
    overrides = type_overrides or {}
    family = classify_type_family(source_engine)
    schemas_of = _schemas_of(tables) if qualified else {}

    left_out: list[dict] = []
    for unnamed in tables:
        schema = unnamed.schema_name if qualified else None
        ref = (unnamed.table_name, schema)
        # The design's name in every note and key; the SQL quotes ``ref`` instead.
        source_table = unnamed.model_copy(
            update={"table_name": design_table_name(unnamed, qualified)}
        )
        if has_unprintable(source_table.table_name):
            left_out.append(_unsafe(source_table.table_name, "table", source_table.table_name))
            table_ddls.append(_unsafe_table(source_table, dialect))
            continue
        table = _without_unsafe_columns(source_table, left_out)
        keyed = (
            _constraint_columns(table)
            if family in _NATIVE_KEY_FAMILIES
            else _key_columns(table, dialect)
        )
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
                create_sql=_create_table_sql(table, columns, dialect, notes, ref),
                index_sql=_index_sql(table, dialect, notes, columns, ref),
                fk_sql=_fk_sql(table, dialect, left_out, ref, schemas_of),
                schema_name=schema,
                schema_sql=create_schema_sql(dialect, schema) if schema else None,
            )
        )
    left_out += [
        _unsafe(n["table"], "index", n["index"]) for n in notes if n.get("kind") == "unsafe_name"
    ]

    return DdlResult(
        tables=table_ddls,
        full_ddl=assemble_full_ddl(table_ddls),
        residuals=residuals,
        index_notes=notes,
        column_notes=column_notes,
        left_out=left_out,
    )


def _unsafe_table(table: AgentTable, dialect: Dialect) -> TableDDL:
    """A table whose name cannot be quoted: no CREATE TABLE, one comment in place of each
    index the draft would carry (``delta_merge`` zips them by name), no foreign key."""
    name = comment_text(_COMMENT_UNSAFE.sub(" ", table.table_name))
    return TableDDL(
        table_name=table.table_name,
        columns=[],
        create_sql=f"-- Table {name} not created: {UNSAFE_NAME_REASON}.",
        index_sql=[
            _unsafe_index_comment(table.table_name, i.index_name)
            for i in carried_indexes(table, dialect)
        ],
    )


def _duplicate_index(table: str, name: str, kept: str) -> dict:
    """A table's later index of one name (#2402), with the reason the customer reads in the
    design's note and in step 1's DDL file."""
    reason = (
        f"the table already has an index named {kept}, so this one was not created; "
        "create it under another name if the queries need it"
    )
    return {"table": table, "kind": "index", "name": name, "reason": reason,
            "feature": "duplicate_index_name"}  # fmt: skip


def quotable_tables(
    tables: list[AgentTable], dialect: Dialect, source_engine: str | None, qualified: bool = False
) -> tuple[list[AgentTable], list[dict]]:
    """``tables`` with every object whose name cannot be quoted into SQL taken out (#2381
    review 2, ``sql_identifiers``), and each one taken out as a ``DdlResult.left_out``
    record: a table, a column (and a primary key that names one), an index whose name or
    columns hold such a character, a foreign key whose name, columns or referenced names do.
    A table's index whose name an earlier index of the table already has is taken out too
    (#2402): one table cannot hold two indexes of one name, and the merge pairs statements
    with names. ``dialect`` decides what one name is: Aurora MySQL compares index names
    without case; Aurora PostgreSQL, where the draft quotes every name, compares them as
    written, so ``Ix_A`` and ``ix_a`` are two indexes there.
    ``AuroraDesignBase`` applies it once, so the draft, the merge, a saved design's check
    and step 1 all read the same quotable tables. With ``qualified`` (#2455) each record
    names the table ``design_table_name`` writes, and a table whose schema name cannot be
    quoted is left out too; so is a foreign key whose bare referenced table two other
    schemas have and its own does not (#2479 review). Every table whose schema and
    name (``table_keys.table_key``, case and quote marks folded) another table has is left
    out, in a design of one schema too (#2478, ``Orders`` and ``orders``); two schemas'
    tables whose names differ only by case are both kept, also in step 1's base, which
    keys tables by their bare names. So is every table of a system
    schema of ``source_engine`` (``table_keys.is_system_schema``, #2478: ``sys`` is one on
    MySQL, a user schema on PostgreSQL)."""
    out: list[AgentTable] = []
    left_out: list[dict] = []

    def key(t: AgentTable) -> str:
        return table_key(t.table_name, t.schema_name)

    designable = [t for t in tables if not is_system_schema(t.schema_name, source_engine)]
    shared = colliding_keys((t.table_name, t.schema_name) for t in designable)
    kept_tables = [t for t in designable if key(t) not in shared]
    schemas_of = _schemas_of(kept_tables) if qualified else {}
    for source in tables:
        label = design_table_name(source, qualified)
        if is_system_schema(source.schema_name, source_engine):
            left_out.append(_system_table(source))
            continue
        if key(source) in shared:
            left_out.append(_colliding_table(label, qualified))
            continue
        if has_unprintable(label):
            left_out.append(_unsafe(label, "table", label))
            continue
        table = _without_unsafe_columns(source, left_out, label)
        if source.columns and not table.columns:
            # Every column left out: no CREATE TABLE has anything to declare.
            left_out.append(
                {**_unsafe(label, "table", label),
                 "reason": "no column of it has a name SQL may quote: " + UNSAFE_NAME_REASON}
            )  # fmt: skip
            continue
        indexes = []
        kept: dict[str, str] = {}  # one name (folded on Aurora MySQL) -> the index kept
        for idx in table.indexes or []:
            names = [idx.index_name, *idx.columns, *(idx.included_columns or [])]
            name = idx.index_name.lower() if dialect is MYSQL else idx.index_name
            if any(has_unprintable(n) for n in names):
                left_out.append(_unsafe(label, "index", idx.index_name))
            elif name in kept:
                left_out.append(_duplicate_index(label, idx.index_name, kept[name]))
            else:
                kept[name] = idx.index_name
                indexes.append(idx)
        fks = []
        for fk in table.foreign_keys or []:
            names = [fk.constraint_name, *fk.columns, fk.referenced_table, *fk.referenced_columns]
            if any(has_unprintable(n) for n in names):
                left_out.append(_unsafe(label, "foreign key", fk.constraint_name))
            elif (
                qualified
                and _referenced(fk.referenced_table, source.schema_name, schemas_of) is None
            ):
                found = schemas_of.get(fk.referenced_table.lower(), [])
                left_out.append(_ambiguous_reference(label, fk, found))
            else:
                fks.append(fk)
        if len(indexes) != len(table.indexes or []) or len(fks) != len(table.foreign_keys or []):
            table = table.model_copy(
                update={"indexes": indexes if table.indexes is not None else None,
                        "foreign_keys": fks or None}
            )  # fmt: skip
        out.append(table)
    return out, left_out


def _without_unsafe_columns(
    table: AgentTable, left_out: list[dict], label: str | None = None
) -> AgentTable:
    """``table`` without the columns whose name cannot be quoted, each left out (under
    ``label``, the design's name for it, default its name); its primary key too when it
    names one of them. Its indexes and foreign keys are kept: the one that names such a
    column is left out where it is rendered."""
    unsafe = [c for c in table.columns if has_unprintable(c.column_name)]
    if not unsafe:
        return table
    label = label or table.table_name
    left_out += [_unsafe(label, "column", c.column_name) for c in unsafe]
    names = {c.column_name for c in unsafe}
    update: dict = {"columns": [c for c in table.columns if c.column_name not in names]}
    if any(c in names for c in table.primary_key or []):
        left_out.append(_unsafe(label, "primary key", "PRIMARY KEY"))
        update["primary_key"] = []
    return table.model_copy(update=update)


def assemble_full_ddl(table_ddls: list[TableDDL]) -> str:
    """Each schema's CREATE SCHEMA (a design of several schemas, #2455), then all CREATE
    TABLEs, then all indexes, then all FKs (so ordering never breaks a reference)."""
    parts: list[str] = list(dict.fromkeys(t.schema_sql for t in table_ddls if t.schema_sql))
    parts += [t.create_sql for t in table_ddls]
    for t in table_ddls:
        parts.extend(t.index_sql)
    for t in table_ddls:
        parts.extend(t.fk_sql)
    return "\n\n".join(parts)


def generate_pg_ddl(
    tables: list[AgentTable],
    type_overrides: TypeOverrides | None = None,
    source_engine: str = "",
    qualified: bool = False,
) -> DdlResult:
    """Translate normalized source tables into Aurora PostgreSQL DDL.

    ``type_overrides`` maps ``(table_name, column_name)`` to a model-decided
    type (#273); those columns are emitted with it and are not residuals.
    ``source_engine`` is the collector's source engine: for a PostgreSQL or
    MySQL source each column's native ``data_type`` is mapped first (#274,
    ``source_types``), and the normalized type is the fallback. ``qualified`` creates
    each table in its source schema (#2455).
    """
    return _generate(tables, POSTGRES, type_overrides, source_engine, qualified)


def generate_mysql_ddl(
    tables: list[AgentTable],
    type_overrides: TypeOverrides | None = None,
    source_engine: str = "",
    qualified: bool = False,
) -> DdlResult:
    """Translate normalized source tables into Aurora MySQL DDL (see ``generate_pg_ddl``):
    with ``qualified``, the schema of each table is its database."""
    return _generate(tables, MYSQL, type_overrides, source_engine, qualified)
