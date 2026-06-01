"""
Assignment Resolver — Auto-generates query→engine mappings from analysis results.

Implements the assignment resolution algorithm:
  1. Build signal-based overrides from triage (query-level pattern→engine mapping)
  2. Build anti-pattern penalties from analysis (query-level demotions)
  3. Build co-dependency groups from significant JOIN relationships
  4. Score each query against each engine (0–100), adjusted by signals + anti-patterns
  5. Assign co-dependent groups atomically (all queries → best engine for group)
  6. Assign remaining queries individually (highest adjusted score wins)
  7. Fallback to aurora for queries with no scores
  8. Derive table assignments

The key insight: assignment happens QUERY BY QUERY, not table by table.
Triage signals like text_search→opensearch override table-level averages
because the signal is about the query's workload pattern, not the table's
general suitability.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from src.agents.referee.engine_exclusions import check_all_exclusions, check_exclusions
from src.contracts.assignment_models import (
    Assignment,
    AssignmentStatus,
    QueryAssignment,
    TableAssignment,
)

AURORA_ENGINES = {"aurora_postgresql", "aurora_mysql"}

# Triage signals that strongly indicate an engine is the RIGHT fit for a query.
# When a signal maps query→engine and that engine was selected by triage,
# the query should go to that engine regardless of table-level confidence.
SIGNAL_ENGINE_OVERRIDES: dict[str, str] = {
    "text_search": "opensearch",
    "leaderboard_pattern": "elasticache",
    "graph_traversal": "neptune",
    "session_store": "elasticache",
}

# Analysis anti-pattern types that indicate an engine is the WRONG fit for a query.
# When a query appears in one of these anti-patterns for an engine, that engine's
# score is penalized for this specific query.
ANTI_PATTERN_PENALTIES: dict[str, int] = {
    # NoSQL engine anti-patterns (queries wrong for DynamoDB/DocumentDB)
    "text_search": 40,  # DynamoDB can't do full-text search at all
    "wildcard-search": 40,  # Same — LIKE '%term%' is not DynamoDB's job
    "complex-aggregation": 50,  # Heavy penalty — better engines exist, but DynamoDB can pre-compute
    "complex_aggregation": 50,  # Alias (some analysis agents use underscore)
    "multi-index-joins": 25,
    "acid-transactions": 20,
    "frequent-writes": 15,
    # Aurora anti-patterns (queries wrong for Aurora — better on purpose-built engines)
    "high-frequency-pk-lookup": 30,  # DynamoDB does this better at scale
    "simple-cache-read": 25,  # ElastiCache does this at microsecond latency
    "no-relational-need": 20,  # DynamoDB can serve simple KV without relational overhead
    "single-access-pattern-table": 15,  # DynamoDB is cheaper for simple patterns
    "high-volume-text-search": 35,  # OpenSearch purpose-built for this
}


class AssignmentResolver:
    """Generates default query→engine assignments from analysis outputs."""

    def resolve(
        self,
        triage: dict,
        analysis_outputs: dict[str, dict],
        collector_output: dict,
    ) -> Assignment:
        """Produce initial assignment based on query-level scoring.

        For each query:
        1. Check if triage signals mandate a specific engine (override)
        2. Compute per-engine confidence adjusted by anti-pattern penalties
        3. Pick the engine with highest adjusted score

        Respects co-dependent query groups (queries sharing significant
        JOINs on the same tables stay together).
        """
        queries = collector_output.get("queries", {}).get("query_patterns", [])
        tables = collector_output.get("database_schema", {}).get("tables", [])
        # selected_agents can be a list of strings or dicts with agent_type
        raw_selected = triage.get("selected_agents", [])
        selected_engines = {a["agent_type"] if isinstance(a, dict) else a for a in raw_selected}

        # Step 1: Build signal-based overrides from triage
        signal_overrides = self._build_signal_overrides(triage, selected_engines)

        # Step 2: Build per-query anti-pattern penalties from analysis
        anti_pattern_map = self._build_anti_pattern_map(analysis_outputs)

        # Step 3: Build co-dependency groups
        co_dep_groups = build_co_dependency_groups(queries, tables)

        # Step 4: Score each query against each engine (adjusted)
        scores: dict[str, dict[str, int]] = {}
        exclusion_notes: dict[str, list[str]] = {}  # query_id → list of exclusion messages
        for engine, analysis in analysis_outputs.items():
            for query in queries:
                qid = query["query_id"]
                if qid not in scores:
                    scores[qid] = {}

                # Hard exclusion check — if the query cannot run on this engine, score=0
                query_text = query.get("query_text", "")
                exclusion = check_exclusions(qid, query_text, engine)
                if exclusion:
                    scores[qid][engine] = 0
                    exclusion_notes.setdefault(qid, []).append(
                        f"[{exclusion.rule_id}] Excluded from {engine}: {exclusion.description}"
                    )
                    continue

                base_score = self._compute_query_confidence(qid, query, engine, analysis)

                # Apply anti-pattern penalty for this query+engine
                penalty = anti_pattern_map.get((qid, engine), 0)
                adjusted = max(0, base_score - penalty)

                scores[qid][engine] = adjusted

        # Step 5: Assign co-dependent groups atomically
        assigned: dict[str, str] = {}
        assigned_confidence: dict[str, int] = {}
        assigned_reason: dict[str, str] = {}

        # Determine Aurora fallback engine from triage selection
        aurora_fallback = _resolve_aurora_fallback(selected_engines)

        for group in co_dep_groups:
            if not analysis_outputs:
                for qid in group:
                    assigned[qid] = aurora_fallback
                    assigned_confidence[qid] = 0
                    assigned_reason[qid] = "no analysis available"
                continue
            best_engine = max(
                analysis_outputs.keys(),
                key=lambda e: (sum(scores.get(qid, {}).get(e, 0) for qid in group) / len(group)),
            )
            for qid in group:
                assigned[qid] = best_engine
                assigned_confidence[qid] = scores.get(qid, {}).get(best_engine, 0)
                assigned_reason[qid] = f"co-dependency group → {best_engine}"

        # Step 6: Assign remaining queries individually
        for query in queries:
            qid = query["query_id"]
            if qid in assigned:
                continue

            # Check signal override first
            if qid in signal_overrides:
                engine = signal_overrides[qid]["engine"]
                signal = signal_overrides[qid]["signal"]
                assigned[qid] = engine
                assigned_confidence[qid] = scores.get(qid, {}).get(engine, 0)
                assigned_reason[qid] = f"signal override: {signal} → {engine}"
                continue

            # Otherwise pick highest adjusted score
            query_scores = scores.get(qid, {})
            if query_scores:
                best = max(query_scores, key=query_scores.get)
                assigned[qid] = best
                assigned_confidence[qid] = query_scores[best]
                assigned_reason[qid] = f"highest confidence for {best}"
            else:
                # Step 7: Fallback to aurora engine from triage
                assigned[qid] = aurora_fallback
                assigned_confidence[qid] = 0
                assigned_reason[qid] = "no engine scored this query"

        # Build query→tables lookup
        query_tables: dict[str, list[str]] = {}
        for query in queries:
            qid = query["query_id"]
            query_tables[qid] = query.get("tables_accessed", [])

        # Build query assignments
        query_assignments: list[QueryAssignment] = []
        for query in queries:
            qid = query["query_id"]
            engine = assigned[qid]
            confidence = assigned_confidence.get(qid, 0)
            reason = assigned_reason.get(qid, f"highest confidence for {engine}")

            # Append exclusion notes to the reason if this query was excluded from other engines
            if qid in exclusion_notes:
                reason = reason + " | " + "; ".join(exclusion_notes[qid])

            query_assignments.append(
                QueryAssignment(
                    query_id=qid,
                    assigned_engine=engine,
                    confidence=confidence,
                    source_tables=query_tables.get(qid, []),
                    assignment_reason=reason,
                )
            )

        # Step 8: Derive table assignments
        table_assignments = derive_table_assignments(query_assignments)

        # Flatten co-dep groups to list of lists of query_ids
        co_dep_lists = [list(g) for g in co_dep_groups]

        return Assignment(
            job_id=collector_output.get("job_id", "unknown"),
            version=1,
            status=AssignmentStatus.AUTO_GENERATED,
            timestamp=datetime.now(tz=UTC),
            query_assignments=query_assignments,
            table_assignments=table_assignments,
            co_dependency_groups=co_dep_lists,
            validation_warnings=[],
        )

    def _build_signal_overrides(
        self,
        triage: dict,
        selected_engines: set[str],
    ) -> dict[str, dict]:
        """Build query_id → {engine, signal} overrides from triage signals.

        Only creates overrides when:
        - The signal type has a known engine mapping (SIGNAL_ENGINE_OVERRIDES)
        - The target engine was selected by triage (available for assignment)
        - The signal has specific query_ids attached
        """
        overrides: dict[str, dict] = {}
        for signal in triage.get("signals", []):
            signal_name = signal.get("signal", "")
            override_engine = SIGNAL_ENGINE_OVERRIDES.get(signal_name)
            if not override_engine:
                continue
            if override_engine not in selected_engines:
                continue
            for qid in signal.get("query_ids", []):
                overrides[qid] = {
                    "engine": override_engine,
                    "signal": signal_name,
                }
        return overrides

    def _build_anti_pattern_map(
        self,
        analysis_outputs: dict[str, dict],
    ) -> dict[tuple[str, str], int]:
        """Build (query_id, engine) → penalty map from analysis anti-patterns.

        When a query appears in an anti-pattern for an engine, its confidence
        score for that engine gets reduced by the penalty amount. This ensures
        queries that are fundamentally wrong for an engine (e.g., text search
        on DynamoDB) don't get assigned there just because the TABLE average
        is high.
        """
        penalties: dict[tuple[str, str], int] = {}
        for engine, analysis in analysis_outputs.items():
            wa = analysis.get("workload_analysis", {})
            for ap in wa.get("anti_patterns_detected") or []:
                ap_type = ap.get("anti_pattern_type", "")
                penalty = ANTI_PATTERN_PENALTIES.get(ap_type, 0)
                if penalty == 0:
                    continue
                for qid in ap.get("query_ids") or []:
                    key = (qid, engine)
                    # Take the max penalty if a query hits multiple anti-patterns
                    penalties[key] = max(penalties.get(key, 0), penalty)

            # Also check patterns_detected from OTHER engines as positive signals
            # (e.g., if OpenSearch detects wildcard-search pattern for a query,
            # that's a signal the query belongs there)
            for pattern in wa.get("patterns_detected") or []:
                pattern_type = pattern.get("pattern_type", "")
                # If a pattern type matches an anti-pattern penalty name,
                # penalize OTHER engines for those queries
                penalty = ANTI_PATTERN_PENALTIES.get(pattern_type, 0)
                if penalty == 0:
                    continue
                for qid in pattern.get("query_ids", []):
                    for other_engine in analysis_outputs:
                        if other_engine != engine:
                            key = (qid, other_engine)
                            penalties[key] = max(penalties.get(key, 0), penalty)

        return penalties

    def _compute_query_confidence(
        self,
        query_id: str,
        query: dict,
        engine: str,
        analysis: dict,
    ) -> int:
        """Compute base confidence (0–100) for a query→engine pair.

        Uses query-level pattern matching first, then falls back to
        table-level averaging only if the query isn't found in any pattern.
        """
        table_recs = {r["table_id"]: r for r in analysis.get("table_recommendations", [])}
        wa = analysis.get("workload_analysis", {})

        # First: check if this query appears in any detected PATTERN for this engine.
        # If so, use the confidence of the tables involved in that pattern.
        pattern_tables: list[str] = []
        for pattern in wa.get("patterns_detected", []):
            if query_id in (pattern.get("query_ids") or []):
                pattern_tables.extend(pattern.get("table_ids") or [])

        pattern_tables = list(dict.fromkeys(pattern_tables))

        if pattern_tables:
            matched_scores = [
                table_recs[t].get("confidence_score", 0) for t in pattern_tables if t in table_recs
            ]
            if matched_scores:
                return int(sum(matched_scores) / len(matched_scores))

        # Second: use the query's tables_accessed to look up table recommendations
        query_tables = query.get("tables_accessed", [])
        if query_tables:
            matched_scores = [
                table_recs[t].get("confidence_score", 0) for t in query_tables if t in table_recs
            ]
            if matched_scores:
                return int(sum(matched_scores) / len(matched_scores))

        # Last resort: average across all table recommendations
        if table_recs:
            scores = [r.get("confidence_score", 0) for r in table_recs.values()]
            return int(sum(scores) / len(scores))
        return 0


# ---------------------------------------------------------------------------
# Aurora fallback resolution
# ---------------------------------------------------------------------------


def _resolve_aurora_fallback(selected_engines: set[str]) -> str:
    """Determine which Aurora engine to use as fallback.

    Prefers the specific Aurora engine selected by triage. If none was selected,
    falls back to the first Aurora engine found. If no Aurora engine at all,
    uses a generic 'aurora' placeholder (legacy behavior).
    """
    aurora_selected = AURORA_ENGINES & selected_engines
    if aurora_selected:
        return next(iter(aurora_selected))
    # No Aurora engine selected — check if any engine in analysis is Aurora
    return "aurora"


# ---------------------------------------------------------------------------
# Co-dependency detection (Task 4.2)
# ---------------------------------------------------------------------------


def is_significant_join(query: dict, table: str) -> bool:
    """Determine if a query's JOIN on a table is significant (load-bearing).

    A JOIN is significant when:
    - join_count >= 2 (multi-table join)
    - has_aggregation is True (GROUP BY across joined tables)
    - table appears in filter_tables (WHERE clause references the joined table)

    Light JOINs that only fetch a display field do not create co-dependencies.

    Requirements: 2.3
    """
    return (
        query.get("join_count", 0) >= 2
        or query.get("has_aggregation", False)
        or table in query.get("filter_tables", [])
    )


def build_co_dependency_groups(
    queries: list[dict],
    tables: list[dict],
) -> list[list[str]]:
    """Build co-dependency groups using union-find with significance filter.

    Groups queries that share significant JOINs on the same tables.
    Only returns groups with 2+ queries.

    Requirements: 2.3
    """
    # Map: table → set of query_ids with significant JOINs on that table
    table_to_queries: dict[str, set[str]] = {}
    for q in queries:
        if q.get("has_joins") or q.get("join_count", 0) > 0:
            for table in q.get("tables_accessed", []):
                if is_significant_join(q, table):
                    table_to_queries.setdefault(table, set()).add(q["query_id"])

    # Union-find with path compression
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])  # path compression
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Union queries that share significant JOINs on the same table
    for _table, query_ids in table_to_queries.items():
        query_list = list(query_ids)
        for i in range(1, len(query_list)):
            union(query_list[0], query_list[i])

    # Collect groups
    groups: dict[str, list[str]] = {}
    for qid in parent:
        root = find(qid)
        groups.setdefault(root, []).append(qid)

    # Also include query_ids that appear in table_to_queries but not in parent
    # (single-entry sets that were never unioned)
    all_qids_in_tables = set()
    for qids in table_to_queries.values():
        all_qids_in_tables.update(qids)
    for qid in all_qids_in_tables:
        if qid not in parent:
            root = find(qid)
            groups.setdefault(root, []).append(qid)

    # Only return groups with 2+ queries
    return [g for g in groups.values() if len(g) >= 2]


# ---------------------------------------------------------------------------
# Table assignment derivation (Task 4.3)
# ---------------------------------------------------------------------------


def derive_table_assignments(
    query_assignments: list[QueryAssignment],
) -> list[TableAssignment]:
    """Derive table-level assignments from query assignments.

    For each table:
    - Find all engines with assigned queries referencing it
    - Set primary_engine to engine with most assigned queries for that table
    - Set multi_engine_reason when engines list has 2+ entries

    Requirements: 2.6, 11.1, 11.2
    """
    # table_id → engine → count of queries
    table_engine_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # table_id → total query count
    table_query_counts: dict[str, int] = defaultdict(int)

    for qa in query_assignments:
        for table_id in qa.source_tables:
            table_engine_counts[table_id][qa.assigned_engine] += 1
            table_query_counts[table_id] += 1

    result: list[TableAssignment] = []
    for table_id in sorted(table_engine_counts.keys()):
        engine_counts = table_engine_counts[table_id]
        engines = sorted(engine_counts.keys())
        primary_engine = max(engine_counts, key=engine_counts.get)

        multi_engine_reason = None
        if len(engines) >= 2:
            multi_engine_reason = (
                f"Table {table_id} has queries assigned to multiple engines: {', '.join(engines)}"
            )

        result.append(
            TableAssignment(
                table_id=table_id,
                primary_engine=primary_engine,
                engines=engines,
                query_count=table_query_counts[table_id],
                multi_engine_reason=multi_engine_reason,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Customer override enforcement (post-review validation)
# ---------------------------------------------------------------------------


def enforce_exclusions_on_overrides(
    assignment: Assignment,
    queries: list[dict],
    analysis_outputs: dict[str, dict],
) -> Assignment:
    """Validate customer overrides against hard exclusions and auto-reassign violations.

    After a customer reviews and modifies assignments, this function checks
    if any customer-chosen engine is excluded for the query. If so, it:
    1. Reassigns the query to the next-best valid engine
    2. Adds a warning explaining why the override was rejected

    Returns a new Assignment with violations corrected.
    """
    query_text_map = {q["query_id"]: q.get("query_text", "") for q in queries}
    available_engines = set(analysis_outputs.keys())

    updated_assignments = []
    reassignment_count = 0

    for qa in assignment.query_assignments:
        query_text = query_text_map.get(qa.query_id, "")
        exclusion = check_exclusions(qa.query_id, query_text, qa.assigned_engine)

        if not exclusion:
            updated_assignments.append(qa)
            continue

        # This assignment violates a hard exclusion — find the next-best engine
        all_exclusions = check_all_exclusions(qa.query_id, query_text)
        excluded_engines = {e.excluded_engine for e in all_exclusions}
        valid_engines = available_engines - excluded_engines

        if valid_engines:
            # Pick the first valid engine (in practice, the resolver would score these)
            new_engine = sorted(valid_engines)[0]
        else:
            new_engine = "aurora"

        warnings = list(qa.warnings)
        warnings.append(exclusion.customer_message)

        updated_assignments.append(
            qa.model_copy(
                update={
                    "assigned_engine": new_engine,
                    "assignment_reason": (
                        f"Auto-reassigned from {qa.assigned_engine}: {exclusion.description}"
                    ),
                    "customer_override": False,
                    "warnings": warnings,
                }
            )
        )
        reassignment_count += 1

    if reassignment_count == 0:
        return assignment

    # Recompute table assignments
    table_assignments = derive_table_assignments(updated_assignments)

    return assignment.model_copy(
        update={
            "query_assignments": updated_assignments,
            "table_assignments": table_assignments,
        }
    )
