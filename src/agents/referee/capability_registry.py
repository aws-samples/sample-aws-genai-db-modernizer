# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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

from collections.abc import Iterable, Mapping

from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.utility_statements import is_utility_statement
from src.shared import catalog
from src.shared.capability_labels import LEGACY_CAPABILITY_IDS
from src.shared.unsupported_pattern import is_dedup_only_group_by
from src.tools.sql.query_facts import QueryFacts, query_facts

__all__ = [
    "ENGINE_CAPABILITIES",
    "CAPABILITY_FACTS",
    "SIGNAL_TO_CAPABILITY",
    "detect_required_capabilities",
    "can_engine_serve_capability",
    "missing_capabilities",
    "LEGACY_CAPABILITY_IDS",
    "current_capability_ids",
    "triage_query_capabilities",
    "is_dedup_only_group_by",
    "requires_aggregation_capability",
    "requires_computed_join_capability",
]

# ---------------------------------------------------------------------------
# Hard capability matrix — architectural constraints, not fit preferences.
#
# A capability here means the engine CAN serve this pattern at all,
# even if poorly. Absence means "structurally impossible." The matrix, the query
# facts that require each capability and the triage signals that imply one are
# ``src/catalog/gates.toml``'s ``[capabilities.<id>]`` tables (ADR-031 Decision 8,
# #921), with the reason for each; the loader checks a capability linked to a
# pattern file (``pattern_capability``) names the engines that file supports. The
# detectors themselves live in ``src.tools.sql.query_facts``, once each.
# ---------------------------------------------------------------------------

ENGINE_CAPABILITIES: dict[str, set[str]] = {}
for _cap, _entry in catalog.hard_capabilities().items():
    for _engine in _entry.engines:
        ENGINE_CAPABILITIES.setdefault(_engine, set()).add(_cap)

# Which engine-neutral query facts (#491) mean a query requires a hard capability.
CAPABILITY_FACTS: dict[str, tuple[str, ...]] = {
    cap: entry.facts for cap, entry in catalog.hard_capabilities().items()
}


def _required_by_facts(facts: QueryFacts) -> set[str]:
    """The capabilities ``facts`` require (:data:`CAPABILITY_FACTS`)."""
    return {
        cap
        for cap, names in CAPABILITY_FACTS.items()
        if any(getattr(facts, name) for name in names)
    }


def requires_aggregation_capability(
    query_text: str | None, source_engine: str | None = None
) -> bool:
    """Whether ``query_text`` needs the "aggregation" hard capability (#338, #336).

    True for any aggregate function call (SUM/COUNT/AVG/MIN/MAX/GROUP_CONCAT/
    STRING_AGG/ARRAY_AGG, ...), MySQL's FOUND_ROWS()/SQL_CALC_FOUND_ROWS, or a
    window function, regardless of GROUP BY or join count. For a plain
    GROUP BY with none of those, defers to is_dedup_only_group_by (#336):
    true unless that call says the GROUP BY is only de-duplicating rows.
    The fact is ``QueryFacts.needs_aggregation`` (``src.tools.sql.query_facts``),
    read in ``source_engine``'s dialect.
    """
    return query_facts(query_text, source_engine).needs_aggregation


def requires_computed_join_capability(
    query_text: str | None, source_engine: str | None = None
) -> bool:
    """Whether ``query_text`` joins on a computed expression (review finding B2).

    True when some ``JOIN ... ON`` clause's condition applies a function call
    to either side of a comparison against a column (``lower(groups.name) =
    users.username_lower``, or the reverse), which a key-value store cannot
    evaluate as part of a key lookup -- a plain ``t1.id = t2.id`` join
    condition does not trigger this, and neither does a function compared
    against a bind parameter (``lower(g.name) = $1``), which is a filter, not
    a computed join key. The fact is ``QueryFacts.computed_join_key``.
    """
    return query_facts(query_text, source_engine).computed_join_key


# ---------------------------------------------------------------------------
# Triage signal → hard capability mapping
#
# When a triage signal implies a hard architectural requirement (a capability's
# ``triage_signals`` in ``src/catalog/gates.toml``). Not all signals imply hard
# capabilities — most are just workload preferences.
# ---------------------------------------------------------------------------

SIGNAL_TO_CAPABILITY: dict[str, str] = {
    signal: cap
    for cap, entry in catalog.hard_capabilities().items()
    for signal in entry.triage_signals
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

    # 2. The query's engine-neutral facts (#491), read in the source's dialect
    if query_text:
        required |= _required_by_facts(query_facts(query_text, source.engine if source else None))

    # 3. Utility/metadata statements (#327) -- only Aurora understands DDL,
    # session variables and transaction control.
    if is_utility_statement(query_text, tables_accessed, source):
        required.add("sql_admin")

    return sorted(required)


# ``LEGACY_CAPABILITY_IDS`` (src.shared.capability_labels): a triage.json written
# before #493 names ``text_pattern_match`` by its old name; read unmapped, every
# engine would "lack" it and be gated out of its text-search queries on a customer
# edit or a resumed reality check.


def current_capability_ids(capabilities: Iterable[str]) -> list[str]:
    """``capabilities`` with every legacy id mapped to today's (order kept, no duplicates)."""
    return list(dict.fromkeys(LEGACY_CAPABILITY_IDS.get(c, c) for c in capabilities))


def triage_query_capabilities(triage: Mapping | None) -> dict[str, list[str]]:
    """A triage artifact's ``query_capabilities``, with legacy capability ids mapped.

    The one reader of that field: :meth:`ScoringContext.from_pipeline
    <src.agents.referee.query_scoring.ScoringContext.from_pipeline>` and the
    reality check handler both call it, so an assessment saved before a rename
    gates exactly as a new one does.
    """
    raw = (triage or {}).get("query_capabilities") or {}
    return {str(qid): current_capability_ids(caps or []) for qid, caps in raw.items()}


def missing_capabilities(engine: str, required: Iterable[str]) -> list[str]:
    """The ``required`` capabilities ``engine`` lacks, sorted (empty: it can
    serve them all). The one fact :func:`can_engine_serve_capability` tests,
    so a caller naming WHICH capability is missing (the customer gate's
    rejection, #801) never re-derives it from ``ENGINE_CAPABILITIES``. A legacy
    id (:data:`LEGACY_CAPABILITY_IDS`) is read as today's."""
    engine_caps = ENGINE_CAPABILITIES.get(engine, set())
    return sorted({cap for cap in current_capability_ids(required) if cap not in engine_caps})


def can_engine_serve_capability(engine: str, required: list[str]) -> bool:
    """Check if an engine can serve ALL required capabilities.

    Returns True if:
    - The query has no required capabilities (any engine can serve it), OR
    - The engine has ALL required capabilities in its capability set.
    """
    return not missing_capabilities(engine, required)
