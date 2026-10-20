"""Derive Aurora access patterns deterministically from the observed query log.

Every other engine's schema designer *authors* its access patterns with the LLM,
because the source SQL has to be redesigned into that engine's idiom — a
DynamoDB key-condition expression is not mechanically recoverable from a SELECT.
Relational is the exception: on ``carry_over`` the source statement runs as-is,
and on ``translate`` it stays the same query modulo dialect. So an Aurora access
pattern is a *projection* of something already measured rather than a design
invented from scratch.

That matters for two reasons beyond tidiness (ADR-028 script-first):

* ``design_rps`` comes from the collector's ``calls_per_second``, a measured
  value. The DocumentDB path fills it with the LLM, which is why its load-test
  handler has to filter ``design_rps > 0`` and can silently end up with zero
  testable patterns.
* ``source_latency_ms_p95`` and ``source_db_load_pct`` carry the *source*
  baseline for the same statement, so a relational load test can report a
  before/after per query. No redesigning engine can do that, because it has no
  comparable before-number.

The single genuine design decision is ``index_used`` — which index serves the
predicate. That is resolved here when the leftmost-prefix rule gives an
unambiguous answer, and handed to the LLM as a typed residual when it does not.
Never a silent guess.

``queries`` is already assignment-scoped to this engine (#157 review, I1):
callers project the collector's full query log down to this engine's own
in-scope queries before deriving anything. So ``in_scope`` on every pattern
here mirrors that scope unconditionally, never a per-query table lookup — a
query whose table cannot be identified (the collector's own ``"unknown"``
placeholder, say) is still in scope, just not load-testable (``load_testable``,
a separate field).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.contracts.schema_design_input import AgentQueryPattern, AgentTable
from src.tools.sql.query_facts import query_facts

# A query is reported as unserved rather than guessed at when no index leads
# with any of its filter columns. That is a finding, not a gap: it means the
# planner is expected to sequential-scan, and the collector's own
# `full_table_scans` / `queries_without_index` counters corroborate it.
NO_INDEX = "__seq_scan__"


@dataclass(frozen=True)
class IndexCandidate:
    """One index considered for a query, with how far its leading columns match."""

    table_name: str
    index_name: str
    columns: tuple[str, ...]
    is_unique: bool
    is_primary: bool
    prefix_len: int


@dataclass
class DerivedPatterns:
    """Result of a derivation pass."""

    patterns: list[dict] = field(default_factory=list)
    residuals: list[dict] = field(default_factory=list)


def _norm(name: str) -> str:
    """Fold an identifier for comparison only — never for emission.

    Matches schema-insensitively, the same way ``delta_merge._table_key``
    does (#157 review, C1): strip bracket/quote characters anywhere in the
    name, fold case, and keep only the last ``.``-separated segment. A
    collector table name like ``wordpress.wp_posts`` therefore matches a
    design table named bare ``wp_posts`` — without this, every pattern on a
    schema-qualified source looks undesigned and comes back out of scope.
    """
    cleaned = "".join(c for c in name if c not in '`"[]').strip().lower()
    return cleaned.rsplit(".", 1)[-1].strip()


def _leading_prefix_len(index_columns: list[str], filter_columns: set[str]) -> int:
    """How many of an index's leading columns are covered by the predicate.

    B-tree indexes are usable left-to-right, so an index on ``(a, b, c)`` serves
    a filter on ``{a}`` and on ``{a, b}`` but not one on ``{b}`` alone. Counting
    the leading run is therefore the whole of the deterministic part — anything
    beyond it (selectivity, correlation, partial-index predicates) is judgment
    and deliberately not attempted here.
    """
    n = 0
    for col in index_columns:
        if _norm(col) in filter_columns:
            n += 1
        else:
            break
    return n


def _pk_index_name(table: AgentTable, engine: str) -> str:
    """The serving index's name for a table's own primary key, by dialect.

    MySQL always names it ``PRIMARY``; PostgreSQL auto-names it
    ``<table>_pkey`` (``ddl_generator.is_primary_key_index`` already knows
    this split for the collector side). Reporting the dialect's real default
    name here, rather than MySQL's unconditionally, is what a reader expects
    to see when confirming the index in a PostgreSQL console (#157 review).
    """
    if engine == "aurora_postgresql":
        return f"{table.table_name}_pkey"
    return "PRIMARY"


def _candidates_for(
    query: AgentQueryPattern, tables_by_name: dict[str, AgentTable], engine: str
) -> list[IndexCandidate]:
    """Every index on the query's tables whose leading column the filter covers."""
    filter_cols = {_norm(c) for c in (query.filter_columns or [])}
    if not filter_cols:
        return []

    found: list[IndexCandidate] = []
    for raw_name in query.tables_accessed:
        table = tables_by_name.get(_norm(raw_name))
        if table is None:
            continue

        # The primary key is an index even when the collector does not list it
        # as one; a PK lookup is the single most common relational pattern and
        # omitting it would send most point reads to the LLM as ambiguous.
        # The synthetic dialect-default name (_pk_index_name) is only a guess
        # for that gap, though: when the collector DID report the PK's real
        # index (an entry with is_primary=True and its own index_name), that
        # real name is used instead (#157 review, S3) rather than overridden
        # by a guess the collector already answered.
        declared: list[tuple[str, list[str], bool, bool]] = []
        has_real_pk_index = any(idx.is_primary for idx in table.indexes or [])
        if table.primary_key and not has_real_pk_index:
            declared.append((_pk_index_name(table, engine), list(table.primary_key), True, True))
        for idx in table.indexes or []:
            declared.append(
                (idx.index_name, list(idx.columns), idx.is_unique, bool(idx.is_primary))
            )

        for index_name, columns, is_unique, is_primary in declared:
            prefix = _leading_prefix_len(columns, filter_cols)
            if prefix > 0:
                found.append(
                    IndexCandidate(
                        table_name=table.table_name,
                        index_name=index_name,
                        columns=tuple(columns),
                        is_unique=is_unique,
                        is_primary=is_primary,
                        prefix_len=prefix,
                    )
                )
    return found


def _resolve_index(
    candidates: list[IndexCandidate],
    *,
    has_predicate: bool,
    predicate_known: bool,
) -> tuple[str, bool, str]:
    """Pick the serving index. Returns (index_used, needs_judgment, reason).

    Deterministic only when one candidate is strictly better than the rest.
    Ties are handed over rather than broken by an arbitrary rule, because the
    tiebreak a human would apply — selectivity — is not in the contract.

    ``predicate_known`` separates an unknown predicate from a confirmed empty
    one (#157 review, I3): the collector reports ``filter_columns=None`` when
    it never recorded predicate columns for a query at all, which is a
    different fact from an empty list (a confirmed scan with no predicate).
    Only the known case gets a claim either way; the unknown one makes
    neither claim and is handed to the LLM.

    ``has_predicate`` separates two cases that both produce zero candidates and
    are not the same finding. A query with filter columns that no index leads
    with is a *missing index*, and worth asking about. A query with no filter at
    all — ``SELECT count(*)``, an unqualified report scan — is correctly served
    by a sequential scan, and asking the LLM to "confirm whether an index should
    be added" invents a question with an obvious answer. Only the first is a
    residual.
    """
    if not predicate_known:
        return (
            NO_INDEX,
            True,
            "Filter predicate unknown; the collector did not record one for "
            "this query. Cannot confirm an index serves it or that a "
            "sequential scan is the expected plan. Confirm the predicate "
            "and whether an index should be added.",
        )
    if not candidates:
        if has_predicate:
            return (
                NO_INDEX,
                True,
                "No index leads with any filtered column; a sequential scan is expected. "
                "Confirm whether an index should be added for this pattern.",
            )
        return (
            NO_INDEX,
            False,
            "No filter predicate; a full scan is the expected plan.",
        )

    best = max(c.prefix_len for c in candidates)
    top = [c for c in candidates if c.prefix_len == best]

    if len(top) == 1:
        return top[0].index_name, False, ""

    # A unique index covering the same prefix is strictly more selective, so
    # that one tie is safe to break.
    unique_top = [c for c in top if c.is_unique]
    if len(unique_top) == 1:
        return unique_top[0].index_name, False, ""

    names = ", ".join(sorted(f"{c.table_name}.{c.index_name}" for c in top))
    return (
        top[0].index_name,
        True,
        f"{len(top)} indexes cover the same {best}-column prefix ({names}); "
        "confirm which one should serve this pattern.",
    )


def _describe(query: AgentQueryPattern) -> str:
    """A one-line human description, built from structure rather than prose."""
    op = query.query_type.value if query.query_type else "QUERY"
    tables = ", ".join(query.tables_accessed) or "unknown table"
    parts = [f"{op} on {tables}"]
    if query.filter_columns:
        parts.append(f"filtered by {', '.join(query.filter_columns)}")
    if query.sort_columns:
        parts.append(f"sorted by {', '.join(query.sort_columns)}")
    if query.has_joins and query.join_count:
        parts.append(f"{query.join_count} join(s)")
    if query.has_aggregations:
        parts.append("aggregated")
    return "; ".join(parts)


def _design_rps(query: AgentQueryPattern) -> float:
    """Measured request rate. Falls back to the hourly figure, never to a guess.

    ``calls_per_second`` is populated by every source tool module, but it is
    Optional in the contract, so the hourly counter is the backstop. Both are
    observations; if neither exists the pattern is reported at 0.0 and marked
    not load-testable rather than assigned an invented rate.
    """
    if query.calls_per_second is not None:
        return round(float(query.calls_per_second), 6)
    if query.frequency_per_hour:
        return round(float(query.frequency_per_hour) / 3600.0, 6)
    return 0.0


# Commands that read no table rows (#1250). A procedure call (``CALL``, ``EXEC``)
# is a command too, but it reads rows, so it is not listed: its predicate stays
# unknown.
_NO_ROW_COMMANDS = frozenset(
    {
        "show",
        "set",
        "use",
        "describe",
        "desc",
        "begin",
        "start",
        "commit",
        "rollback",
        "savepoint",
        "release",
    }
)


def _first_word(text: str) -> str:
    words = text.lstrip(" \t\r\n(").split(None, 1)
    return words[0].lower() if words else ""


def _read_predicate(
    query: AgentQueryPattern, source_engine: str | None
) -> tuple[AgentQueryPattern, bool]:
    """``query`` with the predicate columns its own text shows, when the collector
    recorded none (#1250).

    A SQL-script collection (the bundled samples) carries no ``filter_columns``, but the
    statement is right there: the SQL facade reads it in the source dialect, and each
    scope's equality and range columns (``WHERE`` and ``JOIN`` terms held to a value or
    bounded) are the columns an index can lead with. A statement with no ``WHERE`` and
    no such term is a confirmed no-predicate read (``[]``). A ``WHERE`` made only of
    other terms (an ``OR`` group, ``IS NULL``, a function test) keeps ``None``, as does
    text the facade cannot parse or read only up to a cut (a truncated digest): the
    predicate stays unknown rather than guessed. Without a source engine nothing is
    read (the dialect would be a guess).

    The flag is False for a statement that reads no table rows at all: an
    ``INSERT ... VALUES`` or a command that reads no rows (``SHOW``, ``SET``,
    transaction control, ...). It has no predicate (``[]``), and no index or scan
    serves it. A procedure call (``CALL``, ``EXEC``) reads rows, so it stays unknown.
    """
    if query.filter_columns is not None or not source_engine or not query.query_text:
        return query, True
    access = query_facts(query.query_text, source_engine).access_facts()
    no_rows = (access.kind == "insert" and access.inserts_values) or (
        access.kind == "other" and _first_word(query.query_text) in _NO_ROW_COMMANDS
    )
    if no_rows and (access.parsed_by == "sqlglot" or access.fallback_reason == "command"):
        return query.model_copy(update={"filter_columns": []}), False
    if access.parsed_by != "sqlglot" or not access.scopes or access.truncated_read:
        return query, True
    columns: list[str] = []
    for scope in access.scopes:
        for column in (*scope.equalities, *scope.ranges):
            if column.name not in columns:
                columns.append(column.name)
    if not columns and any(s.has_where or s.other_filters for s in access.scopes):
        return query, True
    return query.model_copy(update={"filter_columns": columns}), True


def derive_access_patterns(
    queries: list[AgentQueryPattern],
    tables: list[AgentTable],
    engine: str = "aurora_mysql",
    source_engine: str | None = None,
) -> DerivedPatterns:
    """Project observed queries onto Aurora access patterns.

    Pure and deterministic: same inputs, same output, no LLM and no I/O. Each
    pattern records ``script_derived`` so a reader can tell a projection from a
    judgment, matching how ``ddl_generator`` stamps column types. ``engine``
    only affects how a resolved primary-key index is named (see
    ``_pk_index_name``); defaulted to MySQL's convention for callers (and
    existing tests) that predate the PostgreSQL/MySQL split.

    ``queries`` is already assignment-scoped to this engine (#157 review,
    I1): the projected design input filters the collector's full query log
    down to this engine's own in-scope queries before this ever runs. Every
    pattern derived here is therefore ``in_scope: true`` unconditionally --
    it is never computed from whether a table happens to match the design.

    ``source_engine`` (the collector's source dialect) lets a query with no recorded
    ``filter_columns`` take its predicate from its own text (:func:`_read_predicate`).
    """
    tables_by_name = {_norm(t.table_name): t for t in tables}
    result = DerivedPatterns()

    for query, reads_rows in (_read_predicate(q, source_engine) for q in queries):
        # A query whose table this method cannot identify (typically the
        # collector's own "unknown" placeholder) is a load-testability gap,
        # not an assignment-scope one: there is nothing to resolve an index
        # against, but the query is still on this engine (see the docstring
        # above) -- in_scope stays true, only load_testable turns false.
        designed = [t for t in query.tables_accessed if _norm(t) in tables_by_name]
        if not designed:
            reason = (
                "None of this query's tables could be identified in the Aurora "
                "design (commonly the collector's own 'unknown' placeholder); no "
                "index can be resolved and this pattern cannot be load-tested."
            )
            result.patterns.append(
                {
                    "pattern_id": query.query_id,
                    "description": _describe(query),
                    "operation": query.query_type.value if query.query_type else "OTHER",
                    "sql_text": query.query_text,
                    "source_tables": list(query.tables_accessed),
                    "source_query_ids": [query.query_id],
                    "filter_columns": list(query.filter_columns or []),
                    "sort_columns": list(query.sort_columns or []),
                    "index_used": NO_INDEX,
                    "design_rps": _design_rps(query),
                    "source_latency_ms_p95": query.execution_time_ms_p95,
                    "source_db_load_pct": query.db_load_contribution_percent,
                    "script_derived": True,
                    "needs_judgment": False,
                    "judgment_reason": reason,
                    "in_scope": True,
                    "out_of_scope_reason": None,
                    "load_testable": False,
                    "load_testable_reason": reason,
                }
            )
            continue

        candidates = _candidates_for(query, tables_by_name, engine)
        has_predicate = bool(query.filter_columns)
        predicate_known = query.filter_columns is not None
        index_used, needs_judgment, reason = _resolve_index(
            candidates, has_predicate=has_predicate, predicate_known=predicate_known
        )
        if not reads_rows:
            reason = (
                "No filter predicate: the statement reads no table rows, so no index serves it."
            )

        rps = _design_rps(query)
        # load_testable is a separate fact from in_scope (#157 review, I4): a
        # query can be squarely in this design's scope with nothing measured
        # to drive a load test by (no calls_per_second and no hourly count in
        # the collector). in_scope is unconditionally true (see the docstring
        # above), which is the only thing it mirrors.
        load_testable = rps > 0
        load_testable_reason = (
            None
            if load_testable
            else "No measured call rate in the query log; cannot be load-tested at a "
            "representative rate."
        )

        result.patterns.append(
            {
                "pattern_id": query.query_id,
                "description": _describe(query),
                "operation": query.query_type.value if query.query_type else "OTHER",
                "sql_text": query.query_text,
                "source_tables": list(query.tables_accessed),
                "source_query_ids": [query.query_id],
                "filter_columns": list(query.filter_columns or []),
                "sort_columns": list(query.sort_columns or []),
                "index_used": index_used,
                "design_rps": rps,
                "source_latency_ms_p95": query.execution_time_ms_p95,
                "source_db_load_pct": query.db_load_contribution_percent,
                "script_derived": not needs_judgment,
                "needs_judgment": needs_judgment,
                "judgment_reason": reason,
                "in_scope": True,
                "out_of_scope_reason": None,
                "load_testable": load_testable,
                "load_testable_reason": load_testable_reason,
            }
        )

        if needs_judgment:
            result.residuals.append(
                {
                    "pattern_id": query.query_id,
                    "tables": list(query.tables_accessed),
                    "filter_columns": list(query.filter_columns or []),
                    "fallback_index": index_used,
                    "reason": reason,
                }
            )

    return result
