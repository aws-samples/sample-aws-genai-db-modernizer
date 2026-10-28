"""Cut an ``assess()`` result down to some of its tables, to assess that piece again
(#1633, contract 1.3).

>>> from src.assessment import assess, cut_collection
>>> whole = assess({"database_name": "shop", "collection": raw})  # doctest: +SKIP
>>> cut = cut_collection(whole, table_ids=["shop.Orders", "shop.OrderLines"])  # doctest: +SKIP
>>> assess({"database_name": "orders", "collection": cut.collection, "scope": cut.scope})  # doctest: +SKIP

A cut keeps the given tables (and views the collection lists) and every statement whose
tables all lie among them, whole. A statement that touches a kept table and a dropped one
crosses the cut: it is left out, unless the caller gives a *part* for it, its own rewrite
of the statement over kept tables only. A statement that reads no table of the collection
(a procedure call, a sequence read, a function call, a read of system catalogs) belongs to
no cut: each cut lists it in ``scope.unplaced_statements``, and the caller can give a part
for it too. The library builds each part's ``query_id`` (:mod:`src.assessment.part_ids`),
and the part inherits every execution statistic of the statement it was cut from (the
database measured the statement as a whole, so splitting its calls or timings would invent
numbers): a part's cost can be overstated, never understated. Each part is checked, and
refused with a :class:`CutError`, never dropped.

``CollectionCut.collection`` is opaque: pass it to ``assess()`` as the ``collection``
(with ``CollectionCut.scope`` as the ``scope``), and do not read it. Read the data from
the result (``summary.table_facts[]``, ``statements[]``). Every later release accepts a
cut's ``collection`` written by an earlier one.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.assessment.models import AssessmentResult, UnplacedStatement, WorkloadScope
from src.assessment.part_ids import part_query_id

__all__ = [
    "CollectionCut",
    "CutError",
    "CutSourceError",
    "DuplicatePartError",
    "EmptyCutError",
    "InvalidPartError",
    "InvalidTableIdsError",
    "NotACrossingStatementError",
    "PartDialectError",
    "PartIdCollisionError",
    "PartOfKeptStatementError",
    "PartParseError",
    "PartTableNotInStatementError",
    "PartTableOutsideCutError",
    "StatementPart",
    "UnknownStatementError",
    "UnknownTableError",
    "ViewOutsideCutError",
    "cut_collection",
]


# --------------------------------------------------------------------------- errors


class CutError(ValueError):
    """``cut_collection`` refused the cut; the message says why. Every refusal is one of
    its subclasses."""


class CutSourceError(CutError):
    """The result holds no collection to cut: it was read from JSON without
    ``artifacts`` (ask ``assess()`` for ``include_artifacts=True``, or cut the result
    ``assess()`` returned)."""


class InvalidTableIdsError(CutError):
    """``table_ids`` is not a list of table ids (a string, None, or an item that is not a
    string)."""


class EmptyCutError(CutError):
    """``table_ids`` names no table."""


class UnknownTableError(CutError):
    """``table_ids`` names a table (or view) the result's collection does not hold."""

    def __init__(self, table_ids: list[str]) -> None:
        self.table_ids = table_ids
        super().__init__(
            "table_ids names tables the collection does not hold (neither a table nor a "
            f"view): {', '.join(table_ids)}"
        )


class ViewOutsideCutError(CutError):
    """``table_ids`` names a view that reads a table outside ``table_ids`` (or whose
    definition names no table the parser can read): keeping it would pull another
    service's table back in."""

    def __init__(self, view_id: str, outside: list[str]) -> None:
        self.view_id = view_id
        self.outside = outside
        why = (
            f"reads tables outside table_ids: {', '.join(outside)}"
            if outside
            else "its definition names no table the parser can read"
        )
        super().__init__(f"view {view_id} {why}")


class InvalidPartError(CutError):
    """``parts`` is not a list of ``{"from_query_id": ..., "query_text": ...}``."""


class _PartError(CutError):
    def __init__(self, from_query_id: str, message: str) -> None:
        self.from_query_id = from_query_id
        super().__init__(f"part of {from_query_id}: {message}")


class NotACrossingStatementError(_PartError):
    """A part's ``from_query_id`` takes no part in this cut: none of its tables is kept,
    and it is not a statement the cut cannot place (or the statement is unknown:
    :class:`UnknownStatementError`)."""


class UnknownStatementError(NotACrossingStatementError):
    """A part's ``from_query_id`` is no statement of the collection."""


class PartOfKeptStatementError(_PartError):
    """A part's ``from_query_id`` is kept whole by the cut: a statement is either kept
    whole or cut into a part, never both."""


class DuplicatePartError(_PartError):
    """Two parts name the same statement: a cut takes one part per statement."""


class PartParseError(_PartError):
    """The source dialect's parser cannot read a part's text as one statement over
    tables of the collection."""


class PartDialectError(PartParseError):
    """A part's text uses a construct of another database's that the fixed list
    ``_FOREIGN_SYNTAX`` refuses (for a SQL Server source, for example, PostgreSQL's
    ``$1``, ``::``, ``ILIKE`` or ``RETURNING``, or MySQL's backticks and ``LIMIT``)."""

    def __init__(self, from_query_id: str, source_engine: str, found: str) -> None:
        self.found = found
        super().__init__(
            from_query_id,
            f"the text is not in the {source_engine} dialect: it uses {found}",
        )


class PartTableOutsideCutError(_PartError):
    """A part names a table outside ``table_ids``."""

    def __init__(self, from_query_id: str, outside: list[str]) -> None:
        self.outside = outside
        super().__init__(from_query_id, f"names tables outside table_ids: {', '.join(outside)}")


class PartTableNotInStatementError(_PartError):
    """A part names a kept table its statement never reads: it would carry that
    statement's statistics onto another table. A view counts as the base tables it
    reads. A part of a statement the cut cannot place is not checked."""

    def __init__(self, from_query_id: str, extra: list[str]) -> None:
        self.extra = extra
        super().__init__(
            from_query_id, f"names tables its statement does not read: {', '.join(extra)}"
        )


class PartIdCollisionError(_PartError):
    """A part's id is already a statement's id in the collection."""


# --------------------------------------------------------------------------- models


class StatementPart(BaseModel):
    """A caller's rewrite of a statement that crosses the cut (or that the cut cannot
    place), over kept tables only, in the source database's dialect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_query_id: str = Field(..., min_length=1, description="The statement's query_id")
    query_text: str = Field(..., description="The rewritten statement, in the source dialect")


class CollectionCut(BaseModel):
    """One cut: what it kept, and what to pass to the next ``assess()`` call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table_ids: list[str] = Field(..., description="The tables (and views) kept, sorted")
    query_ids: list[str] = Field(
        ...,
        description=(
            "The statements kept whole and the parts' ids (built by the library), in the "
            "collection's order; a part stands where its statement stood"
        ),
    )
    scope: WorkloadScope = Field(
        ...,
        description=(
            "Pass as the next request's scope: the tables kept, the parts in "
            "rewritten_query_ids, the foreign keys the cut dropped in dropped_foreign_keys "
            "and the statements it could not place in unplaced_statements"
        ),
    )
    collection: dict[str, Any] = Field(
        ...,
        description="Pass as the next request's collection. Opaque: do not read it",
    )


# --------------------------------------------------------------------------- the cut


def cut_collection(
    result: AssessmentResult,
    *,
    table_ids: Iterable[str],
    parts: Iterable[StatementPart | Mapping[str, Any]] = (),
) -> CollectionCut:
    """Cut ``result``'s collection down to ``table_ids``, with the caller's ``parts``
    for statements that cross the cut or that it cannot place.

    Raises a :class:`CutError` subclass for a refused cut; nothing is dropped silently.
    """
    source = result.cut_source()
    if source is None:
        raise CutSourceError(
            "the result holds no collection to cut: cut the result assess() returned, or "
            "one read from JSON that was assessed with include_artifacts=True"
        )
    kept = _table_ids(table_ids)
    tables = _tables(source)
    views = _view_tables(source, tables)
    placeable = {str(t["table_id"]) for t in tables} | set(views)
    unknown = sorted(kept - placeable)
    if unknown:
        raise UnknownTableError(unknown)
    for view in sorted(kept & set(views)):
        base = views[view]
        if base is None:
            raise ViewOutsideCutError(view, [])
        outside = [t for t in base if t not in kept]
        if outside:
            raise ViewOutsideCutError(view, outside)

    patterns = _patterns(source)
    whole: set[str] = set()
    crossing: set[str] = set()
    unplaced: dict[str, str] = {}
    for qid, p in patterns.items():
        read = _tables_of(p)
        if not placeable & set(read):
            unplaced[qid] = _unplaced_reason(read)
        elif set(read) <= kept:
            whole.add(qid)
        elif kept & set(read):
            crossing.add(qid)
    rows = _part_rows(source, tables, views, kept, patterns, whole, crossing, unplaced, parts)
    given = {str(row["_from"]) for row in rows}

    out = {
        k: copy.deepcopy(v) for k, v in source.items() if k not in ("database_schema", "queries")
    }
    schema = dict(source.get("database_schema") or {})
    cut_tables, dropped_keys = _cut_tables(tables, kept)
    schema["tables"] = cut_tables
    if "triggers" in schema:
        schema["triggers"] = _cut_triggers(schema["triggers"], tables, kept)
    out["database_schema"] = copy.deepcopy(schema)

    by_statement = {str(row["_from"]): row for row in rows}
    query_patterns: list[dict[str, Any]] = []
    for qid, pattern in patterns.items():
        if qid in whole:
            query_patterns.append(copy.deepcopy(dict(pattern)))
        elif qid in by_statement:
            query_patterns.append({k: v for k, v in by_statement[qid].items() if k != "_from"})
    queries = {k: copy.deepcopy(v) for k, v in (source.get("queries") or {}).items()}
    queries["query_patterns"] = query_patterns
    queries["total_queries_analyzed"] = len(query_patterns)
    out["queries"] = queries

    query_ids = [str(p["query_id"]) for p in query_patterns]
    return CollectionCut(
        table_ids=sorted(kept),
        query_ids=query_ids,
        scope=_scope(result, kept, query_ids, whole, dropped_keys, unplaced, given, patterns),
        collection=out,
    )


def _table_ids(table_ids: Any) -> set[str]:
    if (
        table_ids is None
        or isinstance(table_ids, str | bytes)
        or not isinstance(table_ids, Iterable)
    ):
        raise InvalidTableIdsError(
            f"table_ids is a list of table ids, not {type(table_ids).__name__}"
        )
    kept = list(table_ids)
    if not all(isinstance(t, str) and t for t in kept):
        raise InvalidTableIdsError("every table id in table_ids is a non-empty string")
    if not kept:
        raise EmptyCutError("table_ids names no table")
    return set(kept)


def _scope(
    result: AssessmentResult,
    kept: set[str],
    query_ids: list[str],
    whole: set[str],
    dropped_keys: list[dict[str, Any]],
    unplaced: dict[str, str],
    given: set[str],
    patterns: dict[str, Mapping[str, Any]],
) -> WorkloadScope:
    """The cut's scope, carrying forward what an earlier cut recorded (a cut of a cut):
    its parts still kept stay rewritten, its dropped foreign keys that still touch a kept
    table stay listed, its unplaced statements stay listed, and its ``derived_from`` is
    kept."""
    earlier = result.scope
    rewritten = set(earlier.rewritten_query_ids) if earlier else set()
    parts = [q for q in query_ids if q not in whole or q in rewritten]

    keys = list(dropped_keys)
    for entry in earlier.dropped_foreign_keys if earlier else []:
        ours = {"constraint_name", "table_id", "referenced_table_id"} <= set(entry)
        if not ours or entry["table_id"] in kept or entry["referenced_table_id"] in kept:
            keys.append(entry)
    seen: list[dict[str, Any]] = []
    for entry in keys:
        if entry not in seen:
            seen.append(entry)
    seen.sort(key=lambda k: tuple(str(k.get(n)) for n in _KEY_ORDER))

    # An earlier cut's first, then this cut's, in the collection's order.
    lost = {e.query_id: e for e in (earlier.unplaced_statements if earlier else [])}
    for qid in patterns:
        if qid in unplaced and qid not in given:
            lost.setdefault(qid, UnplacedStatement(query_id=qid, reason=unplaced[qid]))  # type: ignore[arg-type]
    unplaced_list = list(lost.values())
    return WorkloadScope(
        tables=sorted(kept),
        derived_from=earlier.derived_from if earlier else None,
        rewritten_query_ids=parts,
        dropped_foreign_keys=seen,
        unplaced_statements=unplaced_list,
    )


_KEY_ORDER = ("table_id", "constraint_name", "referenced_table_id")


def _unplaced_reason(read: list[str]) -> str:
    """Why the cut cannot place a statement: it reads no table at all (a procedure call,
    a sequence read, a function call, a session command), or only tables the collection
    does not hold (system catalogs, a materialized view it does not list)."""
    if not [t for t in read if t != "unknown"]:
        return "no_tables_read"
    return "tables_not_collected"


def _tables(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        t
        for t in (source.get("database_schema") or {}).get("tables") or []
        if isinstance(t, Mapping) and t.get("table_id")
    ]


def _view_tables(
    source: Mapping[str, Any], tables: list[Mapping[str, Any]]
) -> dict[str, list[str] | None]:
    """Each view the collection lists, by the id statements name it with, and the base
    tables it reads: its ``referenced_tables`` when the collection gives them, else its
    definition's query as the library's parser reads it in the source dialect. None when
    neither names a table."""
    out: dict[str, list[str] | None] = {}
    for v in (source.get("database_schema") or {}).get("views") or []:
        if not (isinstance(v, Mapping) and v.get("view_id")):
            continue
        view_id = str(v["view_id"])
        base = [str(t) for t in v.get("referenced_tables") or []]
        if not base:
            body = _VIEW_HEADER_RE.sub("", str(v.get("definition") or ""), count=1)
            read = _tables_of(_parsed(source, tables, {"query_id": view_id, "query_text": body}))
            base = [t for t in read if t not in ("unknown", view_id)]
        out[view_id] = list(dict.fromkeys(base)) or None
    return out


#: A view definition's header, up to the ``AS`` before its query (``CREATE [OR ALTER]
#: VIEW name [(columns)] [WITH ...] AS``); a definition that is only the query keeps it all.
_VIEW_HEADER_RE = re.compile(r"^\s*CREATE\b.*?\bVIEW\b.*?\bAS\b", re.IGNORECASE | re.DOTALL)


def _expanded(read: Iterable[str], views: Mapping[str, list[str] | None]) -> set[str]:
    """``read`` with each view replaced by the base tables it reads."""
    out: set[str] = set()
    for t in read:
        base = views.get(t)
        out.update(base if base else [t])
    return out


def _patterns(source: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Every statement of the collection, by id, in the collection's order."""
    out: dict[str, Mapping[str, Any]] = {}
    for p in (source.get("queries") or {}).get("query_patterns") or []:
        if isinstance(p, Mapping) and p.get("query_id"):
            out.setdefault(str(p["query_id"]), p)
    return out


def _tables_of(pattern: Mapping[str, Any]) -> list[str]:
    return [str(t) for t in pattern.get("tables_accessed") or []]


def _cut_tables(
    tables: list[Mapping[str, Any]], kept: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The kept tables, less their foreign keys to a dropped table, and every foreign key
    between a kept table and a dropped one (either way round), as the scope records it."""
    from src.assessment.result_builder import _referenced_table_id

    out: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for t in tables:
        table_id = str(t["table_id"])
        keys: list[Any] = []
        for fk in t.get("foreign_keys") or []:
            ref = (
                _referenced_table_id(fk, t.get("schema_name"), tables)
                if isinstance(fk, Mapping)
                else None
            )
            if ref is not None and (table_id in kept) != (ref in kept):
                dropped.append(
                    {
                        "constraint_name": str(fk.get("constraint_name") or ""),
                        "table_id": table_id,
                        "referenced_table_id": ref,
                    }
                )
                continue
            keys.append(fk)
        if table_id in kept:
            table = copy.deepcopy(dict(t))
            if t.get("foreign_keys") is not None:
                table["foreign_keys"] = copy.deepcopy(keys)
            out.append(table)
    return out, dropped


def _cut_triggers(triggers: Any, tables: list[Mapping[str, Any]], kept: set[str]) -> Any:
    """The triggers less those on a dropped table (a trigger names its table by id or by
    bare name); one whose table is not found is kept."""
    if not isinstance(triggers, list):
        return triggers
    kept_names = kept | {str(t.get("table_name")) for t in tables if str(t["table_id"]) in kept}
    dropped_names = {
        name
        for t in tables
        if str(t["table_id"]) not in kept
        for name in (str(t["table_id"]), str(t.get("table_name")))
    }
    return [
        tr
        for tr in triggers
        if not (
            isinstance(tr, Mapping)
            and str(tr.get("table_id")) in dropped_names
            and str(tr.get("table_id")) not in kept_names
        )
    ]


# --------------------------------------------------------------------------- parts


def _redacted(text: str, source_engine: str) -> str:
    """A part's text with its literal values redacted (#1625): the same
    ``redact_literals`` the collected statements go through, in the source dialect."""
    from src.tools.sql.redact import redact_literals

    return redact_literals(text, source_engine)


def statement_count(text: str, source_engine: str) -> int:
    """How many statements the source dialect's parser reads in ``text``: 0 when it
    cannot read it (where the library's SQL reader falls back to its regex scan).

    One strict parse by the library's SQL reader (``src.tools.sql.facade``: sqlglot in
    the source dialect, which raises on the first error), the one the collected
    statements go through. Kept here, not in the reader, so the routing ruleset
    (``ruleset_hash``, which covers the reader's source) is unchanged."""
    from src.tools.sql.facade import _parse

    return len(_parse(text, source_engine).statements)


def _bracket_name(code: str) -> bool:
    """A ``[name]`` identifier (SQL Server), not an array subscript (``a[1]``,
    ``ARRAY[...]``)."""
    for match in re.finditer(r"\[[A-Za-z_][\w ]*\]", code):
        before = code[: match.start()]
        if not before or not (before[-1].isalnum() or before[-1] in "_])"):
            return True
    return False


_I = re.IGNORECASE
_BACKTICK = ("a MySQL backtick name (`...`)", re.compile(r"`", _I))
# ``$1``; not a SQL Server money literal with a fraction (``$19.99``).
_DOLLAR_PARAM = ("a PostgreSQL $n parameter", re.compile(r"\$\d+(?![.\d])", _I))
_CAST = ("a PostgreSQL :: cast", re.compile(r"::", _I))
_ILIKE = ("PostgreSQL ILIKE", re.compile(r"\bILIKE\b", _I))
_LIMIT = ("LIMIT (MySQL or PostgreSQL)", re.compile(r"\bLIMIT\b", _I))
_ROWNUM = ("Oracle ROWNUM", re.compile(r"\bROWNUM\b", _I))
_TOP = ("SQL Server TOP", re.compile(r"\bTOP\s*\(?\s*(?:\d|@)", _I))
_AT_PARAM = ("a SQL Server @parameter", re.compile(r"(?<![\w@])@[A-Za-z_]\w*", _I))
_RETURNING = ("RETURNING (PostgreSQL)", re.compile(r"\bRETURNING\b", _I))
_ON_CONFLICT = ("ON CONFLICT (PostgreSQL)", re.compile(r"\bON\s+CONFLICT\b", _I))
_CONCAT = ("|| concatenation", re.compile(r"\|\|", _I))
_NOW = ("NOW() (MySQL or PostgreSQL)", re.compile(r"\bNOW\s*\(", _I))
_IFNULL = ("IFNULL (MySQL)", re.compile(r"\bIFNULL\s*\(", _I))
_NVL = ("NVL (Oracle)", re.compile(r"\bNVL\s*\(", _I))
_ISNULL_CALL = ("ISNULL() (SQL Server)", re.compile(r"\bISNULL\s*\(", _I))
_GETDATE = ("GETDATE() (SQL Server)", re.compile(r"\bGETDATE\s*\(", _I))

#: Per source engine, the other dialects' constructs a part is refused for: a fixed list,
#: not a full dialect check. Read on the text with its comments and strings masked and its
#: quoted names neutral (``src.shared.sql_spans.keyword_text``), so only SQL is checked.
#: SQL Server keeps its own ``"..."`` names (``QUOTED_IDENTIFIER ON``, the default), its
#: ``type::Method(...)`` calls (:data:`_TSQL_SCOPE_RE`) and its ``$19.99`` money literals.
_FOREIGN_SYNTAX: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "sqlserver": (
        _BACKTICK,
        _DOLLAR_PARAM,
        _CAST,
        _ILIKE,
        _LIMIT,
        _ROWNUM,
        _RETURNING,
        _ON_CONFLICT,
        _CONCAT,
        _NOW,
        _IFNULL,
        _NVL,
    ),
    "postgresql": (_BACKTICK, _TOP, _AT_PARAM, _ROWNUM, _IFNULL, _NVL, _ISNULL_CALL, _GETDATE),
    "mysql": (
        _TOP,
        _DOLLAR_PARAM,
        _CAST,
        _ILIKE,
        _ROWNUM,
        _RETURNING,
        _ON_CONFLICT,
        _NVL,
        _GETDATE,
    ),
    # MariaDB has RETURNING and NVL of its own.
    "mariadb": (_TOP, _DOLLAR_PARAM, _CAST, _ILIKE, _ROWNUM, _ON_CONFLICT, _GETDATE),
    "oracle": (
        _BACKTICK,
        _TOP,
        _AT_PARAM,
        _DOLLAR_PARAM,
        _CAST,
        _ILIKE,
        _LIMIT,
        _ON_CONFLICT,
        _NOW,
        _IFNULL,
        _ISNULL_CALL,
        _GETDATE,
    ),
}
_FOREIGN_SYNTAX["postgres"] = _FOREIGN_SYNTAX["postgresql"]
#: The engines whose dialect has no ``[name]`` identifiers.
_NO_BRACKETS = frozenset({"postgresql", "postgres", "mysql", "mariadb", "oracle"})
#: SQL Server's own ``::``: a CLR type's static method (``geography::Point(...)``) or a
#: securable's class (``SCHEMA::sales``); removed before the ``::`` cast check.
_TSQL_SCOPE_RE = re.compile(
    r"\b(?:geography|geometry|hierarchyid|schema|object|type|assembly|certificate|login|"
    r"user|role|database|endpoint|server|xml\s+schema\s+collection)\s*::",
    re.IGNORECASE,
)


def foreign_syntax(text: str, source_engine: str) -> str | None:
    """The first construct of another dialect's in ``text`` for ``source_engine``, from
    the fixed list :data:`_FOREIGN_SYNTAX`, or None. An engine with no list here (Db2,
    for one) is checked by its parser only."""
    from src.shared.sql_spans import keyword_text

    engine = (source_engine or "").strip().lower()
    code = keyword_text(text, engine)
    if engine == "sqlserver":
        code = _TSQL_SCOPE_RE.sub(" ", code)
    if engine in _NO_BRACKETS and _bracket_name(code):
        return "a SQL Server bracket name ([...])"
    for what, pattern in _FOREIGN_SYNTAX.get(engine, ()):
        if pattern.search(code):
            return what
    return None


def _part(raw: Any) -> StatementPart:
    if isinstance(raw, StatementPart):
        return raw
    try:
        return StatementPart.model_validate(raw)
    except ValidationError as exc:
        raise InvalidPartError(
            f'a part is {{"from_query_id": ..., "query_text": ...}}: {exc.errors()[0]["msg"]} '
            f"({raw!r:.200})"
        ) from exc


def _part_rows(
    source: Mapping[str, Any],
    tables: list[Mapping[str, Any]],
    views: Mapping[str, list[str] | None],
    kept: set[str],
    patterns: dict[str, Mapping[str, Any]],
    whole: set[str],
    crossing: set[str],
    unplaced: dict[str, str],
    parts: Any,
) -> list[dict[str, Any]]:
    """Each part as a statement of the cut collection, in the order given: a copy of the
    statement it was cut from (every execution statistic inherited), with the part's
    text, the facts the parser reads from it and its library-built id."""
    from src.tools.sql.query_facts import source_engine_of

    if parts is None or isinstance(parts, str | bytes | Mapping) or not isinstance(parts, Iterable):
        raise InvalidPartError(f"parts is a list of parts, not {type(parts).__name__}")
    engine = source_engine_of(source)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in parts:
        part = _part(raw)
        qid = part.from_query_id
        if qid not in patterns:
            raise UnknownStatementError(qid, "no statement of the collection has this id")
        if qid in whole:
            raise PartOfKeptStatementError(
                qid, "the cut keeps this statement whole, so it takes no part"
            )
        if qid not in crossing and qid not in unplaced:
            raise NotACrossingStatementError(
                qid, "the statement does not cross the cut: none of its tables is kept"
            )
        if qid in seen:
            raise DuplicatePartError(qid, "another part names the same statement")
        seen.add(qid)
        # The caller's own text is checked for another dialect's syntax; redaction would
        # rewrite some of it (SQL Server reads ``$1`` as a money literal).
        found = foreign_syntax(part.query_text, engine)
        if found:
            raise PartDialectError(qid, engine, found)
        text = _redacted(part.query_text, engine)
        if statement_count(text, engine) != 1:
            raise PartParseError(
                qid, f"the {engine} parser does not read the text as one statement"
            )
        row = _parsed(source, tables, {**copy.deepcopy(dict(patterns[qid])), "query_text": text})
        read = _tables_of(row)
        if not read or "unknown" in read:
            raise PartParseError(
                qid, f"the {engine} parser reads no table of the collection in the text"
            )
        outside = [t for t in read if t not in kept]
        if outside:
            raise PartTableOutsideCutError(qid, outside)
        if qid not in unplaced:
            # A view counts as the base tables it reads, on both sides.
            read_by_statement = _expanded(_tables_of(patterns[qid]), views)
            extra = sorted(_expanded(read, views) - read_by_statement)
            if extra:
                raise PartTableNotInStatementError(qid, extra)
        part_id = part_query_id(qid, kept)
        if part_id in patterns:
            raise PartIdCollisionError(qid, f"{part_id} is already a statement of the collection")
        rows.append({**row, "query_id": part_id, "_from": qid})
    return rows


def _parsed(
    source: Mapping[str, Any], tables: list[Mapping[str, Any]], pattern: dict[str, Any]
) -> dict[str, Any]:
    """``pattern`` with the facts the library's parser reads from its text, against the
    whole collection's tables and views (``rederive_query_facts``, the reader
    ``assess()`` uses on a collector output)."""
    from src.tools.database.offline_parser import rederive_query_facts
    from src.tools.sql.query_facts import source_engine_of

    names = ("table_id", "table_name", "schema_name")
    schema = source.get("database_schema") or {}
    mini = {
        "metadata": source.get("metadata"),
        "database_schema": {
            "tables": [{k: t.get(k) for k in names} for t in tables],
            "views": schema.get("views"),
        },
        "queries": {"query_patterns": [pattern]},
    }
    row: dict[str, Any] = rederive_query_facts(mini, source_engine_of(source))["queries"][
        "query_patterns"
    ][0]
    return row
