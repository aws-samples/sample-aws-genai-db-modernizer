"""Capability Registry — Engine capabilities and serviceability detection.

Maps each engine's fundamental capabilities (what it CAN do at an
architectural level), and which query facts require which capability. The
facts themselves are detected once, engine-neutrally, in
``src.tools.sql.query_facts`` (#491).

Used by ``query_scoring.score_query_for_engine``'s hard capability gate
(#490/#549) to prevent query absorption when the target engine fundamentally
cannot serve the access pattern. This registry tracks hard architectural
constraints: things an engine structurally cannot do -- a different fact
from workload-pattern fit scoring, which lives entirely in
``query_scoring.py`` now (the reality check's own, separate
``ENGINE_CAPABILITIES`` fit-scoring table this registry used to be
distinguished from is deleted).
"""

from __future__ import annotations

from collections.abc import Iterable

from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.utility_statements import is_utility_statement
from src.shared.unsupported_pattern import is_dedup_only_group_by
from src.tools.sql.query_facts import QueryFacts, query_facts

__all__ = [
    "ENGINE_CAPABILITIES",
    "CAPABILITY_FACTS",
    "SIGNAL_TO_CAPABILITY",
    "detect_required_capabilities",
    "can_engine_serve_capability",
    "missing_capabilities",
    "is_dedup_only_group_by",
    "requires_aggregation_capability",
    "requires_computed_join_capability",
]

# ---------------------------------------------------------------------------
# Hard capability matrix — architectural constraints, not fit preferences.
#
# A capability here means the engine CAN serve this pattern at all,
# even if poorly. Absence means "structurally impossible."
# ---------------------------------------------------------------------------

ENGINE_CAPABILITIES: dict[str, set[str]] = {
    # TransactWriteItems/TransactGetItems are ACID across up to 100 items (ADR-031
    # Decision 3): a transaction is never a capability DynamoDB lacks. A write too
    # wide for one transaction is scored by its shape (transaction_shape, #493).
    "dynamodb": {"multi_doc_acid", "strong_consistency"},
    # $regex (LIKE/regex-equivalent) and $text are both native query
    # operators; a leading-wildcard $regex runs unindexed, the same scan
    # cost Aurora MySQL's LIKE pays below -- so inverted_index is a
    # capability DocumentDB has, even though it is not an indexed one
    # (only Aurora PostgreSQL's pg_trgm indexes it). Previously omitted
    # here, which hard-excluded DocumentDB while admitting Aurora MySQL for
    # the identical unindexed position -- fixed by #485.
    "documentdb": {"multi_doc_acid", "strong_consistency", "aggregation", "inverted_index"},
    "opensearch": {"inverted_index", "scan_engine", "aggregation"},
    "aurora_postgresql": {
        "multi_doc_acid",
        "strong_consistency",
        "scan_engine",
        "inverted_index",
        "aggregation",
        "complex_joins",
        "sql_admin",
        "computed_join",
    },
    # InnoDB FULLTEXT indexes (MATCH ... AGAINST) since MySQL 5.6
    "aurora_mysql": {
        "multi_doc_acid",
        "strong_consistency",
        "scan_engine",
        "inverted_index",
        "aggregation",
        "complex_joins",
        "sql_admin",
        "computed_join",
    },
}

# ---------------------------------------------------------------------------
# Capability facts -- which engine-neutral query facts (#491,
# ``src.tools.sql.query_facts``) mean a query requires a hard capability. The
# detectors themselves live in that module, once each; this registry only
# says what a fact requires.
# ---------------------------------------------------------------------------

CAPABILITY_FACTS: dict[str, tuple[str, ...]] = {
    # A leading-wildcard LIKE, MySQL MATCH ... AGAINST, PostgreSQL full-text
    # search (to_tsvector/to_tsquery/@@).
    "inverted_index": ("leading_wildcard_like", "fulltext_match", "text_search_operator"),
    # A ranking/offset window function, WITH RECURSIVE.
    "scan_engine": ("ranking_window_function", "recursive_cte"),
    # An aggregate call, SQL_CALC_FOUND_ROWS, a window function, or a GROUP
    # BY that does more than de-duplicate (#338, #336:
    # ``QueryFacts.needs_aggregation``).
    "aggregation": ("needs_aggregation",),
    # A join on a computed expression (review of #375, finding B2).
    "computed_join": ("computed_join_key",),
    "multi_doc_acid": (),  # Structural detection, not from the query text
}


def _required_by_facts(facts: QueryFacts) -> set[str]:
    """The capabilities ``facts`` require (:data:`CAPABILITY_FACTS`)."""
    return {
        cap
        for cap, names in CAPABILITY_FACTS.items()
        if any(getattr(facts, name) for name in names)
    }


def requires_aggregation_capability(query_text: str | None) -> bool:
    """Whether ``query_text`` needs the "aggregation" hard capability (#338, #336).

    True for any aggregate function call (SUM/COUNT/AVG/MIN/MAX/FOUND_ROWS/
    GROUP_CONCAT/STRING_AGG/ARRAY_AGG), MySQL's SQL_CALC_FOUND_ROWS modifier, or
    a window function, regardless of GROUP BY or join count. For a plain
    GROUP BY with none of those, defers to is_dedup_only_group_by (#336):
    true unless that call says the GROUP BY is only de-duplicating rows.
    The fact is ``QueryFacts.needs_aggregation`` (``src.tools.sql.query_facts``).
    """
    return query_facts(query_text).needs_aggregation


def requires_computed_join_capability(query_text: str | None) -> bool:
    """Whether ``query_text`` joins on a computed expression (review finding B2).

    True when some ``JOIN ... ON`` clause's condition applies a function call
    to either side of a comparison against a column (``lower(groups.name) =
    users.username_lower``, or the reverse), which a key-value store cannot
    evaluate as part of a key lookup -- a plain ``t1.id = t2.id`` join
    condition does not trigger this, and neither does a function compared
    against a bind parameter (``lower(g.name) = $1``), which is a filter, not
    a computed join key. The fact is ``QueryFacts.computed_join_key``.
    """
    return query_facts(query_text).computed_join_key


# ---------------------------------------------------------------------------
# Triage signal → hard capability mapping
#
# When a triage signal implies a hard architectural requirement, map it here.
# Not all signals imply hard capabilities — most are just workload preferences.
# ---------------------------------------------------------------------------

SIGNAL_TO_CAPABILITY: dict[str, str] = {
    "text_search": "inverted_index",
    # SUM/COUNT/AVG with GROUP BY, or an aggregate combined with a JOIN, is a
    # hard requirement: an engine with no aggregation capability (DynamoDB)
    # cannot run it at all, even on a single query, not just "poorly" (#338).
    "aggregations": "aggregation",
    # 3+ table joins are the same kind of hard requirement -- DynamoDB and
    # OpenSearch have no join capability regardless of how the data is keyed.
    "complex_joins": "complex_joins",
}


def detect_required_capabilities(
    query_text: str,
    signals: list[str],
    tables_accessed: Iterable[str] | None = None,
    source: SourceScope | None = None,
) -> list[str]:
    """Detect hard capability requirements from query text and triage signals.

    Returns a list of capability names that the query structurally requires.
    An empty list means any engine can potentially serve this query.
    ``tables_accessed`` is used only to detect a utility/metadata statement
    (#327): those need "sql_admin", which only the Aurora engines have, so
    the reality check's serviceability gate keeps them there instead of
    letting them follow the normal confidence-scoring path onto DynamoDB.
    ``source`` is the collected database (#835), see :func:`is_utility_statement`.
    """
    required: set[str] = set()

    # 1. Signal-based detection (fast path)
    for sig in signals:
        cap = SIGNAL_TO_CAPABILITY.get(sig)
        if cap:
            required.add(cap)

    # 2. The query's engine-neutral facts (#491)
    if query_text:
        required |= _required_by_facts(query_facts(query_text))

    # 3. Utility/metadata statements (#327) -- only Aurora understands DDL,
    # session variables and transaction control.
    if is_utility_statement(query_text, tables_accessed, source):
        required.add("sql_admin")

    return sorted(required)


def missing_capabilities(engine: str, required: Iterable[str]) -> list[str]:
    """The ``required`` capabilities ``engine`` lacks, sorted (empty: it can
    serve them all). The one fact :func:`can_engine_serve_capability` tests,
    so a caller naming WHICH capability is missing (the customer gate's
    rejection, #801) never re-derives it from ``ENGINE_CAPABILITIES``."""
    engine_caps = ENGINE_CAPABILITIES.get(engine, set())
    return sorted({cap for cap in required if cap not in engine_caps})


def can_engine_serve_capability(engine: str, required: list[str]) -> bool:
    """Check if an engine can serve ALL required capabilities.

    Returns True if:
    - The query has no required capabilities (any engine can serve it), OR
    - The engine has ALL required capabilities in its capability set.
    """
    return not missing_capabilities(engine, required)
