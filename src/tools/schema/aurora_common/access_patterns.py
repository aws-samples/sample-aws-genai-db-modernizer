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
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.contracts.schema_design_input import AgentQueryPattern, AgentTable

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
    """Fold an identifier for comparison only — never for emission."""
    return name.strip().strip('"').strip("`").lower()


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


def _candidates_for(
    query: AgentQueryPattern, tables_by_name: dict[str, AgentTable]
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
        declared: list[tuple[str, list[str], bool, bool]] = []
        if table.primary_key:
            declared.append(("PRIMARY", list(table.primary_key), True, True))
        for idx in table.indexes or []:
            if idx.is_primary and table.primary_key:
                continue  # already counted, avoid a self-tie
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
) -> tuple[str, bool, str]:
    """Pick the serving index. Returns (index_used, needs_judgment, reason).

    Deterministic only when one candidate is strictly better than the rest.
    Ties are handed over rather than broken by an arbitrary rule, because the
    tiebreak a human would apply — selectivity — is not in the contract.

    ``has_predicate`` separates two cases that both produce zero candidates and
    are not the same finding. A query with filter columns that no index leads
    with is a *missing index*, and worth asking about. A query with no filter at
    all — ``SELECT count(*)``, an unqualified report scan — is correctly served
    by a sequential scan, and asking the LLM to "confirm whether an index should
    be added" invents a question with an obvious answer. Only the first is a
    residual.
    """
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
    observations; if neither exists the pattern is reported at 0.0 and flagged
    out of scope rather than assigned an invented rate.
    """
    if query.calls_per_second is not None:
        return round(float(query.calls_per_second), 6)
    if query.frequency_per_hour:
        return round(float(query.frequency_per_hour) / 3600.0, 6)
    return 0.0


def derive_access_patterns(
    queries: list[AgentQueryPattern],
    tables: list[AgentTable],
) -> DerivedPatterns:
    """Project observed queries onto Aurora access patterns.

    Pure and deterministic: same inputs, same output, no LLM and no I/O. Each
    pattern records ``script_derived`` so a reader can tell a projection from a
    judgment, matching how ``ddl_generator`` stamps column types.
    """
    tables_by_name = {_norm(t.table_name): t for t in tables}
    result = DerivedPatterns()

    for query in queries:
        # Assignment splits a source across engines, so a query touching only
        # tables that went elsewhere is not an Aurora access pattern at all. It
        # is reported out of scope rather than as a missing index — otherwise
        # every query assigned to DynamoDB or DocumentDB comes back here as an
        # "add an index" residual, which is both wrong and loud.
        designed = [t for t in query.tables_accessed if _norm(t) in tables_by_name]
        if not designed:
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
                    "judgment_reason": (
                        "None of this query's tables are in the Aurora design; it was "
                        "assigned to another engine or the table is absent from the "
                        "collector."
                    ),
                    "in_scope": False,
                    "out_of_scope_reason": (
                        "Query does not touch any table designed for this target."
                    ),
                }
            )
            continue

        candidates = _candidates_for(query, tables_by_name)
        has_predicate = bool(query.filter_columns)
        index_used, needs_judgment, reason = _resolve_index(candidates, has_predicate=has_predicate)

        rps = _design_rps(query)
        in_scope = rps > 0
        out_of_scope_reason = (
            None
            if in_scope
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
                "in_scope": in_scope,
                "out_of_scope_reason": out_of_scope_reason,
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
