"""Query shape facts shared by every engine's detectors (ADR-031 Decision 8, #690).

A detector here reports a fact about one query -- never a score, never an
engine name. :func:`is_single_table_crud` is the fact the Aurora anti-patterns
``aurora-anti-05`` ("no-relational-need": "all queries are single-table
CRUD") and ``aurora-anti-06`` ("single-access-pattern-table": "all by primary
key or single column filter", #714) are documented on. It lives next to
:mod:`capability_registry` rather than inside the Aurora analysis module so
that every detector that needs the same fact (both anti-patterns, a future
key-value fit detector) reads the same answer instead of keeping a second
copy that can disagree.

It is built from the shared query features the scorer already uses:
``capability_registry.detect_required_capabilities`` (the capability gate's
own detection) and ``unsupported_pattern``'s comment/literal stripping and
scalar-function allowlist. The few new facts it adds (key predicate, range
predicate, CTE, ``DISTINCT``, pattern match, a primary-key-ordered first-row
read) are defined once, here.

A declared fact module (ADR-031 Decision 8, #893): the one place these shape
facts are defined, held to the one-definition guard
(``tests/unit/tools/sql/test_one_definition_per_fact.py``). The single-table
access shape is replaced by the per-query access-pattern classes when ADR-031
Decision 14 Amendment 5 switches routing over (#728).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from src.agents.referee.access_pattern import KEY_LOOKUP, KEY_RANGE, KEY_SCOPED, REFERENCE_READ
from src.agents.referee.capability_registry import detect_required_capabilities
from src.agents.referee.table_resolution import SourceScope
from src.shared.sql_spans import (
    QUOTED_IDENTIFIERS,
    comments_literals_and_identifiers_for,
    sub_spans,
)
from src.shared.unsupported_pattern import non_scalar_function_names, strip_comments_and_literals
from src.tools.sql.query_facts import query_facts
from src.tools.sql.regex_facts import (
    GROUP_BY_RE,
    IN_LIST_RE,
    LITERAL_PREFIX_LIKE_RE,
    ORDER_BY_RE,
    PATTERN_PREDICATE_RE,
    RANGE_PREDICATE_RE,
    SELECT_WORD_RE,
    aggregate_function_names,
)

__all__ = [
    "CRUD",
    "ORDERED_ONE_TO_MANY",
    "RELATIONAL",
    "is_primary_key_first_row_read",
    "is_single_table_crud",
    "locking_clause",
    "primary_key_ordered_limit",
    "skips_locked_rows",
    "single_table_access_shape",
]

_CRUD_QUERY_TYPES: frozenset[str] = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})

# The three answers :func:`single_table_access_shape` gives. Plain-language
# labels, so a trace or reason that records one never has to translate it.
CRUD = "single-table CRUD"
"""A point or simple-key read or write (``ORDER BY <primary key> LIMIT 1`` included)."""
ORDERED_ONE_TO_MANY = "ordered 1:M read"
"""``ORDER BY <primary key> LIMIT n`` with n > 1, or a bind parameter with no
evidence it is 1: one-to-many in key order. Neither CRUD evidence nor
relational evidence -- a key-value store models it fine (a query on a sort
key), so it is never labelled a key-value lookup and never counts against one."""
RELATIONAL = "relational"
"""Anything else: an aggregate, a subquery, a range or bulk write, a table-wide
order over non-key columns, an engine-specific function, ..."""

# A quoted identifier ("t"."col", `col`, [col]) becomes the bare word ``ident``
# BEFORE literals are stripped: ``strip_comments_and_literals`` blanks every
# double-quoted span, which would otherwise erase the column side of
# ``"t"."id" = $1`` and leave nothing to tell it apart from ``$1 = $2``.
# Found by :data:`QUOTED_IDENTIFIERS` in one linear pass (#886), not a regex:
# the old ``"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\]`` rescanned the rest of the text
# from every unterminated opener.

_DISTINCT_RE = re.compile(r"\bDISTINCT\b", re.IGNORECASE)
_CTE_RE = re.compile(
    r"\bWITH\s+(?:RECURSIVE\s+)?(?:\w+\s*)?(?:\([^()]*\)\s*)?AS\s*"
    r"(?:NOT\s+)?(?:MATERIALIZED\s*)?\(",
    re.IGNORECASE,
)
_WHERE_RE = re.compile(
    r"\bWHERE\b(?P<where>.*?)(?=\b(?:ORDER\s+BY|GROUP\s+BY|HAVING|LIMIT|OFFSET|FETCH|"
    r"RETURNING|FOR\s+UPDATE|FOR\s+SHARE|LOCK\s+IN|UNION)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
# "=" that is not part of <=, >=, !=, <=>, :=, ==, =>.
_EQUALS_RE = re.compile(r"(?<![<>!=:])=(?![=>])")
# "IN (" (regex_facts.IN_LIST_RE) -- whether it is "NOT IN" is decided from the operand
# before it.
_NOT_BEFORE_RE = re.compile(r"\bNOT\s*$", re.IGNORECASE)
# The operand right before an operator / right after it.
_LEFT_OPERAND_RE = re.compile(r"([A-Za-z_$@:?0-9][\w$@:.?]*|\))\s*$")
_RIGHT_OPERAND_RE = re.compile(r"^\s*([A-Za-z_$@:?0-9(][\w$@:.?]*)")
# A column reference: an identifier, optionally table-qualified. A bind
# parameter ($1, ?, @p, :name) or a number never matches.
_COLUMN_RE = re.compile(r"^[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*$")
_NOT_A_COLUMN = frozenset({"null", "true", "false", "not", "and", "or"})

# ---------------------------------------------------------------------------
# Primary-key-ordered reads (maintainer decision on #692)
#
# ``SELECT ... FROM t ORDER BY <t's primary key> LIMIT 1`` -- an ORM's "first
# row" read (Rails ``Model.first``) -- reads exactly one row by the table's
# own key: a key-value lookup, so CRUD. ``LIMIT n`` with n > 1 is an ordered
# 1:M read (ORDERED_ONE_TO_MANY): a key-value store models it, but it is not
# a key-value lookup, so it is neither CRUD nor relational evidence. Only the
# table's own primary key from the schema qualifies (never a hard-coded
# ``id``), with no WHERE clause.
#
# The row-limit forms read (#782): ``LIMIT n``, MySQL ``LIMIT <offset>, n``,
# ``LIMIT n OFFSET m`` / ``OFFSET m LIMIT n``, ``[OFFSET m ROW(S)] FETCH
# FIRST|NEXT [n] ROW(S) ONLY`` (no count means 1; T-SQL requires the OFFSET
# clause) and ``SELECT TOP n`` / ``TOP (n)`` [``PERCENT``] [``WITH TIES``].
# A literal one-row limit is a single-row read at any offset, literal or
# placeholder (``LIMIT 1 OFFSET 5``, ``LIMIT 1 OFFSET $1``, MySQL ``LIMIT 5,
# 1``, ``OFFSET @skip ROWS FETCH NEXT 1 ROW ONLY``): one row from a known
# table is a key-value lookup (maintainer decision of 2026-10-15, #857; it
# supersedes the 2026-10-14 reading that any skip is neutral). ``TOP (1)
# PERCENT`` is a share of the table and ``WITH TIES`` can return more than
# one row: both are ordered 1:M reads, with or without an offset.
#
# A bind-parameter limit (``LIMIT $1``/``LIMIT ?``) counts as ``LIMIT 1`` only
# with evidence that the bound value is 1 (``limit_value``). No collector
# provides that today: pg_stat_statements and the MySQL digest both normalize
# every constant to a placeholder, and the collector contract carries no bound
# values, so without evidence a parameterised limit is an ordered 1:M read.
# ---------------------------------------------------------------------------

_ORDER_BY_ITEMS_RE = re.compile(
    r"\bORDER\s+BY\b(?P<items>.*?)(?=\b(?:LIMIT|OFFSET|FETCH)\b|$)", re.IGNORECASE | re.DOTALL
)
_PARAM_OR_INT = r"(?:\$\d+|\?|:\w+|@\w+|\d+)"
_END = r"\s*;?\s*$"
# Each trailing row-limit clause, anchored at the end of the statement.
_TRAILING_LIMIT_RES = (
    # OFFSET m [ROWS] LIMIT n (PostgreSQL accepts either order; tried first,
    # so the trailing LIMIT n is not read alone)
    re.compile(
        rf"\bOFFSET\s+(?P<offset>{_PARAM_OR_INT})(?:\s+ROWS?)?"
        rf"\s+LIMIT\s+(?P<first>{_PARAM_OR_INT}){_END}",
        re.IGNORECASE,
    ),
    # LIMIT n [OFFSET m [ROWS]] / MySQL LIMIT m, n
    re.compile(
        rf"\bLIMIT\s+(?P<first>{_PARAM_OR_INT})(?:\s*,\s*(?P<second>{_PARAM_OR_INT}))?"
        rf"(?:\s+OFFSET\s+(?P<offset>{_PARAM_OR_INT})(?:\s+ROWS?)?)?{_END}",
        re.IGNORECASE,
    ),
    # [OFFSET m ROW(S)] FETCH FIRST|NEXT [n] ROW(S) ONLY|WITH TIES -- no count means 1
    re.compile(
        rf"(?:\bOFFSET\s+(?P<offset>{_PARAM_OR_INT})(?:\s+ROWS?)?\s+)?"
        rf"\bFETCH\s+(?:FIRST|NEXT)(?:\s+(?P<first>{_PARAM_OR_INT}))?\s+ROWS?"
        rf"\s+(?:ONLY|(?P<ties>WITH\s+TIES)){_END}",
        re.IGNORECASE,
    ),
)

# ---------------------------------------------------------------------------
# Locking reads (#799, ADR-031 Decisions 3 and 14)
#
# A locking clause takes a row lock inside a transaction. On its own it does
# not change which rows a read returns, so it does not change the read's
# shape: a locking point read by the primary key is still a key lookup (CRUD),
# a locking ordered or range read keeps its non-locking classification.
# ``NOWAIT`` only changes what happens when a row is already locked (an error
# instead of a wait), so it is shape-neutral too.
#
# The exception is a lock that skips rows: ``SKIP LOCKED`` (PostgreSQL, MySQL
# 8) and T-SQL's ``READPAST`` hint return the first row nobody else holds --
# the work-queue claim pattern -- not the row the key or the key order names.
# The row is unknown -- unlike an offset, which names a position in a known
# order (#857) -- so (review of #840) such a read, with or without an
# offset, is at most a neutral ordered 1:M read, never a
# key-value lookup; relational evidence (a range, an aggregate, a non-key
# order) stays relational.
#
# The lock is recorded on its own, as a transactional need
# (:func:`locking_clause`), never as relational evidence and never as a score:
# DynamoDB supports transactions (ADR-031 Decision 3). The ownership gate that
# keeps a locking read off an engine that is not a system of record
# (``cache_overlay.is_locking_read``, Decision 2 step 3) is a separate rule, but
# it reads the lock with this same function (#838), and the UI engine picker
# runs a JavaScript copy (``src/ui/src/utils/sqlLocking.js``) held to the same
# cases (``tests/unit/agents/referee/locking_clause_cases.json``).
#
# The forms read: PostgreSQL/MySQL ``FOR UPDATE`` / ``FOR NO KEY UPDATE`` /
# ``FOR SHARE`` / ``FOR KEY SHARE`` (each with an optional ``OF <tables>``,
# schema-qualified or not, and ``NOWAIT`` / ``SKIP LOCKED``; PostgreSQL
# accepts the clause before or after the row limit), MySQL ``LOCK IN SHARE
# MODE``, and a SQL Server table hint list ``WITH (...)`` -- comma-separated
# or not, with ``INDEX(...)`` allowed -- naming ``UPDLOCK``, ``HOLDLOCK``,
# ``XLOCK`` or ``SERIALIZABLE`` (``NOLOCK``/``READPAST`` alone take no lock).
# Only names on the fixed T-SQL table-hint list (``_TABLE_HINT_NAMES``) are
# ever recorded. Everything is matched after comments and literals are
# stripped and quoted identifiers are replaced, so ``"update"`` (a column) or
# ``'FOR UPDATE'`` (a value) is never a lock.
# ---------------------------------------------------------------------------

_LOCK_STRENGTH = r"(?P<strength>NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)"
_LOCKED_TABLE = r"[\w$]+(?:\s*\.\s*[\w$]+)*"
_FOR_LOCKING_RE = re.compile(
    rf"\bFOR\s+{_LOCK_STRENGTH}"
    rf"(?:\s+OF\s+{_LOCKED_TABLE}(?:\s*,\s*{_LOCKED_TABLE})*)?"
    r"(?:\s+(?P<wait>NOWAIT|SKIP\s+LOCKED))?\b",
    re.IGNORECASE,
)
_LOCK_IN_SHARE_MODE_RE = re.compile(r"\bLOCK\s+IN\s+SHARE\s+MODE\b", re.IGNORECASE)
# ``WITH ( ... )`` with at most one level of nested parentheses (``INDEX(pk)``).
_TABLE_HINTS_RE = re.compile(r"\bWITH\s*\((?P<hints>(?:[^()]|\([^()]*\))*)\)", re.IGNORECASE)
_HINT_ARGUMENTS_RE = re.compile(r"\([^()]*\)")
_HINT_WORD_RE = re.compile(r"[A-Za-z_]+")
# The SQL Server table hints (the documented list); nothing else is recorded.
_TABLE_HINT_NAMES = frozenset(
    {
        "FORCESCAN",
        "FORCESEEK",
        "HOLDLOCK",
        "INDEX",
        "NOEXPAND",
        "NOLOCK",
        "NOWAIT",
        "PAGLOCK",
        "READCOMMITTED",
        "READCOMMITTEDLOCK",
        "READPAST",
        "READUNCOMMITTED",
        "REPEATABLEREAD",
        "ROWLOCK",
        "SERIALIZABLE",
        "SNAPSHOT",
        "TABLOCK",
        "TABLOCKX",
        "UPDLOCK",
        "XLOCK",
    }
)
_LOCKING_HINTS = frozenset({"UPDLOCK", "HOLDLOCK", "XLOCK", "SERIALIZABLE"})
_ROW_SKIPPING_HINTS = frozenset({"READPAST"})


def _normalized_sql(text: str, source_engine: str | None) -> str:
    """``text`` with comments and literals blanked and quoted identifiers made ``ident``.

    One left-to-right pass in linear time (review of #871, ``sql_spans``): at each
    step the span that opens first -- a ``/* */`` or ``--`` comment, a ``'...'``
    literal (``''`` and backslash escapes), or a ``"..."`` / `` `...` `` /
    ``[...]`` identifier -- is replaced whole, so a quote inside one never
    pairs with a quote outside it. An opener with no closer is plain text.

    ``source_engine`` adds the dialect's own forms (#1114): MySQL and MariaDB ``#``
    comments, PostgreSQL ``$$...$$`` strings. A backtick identifier is one span, so
    the ``#`` of `` `order#items` `` is not a comment.
    """
    return sub_spans(
        text,
        comments_literals_and_identifiers_for(source_engine),
        lambda span: " " if span.startswith(("/*", "--", "#", "'", "$")) else " ident ",
    )


def _hint_names(hints: str) -> list[str]:
    """The known table-hint names in a ``WITH (...)`` body, in order, arguments dropped."""
    words = _HINT_WORD_RE.findall(_HINT_ARGUMENTS_RE.sub(" ", hints))
    names = [w.upper() for w in words if w.upper() in _TABLE_HINT_NAMES]
    return list(dict.fromkeys(names))


def _table_hint_lists(sql: str) -> list[list[str]]:
    """The known hint names of each ``WITH (...)`` table-hint list in ``sql``."""
    return [
        names for m in _TABLE_HINTS_RE.finditer(sql) if (names := _hint_names(m.group("hints")))
    ]


def _locking_hint_lists(sql: str) -> list[list[str]]:
    """Each table-hint list in ``sql`` that names a locking hint."""
    return [names for names in _table_hint_lists(sql) if _LOCKING_HINTS.intersection(names)]


def _without_locking_clauses(sql: str) -> str:
    """``sql`` (already normalized) with every locking clause removed."""
    sql = _FOR_LOCKING_RE.sub(" ", sql)
    sql = _LOCK_IN_SHARE_MODE_RE.sub(" ", sql)
    return _TABLE_HINTS_RE.sub(
        lambda m: " " if _locking_hint_lists(m.group(0)) else m.group(0), sql
    )


def skips_locked_rows(query_text: str | None, source_engine: str | None = None) -> bool:
    """Whether ``query_text`` skips rows other transactions hold (``SKIP LOCKED``, ``READPAST``).

    Such a read returns an unknown row (a work-queue claim), so it is never a
    key-value lookup (review of #840).
    """
    sql = _normalized_sql(query_text or "", source_engine)
    if any(
        " ".join((m.group("wait") or "").upper().split()) == "SKIP LOCKED"
        for m in _FOR_LOCKING_RE.finditer(sql)
    ):
        return True
    return any(_ROW_SKIPPING_HINTS.intersection(names) for names in _table_hint_lists(sql))


def locking_clause(query_text: str | None, source_engine: str | None = None) -> str | None:
    """The locking clause of ``query_text`` in canonical form, else ``None``.

    ``"FOR UPDATE"``, ``"FOR NO KEY UPDATE NOWAIT"``, ``"FOR UPDATE SKIP
    LOCKED"`` (an ``OF <tables>`` list is not part of the answer), ``"LOCK IN
    SHARE MODE"`` or the hint list as ``"WITH (UPDLOCK, ROWLOCK)"`` (known
    hint names only, arguments dropped: ``INDEX(pk)`` is ``INDEX``). Several
    locking clauses (PostgreSQL allows one per table) are joined with ``"; "``.
    This is the transactional need a locking read records (#799). It changes
    :func:`single_table_access_shape` only through :func:`skips_locked_rows`.
    """
    sql = _normalized_sql(query_text or "", source_engine)
    clauses = []
    for match in _FOR_LOCKING_RE.finditer(sql):
        words = ["FOR", " ".join(match.group("strength").upper().split())]
        if match.group("wait"):
            words.append(" ".join(match.group("wait").upper().split()))
        clauses.append(" ".join(words))
    if _LOCK_IN_SHARE_MODE_RE.search(sql):
        clauses.append("LOCK IN SHARE MODE")
    clauses.extend(f"WITH ({', '.join(hints)})" for hints in _locking_hint_lists(sql))
    return "; ".join(clauses) or None


_TOP_RE = re.compile(
    rf"^\s*SELECT\s+TOP\s*(?:\(\s*(?P<paren>{_PARAM_OR_INT})\s*\)|(?P<bare>{_PARAM_OR_INT}))"
    r"(?P<percent>\s*PERCENT\b)?(?P<ties>\s*WITH\s+TIES\b)?",
    re.IGNORECASE,
)
_OFFSET_RE = re.compile(r"\bOFFSET\b", re.IGNORECASE)
_WHERE_WORD_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_SORT_ITEM_RE = re.compile(
    r"^(?P<col>[A-Za-z_][\w$]*(?:\s*\.\s*[A-Za-z_][\w$]*)*)"
    r"(?:\s+(?:ASC|DESC))?(?:\s+NULLS\s+(?:FIRST|LAST))?$",
    re.IGNORECASE,
)


def _unquote_identifiers(text: str) -> str:
    """``"t"."id"`` / `` `id` `` / ``[id]`` -> ``t.id`` / ``id``, keeping the names."""
    return sub_spans(text, QUOTED_IDENTIFIERS, lambda span: span[1:-1].replace('""', '"'))


def readable_sql(text: str, source_engine: str | None) -> str:
    """``text`` without comments and literals, identifiers unquoted.

    Both steps in one scan, in source order and in the dialect's grammar
    (``sql_spans.comments_literals_and_identifiers_for``): unquoting first would expose
    the ``#`` of MySQL's `` `order#items` `` (#1101) or the apostrophe of SQL Server's
    ``[O'Brien]`` (#1416) to the strip that follows.
    """

    def read(span: str) -> str:
        if span[0] in '"`[':
            return span[1:-1].replace('""', '"')
        return " "

    return sub_spans(text, comments_literals_and_identifiers_for(source_engine), read)


def _qualifier(match: re.Match[str]) -> str:
    """``"PERCENT"``, ``"WITH TIES"``, both or ``""``: what makes ``n`` not a row count."""
    groups = match.groupdict()
    words = [
        word for key, word in (("percent", "PERCENT"), ("ties", "WITH TIES")) if groups.get(key)
    ]
    return " ".join(words)


def _row_limit(sql: str) -> tuple[str, str | None, str] | None:
    """``(limit, offset, qualifier)`` of ``sql``'s row-limit clause, else ``None``.

    ``limit`` and ``offset`` are as written (digits or a placeholder);
    ``offset`` is ``None`` when there is no skip at all. A ``FETCH`` with no
    count is a limit of ``"1"``. ``qualifier`` is ``"PERCENT"`` for ``TOP n
    PERCENT``, ``"WITH TIES"`` for ``TOP n WITH TIES`` / ``FETCH ... WITH
    TIES`` (both, space-separated, when both are written), else ``""``.
    """
    top = _TOP_RE.search(sql)
    if top:
        if _OFFSET_RE.search(sql):
            return None
        return top.group("paren") or top.group("bare"), None, _qualifier(top)
    for pattern in _TRAILING_LIMIT_RES:
        match = pattern.search(sql)
        if not match:
            continue
        first, offset = match.group("first") or "1", match.group("offset")
        second = match.groupdict().get("second")
        if second is not None:  # MySQL LIMIT <offset>, <count>
            first, offset = second, first
        return first, offset, _qualifier(match)
    return None


def _primary_key_ordered_row_limit(
    query_text: str | None, primary_key: Sequence[str], source_engine: str | None
) -> tuple[str, str | None, str] | None:
    """:func:`_row_limit` of a read ordered by the table's own primary key, else ``None``."""
    key = [c.lower() for c in primary_key or []]
    if not key:
        return None
    sql = readable_sql(query_text or "", source_engine)
    # A lock changes no row the read returns (#799): read the shape without it.
    sql = _without_locking_clauses(sql).strip()
    if _WHERE_WORD_RE.search(sql):
        return None
    limit = _row_limit(sql)
    order = _ORDER_BY_ITEMS_RE.search(sql)
    if not limit or not order:
        return None
    columns = []
    for item in order.group("items").split(","):
        match = _SORT_ITEM_RE.match(item.strip())
        if not match:
            return None
        columns.append(re.split(r"\s*\.\s*", match.group("col"))[-1].lower())
    if columns != key:
        return None
    return limit


def primary_key_ordered_limit(
    query_text: str | None, primary_key: Sequence[str], source_engine: str | None = None
) -> str | None:
    """The row limit of a read ordered by the table's own primary key, else ``None``.

    For ``SELECT ... ORDER BY <every primary_key column, in order>`` with a
    row limit (``LIMIT n``, ``LIMIT m, n``, ``LIMIT n OFFSET m``, ``[OFFSET m
    ROWS] FETCH FIRST|NEXT [n] ROWS ONLY|WITH TIES``, ``SELECT TOP (n) [PERCENT]
    [WITH TIES]``) and no WHERE, returns ``n`` as written: digits for a
    literal, the placeholder (``$1``, ``?``, ...) for a bind parameter, ``"1"``
    for a ``FETCH`` with no count, ``"n PERCENT"`` / ``"n WITH TIES"`` when
    ``n`` is not a plain row count. The offset is not part of
    the answer and does not change the shape (#857). Each sort
    column may be table-qualified and quoted, and may be ASC or DESC; the
    comparison with ``primary_key`` is case-insensitive. An empty
    ``primary_key`` (no key in the schema) never matches.
    """
    limit = _primary_key_ordered_row_limit(query_text, primary_key, source_engine)
    if limit is None:
        return None
    n, _offset, qualifier = limit
    return f"{n} {qualifier}" if qualifier else n


def is_primary_key_first_row_read(
    query_text: str | None,
    primary_key: Sequence[str],
    limit_value: int | None = None,
    source_engine: str | None = None,
) -> bool:
    """Whether ``query_text`` reads exactly the first row in the table's primary-key order.

    :func:`primary_key_ordered_limit` with a limit of 1 -- a literal ``1``, or a
    bind parameter whose bound value ``limit_value`` (when a collector supplies
    it) is 1 -- at any offset: none, a literal (``OFFSET 5``, ``LIMIT 5, 1``)
    or a placeholder (``OFFSET $1``). One row from a known table is a
    key-value lookup wherever it sits in the key order (maintainer decision
    of 2026-10-15, #857), so "first row" here means the single row the read
    returns. With no evidence (``None``) a parameterised limit is not a
    single-row read; ``TOP (1) PERCENT`` and ``WITH TIES`` (which can return
    more rows than ``n``) never are, and neither is a read that skips locked
    rows (``SKIP LOCKED``, ``READPAST``: the first row nobody else holds is
    an unknown row, review of #840).
    """
    limit = _primary_key_ordered_row_limit(query_text, primary_key, source_engine)
    if limit is None or skips_locked_rows(query_text, source_engine):
        return False
    n, _offset, qualifier = limit
    if qualifier:
        return False
    bound = int(n) if n.isdigit() else limit_value
    return bound == 1


def _is_column(operand: str) -> bool:
    """A column (or an expression over one, like ``lower(email)``), not a parameter or literal."""
    if operand == ")":
        return True
    return bool(_COLUMN_RE.match(operand)) and operand.lower() not in _NOT_A_COLUMN


def _has_key_predicate(where: str) -> bool:
    """Whether ``where`` addresses rows by a key value: ``col = x`` or ``col IN (...)``.

    At least one side of the ``=`` must be a column, so a placeholder or
    literal tautology (``$2=$3``, ``? = ?``, ``1=1``) is not a key -- an
    ORM's "match everything" filter is an unkeyed bulk predicate. ``NOT IN``
    (any spacing) excludes rows rather than addressing them, so it is not a
    key either.
    """
    for m in _EQUALS_RE.finditer(where):
        left = _LEFT_OPERAND_RE.search(where[: m.start()])
        right = _RIGHT_OPERAND_RE.search(where[m.end() :])
        if (left and _is_column(left.group(1))) or (right and _is_column(right.group(1))):
            return True
    for m in IN_LIST_RE.finditer(where):
        before = where[: m.start()]
        if _NOT_BEFORE_RE.search(before):
            continue
        left = _LEFT_OPERAND_RE.search(before)
        if left and _is_column(left.group(1)):
            return True
    return False


def is_single_table_crud(
    query: Mapping,
    primary_key: Sequence[str] | None = None,
    limit_value: int | None = None,
    source: SourceScope | None = None,
) -> bool:
    """Whether ``query`` is single-table CRUD (:func:`single_table_access_shape` is ``CRUD``)."""
    return single_table_access_shape(query, primary_key, limit_value, source) == CRUD


def single_table_access_shape(
    query: Mapping,
    primary_key: Sequence[str] | None = None,
    limit_value: int | None = None,
    source: SourceScope | None = None,
    access_class: str | None = None,
    text_is_readable: bool = False,
) -> str:
    """``CRUD``, ``ORDERED_ONE_TO_MANY`` or ``RELATIONAL`` for ``query``.

    ``CRUD`` -- a point or simple-key read or write -- only when ALL of these hold:

    - ``SELECT``/``INSERT``/``UPDATE``/``DELETE`` on exactly one table, no join;
    - no hard capability need (``capability_registry.detect_required_capabilities``:
      aggregates, a non-dedup ``GROUP BY``, window functions, ``WITH RECURSIVE``,
      text search, a computed join key, an admin statement);
    - no CTE and no nested ``SELECT`` (subquery, ``INSERT ... SELECT``, ``UNION``);
    - no ``DISTINCT`` and no ``GROUP BY`` at all: de-duplicating a result set
      is a set operation over many rows, not a point read. The capability gate
      lets a dedup-only ``GROUP BY`` through (#336) because an engine CAN
      serve it with client-side de-duplication; that answers "can this engine
      run it", not "is this plain CRUD";
    - every function call on the shared scalar allowlist
      (``unsupported_pattern.non_scalar_function_names``), so an engine-specific
      function such as ``family()`` on ``inet`` disqualifies the query;
    - no pattern match (``LIKE``/``ILIKE``/``SIMILAR TO``/``REGEXP``) except a
      literal-prefix ``LIKE 'abc%'``, which counts as a range predicate. A
      parameterised ``LIKE $1`` is not CRUD: its pattern is unknown, so it
      may well be a leading-wildcard search;
    - an ``UPDATE``/``DELETE`` addresses rows by key (a ``col = x`` or
      ``col IN (...)`` predicate, never a placeholder tautology such as
      ``$2=$3``, never ``NOT IN``) and has no range predicate: a range or
      unkeyed write is a bulk mutation, not CRUD;
    - a ``SELECT`` with no key predicate has no ``ORDER BY`` and no range
      predicate: ordering or ranging over the whole table is not a simple-key
      read. An unordered, unfiltered read (a full scan) still is, and so is a
      single-row read (``LIMIT 1``, at any offset, #857) in the table's own
      primary-key order
      (:func:`is_primary_key_first_row_read`, given ``primary_key``, the
      table's primary-key columns from the schema, and ``limit_value``, the
      bound value of a parameterised limit when known; without the key no
      ordered, unkeyed read qualifies).

    ``ORDERED_ONE_TO_MANY`` for the same primary-key-ordered read with a limit
    other than 1 (or a bind parameter with no evidence it is 1), ``TOP n
    PERCENT`` or ``WITH TIES``, at any offset. ``RELATIONAL`` for everything
    else.

    ``source`` is the collected database (#835), see
    :func:`~src.agents.referee.utility_statements.is_utility_statement`.

    Comments and string literals are stripped first, so a ``<`` or a function
    call inside a quoted value never changes the verdict. A locking clause
    (``FOR UPDATE``, ``LOCK IN SHARE MODE``, ``WITH (UPDLOCK)``, ``NOWAIT``
    included) does not change it either (#799): the answer is the one for the
    same read without the lock, and the lock is a separate fact,
    :func:`locking_clause`. A read that skips locked rows (``SKIP LOCKED``,
    ``READPAST``, :func:`skips_locked_rows`) returns an unknown row, so it is
    at most ``ORDERED_ONE_TO_MANY``, never ``CRUD`` (review of #840).

    ``access_class`` (the query's access-pattern class,
    :func:`src.agents.referee.access_pattern.classify_query`) applies ADR-031
    Decision 14 Amendment 5 to a read (#728): a key-anchored join, an aggregate with
    no ``GROUP BY`` over keyed rows, a ``GROUP BY`` or ``DISTINCT`` that only
    de-duplicates keyed rows, a range over a key with no pinned value and an order
    over a non-primary key, each relational to the text check alone, are neutral when
    the class is a key-scoped read, a range or ordered read by key or a small
    reference-table read (:func:`_class_shape`); a read with no predicate is neutral,
    not CRUD. Without it (and for every write) the answer is the text check's.

    ``text_is_readable`` says ``query_text`` already had its comments and literals
    removed and identifiers unquoted (``readable_sql``), so no dialect's comment or
    string syntax is scanned for again: an unquoted MySQL name such as
    `` order#items `` would otherwise read as a ``#`` comment (#1114).
    """
    reasons = _relational_reasons(query, primary_key, limit_value, source, text_is_readable)
    if isinstance(reasons, str):
        shape = reasons
    else:
        shape = RELATIONAL if reasons else CRUD
    if access_class is None or str(query.get("query_type") or "").upper() != "SELECT":
        return shape
    return _class_shape(shape, reasons, access_class, query)


# The reasons a read is relational that a key-anchored access-pattern class supersedes
# (ADR-031 Decision 14 Amendment 5, #728): a join every table of which is reached by
# key, an aggregate with no GROUP BY over keyed rows, a GROUP BY or DISTINCT that only
# de-duplicates, a range over a key with no pinned value, and an order over a key that
# is not the primary key. Every other reason (a subquery, a CTE, an engine-specific
# function, a pattern match, a window function, a non-CRUD statement) keeps the read
# relational whatever its class.
_CLASS_SUPERSEDES = frozenset({"join", "aggregate", "dedup", "range", "order"})
_NEUTRAL_CLASSES = frozenset({KEY_SCOPED, KEY_RANGE, REFERENCE_READ})


def _class_shape(shape: str, reasons: object, access_class: str, query: Mapping) -> str:
    """Amendment 5's step-1 answers for a read, from its access-pattern class.

    A key-scoped 1:N read, a range or ordered read by key and a small reference-table
    read are neutral (an ordered 1:M read: neither CRUD nor relational evidence) where
    the text check alone found them relational for a reason the class supersedes; a
    key lookup that only de-duplicates its rows is CRUD. A read with no predicate is
    neutral, not CRUD: it reads the whole key range.
    """
    if shape == RELATIONAL:
        if not isinstance(reasons, frozenset) or not reasons or not reasons <= _CLASS_SUPERSEDES:
            return shape
        if access_class in _NEUTRAL_CLASSES:
            return ORDERED_ONE_TO_MANY
        if access_class == KEY_LOOKUP and reasons == {"dedup"}:
            return CRUD
        return shape
    if shape == CRUD and access_class in (KEY_RANGE, REFERENCE_READ):
        return ORDERED_ONE_TO_MANY
    return shape


def _relational_reasons(
    query: Mapping,
    primary_key: Sequence[str] | None,
    limit_value: int | None,
    source: SourceScope | None,
    text_is_readable: bool = False,
) -> frozenset[str] | str:
    """Every reason the text check finds ``query`` relational (empty: CRUD), or a final
    answer (``RELATIONAL`` or ``ORDERED_ONE_TO_MANY``) no class can change."""
    query_type = str(query.get("query_type") or "").upper()
    if query_type not in _CRUD_QUERY_TYPES:
        return RELATIONAL
    reasons: set[str] = set()
    tables = list(query.get("tables_accessed") or [])
    if len(tables) != 1 or query.get("has_joins"):
        reasons.add("join")
    text = str(query.get("query_text") or "")
    capabilities = detect_required_capabilities(text, [], tables, source)
    if capabilities:
        facts = query_facts(text, source.engine if source else None)
        plain_aggregate = not (facts.window_function or facts.group_by or facts.found_rows)
        if capabilities == ["aggregation"] and plain_aggregate:
            reasons.add("aggregate")
        else:
            return RELATIONAL
    # Text that is already readable has no comment or string left to find (#1114).
    engine = None if text_is_readable or source is None else source.engine
    functions = non_scalar_function_names(text, engine)
    if "aggregate" in reasons:
        # The aggregate's own function is the aggregate reason, not an engine feature.
        functions -= aggregate_function_names(text)
    if functions:
        return RELATIONAL
    # Comments gone and identifiers unquoted in one scan, in the source's dialect, so a
    # ``#`` inside a backtick name is no comment and a ``#`` comment hides its quotes.
    with_idents = sub_spans(
        text,
        comments_literals_and_identifiers_for(engine),
        lambda span: (
            " " if span.startswith(("/*", "--", "#")) else span if span[0] in "'$" else " ident "
        ),
    )
    sql = strip_comments_and_literals(with_idents, engine)
    if _CTE_RE.search(sql):
        return RELATIONAL
    if len(SELECT_WORD_RE.findall(sql)) > (1 if query_type == "SELECT" else 0):
        return RELATIONAL
    if _DISTINCT_RE.search(sql) or GROUP_BY_RE.search(sql):
        reasons.add("dedup")
    where_match = _WHERE_RE.search(sql)
    where = where_match.group("where") if where_match else ""
    # A pattern-match predicate (LIKE/ILIKE/SIMILAR TO/REGEXP/RLIKE) and a literal-prefix
    # LIKE ('abc%', no leading wildcard) are read on the text before literals are stripped.
    pattern_match = bool(PATTERN_PREDICATE_RE.search(where))
    if pattern_match and not LITERAL_PREFIX_LIKE_RE.search(with_idents):
        return RELATIONAL
    keyed = _has_key_predicate(where)
    ranged = pattern_match or bool(RANGE_PREDICATE_RE.search(where))
    if query_type in {"UPDATE", "DELETE"}:
        return frozenset(reasons) if keyed and not ranged and not reasons else RELATIONAL
    if query_type == "SELECT" and not keyed:
        if ranged:
            reasons.add("range")
        elif ORDER_BY_RE.search(sql):
            if is_primary_key_first_row_read(text, primary_key or [], limit_value, engine):
                if not reasons:
                    return frozenset()
            elif primary_key_ordered_limit(text, primary_key or [], engine) is not None:
                return RELATIONAL if reasons else ORDERED_ONE_TO_MANY
            else:
                reasons.add("order")
    if not reasons and query_type == "SELECT" and skips_locked_rows(text, engine):
        # SKIP LOCKED / READPAST returns an unknown row: at most neutral (#840).
        return ORDERED_ONE_TO_MANY
    return frozenset(reasons)
