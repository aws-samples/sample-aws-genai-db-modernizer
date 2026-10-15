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
from dataclasses import dataclass, field, replace
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
    """One table reference as written: ``[catalog.][schema.]name``.

    ``quoted`` says the name itself was a quoted identifier (``"Topics"``,
    ``[Orders]``), which keeps its case in PostgreSQL, Oracle and DB2. It is
    not part of the reference's identity: ``"topics"`` and ``topics`` are one
    reference (the first one seen is kept). A regex-fallback reference is
    never marked quoted.
    """

    name: str
    schema: str | None = None
    catalog: str | None = None
    quoted: bool = field(default=False, compare=False)

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
    ref = TableRef(
        name=table.name,
        schema=table.db or None,
        catalog=table.catalog or None,
        quoted=bool(table.this.args.get("quoted")),
    )
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


# ---------------------------------------------------------------------------
# Access-pattern facts (#728, ADR-031 Decision 14 Amendment 5)
#
# What a statement reads, per SELECT scope, as plain facts: which tables it
# reads (by alias), which columns it pins to a value, which it bounds by a
# range, which column pairs join two of its tables, and its order, row limit,
# grouping and aggregates. No schema knowledge and no class: the caller
# (``src.agents.referee.access_pattern``) adds the keys and decides the class.
# ---------------------------------------------------------------------------

StatementKind = Literal["select", "insert", "update", "delete", "merge", "other"]

# Functions that are a text or similarity match, whatever the dialect calls them.
_TEXT_SEARCH_FUNCTIONS = frozenset(
    {
        "to_tsvector",
        "to_tsquery",
        "plainto_tsquery",
        "phraseto_tsquery",
        "websearch_to_tsquery",
        "ts_rank",
        "ts_rank_cd",
        "similarity",
        "word_similarity",
        "strict_word_similarity",
        "freetext",
        "freetexttable",
        "containstable",
    }
)
_TEXT_SEARCH_NODES: tuple[type[exp.Expr], ...] = (
    exp.MatchAgainst,
    exp.RegexpLike,
    exp.RegexpILike,
    exp.SimilarTo,
    exp.Contains,
)
_RANGE_NODES: tuple[type[exp.Expr], ...] = (exp.GT, exp.GTE, exp.LT, exp.LTE)
# T-SQL table hints that skip rows another transaction holds (like SKIP LOCKED).
_ROW_SKIPPING_HINTS = frozenset({"READPAST"})


@dataclass(frozen=True)
class ColumnFact:
    """A column as written: its qualifier (alias or table, lower-case) and name."""

    qualifier: str | None
    name: str


@dataclass(frozen=True)
class ScopeSource:
    """One table a scope reads, under the alias the scope uses for it.

    ``table`` is None for a derived table, a table function or a CTE; ``derived``
    is then the index (in :attr:`AccessFacts.scopes`) of the scope that produces
    its rows, when there is exactly one. ``optional``: the source is the
    optional side of a ``LEFT`` or ``FULL`` join (its rows may be missing).
    ``function``: a table function or a function of another source's row
    (``unnest(...)``, ``CROSS APPLY OPENJSON(...)``, ``generate_series(...)``),
    which reads no stored table.
    """

    alias: str
    table: TableRef | None
    derived: int | None = None
    optional: bool = False
    function: bool = False


@dataclass(frozen=True)
class ScopeFacts:
    """The facts of one SELECT (or UPDATE/DELETE) scope.

    ``equalities``: columns pinned to a value by a top-level ``AND`` term of the
    WHERE or a JOIN condition (``col = <value>``, ``col IN (...)``, ``col =
    ANY(...)``); the value holds no column of this scope (a literal, a
    placeholder, a subquery or an outer column). ``ranges``: ``<``, ``<=``,
    ``>``, ``>=``, ``BETWEEN`` and a literal-prefix ``LIKE 'abc%'``. ``joins``:
    ``col = col`` terms over two of the scope's columns, plus ``JOIN ... USING``.
    ``other_filters``: every other predicate term (an ``OR`` group, ``NOT IN``,
    ``IS NULL``, a function test, ...). ``order_by``: each sort item's column,
    or None for an expression. ``limit``: the row count as written (digits), or
    ``"?"`` for a placeholder, ``PERCENT`` or ``WITH TIES`` (not a row count);
    None with no limit. ``skip_locked``: ``SKIP LOCKED`` or a ``READPAST`` hint.
    ``text_search``: in this scope's own predicates or projection (not a nested
    query's), a ``LIKE``/``ILIKE`` whose pattern starts with a wildcard
    (``wildcard_leading``, the same definition as :class:`SqlFacts`), a
    regular-expression or ``SIMILAR TO`` match, ``MATCH ... AGAINST``, ``@@``,
    ``CONTAINS`` or a full-text or similarity function. ``like_parameter``: a
    ``LIKE``/``ILIKE`` whose whole pattern is a bind parameter (``LIKE ?``): its
    value is unknown, so it is neither a leading wildcard nor a prefix.
    """

    sources: tuple[ScopeSource, ...]
    equalities: tuple[ColumnFact, ...] = ()
    ranges: tuple[ColumnFact, ...] = ()
    joins: tuple[tuple[ColumnFact, ColumnFact], ...] = ()
    other_filters: int = 0
    order_by: tuple[ColumnFact | None, ...] = ()
    limit: str | None = None
    has_where: bool = False
    group_by: bool = False
    aggregate: bool = False
    skip_locked: bool = False
    text_search: bool = False
    like_parameter: bool = False


@dataclass(frozen=True)
class AccessFacts:
    """The access-pattern facts of one piece of SQL text.

    ``kind`` is the first statement's kind (``other`` for SET, SHOW,
    transaction control, DDL, ...). ``inserts_values``: an ``INSERT ... VALUES``
    (or ``DEFAULT VALUES``). ``windowed_aggregate``: a window function that
    combines values (``SUM(...) OVER``, ``COUNT(*) OVER ()``) anywhere; the
    ranking and offset window functions (``ROW_NUMBER``, ``RANK``, ``LAG``, ...)
    combine nothing, so alone they are not one (the ``has_aggregation``
    definition of :class:`SqlFacts`). When the facade falls back, ``parsed_by``
    is ``"regex"`` and ``scopes`` is empty: no fact is guessed from the text.
    """

    kind: StatementKind
    scopes: tuple[ScopeFacts, ...]
    inserts_values: bool
    windowed_aggregate: bool
    parsed_by: ParsedBy
    fallback_reason: FallbackReason | None


def access_facts(sql: str, source_engine: str) -> AccessFacts:
    """The access-pattern facts of ``sql``, read with the source engine's dialect.

    Never raises; text the facade cannot parse comes back with
    ``parsed_by="regex"``, the reason and no scopes. A ``SHOW`` (which sqlglot
    reads as a generic command) is kind ``other``.
    """
    parsed = _parse(sql, source_engine)
    reason = parsed.fallback_reason
    if reason is not None:
        return AccessFacts(
            kind="other" if reason == "command" else "select",
            scopes=(),
            inserts_values=False,
            windowed_aggregate=False,
            parsed_by="regex",
            fallback_reason=reason,
        )
    try:
        return _access_facts(list(parsed.statements))
    except Exception:  # noqa: BLE001 -- a tree the walk does not expect falls back too
        return AccessFacts("select", (), False, False, "regex", "parse_error")


def _statement_kind(statement: exp.Expr) -> StatementKind:
    if isinstance(statement, exp.Query):
        return "select"
    for node_type, kind in (
        (exp.Insert, "insert"),
        (exp.Update, "update"),
        (exp.Delete, "delete"),
        (exp.Merge, "merge"),
    ):
        if isinstance(statement, node_type):
            return kind  # type: ignore[return-value]
    return "other"


def _access_facts(statements: list[exp.Expr]) -> AccessFacts:
    kind = _statement_kind(statements[0])
    scopes: list[ScopeFacts] = []
    inserts_values = False
    for statement in statements:
        if isinstance(statement, exp.Insert):
            body = statement.expression
            inserts_values = inserts_values or body is None or isinstance(body, exp.Values)
        roots: list[exp.Expr] = (
            [statement] if isinstance(statement, (exp.Update, exp.Delete)) else []
        )
        roots.extend(statement.find_all(exp.Select))
        index_of = {id(root): i + len(scopes) for i, root in enumerate(roots)}
        cte_bodies = _cte_bodies(statement, index_of)
        scopes.extend(_scope_facts(root, index_of, cte_bodies) for root in roots)
    return AccessFacts(
        kind=kind,
        scopes=tuple(scopes),
        inserts_values=inserts_values,
        windowed_aggregate=any(
            isinstance(node, exp.Window) and _is_aggregation(node.this)
            for s in statements
            for node in s.walk()
        ),
        parsed_by="sqlglot",
        fallback_reason=None,
    )


def _cte_bodies(statement: exp.Expr, index_of: dict[int, int]) -> dict[str, int]:
    """CTE name (lower-case) -> scope index of its body, when the body is one SELECT."""
    bodies: dict[str, int] = {}
    for cte in statement.find_all(exp.CTE):
        body = cte.this
        if isinstance(body, exp.Select) and id(body) in index_of:
            bodies[cte.alias_or_name.lower()] = index_of[id(body)]
    return bodies


def _is_text_search(node: exp.Expr) -> bool:
    if isinstance(node, _TEXT_SEARCH_NODES):
        return True
    if isinstance(node, _LIKE_NODES):
        return _like_kind(node) == "leading_wildcard"
    if isinstance(node, exp.Func):
        name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
        return name in _TEXT_SEARCH_FUNCTIONS
    return False


def _pattern_pieces(pattern: exp.Expr) -> list[exp.Expr]:
    """The parts of a concatenated pattern (``||``, ``+``, ``CONCAT(...)``), in order."""
    while isinstance(pattern, exp.Paren):
        pattern = pattern.this
    if isinstance(pattern, (exp.DPipe, exp.Add)):
        return _pattern_pieces(pattern.this) + _pattern_pieces(pattern.expression)
    if isinstance(pattern, exp.Concat):
        return [p for e in pattern.expressions for p in _pattern_pieces(e)]
    return [pattern]


# A bind parameter. ``NULL`` too: the digest step writes a MySQL ``?`` as ``NULL``
# (:mod:`src.tools.sql.normalize`), and a literal ``LIKE NULL`` matches nothing.
_BIND_NODES: tuple[type[exp.Expr], ...] = (exp.Placeholder, exp.Parameter, exp.Null)

LikeKind = Literal["leading_wildcard", "prefix", "parameter", "other"]


def _like_kind(node: exp.Expr) -> LikeKind:
    """How a ``LIKE``/``ILIKE`` pattern addresses rows.

    ``leading_wildcard``: the pattern's first part is a literal starting with ``%``
    (``wildcard_leading``). ``prefix``: it is anchored at its start, a range on the
    column (``'abc%'``, ``? || '%'``). ``parameter``: the whole pattern is a bind
    parameter, so its value is unknown. ``other``: anything else (``'_bc%'``, a
    column).
    """
    pattern = node.expression
    if _starts_with_wildcard(pattern):
        return "leading_wildcard"
    pieces = _pattern_pieces(pattern)
    first, last = pieces[0], pieces[-1]
    if isinstance(first, exp.Literal) and first.is_string:
        text = first.this or ""
        return "prefix" if text and text[0] not in "%_" else "other"
    if (
        len(pieces) > 1
        and isinstance(last, exp.Literal)
        and last.is_string
        and str(last.this or "").endswith("%")
    ):
        return "prefix"
    if len(pieces) == 1 and isinstance(first, _BIND_NODES):
        return "parameter"
    return "other"


def _own_nodes(root: exp.Expr) -> list[exp.Expr]:
    """``root`` and its descendants, not looking inside a nested query."""
    found: list[exp.Expr] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node is not root and isinstance(node, (exp.Select, exp.Subquery)):
            continue
        found.append(node)
        stack.extend(node.iter_expressions())
    return found


def _scope_sources(
    root: exp.Expr, index_of: dict[int, int], cte_bodies: dict[str, int]
) -> tuple[list[ScopeSource], list[exp.Join]]:
    """The tables ``root`` reads directly and every JOIN node that adds one."""
    nodes: list[exp.Expr] = []
    joins: list[exp.Join] = []
    if isinstance(root, (exp.Update, exp.Delete)) and root.this is not None:
        nodes.append(root.this)
    from_ = root.args.get("from_")
    if isinstance(from_, exp.From):
        nodes.append(from_.this)
        nodes.extend(from_.expressions or [])
    using = root.args.get("using")
    if isinstance(using, list):
        nodes.extend(using)
    for join in root.args.get("joins") or []:
        joins.append(join)
    sources: list[ScopeSource] = []
    pending = [(n, False) for n in nodes] + [(j.this, _optional_side(j)) for j in joins]
    seen: set[int] = set()
    while pending:
        node, optional = pending.pop(0)
        if id(node) in seen:
            continue
        seen.add(id(node))
        for join in node.args.get("joins") or []:
            joins.append(join)
            pending.append((join.this, _optional_side(join)))
        source = _source(node, index_of, cte_bodies)
        if source is not None:
            sources.append(replace(source, optional=optional) if optional else source)
    return sources, joins


def _optional_side(join: exp.Join) -> bool:
    return str(join.args.get("side") or "").upper() in ("LEFT", "FULL")


def _source(
    node: exp.Expr, index_of: dict[int, int], cte_bodies: dict[str, int]
) -> ScopeSource | None:
    alias = node.alias_or_name.lower() if hasattr(node, "alias_or_name") else ""
    if isinstance(node, exp.Table):
        ref = _ref_from_node(node)
        if ref is None:
            if isinstance(node.this, exp.Identifier) and node.name.lower() in _PSEUDO_TABLES:
                return None
            return ScopeSource(alias=alias or "", table=None, function=True)
        if not ref.qualifiers and ref.name.lower() in cte_bodies and _is_cte_reference(node, ref):
            return ScopeSource(alias=alias, table=None, derived=cte_bodies[ref.name.lower()])
        return ScopeSource(alias=alias, table=ref)
    if isinstance(node, exp.Subquery):
        body = node.this
        derived = index_of.get(id(body)) if isinstance(body, exp.Select) else None
        return ScopeSource(alias=alias, table=None, derived=derived)
    return ScopeSource(alias=alias, table=None, function=True)


def _conjuncts(condition: exp.Expr | None) -> list[exp.Expr]:
    if condition is None:
        return []
    if isinstance(condition, exp.Where):
        condition = condition.this
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.And):
        return _conjuncts(condition.this) + _conjuncts(condition.expression)
    return [condition]


def _own_columns(node: exp.Expr) -> list[exp.Column]:
    """Columns in ``node``, not looking inside a nested query."""
    found: list[exp.Column] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, exp.Column):
            found.append(current)
            continue
        if isinstance(current, (exp.Subquery, exp.Select)) and current is not node:
            continue
        stack.extend(current.iter_expressions())
    return found


def _column_fact(column: exp.Column) -> ColumnFact | None:
    if isinstance(column.this, exp.Star) or not column.name:
        return None
    qualifier = column.table.lower() if column.table else None
    return ColumnFact(qualifier=qualifier, name=column.name.lower())


class _ScopeReader:
    """Sorts one scope's predicate terms into the :class:`ScopeFacts` fields."""

    def __init__(self, aliases: set[str]) -> None:
        self.aliases = aliases
        self.equalities: list[ColumnFact] = []
        self.ranges: list[ColumnFact] = []
        self.joins: list[tuple[ColumnFact, ColumnFact]] = []
        self.other = 0

    def _in_scope(self, column: exp.Column) -> bool:
        qualifier = column.table.lower() if column.table else None
        return qualifier is None or qualifier in self.aliases

    def _scope_column(self, node: exp.Expr) -> ColumnFact | None:
        """``node`` as one of this scope's columns, or None."""
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.Column) and self._in_scope(node):
            return _column_fact(node)
        return None

    def _is_value(self, node: exp.Expr) -> bool:
        """Holds none of this scope's columns (a literal, placeholder, outer column, ...)."""
        return not any(self._in_scope(c) for c in _own_columns(node))

    def term(self, node: exp.Expr) -> None:
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.Escape):
            node = node.this
        if isinstance(node, (exp.EQ, exp.NullSafeEQ)):
            left, right = self._scope_column(node.this), self._scope_column(node.expression)
            if left and right:
                self.joins.append((left, right))
            elif left and self._is_value(node.expression):
                self.equalities.append(left)
            elif right and self._is_value(node.this):
                self.equalities.append(right)
            else:
                self.other += 1
            return
        if isinstance(node, exp.In):
            column = self._scope_column(node.this)
            values = list(node.expressions or [])
            if column and all(self._is_value(v) for v in values):
                self.equalities.append(column)
            else:
                self.other += 1
            return
        if isinstance(node, exp.Between):
            column = self._scope_column(node.this)
            if column:
                self.ranges.append(column)
            else:
                self.other += 1
            return
        if isinstance(node, _RANGE_NODES):
            column = self._scope_column(node.this) or self._scope_column(node.expression)
            if column:
                self.ranges.append(column)
            else:
                self.other += 1
            return
        if isinstance(node, (exp.Like, exp.ILike)) and _like_kind(node) == "prefix":
            column = self._scope_column(node.this)
            if column:
                self.ranges.append(column)
                return
        self.other += 1

    def using(self, join: exp.Join, left_aliases: list[str], right_alias: str) -> None:
        for identifier in join.args.get("using") or []:
            name = identifier.name.lower()
            for alias in left_aliases:
                self.joins.append((ColumnFact(alias, name), ColumnFact(right_alias, name)))


def _row_limit(root: exp.Expr) -> str | None:
    limit = root.args.get("limit")
    if limit is None:
        return None
    options = limit.args.get("limit_options")
    if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
        return "?"
    count = limit.args.get("count") if isinstance(limit, exp.Fetch) else limit.expression
    if count is None:
        return "1" if isinstance(limit, exp.Fetch) else None
    if isinstance(count, exp.Literal) and not count.is_string and str(count.this).isdigit():
        return str(int(count.this))
    return "?"


def _skips_locked(root: exp.Expr, sources_nodes: list[exp.Expr]) -> bool:
    for lock in root.args.get("locks") or []:
        if lock.args.get("wait") is False:
            return True
    for node in sources_nodes:
        for hint in node.args.get("hints") or []:
            if isinstance(hint, exp.WithTableHint) and any(
                h.name.upper() in _ROW_SKIPPING_HINTS for h in hint.expressions
            ):
                return True
    return False


def _has_own_aggregate(root: exp.Expr) -> bool:
    """An aggregate function in ``root``'s own projection, HAVING or ORDER BY."""
    parts: list[exp.Expr] = list(root.args.get("expressions") or [])
    for key in ("having", "order"):
        if root.args.get(key) is not None:
            parts.append(root.args[key])
    stack = parts
    while stack:
        node = stack.pop()
        if isinstance(node, (exp.Subquery, exp.Select, exp.Window)):
            continue
        if _is_aggregation(node):
            return True
        stack.extend(node.iter_expressions())
    return False


def _scope_facts(
    root: exp.Expr, index_of: dict[int, int], cte_bodies: dict[str, int]
) -> ScopeFacts:
    own = _own_nodes(root)
    sources, joins = _scope_sources(root, index_of, cte_bodies)
    aliases = {s.alias for s in sources if s.alias}
    aliases |= {s.table.name.lower() for s in sources if s.table is not None}
    reader = _ScopeReader(aliases)
    where = root.args.get("where")
    for term in _conjuncts(where):
        reader.term(term)
    seen_aliases = [s.alias for s in sources[: len(sources) - len(joins)]]
    for join in joins:
        for term in _conjuncts(join.args.get("on")):
            reader.term(term)
        right = join.this.alias_or_name.lower() if hasattr(join.this, "alias_or_name") else ""
        if join.args.get("using"):
            reader.using(join, seen_aliases, right)
        seen_aliases.append(right)
    order = root.args.get("order")
    order_by = tuple(
        _column_fact(o.this) if isinstance(o.this, exp.Column) else None
        for o in (order.expressions if isinstance(order, exp.Order) else [])
    )
    source_nodes = [j.this for j in joins]
    if isinstance(root.args.get("from_"), exp.From):
        source_nodes.append(root.args["from_"].this)
    return ScopeFacts(
        sources=tuple(sources),
        equalities=tuple(reader.equalities),
        ranges=tuple(reader.ranges),
        joins=tuple(reader.joins),
        other_filters=reader.other,
        order_by=order_by,
        limit=_row_limit(root),
        has_where=where is not None,
        group_by=root.args.get("group") is not None,
        aggregate=_has_own_aggregate(root),
        skip_locked=_skips_locked(root, source_nodes),
        text_search=any(_is_text_search(n) for n in own),
        like_parameter=any(
            isinstance(n, (exp.Like, exp.ILike)) and _like_kind(n) == "parameter" for n in own
        ),
    )
