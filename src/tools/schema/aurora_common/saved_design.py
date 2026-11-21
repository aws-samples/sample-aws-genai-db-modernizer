# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A saved Aurora design's DDL strings, rebuilt from checked parts on every read (#2199).

A saved ``schema-aurora_*/[v<N>/]schema_output.json`` carries three kinds of SQL text
besides its column types (checked by ``check_design_column_types``, #2155): each table's
``indexes`` and ``foreign_keys`` statements and the whole ``generated_ddl`` script. The UI
shows them and the assess() result hands them on, so none is trusted as stored:

- each index and foreign key is parsed into its parts (quoted identifiers, the access
  method, key prefixes, INCLUDE columns, the partial-index predicate, the ON DELETE
  action) and rendered again with the generator's own ``render_index`` /
  ``render_foreign_key``; the predicate passes ``render_predicate``'s grammar against the
  table's columns, the statement must name the table it is listed under, and its key,
  INCLUDE and foreign-key columns must be that table's;
- ``generated_ddl`` is rebuilt as the generator assembles it: one ``CREATE TABLE`` per
  table definition, from its checked column names, types and primary key (only each
  column's identity, ``NOT NULL`` and ``DEFAULT`` clause, which the table definitions do
  not carry, are read from the stored script, under the closed grammar
  ``constraint_translator`` writes), then each table's checked index statements, then
  the checked foreign keys. On Aurora MySQL a table's indexes may include the one-line
  comment that stands in for an index the key limit rules out, but only exactly as
  ``skipped_index_comment`` writes it for an ``index_key_prefix`` note the merge wrote
  for that table, and at most once per note;
- ``generated_ddl`` may close with one ``-- Not created: <name>: <reason>`` comment per
  object the design leaves out (#2484), but only exactly as
  ``left_out_objects.with_not_created_comments`` writes them from the design's own
  app-layer notes; a design saved before that has none;
- SQL text with nothing to rebuild it from is refused: a design that carries
  ``generated_ddl``, ``indexes`` or ``foreign_keys`` without a list of table definitions,
  and a skipped design (``check_skipped_design``) that carries any of them or tables.

A table definition with a ``schema_name`` (a design of several schemas, #2455) is created in
that schema: its statements name ``"schema"."table"``, the script opens with one
``CREATE SCHEMA IF NOT EXISTS`` per schema, and notes name it ``schema.table``. A design
saved before that (two schemas' tables of one name sharing ``CREATE TABLE "orders"``) has
no ``schema_name`` and is checked as it was written.

Each table's ``ddl_statements`` (its own cut of that script, #2325) is checked against the
rebuilt cut the same way. A string that does not come back identical is refused (:class:`SavedDesignError`, with
its position and no stored text), so a design the generator wrote, in any release that
wrote this format, loads unchanged, and an edited one never reaches a deliverable.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Mapping
from typing import Any

from src.contracts.aurora_design_delta import SavedDesignError
from src.contracts.schema_design_input import ForeignKeyAction
from src.shared.left_out_objects import without_not_created_comments
from src.shared.table_keys import table_schema
from src.tools.schema.aurora_common.ddl_generator import (
    MYSQL,
    NOT_CREATED_REASON,
    POSTGRES,
    SKIPPED_INDEX_PREFIX,
    SKIPPED_INDEX_SUFFIX,
    Dialect,
    create_schema_sql,
    quote_table,
    render_create_table,
    render_foreign_key,
    render_index,
    skipped_index_comment,
)
from src.tools.schema.aurora_common.sql_safety import SqlFragmentError, render_predicate

DIALECTS = {"aurora_postgresql": POSTGRES, "aurora_mysql": MYSQL}

DDL_KIND = "DDL script"
INDEX_KIND = "index definition"
FK_KIND = "foreign key definition"
_KINDS = {"generated_ddl": DDL_KIND, "indexes": INDEX_KIND, "foreign_keys": FK_KIND}


class _Refused(ValueError):
    """A stored string that does not rebuild; the message names no stored text."""


# ---------------------------------------------------------------------------
# Quoted identifiers
# ---------------------------------------------------------------------------


def _qid(dialect: Dialect) -> str:
    """A quoted identifier as ``Dialect.q`` writes it: the quote character doubled inside."""
    q = re.escape(dialect.quote_char)
    return rf"{q}(?:[^{q}]|{q}{q})*+{q}"


def _unquote(dialect: Dialect, token: str) -> str:
    c = dialect.quote_char
    return token[1:-1].replace(c + c, c)


def _key(dialect: Dialect, prefixes: bool) -> str:
    return rf"({_qid(dialect)})" + (r"(?:\((\d+)\))?" if prefixes else "")


def _key_list(dialect: Dialect, *, prefixes: bool = False) -> re.Pattern[str]:
    part = _key(dialect, prefixes)
    return re.compile(rf"{part}(?:, {part})*")


def _names(dialect: Dialect, text: str, *, prefixes: bool = False) -> list[tuple[str, int | None]]:
    """``"a", "b"(10)``: each identifier and its key prefix (only when ``prefixes``)."""
    part = _key(dialect, prefixes)
    if not _key_list(dialect, prefixes=prefixes).fullmatch(text):
        raise _Refused("is not a list of quoted identifiers")
    return [
        (_unquote(dialect, m.group(1)), int(m.group(2)) if prefixes and m.group(2) else None)
        for m in re.finditer(part, text)
    ]


# ---------------------------------------------------------------------------
# Index and foreign-key statements
# ---------------------------------------------------------------------------

_MYSQL_KINDS = {"FULLTEXT ": "fulltext", "SPATIAL ": "spatial"}


def _table_name(dialect: Dialect) -> str:
    """A table as ``quote_table`` writes it: ``"table"`` or ``"schema"."table"`` (#2455)."""
    qid = _qid(dialect)
    return rf"{qid}(?:\.{qid})?"


def _index_shape(dialect: Dialect) -> re.Pattern[str]:
    qid = _qid(dialect)
    key = rf"{qid}(?:\(\d+\))?"
    return re.compile(
        r"CREATE (?P<kind>UNIQUE |FULLTEXT |SPATIAL )?INDEX "
        rf"(?P<name>{qid}) ON (?P<table>{_table_name(dialect)})(?: USING (?P<method>[a-z]+))? "
        rf"\((?P<cols>{key}(?:, {key})*)\)"
        rf"(?: INCLUDE \((?P<include>{qid}(?:, {qid})*)\))?"
        r"(?: WHERE (?P<where>[^\n]+))?;"
    )


_INDEX_SHAPES = {name: _index_shape(d) for name, d in DIALECTS.items()}


def check_index(
    dialect: Dialect,
    table: str,
    columns: list[str],
    statement: object,
    schema: str | None = None,
) -> str:
    """``statement`` re-rendered from its parsed parts; refused unless it comes back the same.
    ``schema``: the schema the table definition is created in (#2455), if any."""
    if not isinstance(statement, str):
        raise _Refused("is not a string")
    match = _INDEX_SHAPES[dialect.name].fullmatch(statement)
    if match is None:
        raise _Refused("is not a CREATE INDEX statement the DDL generator writes")
    if match["table"] != _quoted_table(dialect, table, schema):
        raise _Refused("is not on the table it is listed under")
    kind = match["kind"] or ""
    keys = _names(dialect, match["cols"], prefixes=dialect is MYSQL)
    if dialect is POSTGRES and kind in _MYSQL_KINDS:
        raise _Refused("uses an Aurora MySQL-only index kind")
    if dialect is MYSQL and (match["include"] or match["where"]):
        raise _Refused("uses Aurora PostgreSQL-only index syntax")
    if kind in _MYSQL_KINDS and match["method"]:
        raise _Refused("names both an index kind and an access method")
    include = [n for n, _ in _names(dialect, match["include"])] if match["include"] else []
    known = {c.lower() for c in columns}
    if any(n.lower() not in known for n, _ in keys) or any(n.lower() not in known for n in include):
        raise _Refused("indexes a column the table definition does not have")
    where_sql = None
    if match["where"]:
        try:
            where_sql = render_predicate(match["where"], columns, dialect.q)
        except SqlFragmentError as exc:
            raise _Refused("has a partial-index predicate outside the grammar") from exc
    try:
        rebuilt = render_index(
            dialect,
            table,
            _unquote(dialect, match["name"]),
            [name for name, _ in keys],
            unique=kind == "UNIQUE ",
            method=_MYSQL_KINDS.get(kind) or match["method"],
            include=include or None,
            where_sql=where_sql,
            prefix_lengths=[n for _, n in keys] if dialect is MYSQL else None,
            schema=schema,
        )
    except SqlFragmentError as exc:
        raise _Refused("does not render as written") from exc
    if rebuilt != statement:
        raise _Refused("does not render as written")
    return rebuilt


_ACTIONS = {a.value: a for a in ForeignKeyAction}


def _all_in(names: list[str], columns: list[str]) -> bool:
    """Each of ``names`` is one of ``columns``, ignoring case (a collector may report a key
    column in another case than the column itself)."""
    known = {c.lower() for c in columns}
    return all(n.lower() in known for n in names)


def _fk_shape(dialect: Dialect) -> re.Pattern[str]:
    qid = _qid(dialect)
    names = rf"{qid}(?:, {qid})*"
    actions = "|".join(re.escape(a) for a in _ACTIONS)
    return re.compile(
        rf"ALTER TABLE (?P<table>{_table_name(dialect)}) ADD CONSTRAINT (?P<name>{qid}) "
        rf"FOREIGN KEY \((?P<cols>{names})\) REFERENCES (?:(?P<refschema>{qid})\.)?(?P<ref>{qid}) "
        rf"\((?P<refcols>{names})\)"
        rf"(?: ON DELETE (?P<action>{actions}))?;"
    )


def _design_name(table: str, schema: str | None) -> str:
    """The table as the design's notes and keys name it (``ddl_generator.design_table_name``)."""
    return f"{schema}.{table}" if schema else table


def _schema(table: Mapping[str, Any]) -> str | None:
    """A table definition's ``schema_name`` (#2455): a non-empty string, or None."""
    return table_schema(table)


def _quoted_table(dialect: Dialect, table: str, schema: str | None) -> str:
    """The table as its statements must name it; a name that cannot be quoted names none."""
    try:
        return quote_table(dialect, table, schema)
    except ValueError as exc:
        raise _Refused("belongs to a table whose name cannot be quoted") from exc


_FK_SHAPES = {name: _fk_shape(d) for name, d in DIALECTS.items()}


def check_foreign_key(
    dialect: Dialect,
    table: str,
    columns: dict[str, list[str]],
    statement: object,
    schema: str | None = None,
) -> str:
    """``statement`` re-rendered from its parsed parts; refused unless it comes back the same.
    ``columns`` maps each table of the design (by ``_design_name``) to its columns: the
    key's own columns must be the table's. Its referenced columns are not checked: the
    generator writes them as the collector reports them, and the referenced table is often
    not in this design. ``schema`` as for ``check_index``: only a table created in a schema
    references a table in one."""
    if not isinstance(statement, str):
        raise _Refused("is not a string")
    match = _FK_SHAPES[dialect.name].fullmatch(statement)
    if match is None:
        raise _Refused("is not a FOREIGN KEY statement the DDL generator writes")
    if match["table"] != _quoted_table(dialect, table, schema):
        raise _Refused("is not on the table it is listed under")
    if match["refschema"] is not None and schema is None:
        raise _Refused("references a schema from a table in none")
    local = [n for n, _ in _names(dialect, match["cols"])]
    ref_table = _unquote(dialect, match["ref"])
    ref_schema = _unquote(dialect, match["refschema"]) if match["refschema"] else None
    ref_columns = [n for n, _ in _names(dialect, match["refcols"])]
    if not _all_in(local, columns.get(_design_name(table, schema), [])):
        raise _Refused("keys a column the table definition does not have")
    rebuilt = render_foreign_key(
        dialect,
        table,
        _unquote(dialect, match["name"]),
        local,
        ref_table,
        ref_columns,
        _ACTIONS[match["action"]] if match["action"] else None,
        schema=schema,
        referenced_schema=ref_schema,
    )
    if rebuilt != statement:
        raise _Refused("does not render as written")
    return rebuilt


# ---------------------------------------------------------------------------
# Column clauses (constraint_translator's output)
# ---------------------------------------------------------------------------

_NUMBER = r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
# A literal as _pg_quote / _mysql_quote write it: quotes doubled, a backslash only doubled
# (and on PostgreSQL only inside E''), never a NUL.
_PLAIN_LITERAL = r"'(?:[^'\\\x00]|'')*+'"
_ESCAPED_LITERAL = r"'(?:[^'\\\x00]|''|\\\\)*+'"
_PG_LITERAL = re.compile(rf"E{_ESCAPED_LITERAL}|{_PLAIN_LITERAL}", re.ASCII)
_PG_DEFAULT = re.compile(
    r"CURRENT_TIMESTAMP(?:\(\d\))?|CURRENT_DATE|CURRENT_TIME|TRUE|FALSE|NULL"
    rf"|gen_random_uuid\(\)|B'[01]*'|{_NUMBER}",
    re.ASCII,
)
_MYSQL_LITERAL = re.compile(_ESCAPED_LITERAL, re.ASCII)
_MYSQL_DEFAULT = re.compile(
    rf"CURRENT_TIMESTAMP(?:\(\d+\))?|\(CURDATE\(\)\)|\(UUID\(\)\)|NULL|b'[01]*'|{_NUMBER}",
    re.ASCII,
)
_NOT_NULL = " NOT NULL"
_DEFAULT = " DEFAULT "


def _default_end(dialect: Dialect, aurora_type: str, text: str, pos: int) -> int:
    """Where the DEFAULT expression starting at ``pos`` ends."""
    literal = (_PG_LITERAL if dialect is POSTGRES else _MYSQL_LITERAL).match(text, pos)
    if literal is not None:
        end = literal.end()
        if dialect is POSTGRES and text.startswith("::", end):
            # _render_pg keeps a literal's cast only when it is the column's own type.
            cast = text[end + 2 : end + 2 + len(aurora_type)]
            if cast.lower() != aurora_type.lower():
                raise _Refused("has a DEFAULT cast to another type")
            end += 2 + len(aurora_type)
        return end
    other = (_PG_DEFAULT if dialect is POSTGRES else _MYSQL_DEFAULT).match(text, pos)
    if other is None:
        raise _Refused("has a DEFAULT outside the grammar the DDL generator writes")
    return other.end()


def _column_end(dialect: Dialect, aurora_type: str, text: str, pos: int) -> int:
    """Where a column's clauses (identity, NOT NULL, DEFAULT) starting at ``pos`` end."""
    identity = dialect.auto_increment(True)
    has_identity = text.startswith(identity, pos)
    if has_identity:
        pos += len(identity)
    if text.startswith(_NOT_NULL, pos):
        pos += len(_NOT_NULL)
    if text.startswith(_DEFAULT, pos):
        if has_identity:
            raise _Refused("has an identity column with a DEFAULT")
        pos = _default_end(dialect, aurora_type, text, pos + len(_DEFAULT))
    return pos


# ---------------------------------------------------------------------------
# generated_ddl
# ---------------------------------------------------------------------------


def _expect(text: str, pos: int, token: str) -> int:
    if not text.startswith(token, pos):
        raise _Refused("is not the script the DDL generator writes for these tables")
    return pos + len(token)


def _create_table(
    dialect: Dialect, table: Mapping[str, Any], text: str, pos: int
) -> tuple[str, int]:
    """The table's CREATE TABLE, rebuilt from its checked columns and the stored clauses."""
    name, schema = table["table_name"], table.get("schema_name")
    pos = _expect(text, pos, f"CREATE TABLE {_quoted_table(dialect, name, schema)} (\n")
    fragments: list[str] = []
    for i, column in enumerate(table["columns"]):
        if i:
            pos = _expect(text, pos, ",\n")
        head = f"{dialect.q(column['name'])} {column['aurora_type']}"
        start = _expect(text, pos, f"  {head}")
        pos = _column_end(dialect, column["aurora_type"], text, start)
        fragments.append(head + text[start:pos])
    primary_key: list[tuple[str, int | None]] = []
    if table["primary_key"]:
        pos = _expect(text, pos, ",\n  PRIMARY KEY (")
        keys = _key_list(dialect, prefixes=dialect is MYSQL).match(text, pos)
        if keys is None:
            raise _Refused("has a PRIMARY KEY the DDL generator does not write")
        primary_key = _names(dialect, keys.group(0), prefixes=dialect is MYSQL)
        if [n for n, _ in primary_key] != table["primary_key"]:
            raise _Refused("has a PRIMARY KEY other than the table definition's")
        pos = _expect(text, keys.end(), ")")
    pos = _expect(text, pos, "\n);")
    try:
        return render_create_table(dialect, name, fragments, primary_key, schema=schema), pos
    except SqlFragmentError as exc:
        raise _Refused("has a PRIMARY KEY the DDL generator does not write") from exc


_SEPARATOR = "\n\n"
_NOT_CREATED_NOTE = f"Index not created: {NOT_CREATED_REASON}"


def _statement_at(text: str, pos: int, statement: str) -> bool:
    end = pos + len(statement)
    return text.startswith(statement, pos) and (
        end == len(text) or text.startswith(_SEPARATOR, end)
    )


def skipped_comments(
    payload: Mapping[str, Any],
    tables: list[Mapping[str, Any]],
    created: list[list[str]] | None = None,
    legacy: list[dict[str, str]] | None = None,
) -> list[list[str]]:
    """Per table, the skipped-index comments its design may carry (Aurora MySQL): one for each
    ``index_key_prefix`` note the merge wrote for an index the key limit ruled out, exactly
    as ``skipped_index_comment`` writes it for that table and index. A note names
    ``table.index``; it belongs to the longest table name it starts with. A note for an
    index the table creates (``created[i]``, its checked statements) allows no comment: that
    index was not skipped. ``legacy[i]``, when given, maps main's exact spelling of each of
    table ``i``'s comments (``_main_comment``) to today's, where the two differ."""
    made = [
        {_index_name(statement) for statement in statements}
        for statements in (created if created is not None else [[] for _ in tables])
    ]
    out: list[list[str]] = [[] for _ in tables]
    notes = payload.get("app_layer_notes")
    for note in notes if isinstance(notes, list) else []:
        if not (
            isinstance(note, Mapping)
            and note.get("feature") == "index_key_prefix"
            and note.get("recommendation") == _NOT_CREATED_NOTE
            and isinstance(note.get("source_object"), str)
        ):
            continue
        source = note["source_object"]
        names = [_design_name(t["table_name"], t.get("schema_name")) for t in tables]
        owners = [(len(n), i) for i, n in enumerate(names) if source.startswith(n + ".")]
        if owners:
            _, i = max(owners)
            name = names[i]
            index = source[len(name) + 1 :]
            if index not in made[i]:
                today = skipped_index_comment(name, index)
                out[i].append(today)
                if legacy is not None and (old := _main_comment(name, index)) != today:
                    legacy[i][old] = today
    return out


def _index_name(statement: str) -> str | None:
    """The index name of a checked CREATE INDEX statement."""
    for dialect in DIALECTS.values():
        match = _INDEX_SHAPES[dialect.name].fullmatch(statement)
        if match is not None:
            return _unquote(dialect, match["name"])
    return None


def _main_comment(table_name: str, index_name: str) -> str:
    """The skipped-index comment main wrote before #2381: its ``comment_name`` replaced
    C0, C1 and the Unicode line separators with a space and collapsed the whitespace, but
    kept a format character (U+200B, U+202E, U+FEFF). A design main saved must keep
    loading, so this exact string is accepted for a note, besides today's comment."""
    name = " ".join(_LEGACY_UNSAFE.sub(" ", f"{table_name}.{index_name}").split())
    return f"{SKIPPED_INDEX_PREFIX}{name}{SKIPPED_INDEX_SUFFIX}"


#: What main's ``comment_name`` replaced (#2199), before #2381 cleaned format characters too.
_LEGACY_UNSAFE = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029]")


def rebuild_ddl(
    dialect: Dialect,
    tables: list[Mapping[str, Any]],
    indexes: list[list[str]],
    fks: list[str],
    text: str,
    comments: list[list[str]] | None = None,
    legacy: list[dict[str, str]] | None = None,
    per_table: list[list[str]] | None = None,
    fk_owners: list[int] | None = None,
) -> str:
    """``generated_ddl`` as ``assemble_full_ddl`` writes it for ``tables``: every CREATE
    TABLE, then each table's indexes (``indexes[i]``, with, on Aurora MySQL, at most one of
    each of ``comments[i]`` for an index the key limit ruled out), then the foreign keys.
    Refused unless the stored ``text`` is exactly that script. When ``per_table`` is given
    it receives each table's statements (its CREATE TABLE, its indexes and comments, its
    foreign keys, in script order), ``fk_owners[k]`` naming the table of ``fks[k]``."""
    pieces: list[str] = []
    pos = 0
    schemas = dict.fromkeys(t["schema_name"] for t in tables if t.get("schema_name"))
    for schema in schemas:
        if pieces:
            pos = _expect(text, pos, _SEPARATOR)
        try:
            statement = create_schema_sql(dialect, schema)
        except ValueError as exc:
            raise _Refused("names a schema whose name cannot be quoted") from exc
        pos = _expect(text, pos, statement)
        pieces.append(statement)
    for i, table in enumerate(tables):
        if pieces:
            pos = _expect(text, pos, _SEPARATOR)
        statement, pos = _create_table(dialect, table, text, pos)
        pieces.append(statement)
        if per_table is not None:
            per_table[i].append(statement)
    allowed = comments if comments is not None and dialect is MYSQL else [[] for _ in tables]
    aliases = legacy if legacy is not None else [{} for _ in tables]
    for t_i, (table_indexes, table_comments, table_aliases) in enumerate(
        zip(indexes, allowed, aliases, strict=True)
    ):
        queue = deque(table_indexes)
        notes = Counter(table_comments)  # each comment at most as often as its note
        left = len(table_comments)
        while (queue or left) and text.startswith(_SEPARATOR, pos):
            start = pos + len(_SEPARATOR)
            if queue and _statement_at(text, start, queue[0]):
                piece = queue.popleft()
            else:
                end = text.find("\n", start)
                line = text[start : end if end >= 0 else len(text)]
                key = line if notes[line] > 0 else table_aliases.get(line)
                if key is None or notes[key] <= 0:
                    break
                notes[key] -= 1
                left -= 1
                piece = line
            pieces.append(piece)
            if per_table is not None:
                per_table[t_i].append(piece)
            pos = start + len(piece)
        if queue:
            raise _Refused("does not list the table definitions' indexes")
    for k, fk in enumerate(fks):
        start = _expect(text, pos, _SEPARATOR)
        if not _statement_at(text, start, fk):
            raise _Refused("does not list the table definitions' foreign keys")
        pieces.append(fk)
        if per_table is not None and fk_owners is not None:
            per_table[fk_owners[k]].append(fk)
        pos = start + len(fk)
    rebuilt = _SEPARATOR.join(pieces)
    if pos != len(text) or rebuilt != text:
        raise _Refused("has statements the DDL generator does not write for these tables")
    return rebuilt


# ---------------------------------------------------------------------------
# The read check
# ---------------------------------------------------------------------------


def _table_shape(table: object) -> Mapping[str, Any]:
    """A table definition the DDL can be rebuilt from, or refused."""
    if not isinstance(table, Mapping):
        raise _Refused("is not a table definition the DDL can be rebuilt from")
    columns = table.get("columns")
    primary_key = table.get("primary_key", [])
    ok = (
        isinstance(table.get("table_name"), str)
        and isinstance(table.get("schema_name"), str | None)
        and table.get("schema_name") != ""
        and isinstance(columns, list)
        and bool(columns)
        and all(
            isinstance(c, Mapping) and isinstance(c.get("name"), str)
            and isinstance(c.get("aurora_type"), str)
            for c in columns
        )
        and isinstance(primary_key, list)
        and all(isinstance(c, str) for c in primary_key)
    )  # fmt: skip
    if not ok:
        raise _Refused("is not a table definition the DDL can be rebuilt from")
    return {**table, "primary_key": list(primary_key)}


# The fields that hold SQL text: a table's statements, and the whole script.
DDL_FIELDS = ("generated_ddl", "indexes", "foreign_keys")


def _carried(value: object) -> bool:
    """Whether a stored field holds anything (``None``, ``""`` and ``[]`` hold nothing)."""
    return value not in (None, "", [])


def check_skipped_design(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """A skipped schema design (``status: "skipped"``) carries no table and no SQL text, so
    one that carries ``table_definitions`` or any of :data:`DDL_FIELDS` is refused (#2199):
    no reader checks a placeholder's design fields, and the UI would show them."""
    for field in ("table_definitions", *DDL_FIELDS):
        if _carried(payload.get(field)):
            kind = DDL_KIND if field in ("generated_ddl", "table_definitions") else _KINDS[field]
            raise SavedDesignError(field, None, "is in a skipped design", kind=kind)
    return payload


def check_design_ddl(payload: Mapping[str, Any], engine: str) -> Mapping[str, Any]:
    """``payload`` (a saved ``engine`` design, column types already checked) once every
    index, foreign key and ``generated_ddl`` string is rebuilt as written (#2199).

    Raises :class:`SavedDesignError` (``where`` its position, no stored text) on the first
    string that does not rebuild, and when the design carries any SQL text
    (:data:`DDL_FIELDS`) with no list of table definitions to rebuild it from. Each rebuilt
    string equals the stored one, so ``payload`` itself is returned.
    """
    dialect = DIALECTS[engine]
    tables = payload.get("table_definitions")
    if not isinstance(tables, list):
        for field in DDL_FIELDS:
            if _carried(payload.get(field)):
                raise SavedDesignError(
                    field, None, "has no table definitions to rebuild it from", kind=_KINDS[field]
                )
        return payload
    columns = {
        _design_name(t["table_name"], _schema(t)): [
            c.get("name") for c in t["columns"] if isinstance(c, Mapping)
        ]
        for t in tables
        if isinstance(t, Mapping)
        and isinstance(t.get("table_name"), str)
        and isinstance(t.get("columns"), list)
    }
    names = {table: [c for c in cols if isinstance(c, str)] for table, cols in columns.items()}
    indexes: list[list[str]] = []
    fks: list[str] = []
    fk_owners: list[int] = []
    for i, table in enumerate(tables):
        table_indexes: list[str] = []
        indexes.append(table_indexes)
        if not isinstance(table, Mapping):
            continue
        name = table.get("table_name")
        schema = _schema(table)
        if table.get("schema_name") is not None and schema is None:
            raise SavedDesignError(
                f"table_definitions[{i}].schema_name", None, "is not a schema name", kind=DDL_KIND
            )
        for field, out in (("indexes", table_indexes), ("foreign_keys", fks)):
            kind = _KINDS[field]
            statements = table.get(field)
            if statements is None:
                continue
            if not isinstance(statements, list):
                raise SavedDesignError(
                    f"table_definitions[{i}].{field}", None, "is not a list", kind=kind
                )
            for j, statement in enumerate(statements):
                where = f"table_definitions[{i}].{field}[{j}]"
                try:
                    if not isinstance(name, str):
                        raise _Refused("belongs to a table with no name")
                    if field == "indexes":
                        own = names.get(_design_name(name, schema), [])
                        out.append(check_index(dialect, name, own, statement, schema))
                    else:
                        out.append(check_foreign_key(dialect, name, names, statement, schema))
                        fk_owners.append(i)
                except _Refused as exc:
                    raise SavedDesignError(where, None, str(exc), kind=kind) from None
    for field in ("indexes", "foreign_keys"):
        if _carried(payload.get(field)):
            raise SavedDesignError(field, None, "is not a design field", kind=_KINDS[field])
    if "generated_ddl" not in payload:
        for i, table in enumerate(tables):
            if isinstance(table, Mapping) and _carried(table.get("ddl_statements")):
                raise SavedDesignError(
                    f"table_definitions[{i}].ddl_statements",
                    None,
                    "has no script to be a part of",
                    kind=DDL_KIND,
                )
        return payload
    try:
        text = payload["generated_ddl"]
        if not isinstance(text, str):
            raise _Refused("is not a string")
        shaped = [_table_shape(t) for t in tables]
        legacy: list[dict[str, str]] = [{} for _ in shaped]
        comments = skipped_comments(payload, shaped, indexes, legacy)
        per_table: list[list[str]] = [[] for _ in shaped]
        # #2484: the comments for the objects the design leaves out close the script,
        # exactly as the merge writes them from the design's own notes (each one line).
        statements = without_not_created_comments(text, payload.get("app_layer_notes"))
        rebuild_ddl(
            dialect, shaped, indexes, fks, statements, comments, legacy, per_table, fk_owners
        )
    except _Refused as exc:
        raise SavedDesignError("generated_ddl", None, str(exc), kind=DDL_KIND) from None
    # A table's stored statements (``ddl_statements``) are the script's own cut of it: a
    # design saved before the field existed has none; one that has it must match exactly.
    # The merge writes the field on every table or on none, so a mix is a dropped field.
    carried = [isinstance(t, Mapping) and t.get("ddl_statements") is not None for t in tables]
    if any(carried) and not all(carried):
        raise SavedDesignError(
            f"table_definitions[{carried.index(False)}].ddl_statements",
            None,
            "is missing while other tables of the design carry theirs",
            kind=DDL_KIND,
        )
    for i, table in enumerate(tables):
        stored = table.get("ddl_statements") if isinstance(table, Mapping) else None
        if stored is not None and stored != per_table[i]:
            raise SavedDesignError(
                f"table_definitions[{i}].ddl_statements",
                None,
                "is not the part of the script the DDL generator writes for this table",
                kind=DDL_KIND,
            )
    return payload
