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
  ``generate_series(...)``.

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
    sql = sql or ""
    dialect = sqlglot_dialect(source_engine)
    if len(sql) > MAX_SQL_CHARS:
        return _fallback(sql, "too_long", dialect, truncated=False)
    normalized = normalize_digest_text(sql, source_engine or "")

    def fallback(reason: FallbackReason) -> TableReferences:
        return _fallback(sql, reason, dialect, truncated=normalized.truncated)

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
    return _from_statements(statements, dialect, normalized.truncated)


def regex_answer(sql: str, source_engine: str, reason: FallbackReason) -> TableReferences:
    """The regex extractor's answer as a fallback with ``reason``, without
    trying sqlglot (for a caller whose :func:`table_references` call raised)."""
    return _fallback(sql or "", reason, sqlglot_dialect(source_engine), truncated=False)


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
            if ref is None or _is_cte_reference(table, ref):
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
