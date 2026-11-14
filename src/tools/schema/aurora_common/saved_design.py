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
- SQL text with nothing to rebuild it from is refused: a design that carries
  ``generated_ddl``, ``indexes`` or ``foreign_keys`` without a list of table definitions,
  and a skipped design (``check_skipped_design``) that carries any of them or tables.

A string that does not come back identical is refused (:class:`SavedDesignError`, with
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
from src.tools.schema.aurora_common.ddl_generator import (
    MYSQL,
    NOT_CREATED_REASON,
    POSTGRES,
    Dialect,
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


def _index_shape(dialect: Dialect) -> re.Pattern[str]:
    qid = _qid(dialect)
    key = rf"{qid}(?:\(\d+\))?"
    return re.compile(
        r"CREATE (?P<kind>UNIQUE |FULLTEXT |SPATIAL )?INDEX "
        rf"(?P<name>{qid}) ON (?P<table>{qid})(?: USING (?P<method>[a-z]+))? "
        rf"\((?P<cols>{key}(?:, {key})*)\)"
        rf"(?: INCLUDE \((?P<include>{qid}(?:, {qid})*)\))?"
        r"(?: WHERE (?P<where>[^\n]+))?;"
    )


_INDEX_SHAPES = {name: _index_shape(d) for name, d in DIALECTS.items()}


def check_index(dialect: Dialect, table: str, columns: list[str], statement: object) -> str:
    """``statement`` re-rendered from its parsed parts; refused unless it comes back the same."""
    if not isinstance(statement, str):
        raise _Refused("is not a string")
    match = _INDEX_SHAPES[dialect.name].fullmatch(statement)
    if match is None:
        raise _Refused("is not a CREATE INDEX statement the DDL generator writes")
    if _unquote(dialect, match["table"]) != table:
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
        rf"ALTER TABLE (?P<table>{qid}) ADD CONSTRAINT (?P<name>{qid}) "
        rf"FOREIGN KEY \((?P<cols>{names})\) REFERENCES (?P<ref>{qid}) \((?P<refcols>{names})\)"
        rf"(?: ON DELETE (?P<action>{actions}))?;"
    )


_FK_SHAPES = {name: _fk_shape(d) for name, d in DIALECTS.items()}


def check_foreign_key(
    dialect: Dialect, table: str, columns: dict[str, list[str]], statement: object
) -> str:
    """``statement`` re-rendered from its parsed parts; refused unless it comes back the same.
    ``columns`` maps each table of the design to its columns: the key's own columns must be
    the table's. Its referenced columns are not checked: the generator writes them as the
    collector reports them, and the referenced table is often not in this design."""
    if not isinstance(statement, str):
        raise _Refused("is not a string")
    match = _FK_SHAPES[dialect.name].fullmatch(statement)
    if match is None:
        raise _Refused("is not a FOREIGN KEY statement the DDL generator writes")
    if _unquote(dialect, match["table"]) != table:
        raise _Refused("is not on the table it is listed under")
    local = [n for n, _ in _names(dialect, match["cols"])]
    ref_table = _unquote(dialect, match["ref"])
    ref_columns = [n for n, _ in _names(dialect, match["refcols"])]
    if not _all_in(local, columns.get(table, [])):
        raise _Refused("keys a column the table definition does not have")
    rebuilt = render_foreign_key(
        dialect,
        table,
        _unquote(dialect, match["name"]),
        local,
        ref_table,
        ref_columns,
        _ACTIONS[match["action"]] if match["action"] else None,
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
    name = table["table_name"]
    pos = _expect(text, pos, f"CREATE TABLE {dialect.q(name)} (\n")
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
        return render_create_table(dialect, name, fragments, primary_key), pos
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
) -> list[list[str]]:
    """Per table, the skipped-index comments its design may carry (Aurora MySQL): one for each
    ``index_key_prefix`` note the merge wrote for an index the key limit ruled out, exactly
    as ``skipped_index_comment`` writes it for that table and index. A note names
    ``table.index``; it belongs to the longest table name it starts with. A note for an
    index the table creates (``created[i]``, its checked statements) allows no comment: that
    index was not skipped."""
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
        owners = [
            (len(t["table_name"]), i)
            for i, t in enumerate(tables)
            if source.startswith(t["table_name"] + ".")
        ]
        if owners:
            _, i = max(owners)
            name = tables[i]["table_name"]
            index = source[len(name) + 1 :]
            if index not in made[i]:
                out[i].append(skipped_index_comment(name, index))
    return out


def _index_name(statement: str) -> str | None:
    """The index name of a checked CREATE INDEX statement."""
    for dialect in DIALECTS.values():
        match = _INDEX_SHAPES[dialect.name].fullmatch(statement)
        if match is not None:
            return _unquote(dialect, match["name"])
    return None


def rebuild_ddl(
    dialect: Dialect,
    tables: list[Mapping[str, Any]],
    indexes: list[list[str]],
    fks: list[str],
    text: str,
    comments: list[list[str]] | None = None,
) -> str:
    """``generated_ddl`` as ``assemble_full_ddl`` writes it for ``tables``: every CREATE
    TABLE, then each table's indexes (``indexes[i]``, with, on Aurora MySQL, at most one of
    each of ``comments[i]`` for an index the key limit ruled out), then the foreign keys.
    Refused unless the stored ``text`` is exactly that script."""
    pieces: list[str] = []
    pos = 0
    for table in tables:
        if pieces:
            pos = _expect(text, pos, _SEPARATOR)
        statement, pos = _create_table(dialect, table, text, pos)
        pieces.append(statement)
    allowed = comments if comments is not None and dialect is MYSQL else [[] for _ in tables]
    for table_indexes, table_comments in zip(indexes, allowed, strict=True):
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
                if notes[line] <= 0:
                    break
                notes[line] -= 1
                left -= 1
                piece = line
            pieces.append(piece)
            pos = start + len(piece)
        if queue:
            raise _Refused("does not list the table definitions' indexes")
    for fk in fks:
        start = _expect(text, pos, _SEPARATOR)
        if not _statement_at(text, start, fk):
            raise _Refused("does not list the table definitions' foreign keys")
        pieces.append(fk)
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
        t["table_name"]: [c.get("name") for c in t["columns"] if isinstance(c, Mapping)]
        for t in tables
        if isinstance(t, Mapping)
        and isinstance(t.get("table_name"), str)
        and isinstance(t.get("columns"), list)
    }
    names = {table: [c for c in cols if isinstance(c, str)] for table, cols in columns.items()}
    indexes: list[list[str]] = []
    fks: list[str] = []
    for i, table in enumerate(tables):
        table_indexes: list[str] = []
        indexes.append(table_indexes)
        if not isinstance(table, Mapping):
            continue
        name = table.get("table_name")
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
                        out.append(check_index(dialect, name, names.get(name, []), statement))
                    else:
                        out.append(check_foreign_key(dialect, name, names, statement))
                except _Refused as exc:
                    raise SavedDesignError(where, None, str(exc), kind=kind) from None
    for field in ("indexes", "foreign_keys"):
        if _carried(payload.get(field)):
            raise SavedDesignError(field, None, "is not a design field", kind=_KINDS[field])
    if "generated_ddl" not in payload:
        return payload
    try:
        text = payload["generated_ddl"]
        if not isinstance(text, str):
            raise _Refused("is not a string")
        shaped = [_table_shape(t) for t in tables]
        comments = skipped_comments(payload, shaped, indexes)
        rebuild_ddl(dialect, shaped, indexes, fks, text, comments)
    except _Refused as exc:
        raise SavedDesignError("generated_ddl", None, str(exc), kind=DDL_KIND) from None
    return payload
