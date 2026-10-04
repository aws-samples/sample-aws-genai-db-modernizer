"""Map a PostgreSQL or MySQL source ``data_type`` to an Aurora type (issue #274).

The normalized type (``type_map``) is lossy (``integer`` covers INT and BIGINT)
and is missing from some collections, so when the source is PostgreSQL or
MySQL the draft resolves each column from its native ``data_type`` first:

* same engine (``carry_over``): the type is kept exactly, with aliases spelled
  the canonical way (PostgreSQL ``int4``/``integer`` -> ``INTEGER``,
  ``character varying(n)`` -> ``VARCHAR(n)``, ``timestamp without time zone``
  -> ``TIMESTAMP``, ``_int4`` -> ``INTEGER[]``; MySQL ``bigint(20) unsigned``
  -> ``BIGINT UNSIGNED``, ``tinyint(1)`` and ``enum(...)`` as written).
  Array udt names carry no element length, so ``_bpchar`` -> ``TEXT[]`` and
  ``_bit`` -> ``VARBIT[]`` rather than truncating ``CHAR[]`` / ``BIT[]``;
* cross engine (``translate``): through the tables below.

Every result is checked against the target engine's type allowlist
(``validate_aurora_type``), so collector text never reaches DDL unchecked.
``None`` means "no source mapping"; the caller then falls back to the
normalized type. A ``TypeResolution`` with ``needs_judgment`` is a genuine
residual (for example a PostgreSQL ``tsvector`` on Aurora MySQL).

PostgreSQL -> Aurora MySQL
    smallint/integer/bigint -> SMALLINT/INT/BIGINT; real -> FLOAT;
    double precision -> DOUBLE; numeric(p,s) -> DECIMAL(p,s) (bare numeric is
    a residual: MySQL DECIMAL needs a precision); money -> DECIMAL(19,2);
    boolean -> BOOLEAN; char(n) -> CHAR(n) (VARCHAR(n) above 255);
    varchar(n) -> VARCHAR(n) (LONGTEXT above 16383); text, citext and
    unbounded varchar -> LONGTEXT; bytea -> LONGBLOB; date -> DATE;
    timestamp[tz](p) -> DATETIME(p) (MySQL TIMESTAMP stops in 2038; the tz
    variants carry a "store UTC" app-layer note); time[tz](p) -> TIME(p); json/jsonb/hstore/arrays -> JSON; uuid -> CHAR(36);
    inet/cidr -> VARCHAR(43); macaddr -> VARCHAR(17); macaddr8 -> VARCHAR(23);
    xml -> LONGTEXT; bit(n) -> BIT(n); oid -> INT UNSIGNED;
    geometry/geography -> GEOMETRY. A LONGTEXT/LONGBLOB/JSON/GEOMETRY column,
    or a VARCHAR over 768 characters, that is part of a key or index is a
    residual (MySQL cannot index it directly), and so is a LONGTEXT/LONGBLOB/
    JSON/GEOMETRY column with a DEFAULT (MySQL 8 takes only an expression
    default there). interval, tsvector, tsquery,
    ranges, geometric types, vectors, varbit and user-defined types are
    residuals.

MySQL -> Aurora PostgreSQL
    tinyint (incl. tinyint(1)) and smallint -> SMALLINT (smallint unsigned ->
    INTEGER); mediumint -> INTEGER; int -> INTEGER (unsigned -> BIGINT);
    bigint -> BIGINT; bigint unsigned -> NUMERIC(20,0), except in a key,
    index or foreign key, where it stays BIGINT (identity columns and foreign
    keys need an integer type; values above 2^63-1 do not fit there); decimal(p,s) -> NUMERIC(p,s) (bare
    decimal -> NUMERIC(10,0), MySQL's default); float -> REAL;
    double/real -> DOUBLE PRECISION; bit(n) -> BIT(n); bool -> BOOLEAN;
    char/varchar(n) -> CHAR/VARCHAR(n); *text -> TEXT; binary, varbinary and
    *blob -> BYTEA; date -> DATE; datetime(p) -> TIMESTAMP(p);
    timestamp(p) -> TIMESTAMPTZ(p); time(p) -> TIME(p); year -> SMALLINT;
    json -> JSONB; enum(...) -> VARCHAR(longest label); set(...) -> TEXT.
    Spatial types are residuals (PostGIS is a design decision).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.contracts.aurora_design_delta import is_enum_type, validate_aurora_type
from src.tools.schema.aurora_common.type_map import TypeResolution

_LITERAL = re.compile(r"'((?:[^'\\\n\r]|'')*)'")
_SHAPE = re.compile(
    r"(?P<name>[a-z][a-z0-9_]*(?: [a-z][a-z0-9_]*)*?)"
    r"\s*(?:\(\s*(?P<p1>\d+)\s*(?:,\s*(?P<p2>\d+)\s*)?\))?"
    r"(?P<post>(?: [a-z][a-z0-9_]*)*)"
    r"(?P<arrays>(?:\s*\[\d*\])*)"
)


@dataclass(frozen=True)
class _Parsed:
    base: str  # canonical lower-case base name for the source family
    params: tuple[int, ...]
    arrays: int = 0
    unsigned: bool = False
    zerofill: bool = False
    labels: tuple[str, ...] = ()  # MySQL ENUM/SET labels
    raw: str = ""


def _split(raw: str):
    text = " ".join(raw.strip().lower().split())
    match = _SHAPE.fullmatch(text)
    if match is None:
        return None
    params = tuple(int(p) for p in (match.group("p1"), match.group("p2")) if p is not None)
    words = match.group("name").split() + match.group("post").split()
    arrays = match.group("arrays").count("[")
    return words, params, arrays


# --- PostgreSQL ------------------------------------------------------------

_PG_ALIASES = {
    "int2": "smallint",
    "smallint": "smallint",
    "smallserial": "smallint",
    "serial2": "smallint",
    "int": "integer",
    "int4": "integer",
    "integer": "integer",
    "serial": "integer",
    "serial4": "integer",
    "int8": "bigint",
    "bigint": "bigint",
    "bigserial": "bigint",
    "serial8": "bigint",
    "float4": "real",
    "real": "real",
    "float8": "double precision",
    "double precision": "double precision",
    "numeric": "numeric",
    "decimal": "numeric",
    "money": "money",
    "bool": "boolean",
    "boolean": "boolean",
    "varchar": "varchar",
    "character varying": "varchar",
    "bpchar": "char",
    "char": "char",
    "character": "char",
    "text": "text",
    "citext": "citext",
    "name": "varchar",
    "bytea": "bytea",
    "timestamp": "timestamp",
    "timestamp without time zone": "timestamp",
    "timestamptz": "timestamptz",
    "timestamp with time zone": "timestamptz",
    "time": "time",
    "time without time zone": "time",
    "timetz": "timetz",
    "time with time zone": "timetz",
    "date": "date",
    "interval": "interval",
    "bit": "bit",
    "varbit": "varbit",
    "bit varying": "varbit",
}
# Base names carried over as written (already canonical and allowlisted).
_PG_VERBATIM = frozenset(
    """json jsonb uuid inet cidr macaddr macaddr8 xml tsvector tsquery hstore oid point line
    lseg box path polygon circle int4range int8range numrange tsrange tstzrange daterange
    geometry geography vector halfvec sparsevec""".split()
)
_PG_RENDER = {
    "smallint": "SMALLINT",
    "integer": "INTEGER",
    "bigint": "BIGINT",
    "real": "REAL",
    "double precision": "DOUBLE PRECISION",
    "numeric": "NUMERIC",
    "money": "MONEY",
    "boolean": "BOOLEAN",
    "varchar": "VARCHAR",
    "char": "CHAR",
    "text": "TEXT",
    "citext": "CITEXT",
    "bytea": "BYTEA",
    "timestamp": "TIMESTAMP",
    "timestamptz": "TIMESTAMPTZ",
    "time": "TIME",
    "timetz": "TIMETZ",
    "date": "DATE",
    "interval": "INTERVAL",
    "bit": "BIT",
    "varbit": "VARBIT",
}
_PG_LENGTH_TYPES = {"varchar", "char", "bit", "varbit"}
_PG_PRECISION_TYPES = {"numeric", "timestamp", "timestamptz", "time", "timetz", "interval"}
_PG_ARRAY_HINTS = {"array"}  # information_schema says ARRAY without the element type


def _parse_pg(raw: str, max_length: int | None) -> _Parsed | None:
    text = raw.strip()
    arrays = 0
    if text.startswith("_"):  # udt_name of an array: _int4, _varchar
        text, arrays = text[1:], 1
    split = _split(text)
    if split is None:
        return None
    words, params, more = split
    arrays += more
    name = " ".join(words)
    if name in _PG_ARRAY_HINTS:
        return None
    if name.startswith("interval "):  # interval day to second, ...
        name = "interval"
    base = _PG_ALIASES.get(name) or (name if name in _PG_VERBATIM else None)
    if base is None:
        return None
    if arrays and not params:
        # An array's udt_name carries no element length: char[] would become
        # char(1)[] and bit[] bit(1)[], truncating every element.
        base = {"char": "text", "bit": "varbit"}.get(base, base)
    elif base in _PG_LENGTH_TYPES and not params and max_length and max_length > 0:
        params = (max_length,)
    return _Parsed(base=base, params=params, arrays=arrays, raw=raw)


def _render_pg(p: _Parsed) -> str:
    name = _PG_RENDER.get(p.base, p.base.upper())
    keep_params = p.base in _PG_LENGTH_TYPES | _PG_PRECISION_TYPES or p.base in {
        "vector",
        "halfvec",
        "sparsevec",
        "geometry",
    }
    params = f"({','.join(map(str, p.params))})" if p.params and keep_params else ""
    return f"{name}{params}{'[]' * p.arrays}"


# --- MySQL -----------------------------------------------------------------

_MYSQL_ALIASES = {
    "tinyint": "tinyint",
    "smallint": "smallint",
    "mediumint": "mediumint",
    "int": "int",
    "integer": "int",
    "bigint": "bigint",
    "decimal": "decimal",
    "dec": "decimal",
    "numeric": "decimal",
    "fixed": "decimal",
    "float": "float",
    "double": "double",
    "double precision": "double",
    "real": "double",
    "bit": "bit",
    "bool": "boolean",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    "timestamp": "timestamp",
    "time": "time",
    "year": "year",
    "char": "char",
    "nchar": "char",
    "national char": "char",
    "varchar": "varchar",
    "nvarchar": "varchar",
    "national varchar": "varchar",
    "binary": "binary",
    "varbinary": "varbinary",
    "tinyblob": "tinyblob",
    "blob": "blob",
    "mediumblob": "mediumblob",
    "longblob": "longblob",
    "tinytext": "tinytext",
    "text": "text",
    "mediumtext": "mediumtext",
    "longtext": "longtext",
    "json": "json",
}
_MYSQL_SPATIAL = frozenset(
    """geometry point linestring polygon multipoint multilinestring multipolygon
    geometrycollection""".split()
)
_MYSQL_INTEGERS = {"tinyint", "smallint", "mediumint", "int", "bigint"}
_MYSQL_NUMERIC_SIGN = _MYSQL_INTEGERS | {"decimal", "float", "double"}
_MYSQL_ENUM = re.compile(r"(?P<kind>enum|set)\s*\((?P<body>.*)\)", re.IGNORECASE | re.DOTALL)


def _parse_mysql(raw: str, max_length: int | None) -> _Parsed | None:
    text = " ".join(raw.strip().split())
    enum = _MYSQL_ENUM.fullmatch(text)
    if enum:
        labels = tuple(m.replace("''", "'") for m in _LITERAL.findall(enum.group("body")))
        return _Parsed(base=enum.group("kind").lower(), params=(), labels=labels, raw=text)
    split = _split(text)
    if split is None:
        return None
    words, params, arrays = split
    if arrays:
        return None
    flags = {w for w in words if w in ("unsigned", "signed", "zerofill")}
    name = " ".join(w for w in words if w not in flags)
    base = _MYSQL_ALIASES.get(name) or (name if name in _MYSQL_SPATIAL else None)
    if base is None:
        return None
    if base in ("char", "varchar", "binary", "varbinary") and not params:
        if max_length and max_length > 0:
            params = (max_length,)
    return _Parsed(
        base=base,
        params=params,
        unsigned="unsigned" in flags or "zerofill" in flags,
        zerofill="zerofill" in flags,
        raw=text,
    )


def _render_mysql(p: _Parsed) -> str | None:
    if p.base in ("enum", "set"):
        return p.raw[: len(p.base)].upper() + p.raw[len(p.base) :] if p.labels else None
    if p.base in ("varchar", "varbinary") and not p.params:
        return None  # MySQL requires a length
    name = p.base.upper()
    params = p.params
    if p.base in _MYSQL_INTEGERS:
        # Display widths are deprecated; tinyint(1) keeps its boolean convention.
        params = (1,) if p.base == "tinyint" and params == (1,) else ()
    elif p.base in ("float", "double") and len(params) == 2:
        params = ()  # FLOAT(M,D)/DOUBLE(M,D) are deprecated
    elif p.base == "boolean" or p.base in _MYSQL_SPATIAL or p.base.endswith(("blob", "text")):
        params = ()
    suffix = ""
    if p.base in _MYSQL_NUMERIC_SIGN and p.unsigned:
        suffix = " UNSIGNED ZEROFILL" if p.zerofill else " UNSIGNED"
    rendered = f"({','.join(map(str, params))})" if params else ""
    return f"{name}{rendered}{suffix}"


# --- Cross engine -----------------------------------------------------------

_MYSQL_VARCHAR_MAX = 16383  # utf8mb4 characters in a 65,535-byte row
_MYSQL_INDEX_CHARS = 768  # 3072-byte InnoDB key / 4 bytes per utf8mb4 character
_PG_TO_MYSQL_FIXED = {
    "smallint": "SMALLINT",
    "integer": "INT",
    "bigint": "BIGINT",
    "real": "FLOAT",
    "double precision": "DOUBLE",
    "money": "DECIMAL(19,2)",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "json": "JSON",
    "jsonb": "JSON",
    "hstore": "JSON",
    "uuid": "CHAR(36)",
    "inet": "VARCHAR(43)",
    "cidr": "VARCHAR(43)",
    "macaddr": "VARCHAR(17)",
    "macaddr8": "VARCHAR(23)",
    "oid": "INT UNSIGNED",
    "geometry": "GEOMETRY",
    "geography": "GEOMETRY",
}
_PG_TO_MYSQL_LONG = {"text": "LONGTEXT", "citext": "LONGTEXT", "xml": "LONGTEXT"}


def _residual(fallback: str, reason: str) -> TypeResolution:
    return TypeResolution(aurora_type=fallback, needs_judgment=True, reason=reason)


# Aurora MySQL types that cannot be (fully) indexed and take no literal DEFAULT
# (MySQL 8 accepts only an expression default on them).
_MYSQL_UNINDEXABLE = ("LONGTEXT", "LONGBLOB", "JSON", "GEOMETRY")
_UTC_NOTE = (
    "Aurora MySQL DATETIME has no time zone; the application must write and read "
    "these values in UTC (PostgreSQL {kind} converted them on the way in)."
)


def _pg_to_mysql_type(p: _Parsed, indexed: bool) -> TypeResolution | str | None:
    if p.arrays:
        return "JSON"
    if p.base in _PG_TO_MYSQL_FIXED:
        return _PG_TO_MYSQL_FIXED[p.base]
    if p.base == "numeric":
        if p.params and p.params[0] <= 65 and (len(p.params) < 2 or p.params[1] <= 30):
            return f"DECIMAL({','.join(map(str, p.params))})"
        return _residual(
            "DECIMAL(38,10)",
            "PostgreSQL numeric has no precision/scale that fits Aurora MySQL; "
            "confirm DECIMAL(p,s).",
        )
    if p.base == "varchar":
        if not p.params or p.params[0] > _MYSQL_VARCHAR_MAX:
            return "LONGTEXT"
        if indexed and p.params[0] > _MYSQL_INDEX_CHARS:
            return _residual(
                f"VARCHAR({_MYSQL_INDEX_CHARS})",
                f"Indexed varchar({p.params[0]}) exceeds the Aurora MySQL index key length; "
                "confirm a shorter VARCHAR or a prefix index.",
            )
        return f"VARCHAR({p.params[0]})"
    if p.base == "char":
        n = p.params[0] if p.params else 1
        return f"CHAR({n})" if n <= 255 else f"VARCHAR({n})"
    if p.base in _PG_TO_MYSQL_LONG:
        return _PG_TO_MYSQL_LONG[p.base]
    if p.base == "bytea":
        return "LONGBLOB"
    if p.base in ("timestamp", "timestamptz", "time", "timetz"):
        precision = min(p.params[0], 6) if p.params else 6
        name = "DATETIME" if p.base.startswith("timestamp") else "TIME"
        if p.base in ("timestamptz", "timetz"):
            return TypeResolution(
                f"{name}({precision})", needs_judgment=False, note=_UTC_NOTE.format(kind=p.base)
            )
        return f"{name}({precision})"
    if p.base == "bit" and (not p.params or p.params[0] <= 64):
        return f"BIT({p.params[0] if p.params else 1})"
    return None


def _pg_to_mysql(p: _Parsed, indexed: bool, has_default: bool) -> TypeResolution | str | None:
    result = _pg_to_mysql_type(p, indexed)
    aurora_type = result.aurora_type if isinstance(result, TypeResolution) else result
    if aurora_type not in _MYSQL_UNINDEXABLE:
        return result
    what = "array" if p.arrays else p.base
    if indexed:
        return _residual(
            "VARCHAR(255)",
            f"PostgreSQL {what} column is part of a key or index, but its Aurora MySQL type "
            f"{aurora_type} cannot be indexed directly; confirm a VARCHAR(n) length, a "
            "generated column or a prefix/functional index.",
        )
    if has_default:
        return _residual(
            aurora_type,
            f"PostgreSQL {what} column has a DEFAULT, but Aurora MySQL {aurora_type} columns "
            "take only an expression default; confirm DEFAULT (expr) or drop it.",
        )
    return result


def _mysql_to_pg(p: _Parsed, indexed: bool) -> str | None:
    if p.base in ("tinyint", "smallint"):
        return "INTEGER" if p.base == "smallint" and p.unsigned else "SMALLINT"
    if p.base == "mediumint":
        return "INTEGER"
    if p.base == "int":
        return "BIGINT" if p.unsigned else "INTEGER"
    if p.base == "bigint":
        # Keys stay BIGINT (identity columns and foreign keys need an integer
        # type); other unsigned values keep their full range.
        return "NUMERIC(20,0)" if p.unsigned and not indexed else "BIGINT"
    if p.base == "decimal":
        return f"NUMERIC({','.join(map(str, p.params))})" if p.params else "NUMERIC(10,0)"
    if p.base == "float":
        return "REAL"
    if p.base == "double":
        return "DOUBLE PRECISION"
    if p.base == "bit":
        return f"BIT({p.params[0] if p.params else 1})"
    if p.base == "boolean":
        return "BOOLEAN"
    if p.base in ("char", "varchar"):
        if not p.params:
            return "TEXT" if p.base == "varchar" else "CHAR(1)"
        return f"{p.base.upper()}({p.params[0]})"
    if p.base.endswith("text"):
        return "TEXT"
    if p.base in ("binary", "varbinary") or p.base.endswith("blob"):
        return "BYTEA"
    if p.base in ("date", "json"):
        return "JSONB" if p.base == "json" else "DATE"
    if p.base in ("datetime", "timestamp", "time"):
        name = {"datetime": "TIMESTAMP", "timestamp": "TIMESTAMPTZ", "time": "TIME"}[p.base]
        return f"{name}({p.params[0]})" if p.params else name
    if p.base == "year":
        return "SMALLINT"
    if p.base == "enum":
        longest = max((len(label) for label in p.labels), default=1)
        return f"VARCHAR({max(longest, 1)})"
    if p.base == "set":
        return "TEXT"
    return None


# --- Entry point ------------------------------------------------------------


def _checked(aurora_type: str, target: str) -> TypeResolution | None:
    """Only allowlisted types reach DDL; anything else falls back to the normalized type."""
    try:
        if is_enum_type(aurora_type) and target != "aurora_mysql":
            return None
        return TypeResolution(validate_aurora_type(aurora_type, target), needs_judgment=False)
    except ValueError:
        return None


def resolve_source_type(
    data_type: str | None,
    *,
    source_family: str,
    target: str,
    max_length: int | None = None,
    indexed: bool = False,
    has_default: bool = False,
) -> TypeResolution | None:
    """Aurora type for a PostgreSQL/MySQL source ``data_type``, or ``None`` if unmapped.

    ``indexed`` says the column is part of the primary key, an index or a
    foreign key (Aurora MySQL cannot index unbounded text); ``has_default``
    that it has a DEFAULT (Aurora MySQL TEXT/BLOB/JSON/GEOMETRY take none).
    """
    if not data_type or source_family not in ("postgresql", "mysql"):
        return None
    result: TypeResolution | str | None
    if source_family == "postgresql":
        parsed = _parse_pg(data_type, max_length)
        if parsed is None:
            return None
        if target == "aurora_postgresql":
            result = _render_pg(parsed)
        else:
            result = _pg_to_mysql(parsed, indexed, has_default)
    else:
        parsed = _parse_mysql(data_type, max_length)
        if parsed is None:
            return None
        if target == "aurora_mysql":
            result = _render_mysql(parsed)
        else:
            result = _mysql_to_pg(parsed, indexed)
    if result is None:
        return None
    if isinstance(result, TypeResolution):
        if result.needs_judgment:
            return result
        checked = _checked(result.aurora_type, target)
        return checked and TypeResolution(checked.aurora_type, False, note=result.note)
    return _checked(result, target)
