"""
Generic evidence-based scoring for database migration analysis.

Provides TableProfile aggregation and base scoring that can be reused
across target-specific agents (Redis, DynamoDB, DocumentDB, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from src.contracts.analysis_output import MigrationComplexity, ScoreBreakdown, WorkloadAnalysis


@dataclass
class TableProfile:
    """Aggregated per-table stats built from collector_output + workload_analysis."""

    table_id: str
    row_count: int
    size_mb: float
    column_count: int
    has_primary_key: bool
    foreign_key_count: int

    # Query aggregates
    total_query_count: int = 0
    read_query_count: int = 0
    write_query_count: int = 0
    read_ratio: float = 0.0
    total_calls_per_second: float = 0.0
    total_db_load_percent: float = 0.0
    avg_execution_time_ms: float = 0.0
    max_rows_returned_avg: float = 0.0
    has_joins: bool = False
    max_join_count: int = 0

    # Pattern aggregates
    pattern_count: int = 0
    anti_pattern_count: int = 0
    pattern_types: list[str] = field(default_factory=list)


_READ_QUERY_TYPES = frozenset({"SELECT"})
_WRITE_QUERY_TYPES = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE"})


def build_table_profiles(
    collector_output: dict,
    workload_analysis,
) -> dict[str, TableProfile]:
    """Build a TableProfile for every table in the schema.

    Args:
        collector_output: Raw collector output dict.
        workload_analysis: WorkloadAnalysis with patterns/anti-patterns.

    Returns:
        dict mapping table_id → TableProfile.
    """
    tables = (collector_output.get("database_schema") or {}).get("tables") or []
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []

    # --- Group queries by table ---
    table_queries: dict[str, list[dict]] = {}
    for q in queries:
        for tid in q.get("tables_accessed") or []:
            table_queries.setdefault(tid, []).append(q)

    # --- Pattern/anti-pattern lookup ---
    table_pattern_counts: dict[str, int] = {}
    table_pattern_types: dict[str, set[str]] = {}
    for p in workload_analysis.patterns_detected:
        for tid in p.table_ids or []:
            table_pattern_counts[tid] = table_pattern_counts.get(tid, 0) + 1
            table_pattern_types.setdefault(tid, set()).add(p.pattern_type)

    table_anti_pattern_counts: dict[str, int] = {}
    for ap in workload_analysis.anti_patterns_detected or []:
        for tid in ap.table_ids or []:
            table_anti_pattern_counts[tid] = table_anti_pattern_counts.get(tid, 0) + 1

    # --- Build profiles ---
    profiles: dict[str, TableProfile] = {}
    for table in tables:
        tid = table.get("table_id")
        columns = table.get("columns") or []
        pk = table.get("primary_key")
        fks = table.get("foreign_keys") or []

        profile = TableProfile(
            table_id=tid,
            row_count=table.get("row_count") or 0,
            size_mb=table.get("size_mb") or 0.0,
            column_count=len(columns),
            has_primary_key=bool(pk),
            foreign_key_count=len(fks),
            pattern_count=table_pattern_counts.get(tid, 0),
            anti_pattern_count=table_anti_pattern_counts.get(tid, 0),
            pattern_types=sorted(table_pattern_types.get(tid, set())),
        )

        # Compute query aggregates
        tq = table_queries.get(tid, [])
        if tq:
            reads = 0
            writes = 0
            total_cps = 0.0
            total_load = 0.0
            weighted_exec_sum = 0.0
            max_rows = 0.0
            any_joins = False
            max_joins = 0

            for q in tq:
                qt = q.get("query_type", "").upper()
                if qt in _READ_QUERY_TYPES:
                    reads += 1
                elif qt in _WRITE_QUERY_TYPES:
                    writes += 1

                cps = q.get("calls_per_second") or 0.0
                total_cps += cps
                total_load += q.get("db_load_contribution_percent") or 0.0
                weighted_exec_sum += cps * (q.get("execution_time_ms_avg") or 0.0)

                rows_avg = q.get("rows_returned_avg") or 0.0
                if rows_avg > max_rows:
                    max_rows = rows_avg

                if q.get("has_joins", False):
                    any_joins = True
                jc = q.get("join_count") or 0
                if jc > max_joins:
                    max_joins = jc

            total = reads + writes
            profile.total_query_count = len(tq)
            profile.read_query_count = reads
            profile.write_query_count = writes
            profile.read_ratio = reads / total if total > 0 else 0.0
            profile.total_calls_per_second = total_cps
            profile.total_db_load_percent = total_load
            profile.avg_execution_time_ms = weighted_exec_sum / total_cps if total_cps > 0 else 0.0
            profile.max_rows_returned_avg = max_rows
            profile.has_joins = any_joins
            profile.max_join_count = max_joins

        profiles[tid] = profile

    return profiles


def compute_base_scores(profile: TableProfile) -> ScoreBreakdown:
    """Compute evidence-based scores starting from 0.

    All four dimensions start at 0 and earn points through evidence.
    """
    return ScoreBreakdown(
        pattern_match_score=_pattern_match(profile),
        complexity_score=_complexity(profile),
        performance_score=_performance(profile),
        cost_score=_cost(profile),
    )


def _pattern_match(p: TableProfile) -> int:
    score = 0

    # Per pattern detected: +25 each, capped at 60
    score += min(p.pattern_count * 25, 60)

    # Distinct pattern type bonuses
    n_types = len(p.pattern_types)
    if n_types >= 3:
        score += 15
    elif n_types >= 2:
        score += 10

    # Calls per second
    if p.total_calls_per_second >= 10:
        score += 15
    elif p.total_calls_per_second >= 1:
        score += 10
    elif p.total_calls_per_second >= 0.1:
        score += 5

    # Read ratio
    if p.read_ratio >= 0.8:
        score += 10
    elif p.read_ratio >= 0.5:
        score += 5

    # Anti-pattern penalty
    score -= p.anti_pattern_count * 15

    return max(0, min(score, 100))


def _complexity(p: TableProfile) -> int:
    score = 0

    if p.has_primary_key:
        score += 25

    if p.foreign_key_count == 0:
        score += 20
    elif p.foreign_key_count <= 2:
        score += 10

    if not p.has_joins:
        score += 20
    elif p.max_join_count <= 1:
        score += 10

    if p.max_rows_returned_avg <= 10:
        score += 15
    elif p.max_rows_returned_avg <= 100:
        score += 10
    elif p.max_rows_returned_avg <= 1000:
        score += 5

    if p.column_count <= 5:
        score += 10
    elif p.column_count <= 15:
        score += 5

    if p.read_ratio >= 0.8:
        score += 10
    elif p.read_ratio >= 0.5:
        score += 5

    return max(0, min(score, 100))


def _performance(p: TableProfile) -> int:
    score = 0

    if p.avg_execution_time_ms >= 50:
        score += 25
    elif p.avg_execution_time_ms >= 10:
        score += 20
    elif p.avg_execution_time_ms >= 2:
        score += 10

    if p.total_calls_per_second >= 10:
        score += 25
    elif p.total_calls_per_second >= 1:
        score += 20
    elif p.total_calls_per_second >= 0.1:
        score += 10

    if p.total_db_load_percent >= 20:
        score += 25
    elif p.total_db_load_percent >= 10:
        score += 20
    elif p.total_db_load_percent >= 5:
        score += 15
    elif p.total_db_load_percent >= 1:
        score += 5

    if p.total_query_count >= 5:
        score += 15
    elif p.total_query_count >= 3:
        score += 10
    elif p.total_query_count >= 1:
        score += 5

    return max(0, min(score, 100))


def _cost(p: TableProfile) -> int:
    score = 0

    if p.size_mb <= 10:
        score += 30
    elif p.size_mb <= 100:
        score += 25
    elif p.size_mb <= 1000:
        score += 15
    elif p.size_mb <= 10000:
        score += 5

    if p.row_count <= 10_000:
        score += 20
    elif p.row_count <= 100_000:
        score += 15
    elif p.row_count <= 1_000_000:
        score += 10
    elif p.row_count <= 10_000_000:
        score += 5

    if p.total_calls_per_second >= 10:
        score += 25
    elif p.total_calls_per_second >= 1:
        score += 20
    elif p.total_calls_per_second >= 0.1:
        score += 10

    if p.read_ratio >= 0.8:
        score += 15
    elif p.read_ratio >= 0.5:
        score += 10

    return max(0, min(score, 100))


# Default score weights (used by Redis and as fallback)
DEFAULT_WEIGHTS: dict[str, float] = {
    "pattern_match": 0.4,
    "complexity": 0.3,
    "performance": 0.2,
    "cost": 0.1,
}


def compute_confidence(scores: ScoreBreakdown, weights: dict[str, float] | None = None) -> int:
    """Weighted confidence from the four score dimensions.

    Args:
        scores: The four score dimensions.
        weights: Optional per-target weights. Keys: pattern_match, complexity,
                 performance, cost. Must sum to 1.0. Defaults to 40/30/20/10.
    """
    w = weights or DEFAULT_WEIGHTS
    return int(
        scores.pattern_match_score * w.get("pattern_match", 0.4)
        + scores.complexity_score * w.get("complexity", 0.3)
        + scores.performance_score * w.get("performance", 0.2)
        + scores.cost_score * w.get("cost", 0.1)
    )


@dataclass
class Aggregate:
    """A group of related tables that should migrate together as a unit.

    Tables are grouped by FK relationships and query co-access patterns.
    The aggregate is the unit of migration planning — downstream consumers
    (Referee-Synthesis) use this to recommend migrating related tables together
    rather than individually.

    Confidence scoring:
    - 80-100: co-access evidence exists (queries reference multiple tables)
    - 40-60: FK-only grouping (no query co-access observed)
    """

    aggregate_id: str  # e.g., "agg-orders"
    root_table: str  # table_id of the root
    member_tables: list[str]  # all table_ids in the aggregate
    co_access_evidence: list[dict]  # [{query_id, tables}]
    co_access_confidence: int  # 0-100; higher when query co-access exists
    combined_migration_complexity: MigrationComplexity


def detect_co_access(collector_output: dict) -> list[dict]:
    """Detect pairs of tables that are co-accessed by the same query.

    Returns a list of dicts: [{query_id, tables: [table_a, table_b], frequency}]
    This is engine-agnostic — it works on FK graphs and query patterns
    regardless of target database.
    """
    query_patterns = collector_output.get("queries", {}).get("query_patterns", [])

    results: list[dict] = []
    for q in query_patterns:
        tables_accessed = q.get("tables_accessed") or []
        if len(tables_accessed) <= 1:
            continue

        query_id = q.get("query_id", "")
        frequency = q.get("frequency_per_hour") or 0.0

        for pair in combinations(sorted(set(tables_accessed)), 2):
            results.append(
                {
                    "query_id": query_id,
                    "tables": list(pair),
                    "frequency": frequency,
                }
            )

    return results


def _resolve_table_id(ref_table: str, tables: list[dict]) -> str | None:
    """Resolve a referenced table name to its full table_id."""
    for t in tables:
        tid: str = t.get("table_id", "")
        tname: str = t.get("table_name", "")
        if tid == ref_table or tname == ref_table:
            return tid
    return None


def identify_aggregates(
    collector_output: dict,
    workload_analysis: WorkloadAnalysis,
) -> list[Aggregate]:
    """Identify aggregates by combining FK graph with co-access evidence.

    Algorithm:
    1. Build FK adjacency graph (reuse _resolve_table_id logic).
    2. Build co-access adjacency from detect_co_access().
    3. Union both graphs. Connected components = aggregates.
    4. For each aggregate, pick root = table with most incoming FK refs
       (tie-break: highest total query frequency).
    5. Confidence: 80-100 if co-access evidence exists, 40-60 if FK-only.
    """
    tables = collector_output.get("database_schema", {}).get("tables", [])
    if not tables:
        return []

    queries = collector_output.get("queries", {}).get("query_patterns", [])

    # Collect all table_ids
    all_table_ids: set[str] = set()
    for t in tables:
        tid = t.get("table_id", "")
        if tid:
            all_table_ids.add(tid)

    if not all_table_ids:
        return []

    # --- Step 1: Build FK adjacency graph ---
    fk_adjacency: dict[str, set[str]] = {tid: set() for tid in all_table_ids}
    # Track directed FK edges: fk_edges[source] contains targets it references
    fk_edges_directed: list[tuple[str, str]] = []  # (from_table, to_table)

    for t in tables:
        tid = t.get("table_id", "")
        if not tid:
            continue
        for fk in t.get("foreign_keys") or []:
            ref_table = fk.get("referenced_table", "")
            ref_tid = _resolve_table_id(ref_table, tables)
            if ref_tid and ref_tid in all_table_ids:
                fk_adjacency[tid].add(ref_tid)
                fk_adjacency[ref_tid].add(tid)
                fk_edges_directed.append((tid, ref_tid))

    # --- Step 2: Build co-access adjacency ---
    co_access_results = detect_co_access(collector_output)
    co_access_adjacency: dict[str, set[str]] = {tid: set() for tid in all_table_ids}
    for ca in co_access_results:
        pair = ca.get("tables", [])
        if len(pair) == 2:
            a, b = pair
            if a in all_table_ids and b in all_table_ids:
                co_access_adjacency[a].add(b)
                co_access_adjacency[b].add(a)

    # --- Step 3: Union both graphs ---
    combined: dict[str, set[str]] = {tid: set() for tid in all_table_ids}
    for tid in all_table_ids:
        combined[tid] = fk_adjacency.get(tid, set()) | co_access_adjacency.get(tid, set())

    # Find connected components via BFS
    visited: set[str] = set()
    components: list[list[str]] = []

    for tid in sorted(all_table_ids):
        if tid in visited:
            continue
        component: list[str] = []
        queue = [tid]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            for neighbor in combined.get(current, set()):
                if neighbor not in visited:
                    queue.append(neighbor)
        components.append(sorted(component))

    # --- Build query frequency lookup ---
    table_frequency: dict[str, float] = {}
    for q in queries:
        freq = q.get("frequency_per_hour") or 0.0
        for tid in q.get("tables_accessed", []):
            table_frequency[tid] = table_frequency.get(tid, 0.0) + freq

    # --- Step 4 & 5: Build aggregates ---
    aggregates: list[Aggregate] = []
    for component in components:
        member_set = set(component)

        # Count incoming FK references within the aggregate
        incoming_fk_count: dict[str, int] = {tid: 0 for tid in component}
        fk_edges_in_aggregate = 0
        for src, dst in fk_edges_directed:
            if src in member_set and dst in member_set:
                incoming_fk_count[dst] = incoming_fk_count.get(dst, 0) + 1
                fk_edges_in_aggregate += 1

        # Pick root: most incoming FK refs, tie-break by highest query frequency
        root = max(
            component,
            key=lambda tid: (incoming_fk_count.get(tid, 0), table_frequency.get(tid, 0.0)),
        )

        # Generate aggregate_id from root table name (part after last dot)
        root_name = root.rsplit(".", 1)[-1] if "." in root else root
        aggregate_id = f"agg-{root_name}"

        # Collect co-access evidence for this aggregate
        evidence = [
            ca
            for ca in co_access_results
            if len(ca.get("tables", [])) == 2
            and ca["tables"][0] in member_set
            and ca["tables"][1] in member_set
        ]

        # Set co_access_confidence
        if evidence:
            co_access_confidence = min(80 + len(evidence) * 5, 100)
        else:
            co_access_confidence = min(40 + fk_edges_in_aggregate * 5, 60)

        # Compute combined_migration_complexity
        if fk_edges_in_aggregate > 3:
            complexity = MigrationComplexity.HIGH
        elif fk_edges_in_aggregate > 0:
            complexity = MigrationComplexity.MEDIUM
        else:
            complexity = MigrationComplexity.LOW

        aggregates.append(
            Aggregate(
                aggregate_id=aggregate_id,
                root_table=root,
                member_tables=component,
                co_access_evidence=evidence,
                co_access_confidence=co_access_confidence,
                combined_migration_complexity=complexity,
            )
        )

    return sorted(aggregates, key=lambda a: a.aggregate_id)
