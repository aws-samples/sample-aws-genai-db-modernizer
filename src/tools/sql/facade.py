"""The one module that calls sqlglot: table references from SQL text (#557).

Every caller asks this facade for a small, frozen fact model
(:class:`TableReferences`) and never sees sqlglot's syntax tree, so a sqlglot
upgrade or a parser swap touches one file. The source engine picks the
dialect (``postgresql`` is ``postgres`` and ``sqlserver`` is ``tsql`` in
sqlglot); collector digest text is normalized first
(:func:`src.tools.sql.normalize.normalize_digest_text`), so a MySQL digest such
as ``IN (...) LIMIT ?, ...`` parses. Three dialect gaps in sqlglot are bridged
here too: Oracle positional binds (``:1``) are renamed (``:b1``), a space is
added after a PostgreSQL ``$1`` placeholder written flush against the next
token (``$1=$2``, never inside an identifier such as ``price$1usd``), and
T-SQL's ``DELETE``/``UPDATE``/``INSERT TOP (n)`` row limit is dropped when it
is a number, ``@variable`` or ``?``. Each rewrite runs only outside string
literals, quoted identifiers and comments, and none can add, remove or
rename a table name (tests pin each case).

What counts as a table reference:

- every table node in every statement (``FROM``/``JOIN``, subqueries and
  derived tables, ``INSERT INTO``, ``UPDATE``, ``DELETE FROM``, ``MERGE``
  target and source), in the order it appears in the text, duplicates
  collapsed;
- not a ``WITH``-defined CTE name where that CTE is visible (the #682 rules:
  later CTE bodies and the statement after the WITH list; its own body only
  when the CTE is recursive, i.e. ``RECURSIVE`` or a ``UNION`` in the body);
  a qualified name is never a CTE, and names compare case-insensitively;
- not MySQL/Oracle's ``DUAL`` pseudo-table, and not a table function such as
  ``generate_series(...)``;
- not a T-SQL temp table (``#tmp``, ``##tmp``) or table variable (``@t``):
  each is local to the session or batch, never a collected table, and the
  regex fallback never read one either. The query-shape facts still count a
  join to one.

Size cap: text longer than ``MAX_SQL_CHARS`` is not parsed or normalized; it
falls back with reason ``too_long``.

Fallback: when sqlglot raises, returns a generic ``Command`` node (syntax it
doesn't model, e.g. ``REFRESH MATERIALIZED VIEW``) or something that is not a
statement (an expression it read out of garbage text), or the text is empty, the
facade returns the regex extractor's answer
(:func:`src.tools.sql.regex_tables.regex_table_refs`) with
``parsed_by="regex"`` and the reason. It never raises. MySQL's ``LOAD DATA
... INTO TABLE t``, which sqlglot can't parse, takes this path; the regex
reads its target table (:mod:`src.tools.sql.bulk_load`, #777).

Resolving a reference to a collection's table id (``<database>.<table>``)
stays with the caller: it needs the collection's table list.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp

from src.tools.sql.normalize import normalize_digest_text, quoted_mask
from src.tools.sql.regex_facts import (
    regex_has_aggregation,
    regex_has_joins,
    regex_has_subquery,
    regex_join_count,
    regex_wildcard_leading,
)
from src.tools.sql.regex_tables import regex_table_refs

# sqlglot logs a warning for every statement it parses as a generic Command.
# The facade records that as ``fallback_reason="command"`` instead, so the
# warning would only add noise to compact script output.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# Source engine (``DatabaseEngine`` in src/contracts/collector_input.py, plus
# the ``postgres`` alias) -> sqlglot dialect. sqlglot has no DB2 dialect, so
# DB2 (deferred, #555) and anything unknown use sqlglot's generic dialect.
_DIALECTS = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgresql": "postgres",
    "postgres": "postgres",
    "sqlserver": "tsql",
    "oracle": "oracle",
}

# Longer text is not parsed: it falls back to the regex with reason
# ``too_long``, so one huge statement can't slow a collection down. The
# samples' longest query is about 5,300 characters.
MAX_SQL_CHARS = 100_000

# The bridges below run only outside string literals, quoted identifiers and
# comments (the normalizer's scan), so none of them can rename a table.

# Oracle positional binds (``:1``) that sqlglot's Oracle dialect rejects; named
# binds (``:b1``, ``:name``) parse. The lookbehind keeps ``12:30`` intact.
_ORACLE_POSITIONAL_BIND_RE = re.compile(r"(?<![\w:]):(\d+)\b")

# PostgreSQL ``$1`` placeholders written with no space before the next token
# (``$1=$2``, ``IN ($3,$4)``): sqlglot's tokenizer reads ``$3,$`` as the start
# of a dollar-quoted string, so the facade adds the space. The lookbehind
# keeps identifiers that contain ``$<digits>`` (``price$1usd``) whole.
_PG_PLACEHOLDER_RE = re.compile(r"(?<![\w$])(\$\d+)(?=[^\s\d])")

# T-SQL ``DELETE TOP (n) FROM t`` / ``UPDATE TOP (n) t`` / ``INSERT TOP (n) INTO
# t``: sqlglot reads ``TOP (5000)`` as a table named TOP (a MySQL-style
# multi-table DELETE) or fails, so the facade drops the row limit. Only a
# number, a ``@variable`` or a ``?`` placeholder is dropped; any other
# expression (a subquery can name a table) is left for sqlglot or the fallback.
_TSQL_DML_TOP_RE = re.compile(
    r"\b(DELETE|UPDATE|INSERT)\s+TOP\s*\(\s*(?:\d+|@\w+|\?)\s*\)(?:\s*PERCENT\b)?",
    re.IGNORECASE,
)

# A parsed statement must be one of these; anything else (a stray expression
# sqlglot read out of garbage text) is not a statement.
_STATEMENT_TYPES = (
    exp.Query,
    exp.DML,
    exp.DDL,
    exp.Show,
    exp.Set,
    exp.Use,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)

# Pseudo-tables that name no stored data.
_PSEUDO_TABLES = frozenset({"dual"})

ParsedBy = Literal["sqlglot", "regex"]
# ``facade_error`` is never produced here: the offline parser records it when
# this module raises despite its contract, and it uses the regex answer.
FallbackReason = Literal[
    "parse_error", "not_a_statement", "command", "empty", "too_long", "facade_error"
]


@dataclass(frozen=True)
class TableRef:
    """One table reference as written: ``[catalog.][schema.]name``."""

    name: str
    schema: str | None = None
    catalog: str | None = None

    @property
    def qualifiers(self) -> list[str]:
        """``[catalog, schema]`` without the empty parts (what the resolver takes)."""
        return [q for q in (self.catalog, self.schema) if q]

    @property
    def written(self) -> str:
        return ".".join([*self.qualifiers, self.name])


@dataclass(frozen=True)
class TableReferences:
    """The table references in one piece of SQL text.

    Attributes:
        tables: Table references in first-seen order, duplicates and CTE
            names removed.
        cte_names: The ``WITH``-defined names in the text (lower-case), so a
            caller can tell a dropped CTE from a missing table.
        parsed_by: ``"sqlglot"``, or ``"regex"`` when the facade fell back.
        fallback_reason: Why it fell back, or None.
        dialect: The sqlglot dialect used (``""`` is sqlglot's generic one).
        truncated: The text looks cut off by a digest length cap (from the
            normalization step).
    """

    tables: tuple[TableRef, ...]
    cte_names: tuple[str, ...]
    parsed_by: ParsedBy
    fallback_reason: FallbackReason | None
    dialect: str
    truncated: bool


@dataclass(frozen=True)
class SqlFacts:
    """Query-shape facts about one piece of SQL text (#796).

    Read from the syntax tree, so a comment, a string literal or a column name
    (``count``, ``user_selectable``) is never read as SQL; the regex facts
    (:mod:`src.tools.sql.regex_facts`) when the facade falls back, so a
    statement sqlglot can't read keeps exactly its pre-#796 answer.

    Attributes:
        join_count: Joins to another row source: every ``JOIN`` clause and
            every further item of a ``FROM``/``USING`` list (``FROM a, b`` is
            one join), in every statement and subquery. A join to a table
            function of the row (``CROSS APPLY OPENJSON(...)``, ``CROSS JOIN
            unnest(...)``, ``generate_series(...)``) reads no other table, so
            it is not counted. ``UPDATE ... FROM`` / ``DELETE ... USING``
            count their list's further items, not the first one, the way the
            regex never counted them either.
        has_joins: ``join_count > 0`` (the regex fallback keeps its own
            `` join `` test).
        has_aggregation: A ``GROUP BY``, or an aggregate function -- one that
            combines many rows' values into one (``COUNT``, ``SUM``, ``AVG``,
            ``MIN``, ``MAX``, ``GROUP_CONCAT``, ``STRING_AGG``, ``ARRAY_AGG``,
            ...), windowed (``COUNT(*) OVER ()``) or not. The ranking and
            offset window functions (``ROW_NUMBER``, ``RANK``, ``DENSE_RANK``,
            ``PERCENT_RANK``, ``CUME_DIST``, ``NTILE``, ``LAG``, ``LEAD``,
            ``FIRST_VALUE``, ``LAST_VALUE``, ``NTH_VALUE``) combine nothing,
            so alone they are not one.
        has_subquery: More than one ``SELECT`` in the statement: a subquery,
            a derived table, a CTE body, a second ``UNION``/``EXCEPT`` branch
            (the regex's meaning, without its misreads).
        wildcard_leading: A ``LIKE``/``ILIKE`` (``NOT`` and ``ESCAPE``
            included) whose pattern starts with ``%``: a string literal, or a
            concatenation (``'%' || ?``, ``'%' + @t``, ``CONCAT('%', ?)``)
            whose first part is one. A bind parameter (``LIKE ?``) is not:
            its value is unknown.
        parsed_by: ``"sqlglot"``, or ``"regex"`` when the facade fell back.
        fallback_reason: Why it fell back, or None.
        dialect: The sqlglot dialect used (``""`` is sqlglot's generic one).
    """

    join_count: int
    has_joins: bool
    has_aggregation: bool
    has_subquery: bool
    wildcard_leading: bool
    parsed_by: ParsedBy
    fallback_reason: FallbackReason | None
    dialect: str


@dataclass(frozen=True)
class SqlReading:
    """Everything the facade reads from one piece of SQL text, from one parse."""

    tables: TableReferences
    facts: SqlFacts


def sqlglot_dialect(source_engine: str) -> str:
    """The sqlglot dialect for a source engine (``""`` = generic)."""
    return _DIALECTS.get((source_engine or "").strip().lower(), "")


def table_references(sql: str, source_engine: str) -> TableReferences:
    """The table references in ``sql``, read with the source engine's dialect.

    Never raises: unparseable text, a generic ``Command`` node, empty text, or
    text over ``MAX_SQL_CHARS`` falls back to the regex extractor and says so
    in ``fallback_reason``. Over the cap the text is not normalized either, so
    ``truncated`` is False.
    """
    return _tables(_parse(sql, source_engine))


def sql_facts(sql: str, source_engine: str) -> SqlFacts:
    """The query-shape facts of ``sql`` (:class:`SqlFacts`), read with the
    source engine's dialect; the regex facts when it falls back, exactly as
    :func:`table_references` does. Never raises."""
    return _facts(_parse(sql, source_engine))


def read_sql(sql: str, source_engine: str) -> SqlReading:
    """:func:`table_references` and :func:`sql_facts` from one parse."""
    parsed = _parse(sql, source_engine)
    return SqlReading(tables=_tables(parsed), facts=_facts(parsed))


@dataclass(frozen=True)
class _Parsed:
    """One parse: the statements, or why there are none (the fallback reason)."""

    sql: str
    dialect: str
    truncated: bool
    statements: tuple[exp.Expr, ...] = ()
    fallback_reason: FallbackReason | None = None


def _parse(sql: str, source_engine: str) -> _Parsed:
    """Parse ``sql`` once, or say why the regex fallback answers instead."""
    sql = sql or ""
    dialect = sqlglot_dialect(source_engine)
    if len(sql) > MAX_SQL_CHARS:
        return _Parsed(sql, dialect, truncated=False, fallback_reason="too_long")
    normalized = normalize_digest_text(sql, source_engine or "")

    def fallback(reason: FallbackReason) -> _Parsed:
        return _Parsed(sql, dialect, truncated=normalized.truncated, fallback_reason=reason)

    if not normalized.text.strip():
        return fallback("empty")
    text = normalized.text
    if dialect == "oracle":
        text = _sub_outside_quotes(_ORACLE_POSITIONAL_BIND_RE, r":b\1", text, source_engine)
    elif dialect == "postgres":
        text = _sub_outside_quotes(_PG_PLACEHOLDER_RE, r"\1 ", text, source_engine)
    elif dialect == "tsql":
        text = _sub_outside_quotes(_TSQL_DML_TOP_RE, r"\1", text, source_engine)
    try:
        statements = [s for s in sqlglot.parse(text, read=dialect or None) if s]
    except Exception:  # noqa: BLE001 -- any parser failure falls back, by contract
        return fallback("parse_error")
    if not statements:
        return fallback("empty")
    if any(isinstance(node, exp.Command) for s in statements for node in s.walk()):
        return fallback("command")
    if not all(isinstance(s, _STATEMENT_TYPES) for s in statements):
        return fallback("not_a_statement")
    return _Parsed(sql, dialect, truncated=normalized.truncated, statements=tuple(statements))


def _tables(parsed: _Parsed) -> TableReferences:
    if parsed.fallback_reason is not None:
        return _fallback(
            parsed.sql, parsed.fallback_reason, parsed.dialect, truncated=parsed.truncated
        )
    return _from_statements(list(parsed.statements), parsed.dialect, parsed.truncated)


def _facts(parsed: _Parsed) -> SqlFacts:
    if parsed.fallback_reason is not None:
        return _regex_facts(parsed.sql, parsed.fallback_reason, parsed.dialect)
    return _facts_from_statements(parsed.statements, parsed.dialect)


def regex_answer(sql: str, source_engine: str, reason: FallbackReason) -> TableReferences:
    """The regex extractor's answer as a fallback with ``reason``, without
    trying sqlglot (for a caller whose :func:`table_references` call raised)."""
    return _fallback(sql or "", reason, sqlglot_dialect(source_engine), truncated=False)


def regex_reading(sql: str, source_engine: str, reason: FallbackReason) -> SqlReading:
    """:func:`regex_answer` and the regex facts, for a caller whose
    :func:`read_sql` call raised."""
    dialect = sqlglot_dialect(source_engine)
    return SqlReading(
        tables=_fallback(sql or "", reason, dialect, truncated=False),
        facts=_regex_facts(sql or "", reason, dialect),
    )


def _fallback(
    sql: str, reason: FallbackReason, dialect: str, *, truncated: bool
) -> TableReferences:
    """The regex extractor's answer, labelled as a fallback."""
    refs = [_ref_from_regex(q, table) for q, table in regex_table_refs(sql, dialect)]
    return TableReferences(
        tables=tuple(dict.fromkeys(refs)),
        cte_names=(),
        parsed_by="regex",
        fallback_reason=reason,
        dialect=dialect,
        truncated=truncated,
    )


def _sub_outside_quotes(pattern: re.Pattern[str], repl: str, text: str, engine: str) -> str:
    """``pattern.sub(repl, text)`` for matches wholly outside literals,
    quoted identifiers and comments."""
    if pattern.search(text) is None:
        return text
    mask = quoted_mask(text, engine or "")

    def one(m: re.Match[str]) -> str:
        return m.group(0) if any(mask[m.start() : m.end()]) else m.expand(repl)

    return pattern.sub(one, text)


def _from_statements(statements: list[exp.Expr], dialect: str, truncated: bool) -> TableReferences:
    """The table references in parsed statements (see the module docstring)."""
    found: list[tuple[int, TableRef]] = []
    cte_names: list[str] = []
    for statement in statements:
        cte_names.extend(c.alias_or_name.lower() for c in statement.find_all(exp.CTE))
        for table in statement.find_all(exp.Table):
            ref = _ref_from_node(table)
            if ref is None or _is_temp_table(table) or _is_cte_reference(table, ref):
                continue
            found.append((_position(table), ref))
    found.sort(key=lambda item: item[0])
    return TableReferences(
        tables=tuple(dict.fromkeys(ref for _, ref in found)),
        cte_names=tuple(dict.fromkeys(cte_names)),
        parsed_by="sqlglot",
        fallback_reason=None,
        dialect=dialect,
        truncated=truncated,
    )


def _ref_from_regex(qualifiers: list[str], table: str) -> TableRef:
    if len(qualifiers) == 2:
        return TableRef(name=table, schema=qualifiers[1], catalog=qualifiers[0])
    if len(qualifiers) == 1:
        return TableRef(name=table, schema=qualifiers[0])
    return TableRef(name=table)


def _ref_from_node(table: exp.Table) -> TableRef | None:
    """A TableRef for a stored-table node; None for table functions and DUAL."""
    if not isinstance(table.this, exp.Identifier) or not table.name:
        return None
    ref = TableRef(name=table.name, schema=table.db or None, catalog=table.catalog or None)
    if not ref.qualifiers and ref.name.lower() in _PSEUDO_TABLES:
        return None
    return ref


def _is_temp_table(table: exp.Table) -> bool:
    """A T-SQL ``#local`` or ``##global`` temp table (sqlglot drops the ``#``)."""
    ident = table.this
    return bool(ident.args.get("temporary") or ident.args.get("global_"))


def _position(table: exp.Table) -> int:
    start = table.this.meta.get("start") if isinstance(table.this, exp.Identifier) else None
    return int(start) if isinstance(start, int) else 1 << 62


def _cte_names(ctes: list[exp.CTE]) -> set[str]:
    return {c.alias_or_name.lower() for c in ctes}


def _is_cte_reference(table: exp.Table, ref: TableRef) -> bool:
    """Whether an unqualified table node names a CTE visible where it stands.

    Walks up from the table. A statement that owns a WITH list makes every CTE
    in it visible to its body. Reaching a CTE body instead makes only the CTEs
    listed before it visible (plus itself when recursive); the walk then skips
    that WITH's owning statement and carries on outward, where an enclosing
    query's own WITH list may still apply.
    """
    if ref.qualifiers:
        return False
    name = ref.name.lower()
    node = table.parent
    while node is not None:
        if isinstance(node, exp.CTE) and isinstance(node.parent, exp.With):
            with_node = node.parent
            ctes = list(with_node.expressions)
            index = next(i for i, c in enumerate(ctes) if c is node)
            recursive = bool(with_node.args.get("recursive")) or node.find(exp.Union) is not None
            if name in _cte_names(ctes[: index + 1] if recursive else ctes[:index]):
                return True
            owner = with_node.parent
            node = owner.parent if owner is not None else None
            continue
        owned = node.args.get("with_")
        if isinstance(owned, exp.With) and name in _cte_names(list(owned.expressions)):
            return True
        node = node.parent
    return False


# ---------------------------------------------------------------------------
# Query-shape facts (#796)
# ---------------------------------------------------------------------------

# Window functions that rank or offset rows and combine no values: sqlglot
# models most of them as aggregate functions, but alone they aggregate nothing.
_RANKING_WINDOW_FUNCTIONS: tuple[type[exp.Expr], ...] = (
    exp.RowNumber,
    exp.Rank,
    exp.DenseRank,
    exp.PercentRank,
    exp.CumeDist,
    exp.Ntile,
    exp.Lag,
    exp.Lead,
    exp.FirstValue,
    exp.LastValue,
    exp.NthValue,
)

# Pattern-match predicates whose pattern is ``.expression``.
_LIKE_NODES: tuple[type[exp.Expr], ...] = (exp.Like, exp.ILike)


def _regex_facts(sql: str, reason: FallbackReason, dialect: str) -> SqlFacts:
    """The regex facts (:mod:`src.tools.sql.regex_facts`), labelled as a fallback."""
    return SqlFacts(
        join_count=regex_join_count(sql),
        has_joins=regex_has_joins(sql),
        has_aggregation=regex_has_aggregation(sql),
        has_subquery=regex_has_subquery(sql),
        wildcard_leading=regex_wildcard_leading(sql),
        parsed_by="regex",
        fallback_reason=reason,
        dialect=dialect,
    )


def _facts_from_statements(statements: tuple[exp.Expr, ...], dialect: str) -> SqlFacts:
    """The facts of parsed statements (see :class:`SqlFacts`)."""
    nodes = [node for statement in statements for node in statement.walk()]
    join_count = sum(
        1 for node in nodes if isinstance(node, exp.Join) and _joins_a_row_source(node)
    )
    return SqlFacts(
        join_count=join_count,
        has_joins=join_count > 0,
        has_aggregation=any(_is_aggregation(node) for node in nodes),
        has_subquery=sum(1 for node in nodes if isinstance(node, exp.Select)) > 1,
        wildcard_leading=any(
            isinstance(node, _LIKE_NODES) and _starts_with_wildcard(node.expression)
            for node in nodes
        ),
        parsed_by="sqlglot",
        fallback_reason=None,
        dialect=dialect,
    )


def _joins_a_row_source(join: exp.Join) -> bool:
    """Whether a join reads a stored table or a subquery, not only a table function."""
    source = join.this
    if isinstance(source, exp.Table):
        return _ref_from_node(source) is not None or source.find(exp.Select) is not None
    return source.find(exp.Select) is not None


def _is_aggregation(node: exp.Expr) -> bool:
    if isinstance(node, exp.Group):
        return True
    return isinstance(node, exp.AggFunc) and not isinstance(node, _RANKING_WINDOW_FUNCTIONS)


def _starts_with_wildcard(pattern: exp.Expr | None) -> bool:
    """Whether a LIKE pattern's first part is a string literal starting with ``%``."""
    while pattern is not None:
        if isinstance(pattern, exp.Literal):
            return bool(pattern.is_string and str(pattern.this).startswith("%"))
        if isinstance(pattern, exp.Paren | exp.DPipe | exp.Add):
            pattern = pattern.this
        elif isinstance(pattern, exp.Concat | exp.ConcatWs):
            parts = list(pattern.expressions)
            if isinstance(pattern, exp.ConcatWs):
                parts = parts[1:]  # the first argument is the separator
            pattern = parts[0] if parts else None
        else:
            return False
    return False
