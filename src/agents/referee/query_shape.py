"""Query shape facts shared by every engine's detectors (ADR-031 Decision 8, #690).

A detector here reports a fact about one query -- never a score, never an
engine name. :func:`is_single_table_crud` is the fact the Aurora anti-pattern
``aurora-anti-05`` ("no-relational-need") is documented on ("all queries are
single-table CRUD"). It lives next to :mod:`capability_registry` rather than
inside the Aurora analysis module so that any other detector that needs the
same fact (the ``aurora-anti-06`` follow-up, a key-value fit detector) reads
the same answer instead of keeping a second copy that can disagree.

It is built from the shared query features the scorer already uses:
``capability_registry.detect_required_capabilities`` (the capability gate's
own detection) and ``unsupported_pattern``'s comment/literal stripping and
scalar-function allowlist. The few new facts it adds (key predicate, range
predicate, CTE, ``DISTINCT``, pattern match, a primary-key-ordered first-row
read) are defined once, here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from src.agents.referee.capability_registry import detect_required_capabilities
from src.shared.unsupported_pattern import non_scalar_function_names, strip_comments_and_literals

__all__ = [
    "CRUD",
    "ORDERED_ONE_TO_MANY",
    "RELATIONAL",
    "is_primary_key_first_row_read",
    "is_single_table_crud",
    "primary_key_ordered_limit",
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
_QUOTED_IDENT_RE = re.compile(r'"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\]')

_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
_DISTINCT_RE = re.compile(r"\bDISTINCT\b", re.IGNORECASE)
_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
_CTE_RE = re.compile(
    r"\bWITH\s+(?:RECURSIVE\s+)?(?:\w+\s*)?(?:\([^()]*\)\s*)?AS\s*"
    r"(?:NOT\s+)?(?:MATERIALIZED\s*)?\(",
    re.IGNORECASE,
)
_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
_WHERE_RE = re.compile(
    r"\bWHERE\b(?P<where>.*?)(?=\b(?:ORDER\s+BY|GROUP\s+BY|HAVING|LIMIT|OFFSET|FETCH|"
    r"RETURNING|FOR\s+UPDATE|FOR\s+SHARE|LOCK\s+IN|UNION)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
# "=" that is not part of <=, >=, !=, <=>, :=, ==, =>.
_EQUALS_RE = re.compile(r"(?<![<>!=:])=(?![=>])")
# "IN (" -- whether it is "NOT IN" is decided from the operand before it.
_IN_LIST_RE = re.compile(r"\bIN\s*\(", re.IGNORECASE)
_NOT_BEFORE_RE = re.compile(r"\bNOT\s*$", re.IGNORECASE)
# The operand right before an operator / right after it.
_LEFT_OPERAND_RE = re.compile(r"([A-Za-z_$@:?0-9][\w$@:.?]*|\))\s*$")
_RIGHT_OPERAND_RE = re.compile(r"^\s*([A-Za-z_$@:?0-9(][\w$@:.?]*)")
# A column reference: an identifier, optionally table-qualified. A bind
# parameter ($1, ?, @p, :name) or a number never matches.
_COLUMN_RE = re.compile(r"^[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*$")
_NOT_A_COLUMN = frozenset({"null", "true", "false", "not", "and", "or"})
# <, >, <=, >= or BETWEEN -- but not <>, ->, ->>, @>, <@, <<, >>, <=>.
_RANGE_PREDICATE_RE = re.compile(
    r"\bBETWEEN\b|(?<![<>=!@\-])(?:<=|>=|<|>)(?![<>=@])", re.IGNORECASE
)
# LIKE/ILIKE/SIMILAR TO/REGEXP/RLIKE, and a literal-prefix LIKE ('abc%', no
# leading wildcard) -- read on the text before literals are stripped.
_PATTERN_MATCH_RE = re.compile(r"\b(?:I?LIKE|SIMILAR\s+TO|REGEXP|RLIKE)\b", re.IGNORECASE)
_LITERAL_PREFIX_LIKE_RE = re.compile(r"\bI?LIKE\s+'[^%_']", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Primary-key-ordered reads (maintainer decision on #692)
#
# ``SELECT ... FROM t ORDER BY <t's primary key> LIMIT 1`` -- an ORM's "first
# row" read (Rails ``Model.first``) -- reads exactly one row by the table's
# own key: a key-value lookup, so CRUD. ``LIMIT n`` with n > 1 is an ordered
# 1:M read (ORDERED_ONE_TO_MANY): a key-value store models it, but it is not
# a key-value lookup, so it is neither CRUD nor relational evidence. Only the
# table's own primary key from the schema qualifies (never a hard-coded
# ``id``), with no WHERE clause and no OFFSET.
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
_ROW_LIMIT_RE = re.compile(
    rf"\bLIMIT\s+(?P<limit>{_PARAM_OR_INT})\s*;?\s*$"
    rf"|\bFETCH\s+(?:FIRST|NEXT)\s+(?P<fetch>{_PARAM_OR_INT})\s+ROWS?\s+ONLY\s*;?\s*$"
    rf"|^\s*SELECT\s+TOP\s*\(?\s*(?P<top>{_PARAM_OR_INT})\s*\)?",
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
    return _QUOTED_IDENT_RE.sub(lambda m: m.group(0)[1:-1].replace('""', '"'), text)


def primary_key_ordered_limit(query_text: str | None, primary_key: Sequence[str]) -> str | None:
    """The row limit of a read ordered by the table's own primary key, else ``None``.

    For ``SELECT ... ORDER BY <every primary_key column, in order> LIMIT n``
    (or ``FETCH FIRST n ROWS ONLY``, or ``SELECT TOP (n)``) with no WHERE and
    no OFFSET, returns ``n`` as written: digits for a literal, the placeholder
    (``$1``, ``?``, ...) for a bind parameter. Each sort column may be
    table-qualified and quoted, and may be ASC or DESC; the comparison with
    ``primary_key`` is case-insensitive. An empty ``primary_key`` (no key in
    the schema) never matches.
    """
    key = [c.lower() for c in primary_key or []]
    if not key:
        return None
    sql = strip_comments_and_literals(_unquote_identifiers(query_text or "")).strip()
    if _WHERE_WORD_RE.search(sql) or _OFFSET_RE.search(sql):
        return None
    limit = _ROW_LIMIT_RE.search(sql)
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
    return limit.group("limit") or limit.group("fetch") or limit.group("top")


def is_primary_key_first_row_read(
    query_text: str | None, primary_key: Sequence[str], limit_value: int | None = None
) -> bool:
    """Whether ``query_text`` reads exactly the first row in the table's primary-key order.

    :func:`primary_key_ordered_limit` with a limit of 1: a literal ``1``, or a
    bind parameter whose bound value ``limit_value`` -- when a collector
    supplies it -- is 1. With no evidence (``None``) a parameterised limit is
    not a first-row read.
    """
    n = primary_key_ordered_limit(query_text, primary_key)
    if n is None:
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
    for m in _IN_LIST_RE.finditer(where):
        before = where[: m.start()]
        if _NOT_BEFORE_RE.search(before):
            continue
        left = _LEFT_OPERAND_RE.search(before)
        if left and _is_column(left.group(1)):
            return True
    return False


def is_single_table_crud(
    query: Mapping, primary_key: Sequence[str] | None = None, limit_value: int | None = None
) -> bool:
    """Whether ``query`` is single-table CRUD (:func:`single_table_access_shape` is ``CRUD``)."""
    return single_table_access_shape(query, primary_key, limit_value) == CRUD


def single_table_access_shape(
    query: Mapping, primary_key: Sequence[str] | None = None, limit_value: int | None = None
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
      first-row read (``LIMIT 1``) in the table's own primary-key order
      (:func:`is_primary_key_first_row_read`, given ``primary_key``, the
      table's primary-key columns from the schema, and ``limit_value``, the
      bound value of a parameterised limit when known; without the key no
      ordered, unkeyed read qualifies).

    ``ORDERED_ONE_TO_MANY`` for the same primary-key-ordered read with a limit
    other than 1 (or a bind parameter with no evidence it is 1). ``RELATIONAL``
    for everything else.

    Comments and string literals are stripped first, so a ``<`` or a function
    call inside a quoted value never changes the verdict.
    """
    query_type = str(query.get("query_type") or "").upper()
    if query_type not in _CRUD_QUERY_TYPES:
        return RELATIONAL
    tables = list(query.get("tables_accessed") or [])
    if len(tables) != 1 or query.get("has_joins"):
        return RELATIONAL
    text = str(query.get("query_text") or "")
    if detect_required_capabilities(text, [], tables):
        return RELATIONAL
    if non_scalar_function_names(text):
        return RELATIONAL
    with_idents = _QUOTED_IDENT_RE.sub(" ident ", text)
    sql = strip_comments_and_literals(with_idents)
    if _CTE_RE.search(sql):
        return RELATIONAL
    if len(_SELECT_RE.findall(sql)) > (1 if query_type == "SELECT" else 0):
        return RELATIONAL
    if _DISTINCT_RE.search(sql) or _GROUP_BY_RE.search(sql):
        return RELATIONAL
    where_match = _WHERE_RE.search(sql)
    where = where_match.group("where") if where_match else ""
    pattern_match = bool(_PATTERN_MATCH_RE.search(where))
    if pattern_match and not _LITERAL_PREFIX_LIKE_RE.search(with_idents):
        return RELATIONAL
    keyed = _has_key_predicate(where)
    ranged = pattern_match or bool(_RANGE_PREDICATE_RE.search(where))
    if query_type in {"UPDATE", "DELETE"}:
        return CRUD if keyed and not ranged else RELATIONAL
    if query_type == "SELECT" and not keyed:
        if ranged:
            return RELATIONAL
        if _ORDER_BY_RE.search(sql):
            if is_primary_key_first_row_read(text, primary_key or [], limit_value):
                return CRUD
            if primary_key_ordered_limit(text, primary_key or []) is not None:
                return ORDERED_ONE_TO_MANY
            return RELATIONAL
    return CRUD
