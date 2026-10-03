"""
Reality Check — CTO-level optimization of query-to-engine assignments.

Runs after the initial assignment and before schema design. Thinks like a CTO:
"Every additional database engine = monitoring, backups, failover, expertise,
and operational burden. Does this engine earn its place?"

Default posture: 2 engines max. A third engine must provide genuinely unique
capabilities that no other committed engine can serve.

Five passes:
  0. Unique value assessment — does each engine provide irreplaceable capabilities?
  1. Aurora absorption — absorb orphan queries from low-count engines into committed Aurora
  2. Consolidation — absorb redundant engines into committed engines
  3. Architectural pattern detection — CQRS, materialized views, event sourcing
  4. Integration topology — recommend specific sync mechanisms and patterns

The output is a revised assignment (new version) plus architectural recommendations
that feed into synthesis.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass

from src.agents.referee.capability_registry import (
    can_engine_serve_capability,
    suggest_lightweight_alternative,
)

# ---------------------------------------------------------------------------
# Engine capability matrix — what each engine CAN do (even if not optimal)
# ---------------------------------------------------------------------------

ENGINE_CAPABILITIES: dict[str, set[str]] = {
    "dynamodb": {
        "key_value_lookup",
        "range_query",
        "write_heavy",
        "time_series_simple",
        "leaderboard_simple",  # with GSI + Query
        "metadata_config",
        "session_store",
    },
    "documentdb": {
        "key_value_lookup",
        "range_query",
        "aggregation",
        "nested_document",
        "flexible_schema",
        "text_search_basic",  # $regex, not great but works
        "write_heavy",
        "time_series_simple",
    },
    "opensearch": {
        "text_search",
        "fuzzy_search",
        "aggregation",
        "analytics",
        "time_series",
        "leaderboard_simple",  # top-N via sort
        "range_query",
        "key_value_lookup",  # get by _id
        "nested_document",
        "geo_search",
    },
    "elasticache": {
        "session_store",
        "leaderboard",
        "rate_limiting",
        "pub_sub",
        "caching",
    },
    "aurora_postgresql": {
        "key_value_lookup",
        "range_query",
        "write_heavy",
        "aggregation",
        "analytics",
        "complex_joins",
        "transactions",
        "referential_integrity",
        "text_search_basic",  # tsvector, not as good as OpenSearch
        "time_series_simple",
        "metadata_config",
    },
    "aurora_mysql": {
        "key_value_lookup",
        "range_query",
        "write_heavy",
        "aggregation",
        "analytics",
        "complex_joins",
        "transactions",
        "referential_integrity",
        "text_search_basic",  # FULLTEXT index, limited
        "time_series_simple",
        "metadata_config",
    },
}

# Minimum query threshold — if an engine has fewer queries than this,
# it's a trivial consolidation target (even without unique value check)
MIN_QUERIES_THRESHOLD = 5

# Target engine count — the reality check tries to stay at or below this.
# A third engine must provide genuinely unique, irreplaceable capabilities.
TARGET_ENGINE_COUNT = 2

# Per-engine operational overhead (monthly base cost + ops burden).
# This is not just infrastructure — it includes monitoring, backups,
# failover config, team expertise, security patching, etc.
ENGINE_BASE_COST: dict[str, float] = {
    "dynamodb": 0,  # Serverless, pay per use, minimal ops
    "documentdb": 200,  # Minimum cluster + ops burden
    "opensearch": 150,  # Minimum domain + ops burden
    "elasticache": 50,  # Minimum node + minimal ops
    "aurora_postgresql": 250,  # Minimum cluster (writer + reader) + ops burden
    "aurora_mysql": 250,  # Minimum cluster (writer + reader) + ops burden
}

# Additional operational burden for each engine beyond TARGET_ENGINE_COUNT.
# This captures the non-monetary cost: team needs expertise in N databases,
# N monitoring dashboards, N backup strategies, N failover runbooks, etc.
EXTRA_ENGINE_BURDEN_MONTHLY = 300  # $/mo equivalent per extra engine

# ---------------------------------------------------------------------------
# Aurora Absorption Pass (Pass 1)
# ---------------------------------------------------------------------------

# Engines with fewer queries than this are candidates for Aurora absorption
AURORA_ABSORPTION_QUERY_THRESHOLD = 10

# Aurora must score at least this to absorb a query
AURORA_ABSORPTION_MIN_FIT = 50

# If the specialist engine scores this much higher than Aurora, query is protected
SPECIALIST_DELTA_THRESHOLD = 30

# Mandatory engines (signal overrides) at or below this many queries can still be
# absorbed, when Aurora covers each query's signal at a basic level (#165)
TINY_MANDATORY_QUERY_THRESHOLD = 5

# Set of Aurora engine identifiers
AURORA_ENGINES = {"aurora_postgresql", "aurora_mysql"}


@dataclass
class AuroraAbsorptionResult:
    """Result of the Aurora absorption pass."""

    absorbed_queries: list[dict]
    engines_eliminated: list[str]
    engines_reduced: list[str]
    aurora_engine: str


# ---------------------------------------------------------------------------
# Triage signal → capability mapping
# Used to check if a target engine can handle a query's workload pattern
# ---------------------------------------------------------------------------

SIGNAL_TO_CAPABILITY: dict[str, str] = {
    "text_search": "text_search",
    "key_value_lookups": "key_value_lookup",
    "leaderboard_pattern": "leaderboard_simple",
    "time_series": "time_series_simple",
    "metadata_config": "metadata_config",
    "session_store": "session_store",
    "low_frequency_writes": "write_heavy",
    "junction_tables": "range_query",
    "complex_joins": "complex_joins",
    "subqueries": "complex_joins",
    "transactions": "transactions",
    "referential_integrity": "referential_integrity",
}

# Signals whose specialist capability has a basic form in other engines.
# Used only for tiny mandatory engines (#165): Aurora FULLTEXT can stand in for
# OpenSearch when the search workload is a handful of queries.
SIGNAL_TO_BASIC_CAPABILITY: dict[str, str] = {
    "text_search": "text_search_basic",
}

# ---------------------------------------------------------------------------
# Differentiation scoring — how much better is this engine vs alternatives?
# ---------------------------------------------------------------------------

# Minimum confidence delta for a query to be "unique" to an engine.
# If the best alternative is within this many points, the query is redundant.
UNIQUE_DELTA_THRESHOLD = 15

# Bonus for signal-capability match (engine has the exact capability the query needs)
SIGNAL_MATCH_BONUS = 20

# Base score for engines that can serve basic CRUD but have no specific advantage
BASIC_CRUD_SCORE = 40

# ---------------------------------------------------------------------------
# Architectural patterns
# ---------------------------------------------------------------------------

ARCHITECTURAL_PATTERNS: dict[str, dict] = {
    "CQRS": {
        "name": "Command Query Responsibility Segregation (CQRS)",
        "description": (
            "Separate write operations (commands) from read operations (queries) "
            "across different databases. The write database is optimized for "
            "transactional consistency, while the read database is optimized for "
            "query performance and flexibility."
        ),
        "when": "One engine handles most writes and another handles reads/search/analytics",
        "example": (
            "DynamoDB handles all CRUD operations (writes + simple reads). "
            "OpenSearch serves as a read-optimized view for full-text search, "
            "faceted filtering, and analytics queries. Data flows from DynamoDB "
            "to OpenSearch via zero-ETL integration."
        ),
    },
    "MATERIALIZED_VIEW": {
        "name": "Materialized View Pattern",
        "description": (
            "Maintain a pre-computed, denormalized copy of data in a secondary "
            "database optimized for specific query patterns. The secondary store "
            "is a read-only projection — all writes go through the primary."
        ),
        "when": "A secondary engine serves read-only queries on data owned by the primary",
        "example": (
            "DynamoDB is the source of truth for forum data. OpenSearch maintains "
            "a materialized view of discussions + users for full-text search. "
            "The view is automatically kept in sync via DynamoDB zero-ETL."
        ),
    },
    "EVENT_SOURCING": {
        "name": "Event-Driven Sync",
        "description": (
            "Use database change streams or event logs to propagate changes "
            "between databases asynchronously. Each database maintains its own "
            "optimized representation of the data."
        ),
        "when": "Multiple engines need the same data in different shapes",
        "example": (
            "DynamoDB Streams captures all item changes. A Lambda function "
            "transforms and routes changes to OpenSearch (for search) and "
            "ElastiCache (for leaderboards). Each consumer shapes the data "
            "for its specific access patterns."
        ),
    },
    "POLYGLOT_PERSISTENCE": {
        "name": "Polyglot Persistence",
        "description": (
            "Use different databases for different bounded contexts within "
            "the application. Each service/module owns its data in the database "
            "best suited for its access patterns."
        ),
        "when": "Different parts of the application have fundamentally different data access needs",
        "example": (
            "User profiles and forum CRUD in DynamoDB (key-value + hierarchical). "
            "Search and discovery in OpenSearch (full-text + relevance). "
            "Real-time leaderboards in ElastiCache (sorted sets + pub/sub)."
        ),
    },
}


def run_reality_check(
    assignment: dict,
    triage: dict,
    analysis_outputs: dict[str, dict],
    collector_output: dict,
    query_capabilities: dict[str, list[str]] | None = None,
) -> dict:
    """Run the CTO-level reality check on the assignment.

    Thinks like a pragmatic CTO: "Every additional database is operational
    burden. Does this engine EARN its place, or can another engine already
    in the stack handle these queries?"

    Returns a dict with:
      - revised_assignments: list of QueryAssignment dicts (possibly modified)
      - consolidations: list of consolidation decisions made
      - architectural_patterns: list of recommended patterns
      - recommendations: list of human-readable recommendations
      - unique_value_assessment: per-engine analysis of unique vs redundant queries
    """
    query_assignments = assignment.get("query_assignments", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    query_map = {q["query_id"]: q for q in queries}
    signals = triage.get("signals", [])

    # Load query capabilities (hard architectural requirements)
    if query_capabilities is None:
        query_capabilities = triage.get("query_capabilities", {})

    query_signals = _build_query_signals(signals)

    # Build engine query counts
    engine_queries: dict[str, list[dict]] = defaultdict(list)
    for qa in query_assignments:
        engine_queries[qa["assigned_engine"]].append(qa)

    # Build set of queries with mandatory signal overrides (cannot be consolidated)
    mandatory_query_ids: set[str] = {
        qa["query_id"] for qa in query_assignments if _is_signal_override(qa)
    }

    # Engines committed via mandatory signal overrides (e.g., OpenSearch for text_search)
    mandatory_committed_engines: set[str] = set()
    for qa in query_assignments:
        if qa["query_id"] in mandatory_query_ids:
            mandatory_committed_engines.add(qa["assigned_engine"])

    # Identify the primary engine — prefer low-cost (serverless) engines,
    # then break ties by query count. A $0 DynamoDB with 24 queries is a
    # better primary than a $200 DocumentDB with 28 queries.
    primary_engine_name = ""
    if engine_queries:
        primary_engine_name = min(
            engine_queries,
            key=lambda e: (ENGINE_BASE_COST.get(e, 100), -len(engine_queries[e])),
        )

    # -------------------------------------------------------------------
    # Pass 0: Unique Value Assessment (iterative elimination)
    #
    # Ask: "If we REMOVED this engine, could all its non-mandatory queries
    # be served by the remaining engines?" If yes, the engine is redundant.
    #
    # We evaluate engines from most expensive to cheapest, removing one at
    # a time. This ensures we keep the cheapest viable set.
    # -------------------------------------------------------------------

    # Sort engines by operational cost (most expensive first).
    # Mandatory engines with signal overrides are evaluated last (protected).
    all_engines_sorted = sorted(
        [e for e in engine_queries if engine_queries[e]],
        key=lambda e: (
            0 if e in mandatory_committed_engines else 1,  # mandatory = protected
            0 if e == primary_engine_name else 1,  # primary = protected
            ENGINE_BASE_COST.get(e, 100),  # most expensive first (reversed below)
        ),
        reverse=True,  # evaluate most expensive non-mandatory first
    )

    unique_value_assessment: dict[str, dict] = {}
    engines_to_consolidate: list[str] = []
    surviving_engines = set(all_engines_sorted)  # engines still in the mix

    for engine in all_engines_sorted:
        qas = engine_queries[engine]

        # For each query, compute this engine's fit score vs best alternative.
        # A query is "unique" if this engine's score exceeds the best alt by UNIQUE_DELTA_THRESHOLD.
        other_engines = surviving_engines - {engine}
        unique_queries = []
        redundant_queries = []
        query_deltas = []  # (query_id, this_score, best_alt_score, delta)

        for qa in qas:
            if qa["query_id"] in mandatory_query_ids:
                unique_queries.append(qa["query_id"])
                continue

            this_score = _engine_fit_score(engine, qa, query_signals, query_map, analysis_outputs)
            best_alt_score = 0
            for alt_engine in other_engines:
                alt_score = _engine_fit_score(
                    alt_engine, qa, query_signals, query_map, analysis_outputs
                )
                best_alt_score = max(best_alt_score, alt_score)

            delta = this_score - best_alt_score
            query_deltas.append((qa["query_id"], this_score, best_alt_score, delta))

            if delta >= UNIQUE_DELTA_THRESHOLD:
                unique_queries.append(qa["query_id"])
            else:
                redundant_queries.append(qa["query_id"])

        # Average delta across all non-mandatory queries (diagnostic metric)
        non_mandatory_deltas = [d for _, _, _, d in query_deltas]
        avg_delta = (
            sum(non_mandatory_deltas) / len(non_mandatory_deltas) if non_mandatory_deltas else 0
        )

        unique_value_assessment[engine] = {
            "total_queries": len(qas),
            "unique_queries": unique_queries,
            "redundant_queries": redundant_queries,
            "unique_ratio": len(unique_queries) / len(qas) if qas else 0,
            "avg_delta": round(avg_delta, 1),
            "is_primary": engine == primary_engine_name,
            "is_mandatory": engine in mandatory_committed_engines,
        }

        # Decision: consolidate this engine?
        # Protected engines (primary, mandatory with unique queries) never consolidate
        if engine == primary_engine_name:
            continue

        if engine in mandatory_committed_engines and unique_queries:
            continue

        if not unique_queries:
            # All queries can be served equally well elsewhere — remove
            engines_to_consolidate.append(engine)
            surviving_engines.discard(engine)
            continue

        # Has some unique queries. But if we're over TARGET_ENGINE_COUNT,
        # check if the unique count justifies the operational burden.
        if len(surviving_engines) > TARGET_ENGINE_COUNT:
            base_cost = ENGINE_BASE_COST.get(engine, 100)
            total_burden = base_cost + EXTRA_ENGINE_BURDEN_MONTHLY
            burden_per_unique = total_burden / max(len(unique_queries), 1)
            if burden_per_unique > 100:
                engines_to_consolidate.append(engine)
                surviving_engines.discard(engine)

    # -------------------------------------------------------------------
    # Pass 1: Aurora Absorption
    #
    # If Aurora is already committed, absorb orphan queries from engines
    # with < AURORA_ABSORPTION_QUERY_THRESHOLD queries, provided Aurora
    # can serve them adequately (fit >= 50, specialist delta < 30).
    # -------------------------------------------------------------------

    aurora_absorption = _run_aurora_absorption_pass(
        engine_queries=engine_queries,
        surviving_engines=surviving_engines,
        mandatory_committed_engines=mandatory_committed_engines,
        query_signals=query_signals,
        query_map=query_map,
        analysis_outputs=analysis_outputs,
        query_capabilities=query_capabilities or {},
    )

    # Apply absorption: mark eliminated engines for consolidation
    for engine in aurora_absorption.engines_eliminated:
        if engine not in engines_to_consolidate:
            engines_to_consolidate.append(engine)
        surviving_engines.discard(engine)

    # Move absorbed queries now. Pass 2 skips them, so each query is
    # placed (and counted in consolidations) exactly once.
    revised = deepcopy(query_assignments)
    absorbed_by_id = {aq["query_id"]: aq for aq in aurora_absorption.absorbed_queries}
    absorption_consolidations = _apply_aurora_absorption(
        revised, aurora_absorption, engine_queries, mandatory_committed_engines
    )

    # -------------------------------------------------------------------
    # Pass 2: Consolidation — distribute queries from redundant engines
    # -------------------------------------------------------------------

    # Committed engines = all engines NOT being consolidated
    committed_engines = {
        e for e in engine_queries if e not in engines_to_consolidate and engine_queries[e]
    }

    consolidations = []

    lightweight_recommendations = []

    for engine in list(engines_to_consolidate):
        qas = engine_queries[engine]
        if not qas:
            continue

        # Filter out mandatory queries — they can't be moved
        movable_qas = [
            qa
            for qa in qas
            if qa["query_id"] not in mandatory_query_ids and qa["query_id"] not in absorbed_by_id
        ]
        if not movable_qas:
            continue

        base_cost = ENGINE_BASE_COST.get(engine, 100)

        # Serviceability gate: separate queries into serviceable and unserviceable
        serviceable_qas = []
        unserviceable_qas = []
        for qa in movable_qas:
            qid = qa["query_id"]
            required_caps = query_capabilities.get(qid, [])
            # Check if ANY committed engine can serve this query's capabilities
            any_can_serve = any(
                can_engine_serve_capability(e, required_caps)
                for e in committed_engines
                if e != engine
            )
            if any_can_serve or not required_caps:
                serviceable_qas.append(qa)
            else:
                unserviceable_qas.append(qa)

        # If all queries are unserviceable, engine must stay
        if not serviceable_qas and unserviceable_qas:
            engines_to_consolidate.remove(engine)
            committed_engines.add(engine)
            assessment = unique_value_assessment.get(engine, {})
            assessment["consolidation_blocked"] = (
                f"{len(unserviceable_qas)} queries require capabilities no other engine provides"
            )
            continue

        # Per-query placement into committed engines (only serviceable queries)
        placement: dict[str, list[tuple[dict, str]]] = {}
        all_placed = True
        for qa in serviceable_qas:
            absorber = _find_best_absorber_for_query(
                qa,
                committed_engines,
                engine,
                query_signals,
                query_map,
                analysis_outputs,
                engine_queries,
                mandatory_committed_engines,
                primary_engine_name,
                query_capabilities,
            )
            if absorber:
                target = absorber["target_engine"]
                if target not in placement:
                    placement[target] = []
                placement[target].append((qa, absorber["reason"]))
            else:
                all_placed = False
                break

        if all_placed and placement:
            # Apply all placements
            for target_engine, placed_qas in placement.items():
                placed_ids = {qa["query_id"] for qa, _ in placed_qas}
                for qa in revised:
                    if qa["assigned_engine"] == engine and qa["query_id"] in placed_ids:
                        reason_text = next(
                            r for q, r in placed_qas if q["query_id"] == qa["query_id"]
                        )
                        qa["assigned_engine"] = target_engine
                        qa["assignment_reason"] = (
                            f"reality check: consolidated from {engine} → {target_engine} "
                            f"(no unique value — {reason_text})"
                        )

            total_moved = sum(len(v) for v in placement.values())
            is_partial = len(unserviceable_qas) > 0

            # Record consolidations per target
            for target_engine, placed_qas in placement.items():
                consolidation_entry = {
                    "from_engine": engine,
                    "to_engine": target_engine,
                    "query_count": len(placed_qas),
                    "reason": (
                        f"{engine} provides no unique capabilities — "
                        f"{total_moved} queries can be served by existing engines"
                        + (
                            f" ({len(unserviceable_qas)} retained due to capability requirements)"
                            if is_partial
                            else ""
                        )
                    ),
                    "saved_cost_estimate": 0,
                    "action": "partial" if is_partial else "full",
                    "queries_retained": (
                        [qa["query_id"] for qa in unserviceable_qas] if is_partial else []
                    ),
                    "retention_reason": (
                        "Queries require capabilities no committed engine provides"
                        if is_partial
                        else None
                    ),
                }
                consolidations.append(consolidation_entry)

            # Set saved cost on first consolidation entry for this engine
            if not is_partial and total_moved == len(movable_qas):
                total_saved = base_cost + EXTRA_ENGINE_BURDEN_MONTHLY
                for c in consolidations:
                    if c["from_engine"] == engine:
                        c["saved_cost_estimate"] = total_saved
                        total_saved = 0

            # Partial consolidation: engine stays but with fewer queries
            if is_partial:
                engines_to_consolidate.remove(engine)
                committed_engines.add(engine)

                # Suggest lightweight alternative if orphan set is small
                lw_rec = _suggest_lightweight_for_orphans(
                    unserviceable_qas, len(movable_qas), query_capabilities, engine
                )
                if lw_rec:
                    lightweight_recommendations.append(lw_rec)
        else:
            # Could not place all queries — engine stays but flag it
            engines_to_consolidate.remove(engine)
            committed_engines.add(engine)
            assessment = unique_value_assessment.get(engine, {})
            assessment["consolidation_blocked"] = (
                "Some queries could not be placed in any committed engine"
            )

    # Pass 3: Detect architectural patterns
    patterns = _detect_architectural_patterns(revised, committed_engines, engine_queries)

    # Pass 4: Build recommendations
    recommendations = _build_recommendations(
        revised,
        consolidations,
        patterns,
        engine_queries,
    )

    consolidations = absorption_consolidations + consolidations

    return {
        "revised_assignments": revised,
        "consolidations": consolidations,
        "architectural_patterns": patterns,
        "recommendations": recommendations,
        "unique_value_assessment": unique_value_assessment,
        "lightweight_recommendations": lightweight_recommendations,
        "aurora_absorption": {
            "aurora_engine": aurora_absorption.aurora_engine,
            "engines_eliminated": aurora_absorption.engines_eliminated,
            "engines_reduced": aurora_absorption.engines_reduced,
            "absorbed_count": len(aurora_absorption.absorbed_queries),
        },
    }


def rerun_aurora_absorption(
    revised_assignments: list[dict],
    triage: dict,
    analysis_outputs: dict[str, dict],
    collector_output: dict,
) -> tuple[list[dict], list[dict]]:
    """Run Pass 1 again on an already-revised assignment (#166).

    The LLM validator can restore an Aurora engine that Pass 0 removed, after
    Pass 1 found no Aurora to absorb into. Call this once after corrections so
    small engines get the same chance they would have had.

    Returns (updated_assignments, new_consolidations). Assignments are copied.
    """
    revised = deepcopy(revised_assignments)
    engine_queries: dict[str, list[dict]] = defaultdict(list)
    for qa in revised:
        engine_queries[qa["assigned_engine"]].append(qa)
    mandatory_committed_engines = {
        qa["assigned_engine"] for qa in revised if _is_signal_override(qa)
    }
    queries = collector_output.get("queries", {}).get("query_patterns", [])

    absorption = _run_aurora_absorption_pass(
        engine_queries=engine_queries,
        surviving_engines={e for e, qas in engine_queries.items() if qas},
        mandatory_committed_engines=mandatory_committed_engines,
        query_signals=_build_query_signals(triage.get("signals", [])),
        query_map={q["query_id"]: q for q in queries},
        analysis_outputs=analysis_outputs,
        query_capabilities=triage.get("query_capabilities", {}),
    )
    consolidations = _apply_aurora_absorption(
        revised, absorption, engine_queries, mandatory_committed_engines
    )
    return revised, consolidations


def _build_query_signals(signals: list[dict]) -> dict[str, list[str]]:
    """Map query_id → triage signal names."""
    query_signals: dict[str, list[str]] = defaultdict(list)
    for signal in signals:
        for qid in signal.get("query_ids", []):
            query_signals[qid].append(signal.get("signal", ""))
    return query_signals


def _apply_aurora_absorption(
    revised: list[dict],
    absorption: AuroraAbsorptionResult,
    engine_queries: dict[str, list[dict]],
    mandatory_committed_engines: set[str],
) -> list[dict]:
    """Move absorbed queries in ``revised`` (in place) and return their consolidations (#168)."""
    absorbed_by_id = {aq["query_id"]: aq for aq in absorption.absorbed_queries}
    for qa in revised:
        aq = absorbed_by_id.get(qa["query_id"])
        if aq:
            qa["assigned_engine"] = aq["to_engine"]
            qa["assignment_reason"] = (
                f"reality check: absorbed from {aq['from_engine']} → {aq['to_engine']} "
                f"({aq['reason']})"
            )

    by_source: dict[str, list[dict]] = {}
    for aq in absorption.absorbed_queries:
        by_source.setdefault(aq["from_engine"], []).append(aq)

    consolidations = []
    for from_engine, queries in by_source.items():
        is_full = from_engine in absorption.engines_eliminated
        base_cost = ENGINE_BASE_COST.get(from_engine, 100)
        consolidations.append(
            {
                "from_engine": from_engine,
                "to_engine": absorption.aurora_engine,
                "query_count": len(queries),
                "reason": (
                    f"Aurora absorption: {queries[0]['reason']}"
                    if from_engine in mandatory_committed_engines
                    else f"Aurora absorption: {from_engine} had "
                    f"< {AURORA_ABSORPTION_QUERY_THRESHOLD} queries and Aurora "
                    f"scores adequately on {'all' if is_full else 'absorbable subset'}"
                ),
                "saved_cost_estimate": (base_cost + EXTRA_ENGINE_BURDEN_MONTHLY) if is_full else 0,
                "action": "full" if is_full else "partial",
                "queries_retained": [
                    qa["query_id"]
                    for qa in engine_queries[from_engine]
                    if qa["query_id"] not in absorbed_by_id
                ],
                "retention_reason": (
                    None if is_full else "Specialist engine scores well ahead of Aurora"
                ),
            }
        )
    return consolidations


def _is_signal_override(qa: dict) -> bool:
    """True when a triage signal forced this query to its engine (#151).

    Assignments written before ``signal_override`` existed carry the signal
    only in ``assignment_reason``; fall back to that for legacy artifacts.
    """
    if "signal_override" in qa:
        return bool(qa["signal_override"])
    return str(qa.get("assignment_reason", "")).startswith("signal override")


def _run_aurora_absorption_pass(
    engine_queries: dict[str, list[dict]],
    surviving_engines: set[str],
    mandatory_committed_engines: set[str],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    analysis_outputs: dict[str, dict],
    query_capabilities: dict[str, list[str]],
) -> AuroraAbsorptionResult:
    """Pass 1: Absorb orphan queries from low-count engines into committed Aurora.

    Only runs when an Aurora engine is already committed. Engines with fewer than
    AURORA_ABSORPTION_QUERY_THRESHOLD queries are candidates. Each query must pass
    a dual-condition gate:
      - Aurora fit score >= AURORA_ABSORPTION_MIN_FIT
      - Specialist delta < SPECIALIST_DELTA_THRESHOLD

    If the specialist engine scores significantly higher than Aurora on a query,
    that query is protected (the specialist provides genuine operational value).
    """
    empty_result = AuroraAbsorptionResult(
        absorbed_queries=[], engines_eliminated=[], engines_reduced=[], aurora_engine=""
    )

    # Find committed Aurora engine
    aurora_in_stack = AURORA_ENGINES & surviving_engines
    if not aurora_in_stack:
        return empty_result

    # Pick the Aurora with more queries; prefer PG if tied
    if len(aurora_in_stack) == 1:
        aurora_engine = next(iter(aurora_in_stack))
    else:
        aurora_engine = max(
            aurora_in_stack,
            key=lambda e: (len(engine_queries.get(e, [])), e == "aurora_postgresql"),
        )

    absorbed_queries: list[dict] = []
    engines_eliminated: list[str] = []
    engines_reduced: list[str] = []

    # Identify candidates: non-Aurora, < threshold queries. Mandatory engines
    # qualify only when tiny, and are handled all-or-nothing below.
    candidates = [
        e
        for e in surviving_engines
        if e not in AURORA_ENGINES
        and len(engine_queries.get(e, [])) > 0
        and (
            len(engine_queries[e]) <= TINY_MANDATORY_QUERY_THRESHOLD
            if e in mandatory_committed_engines
            else len(engine_queries[e]) < AURORA_ABSORPTION_QUERY_THRESHOLD
        )
    ]

    for candidate_engine in candidates:
        qas = engine_queries[candidate_engine]

        if candidate_engine in mandatory_committed_engines:
            mandatory_absorbed = _absorb_tiny_mandatory_engine(
                candidate_engine,
                aurora_engine,
                qas,
                query_signals,
                query_map,
                analysis_outputs,
                query_capabilities,
            )
            if mandatory_absorbed:
                absorbed_queries.extend(mandatory_absorbed)
                engines_eliminated.append(candidate_engine)
            continue

        absorbable = []
        protected = []

        for qa in qas:
            aurora_fit = _engine_fit_score(
                aurora_engine, qa, query_signals, query_map, analysis_outputs
            )
            specialist_fit = _engine_fit_score(
                candidate_engine, qa, query_signals, query_map, analysis_outputs
            )
            delta = specialist_fit - aurora_fit

            if aurora_fit >= AURORA_ABSORPTION_MIN_FIT and delta < SPECIALIST_DELTA_THRESHOLD:
                absorbable.append(
                    {
                        "query_id": qa["query_id"],
                        "from_engine": candidate_engine,
                        "to_engine": aurora_engine,
                        "fit_score": aurora_fit,
                        "specialist_score": specialist_fit,
                        "delta": delta,
                        "reason": (
                            f"Aurora fit={aurora_fit}, specialist delta={delta} "
                            f"(below threshold={SPECIALIST_DELTA_THRESHOLD})"
                        ),
                    }
                )
            else:
                protected.append(qa)

        # Decision logic
        if len(absorbable) == len(qas):
            # All queries absorbable: fully eliminate engine
            absorbed_queries.extend(absorbable)
            engines_eliminated.append(candidate_engine)
        elif absorbable and len(protected) <= len(qas) // 2:
            # Some absorbable, minority protected: partial absorption
            absorbed_queries.extend(absorbable)
            engines_reduced.append(candidate_engine)
        # else: most queries protected, skip this engine

    return AuroraAbsorptionResult(
        absorbed_queries=absorbed_queries,
        engines_eliminated=engines_eliminated,
        engines_reduced=engines_reduced,
        aurora_engine=aurora_engine,
    )


def _absorb_tiny_mandatory_engine(
    candidate_engine: str,
    aurora_engine: str,
    qas: list[dict],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    analysis_outputs: dict[str, dict],
    query_capabilities: dict[str, list[str]],
) -> list[dict]:
    """Absorb every query of a tiny mandatory engine into Aurora, or none (#165).

    Each query must pass three checks:
      - Aurora meets the query's hard capability requirements
      - every capability signal is covered by Aurora directly or in basic form
      - Aurora fit, scoring basic-covered signals as neutral, is at least
        AURORA_ABSORPTION_MIN_FIT

    The specialist delta check is skipped on purpose: the specialist always
    wins it through the signal bonus, which is what made the engine mandatory.
    Partial absorption is refused because the engine, and its cost, would stay.
    """
    aurora_caps = ENGINE_CAPABILITIES.get(aurora_engine, set())
    fits: dict[str, int] = {}
    coverage: set[str] = set()

    for qa in qas:
        qid = qa["query_id"]
        if not can_engine_serve_capability(aurora_engine, query_capabilities.get(qid, [])):
            return []

        basic_covered: list[str] = []
        for sig in query_signals.get(qid, []):
            needed_cap = SIGNAL_TO_CAPABILITY.get(sig)
            if not needed_cap or needed_cap in aurora_caps:
                continue
            basic_cap = SIGNAL_TO_BASIC_CAPABILITY.get(sig)
            if basic_cap not in aurora_caps:
                return []
            basic_covered.append(sig)
            coverage.add(basic_cap)

        neutral_signals = {
            qid: [sig for sig in query_signals.get(qid, []) if sig not in basic_covered]
        }
        fits[qid] = _engine_fit_score(
            aurora_engine, qa, neutral_signals, query_map, analysis_outputs
        )
        if fits[qid] < AURORA_ABSORPTION_MIN_FIT:
            return []

    reason = (
        f"{candidate_engine} carries only {len(qas)} queries and {aurora_engine} "
        f"serves them (fit {min(fits.values())}-{max(fits.values())}"
        + (f", via {', '.join(sorted(coverage))}" if coverage else "")
        + f"), so a dedicated {candidate_engine} deployment is not justified"
    )
    return [
        {
            "query_id": qa["query_id"],
            "from_engine": candidate_engine,
            "to_engine": aurora_engine,
            "fit_score": fits[qa["query_id"]],
            "specialist_score": None,
            "delta": None,
            "reason": reason,
        }
        for qa in qas
    ]


def _engine_fit_score(
    engine: str,
    qa: dict,
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    analysis_outputs: dict[str, dict],
) -> int:
    """Score how well an engine fits a query (0-100).

    Combines:
    - Table-level confidence from analysis (weighted average across accessed tables)
    - Signal-capability bonus (engine has specific capability the query needs)
    - Basic CRUD baseline (any engine with key-value/range can do simple ops)

    Higher = better fit. Used to compute deltas between engines.
    """
    qid = qa["query_id"]
    sigs = query_signals.get(qid, [])
    engine_caps = ENGINE_CAPABILITIES.get(engine, set())
    query_tables = query_map.get(qid, {}).get("tables_accessed", [])

    # Start with table-level confidence from analysis
    target_analysis = analysis_outputs.get(engine, {})
    table_recs = {r["table_id"]: r for r in target_analysis.get("table_recommendations", [])}
    table_scores = [
        table_recs[t].get("confidence_score", 0) for t in query_tables if t in table_recs
    ]

    if table_scores:
        base_score = sum(table_scores) / len(table_scores)
    elif {"key_value_lookup", "range_query"} & engine_caps:
        base_score = BASIC_CRUD_SCORE
    else:
        base_score = 0

    # Bonus for signal-capability match
    has_capability_signal = False
    signal_matched = False
    for sig in sigs:
        needed_cap = SIGNAL_TO_CAPABILITY.get(sig)
        if needed_cap:
            has_capability_signal = True
            if needed_cap in engine_caps:
                signal_matched = True
                break

    if has_capability_signal and signal_matched:
        base_score = min(100, base_score + SIGNAL_MATCH_BONUS)
    elif has_capability_signal and not signal_matched:
        # Engine lacks a capability the query specifically needs — penalize
        base_score = max(0, base_score - SIGNAL_MATCH_BONUS)

    return int(base_score)


def _can_engine_serve_query(
    engine: str,
    qa: dict,
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    analysis_outputs: dict[str, dict],
) -> bool:
    """Check if an engine has the capability to serve a given query.

    Uses signals first (fast path), then falls back to analysis confidence.
    """
    qid = qa["query_id"]
    sigs = query_signals.get(qid, [])
    engine_caps = ENGINE_CAPABILITIES.get(engine, set())

    # Check via signal → capability mapping
    has_capability_signal = False
    for sig in sigs:
        needed_cap = SIGNAL_TO_CAPABILITY.get(sig)
        if needed_cap:
            has_capability_signal = True
            if needed_cap in engine_caps:
                return True

    # If no signals require specific capabilities (either no signals at all,
    # or signals like "low_frequency_reads" that are workload characteristics
    # rather than capability requirements), fall through to broader checks.
    if not has_capability_signal:
        # Check analysis confidence for the query's tables
        query_tables = query_map.get(qid, {}).get("tables_accessed", [])
        target_analysis = analysis_outputs.get(engine, {})
        table_recs = {r["table_id"]: r for r in target_analysis.get("table_recommendations", [])}
        scores = [table_recs[t].get("confidence_score", 0) for t in query_tables if t in table_recs]
        if scores and sum(scores) / len(scores) >= 50:
            return True

        # Basic CRUD patterns can be served by any engine with key-value or range
        if {"key_value_lookup", "range_query"} & engine_caps:
            return True

    return False


def _find_best_absorber_for_query(
    qa: dict,
    committed_engines: set[str],
    source_engine: str,
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    analysis_outputs: dict[str, dict],
    engine_queries: dict[str, list[dict]],
    mandatory_committed_engines: set[str],
    primary_engine: str,
    query_capabilities: dict[str, list[str]] | None = None,
) -> dict | None:
    """Find the best committed engine to absorb a single query.

    Ranks by: fit score (highest wins), with mandatory secondary engines
    getting a tiebreaker bonus (data already flowing via sync).

    Respects the serviceability gate: if query_capabilities are provided,
    only considers engines that can serve the query's hard requirements.
    """
    qid = qa["query_id"]
    query_tables = query_map.get(qid, {}).get("tables_accessed", [])
    required_caps = (query_capabilities or {}).get(qid, [])
    candidates = []

    for target_engine in committed_engines:
        if target_engine == source_engine:
            continue

        # Serviceability gate: skip engines that can't serve hard requirements
        if required_caps and not can_engine_serve_capability(target_engine, required_caps):
            continue

        fit = _engine_fit_score(target_engine, qa, query_signals, query_map, analysis_outputs)

        # Even low-fit engines can absorb if they have basic capabilities
        if fit <= 0 and not _can_engine_serve_query(
            target_engine, qa, query_signals, query_map, analysis_outputs
        ):
            continue

        # Check table overlap with target
        target_tables = set()
        for tqa in engine_queries.get(target_engine, []):
            target_tables.update(query_map.get(tqa["query_id"], {}).get("tables_accessed", []))
        overlap = set(query_tables) & target_tables

        is_mandatory_secondary = (
            target_engine in mandatory_committed_engines and target_engine != primary_engine
        )

        reason = (
            f"fit={fit}, data synced for {', '.join(sorted(overlap))}"
            if overlap
            else f"fit={fit}, {target_engine} can serve this pattern"
        )

        candidates.append(
            {
                "target_engine": target_engine,
                "fit_score": fit,
                "is_mandatory_secondary": is_mandatory_secondary,
                "table_overlap": len(overlap),
                "reason": reason,
            }
        )

    if not candidates:
        return None

    # Sort: highest fit score wins, with mandatory secondary as tiebreaker
    candidates.sort(
        key=lambda c: (c["fit_score"], c["is_mandatory_secondary"], c["table_overlap"]),
        reverse=True,
    )
    return candidates[0]


def _suggest_lightweight_for_orphans(
    unserviceable_qas: list[dict],
    total_movable: int,
    query_capabilities: dict[str, list[str]],
    source_engine: str,
) -> dict | None:
    """Suggest a lightweight alternative if orphan set is small and uniform.

    Returns a dict suitable for LightweightRecommendation if:
    - Orphan count is <= 10% of total movable queries
    - All orphans share a single common capability requirement
    Otherwise returns None (keep the full engine).
    """
    if not unserviceable_qas or total_movable == 0:
        return None

    orphan_ratio = len(unserviceable_qas) / total_movable
    if orphan_ratio > 0.10:
        return None

    # Check if all orphans share a single capability
    all_caps: set[str] = set()
    for qa in unserviceable_qas:
        caps = query_capabilities.get(qa["query_id"], [])
        all_caps.update(caps)

    if len(all_caps) != 1:
        return None

    shared_cap = next(iter(all_caps))
    alt = suggest_lightweight_alternative(shared_cap)
    if not alt:
        return None

    return {
        "capability": shared_cap,
        "service": alt["service"],
        "query_ids": [qa["query_id"] for qa in unserviceable_qas],
        "pattern": alt["pattern"],
        "cost_profile": alt["cost_profile"],
        "replaces_engine": source_engine,
        "limitations": alt["limitations"],
    }


def _detect_architectural_patterns(
    assignments: list[dict],
    committed_engines: set[str],
    engine_queries: dict[str, list[dict]],
) -> list[dict]:
    """Detect which architectural patterns apply to this multi-engine setup."""
    patterns: list[dict] = []

    # Get engines that actually have queries after consolidation
    active_engines: dict[str, int] = defaultdict(int)
    for qa in assignments:
        active_engines[qa["assigned_engine"]] += 1

    if len(active_engines) < 2:
        return patterns

    # Find the primary engine (most queries)
    primary = max(active_engines, key=lambda e: active_engines[e])
    secondaries = [e for e in active_engines if e != primary and active_engines[e] > 0]

    # Check for CQRS pattern
    # Primary handles writes, secondary handles reads/search
    primary_has_writes = any(qa["assigned_engine"] == primary for qa in assignments)

    read_secondaries = [e for e in secondaries if e in ("opensearch", "elasticache", "dynamodb")]
    if primary_has_writes and read_secondaries:
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["CQRS"])
        pattern["applies_to"] = {
            "write_engine": primary,
            "read_engines": read_secondaries,
        }
        patterns.append(pattern)

    # Check for Materialized View pattern
    if "opensearch" in secondaries:
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["MATERIALIZED_VIEW"])
        pattern["applies_to"] = {
            "source_engine": primary,
            "view_engine": "opensearch",
        }
        patterns.append(pattern)

    # If 3+ engines, suggest Polyglot Persistence
    if len(active_engines) >= 3:
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["POLYGLOT_PERSISTENCE"])
        pattern["applies_to"] = {
            "engines": list(active_engines.keys()),
        }
        patterns.append(pattern)
    elif len(active_engines) == 2 and primary != "dynamodb":
        # Event sourcing when using non-serverless primary
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["EVENT_SOURCING"])
        pattern["applies_to"] = {
            "source_engine": primary,
            "target_engines": secondaries,
        }
        patterns.append(pattern)

    return patterns


def patterns_for_assignment(query_assignments: list[dict]) -> list[dict]:
    """Architectural patterns for the engines that carry in-scope queries."""
    in_scope = [
        qa for qa in query_assignments if qa.get("in_scope", True) and qa.get("assigned_engine")
    ]
    return _detect_architectural_patterns(in_scope, set(), {})


def refresh_patterns_and_recommendations(result: dict) -> None:
    """Recompute ``architectural_patterns`` and ``recommendations`` from the final assignment.

    Patterns are first detected before the LLM corrections, the re-run Aurora absorption,
    the sanity sweep and the customer-override restore, any of which can still move
    queries; the output must describe the assignment it ships with, so no pattern names
    an engine that ends up with no in-scope query (#202). Only in-scope queries count.
    Mutates ``result``.
    """
    patterns = patterns_for_assignment(result["revised_assignments"])
    result["architectural_patterns"] = patterns
    result["recommendations"] = _build_recommendations(
        result["revised_assignments"], result["consolidations"], patterns, {}
    )


def _queries(n: int, engine: str = "") -> str:
    """``"1 query"`` / ``"3 queries"`` (``"3 documentdb queries"`` with ``engine``).

    Local on purpose: the report layer's ``plural_noun`` sits above the agents, and
    importing it here would invert the layering.
    """
    noun = "query" if n == 1 else "queries"
    return f"{n} {engine} {noun}" if engine else f"{n} {noun}"


def reconcile_consolidations(
    before_assignments: list[dict],
    after_assignments: list[dict],
    consolidations: list[dict],
) -> list[dict]:
    """Make the consolidation records describe the net per-query moves (#218).

    Pass 2, the LLM corrections, the re-run Aurora absorption, the sanity sweep and
    the customer-override restore each move queries and edit records on their own,
    so the records drifted from the assignment (unrecorded moves, overstated counts,
    savings for an engine that stays). This derives the moves from the before/after
    assignment and fits the records to them:

    - one record per (from, to) pair with a net move; its ``query_count`` is that move,
      so applying the records to the before distribution gives the after distribution;
    - a move with no record gets one; a record with no net move is dropped;
    - ``action`` is ``full`` only when the source engine had an in-scope query in the
      input and ends with none, and only then are savings claimed, once per engine;
    - a record whose count, action or retained queries changed gets a reason that
      states the final move.

    Record order is kept; new records follow. Inputs are not mutated.
    """
    before = {qa["query_id"]: qa["assigned_engine"] for qa in before_assignments}
    after_engine = {qa["query_id"]: qa["assigned_engine"] for qa in after_assignments}
    had_in_scope = {qa["assigned_engine"] for qa in before_assignments if qa.get("in_scope", True)}
    remaining: dict[str, int] = defaultdict(int)
    for qa in after_assignments:
        if qa.get("in_scope", True):
            remaining[qa["assigned_engine"]] += 1

    moves: dict[tuple[str, str], int] = {}
    for qa in after_assignments:
        src = before.get(qa["query_id"])
        dst = qa["assigned_engine"]
        if src and src != dst:
            moves[(src, dst)] = moves.get((src, dst), 0) + 1

    recorded_saving: dict[str, float] = {}
    first_record: dict[tuple[str, str], dict] = {}
    for c in consolidations:
        src = c["from_engine"]
        recorded_saving[src] = max(recorded_saving.get(src, 0), c.get("saved_cost_estimate") or 0)
        first_record.setdefault((src, c["to_engine"]), c)
    ordered = [p for p in first_record if p in moves] + [p for p in moves if p not in first_record]

    reconciled: list[dict] = []
    savings_claimed: set[str] = set()
    for src, dst in ordered:
        count = moves[(src, dst)]
        full = src in had_in_scope and remaining.get(src, 0) == 0
        action = "full" if full else "partial"
        record = dict(
            first_record.get((src, dst))
            or {"from_engine": src, "to_engine": dst, "queries_retained": []}
        )
        prior_retained = list(record.get("queries_retained") or [])
        retained = [] if full else [q for q in prior_retained if after_engine.get(q) == src]
        if (
            record.get("query_count") != count
            or record.get("action", "full") != action
            or retained != prior_retained
            or not record.get("reason")
        ):
            moved = f"{_queries(count, src)} moved to {dst}"
            if full:
                record["reason"] = f"{moved}; no in-scope {src} query remains"
            elif src not in had_in_scope:
                record["reason"] = f"Partial consolidation: {moved}; {src} had no in-scope query"
            else:
                record["reason"] = (
                    f"Partial consolidation: {moved}; {src} stays for {_queries(remaining[src])}"
                )
        saved = 0.0
        if full and src not in savings_claimed:
            savings_claimed.add(src)
            saved = recorded_saving.get(src) or (
                ENGINE_BASE_COST.get(src, 100) + EXTRA_ENGINE_BURDEN_MONTHLY
            )
        record.update(
            {
                "query_count": count,
                "action": action,
                "saved_cost_estimate": saved,
                "queries_retained": retained,
                "retention_reason": record.get("retention_reason") if retained else None,
            }
        )
        reconciled.append(record)
    return reconciled


def format_pattern_recommendation(p: dict) -> str:
    """Render one architectural pattern as a ``Recommended pattern: ...`` line.

    Shared with synthesis, which regenerates these lines from the effective assignment.
    """
    applies = p.get("applies_to", {})
    if "write_engine" in applies:
        return (
            f"Recommended pattern: {p['name']}. "
            f"Use {applies['write_engine']} for all write operations and "
            f"{', '.join(applies.get('read_engines', []))} for specialized reads. "
            f"{p['description']}"
        )
    if "source_engine" in applies and "view_engine" in applies:
        return (
            f"Recommended pattern: {p['name']}. "
            f"{applies['source_engine']} is the source of truth; "
            f"{applies['view_engine']} maintains a search-optimized projection. "
            f"{p['description']}"
        )
    return f"Recommended pattern: {p['name']}. {p['description']}"


def _build_recommendations(
    assignments: list[dict],
    consolidations: list[dict],
    patterns: list[dict],
    original_engine_queries: dict[str, list[dict]],
) -> list[str]:
    """Build human-readable recommendations."""
    recs = []

    # Consolidation recommendations
    for c in consolidations:
        saved = c.get("saved_cost_estimate", 0)
        source = c["from_engine"]
        if c.get("action") == "partial":
            # The source engine stays, so no cluster is avoided (#218).
            outcome = (
                f"No operational savings are claimed: this move does not remove {source} "
                "from the architecture."
            )
        elif saved:
            outcome = (
                f"Saves ~${saved}/mo in operational overhead by avoiding a dedicated "
                f"{source} cluster."
            )
        else:
            outcome = f"{source} leaves the architecture."
        recs.append(
            f"Consolidated {_queries(c['query_count'])} from {source} → "
            f"{c['to_engine']}: {c['reason']}. {outcome}"
        )

    # Pattern recommendations
    recs.extend(format_pattern_recommendation(p) for p in patterns)

    if not recs:
        recs.append("No consolidation opportunities found — current assignment is optimal.")

    return recs
