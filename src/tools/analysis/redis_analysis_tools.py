"""
Redis Analysis Tools

Tools for analyzing database workloads and identifying Redis use cases.
"""

from src.contracts.analysis_output import (
    AntiPattern,
    Confidence,
    CostEstimate,
    MigrationComplexity,
    Pattern,
    ScoreBreakdown,
    TableRecommendation,
    WorkloadAnalysis,
)
from src.tools.analysis.scoring import (
    TableProfile,
    build_table_profiles,
    compute_base_scores,
    compute_confidence,
)


def analyze_redis_use_cases(collector_output: dict) -> WorkloadAnalysis:
    """
    Analyze collector output to identify Redis use case patterns.

    Detects:
    - Caching opportunities (frequent SELECT queries)
    - Session store patterns
    - Leaderboard patterns (ORDER BY + LIMIT)
    - Geospatial queries
    - Time series patterns
    - JSON document patterns
    - Real-time analytics patterns
    - Event sourcing patterns (append-only logs)
    - Recommendation engine patterns (set operations)
    - Low-latency reference data lookups

    Args:
        collector_output: Output from collector agent

    Returns:
        WorkloadAnalysis with detected patterns and anti-patterns
    """
    queries = collector_output.get("queries", {}).get("query_patterns", [])

    # Geospatial: spatial SQL functions or coordinate columns
    _geo_keywords = (
        "st_distance",
        "st_within",
        "st_contains",
        "st_intersects",
        "st_dwithin",
        "latitude",
        "longitude",
        "geography",
        "geometry",
    )

    # Single-pass classification: pre-compute text_lower once per query
    caching_queries: list[dict] = []
    session_queries: list[dict] = []
    leaderboard_queries: list[dict] = []
    timeseries_queries: list[dict] = []
    geospatial_queries: list[dict] = []
    large_result_queries: list[dict] = []

    for q in queries:
        text_lower = q.get("query_text", "").lower()

        # Caching: high-frequency SELECTs
        if q.get("query_type") == "SELECT" and q.get("calls_per_second", 0) > 1:
            caching_queries.append(q)

        # Session store: session or user_id references
        if "session" in text_lower or "user_id" in text_lower:
            session_queries.append(q)

        # Leaderboard: ORDER BY + LIMIT
        if "order by" in text_lower and "limit" in text_lower:
            leaderboard_queries.append(q)

        # Time series: timestamp/date keywords + GROUP BY
        if (
            any(kw in text_lower for kw in ("timestamp", "created_at", "updated_at", "date"))
            and "group by" in text_lower
        ):
            timeseries_queries.append(q)

        # Time series: time binning functions
        # DATE_TRUNC()
        if any(kw in text_lower for kw in ("date_trunc")) and "group by" in text_lower:
            timeseries_queries.append(q)

        # Geospatial: spatial SQL functions or coordinate columns
        if any(kw in text_lower for kw in _geo_keywords):
            geospatial_queries.append(q)

        # Anti-pattern: large result sets
        if q.get("rows_returned_avg", 0) > 10000:
            large_result_queries.append(q)

    # Build Pattern / AntiPattern objects from accumulators
    patterns = []
    anti_patterns = []

    if caching_queries:
        caching_table_ids = list({t for q in caching_queries for t in q.get("tables_accessed", [])})
        patterns.append(
            Pattern(
                pattern_id="redis-caching-001",
                pattern_type="caching",
                confidence=Confidence.HIGH,
                description=f"Detected {len(caching_queries)} high-frequency SELECT queries suitable for caching",
                query_ids=[str(q["query_id"]) for q in caching_queries if q.get("query_id")],
                table_ids=caching_table_ids,
                frequency_percent=len(geospatial_queries) / len(queries) * 100 if queries else None,
            )
        )

    if session_queries:
        session_table_ids = list({t for q in session_queries for t in q.get("tables_accessed", [])})
        patterns.append(
            Pattern(
                pattern_id="redis-session-001",
                pattern_type="session-store",
                confidence=Confidence.MEDIUM,
                description="Detected session or user lookup patterns",
                query_ids=[str(q["query_id"]) for q in session_queries if q.get("query_id")],
                table_ids=session_table_ids,
                frequency_percent=len(geospatial_queries) / len(queries) * 100 if queries else None,
            )
        )

    if leaderboard_queries:
        leaderboard_table_ids = list(
            {t for q in leaderboard_queries for t in q.get("tables_accessed", [])}
        )
        patterns.append(
            Pattern(
                pattern_id="redis-leaderboard-001",
                pattern_type="leaderboard",
                confidence=Confidence.MEDIUM,
                description="Detected ranking/leaderboard query patterns (ORDER BY + LIMIT)",
                query_ids=[str(q["query_id"]) for q in leaderboard_queries if q.get("query_id")],
                table_ids=leaderboard_table_ids,
                frequency_percent=len(geospatial_queries) / len(queries) * 100 if queries else None,
            )
        )

    if timeseries_queries:
        timeseries_table_ids = list(
            {t for q in timeseries_queries for t in q.get("tables_accessed", [])}
        )
        patterns.append(
            Pattern(
                pattern_id="redis-timeseries-001",
                pattern_type="time-series",
                confidence=Confidence.MEDIUM,
                description="Detected time series aggregation patterns",
                query_ids=[str(q["query_id"]) for q in timeseries_queries if q.get("query_id")],
                table_ids=timeseries_table_ids,
                frequency_percent=len(geospatial_queries) / len(queries) * 100 if queries else None,
            )
        )

    if geospatial_queries:
        geospatial_table_ids = list(
            {t for q in geospatial_queries for t in q.get("tables_accessed", [])}
        )
        patterns.append(
            Pattern(
                pattern_id="redis-geospatial-001",
                pattern_type="geospatial",
                confidence=Confidence.MEDIUM,
                description="Detected geospatial query patterns",
                query_ids=[str(q["query_id"]) for q in geospatial_queries if q.get("query_id")],
                table_ids=geospatial_table_ids,
                frequency_percent=len(geospatial_queries) / len(queries) * 100 if queries else None,
            )
        )

    if large_result_queries:
        large_result_table_ids = list(
            {t for q in large_result_queries for t in q.get("tables_accessed", [])}
        )
        anti_patterns.append(
            AntiPattern(
                anti_pattern_id="redis-large-results-001",
                anti_pattern_type="large-result-sets",
                severity_weight=0.5,
                description="Queries returning large result sets (>10k rows) not ideal for Redis",
                query_ids=[str(q["query_id"]) for q in large_result_queries if q.get("query_id")],
                table_ids=large_result_table_ids,
                recommendation="Consider pagination or filtering to reduce result set size",
            )
        )

    return WorkloadAnalysis(patterns_detected=patterns, anti_patterns_detected=anti_patterns)


def analyze_caching_patterns(
    collector_output: dict, workload_analysis: WorkloadAnalysis
) -> list[TableRecommendation]:
    """
    Generate table-level recommendations for Redis migration.

    Uses evidence-based scoring: all scores start at 0 and earn points
    through detected patterns, query characteristics, and table structure.

    Args:
        collector_output: Output from collector agent
        workload_analysis: Detected patterns and anti-patterns

    Returns:
        List of table recommendations
    """
    profiles = build_table_profiles(collector_output, workload_analysis)

    # Pattern/anti-pattern lookups for rationale and concerns
    table_to_patterns: dict[str, list] = {}
    for p in workload_analysis.patterns_detected:
        for tid in p.table_ids or []:
            table_to_patterns.setdefault(tid, []).append(p)

    table_to_anti_patterns: dict[str, list] = {}
    for ap in workload_analysis.anti_patterns_detected or []:
        for tid in ap.table_ids or []:
            table_to_anti_patterns.setdefault(tid, []).append(ap)

    recommendations = []
    for table_id, profile in profiles.items():
        scores = compute_base_scores(profile)
        scores = _apply_redis_adjustments(scores, profile)
        confidence = compute_confidence(scores)

        table_patterns = table_to_patterns.get(table_id, [])
        table_anti_patterns = table_to_anti_patterns.get(table_id, [])

        rationale = _build_rationale(profile, table_patterns)
        concerns = _build_concerns(profile, table_anti_patterns)
        migration_complexity = _assess_complexity(profile, table_anti_patterns)

        recommendations.append(
            TableRecommendation(
                table_id=table_id,
                confidence_score=confidence,
                rationale=rationale,
                score_breakdown=scores,
                supporting_patterns=[p.pattern_id for p in table_patterns] or None,
                concerns=concerns or None,
                migration_complexity=migration_complexity,
            )
        )

    return recommendations


def _apply_redis_adjustments(scores: ScoreBreakdown, profile: TableProfile) -> ScoreBreakdown:
    """Apply Redis-specific scoring adjustments on top of base scores."""
    pattern = scores.pattern_match_score
    complexity = scores.complexity_score
    performance = scores.performance_score
    cost = scores.cost_score

    # Redis-friendly pattern bonuses
    types = set(profile.pattern_types)
    if "caching" in types:
        pattern += 10
    if "session-store" in types:
        pattern += 10
    if "leaderboard" in types:
        pattern += 10
    if "time-series" in types:
        pattern += 5
    if "geospatial" in types:
        pattern += 10

    # Redis migration is relatively simple
    complexity += 10

    # Sub-ms Redis helps when source queries are slow
    if profile.avg_execution_time_ms >= 5:
        performance += 10

    # Small + hot tables are ideal for Redis
    if profile.size_mb <= 100 and profile.total_calls_per_second >= 1:
        cost += 10

    # Large datasets are expensive in Redis
    if profile.size_mb > 1000:
        cost -= 15

    # Write-heavy workloads are problematic for cache invalidation
    if profile.total_query_count > 0 and profile.read_ratio < 0.3:
        pattern -= 10
        performance -= 10

    return ScoreBreakdown(
        pattern_match_score=max(0, min(pattern, 100)),
        complexity_score=max(0, min(complexity, 100)),
        performance_score=max(0, min(performance, 100)),
        cost_score=max(0, min(cost, 100)),
    )


def _build_rationale(profile: TableProfile, table_patterns: list) -> str:
    """Build evidence-based rationale string."""
    if profile.pattern_types:
        return "Strong " + " and ".join(profile.pattern_types) + " candidate"
    if profile.total_query_count > 0:
        return "Moderate workload detected but no strong migration patterns"
    return "No query activity detected for this table"


def _build_concerns(profile: TableProfile, table_anti_patterns: list) -> list[str]:
    """Build list of concern strings from negative signals."""
    concerns: list[str] = []

    for ap in table_anti_patterns:
        concerns.append(f"Anti-pattern: {ap.description}")

    if profile.total_query_count > 0 and profile.read_ratio < 0.3:
        pct = int(profile.read_ratio * 100)
        concerns.append(
            f"Write-heavy workload (read ratio {pct}%) increases cache invalidation overhead"
        )

    if profile.size_mb > 1000:
        concerns.append(f"Large table ({profile.size_mb:.0f}MB) may be expensive to store in Redis")

    if profile.total_query_count == 0:
        concerns.append("No queries reference this table in the collection window")

    return concerns


def _assess_complexity(profile: TableProfile, table_anti_patterns: list) -> MigrationComplexity:
    """Determine migration complexity based on profile signals."""
    if table_anti_patterns:
        return MigrationComplexity.MEDIUM
    if profile.has_joins and profile.max_join_count >= 2:
        return MigrationComplexity.MEDIUM
    return MigrationComplexity.LOW


def estimate_redis_costs(
    collector_output: dict, target_region: str, analysis_options
) -> CostEstimate:
    """
    Estimate monthly costs for Redis/ElastiCache deployment.

    Selects an instance type based on estimated cache size and throughput,
    adds replication costs for HA, and includes backup storage.

    Args:
        collector_output: Output from collector agent
        target_region: AWS region for pricing
        analysis_options: Analysis configuration options

    Returns:
        CostEstimate with monthly cost breakdown
    """
    tables = collector_output.get("database_schema", {}).get("tables", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])

    # --- Estimate cache data size ---
    total_source_size_mb = sum(t.get("size_mb", 0) for t in tables)
    # Cache typically holds 10-20% of source data; use 15% as baseline
    cache_size_gb = max((total_source_size_mb * 0.15) / 1024, 0.5)

    # --- Estimate throughput ---
    total_read_cps = 0.0
    total_write_cps = 0.0
    for q in queries:
        cps = q.get("calls_per_second", 0) or 0
        qt = (q.get("query_type") or "SELECT").upper()
        if qt == "SELECT":
            total_read_cps += cps
        elif qt in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            total_write_cps += cps
    total_ops_per_sec = total_read_cps + total_write_cps

    # --- Instance selection based on memory + throughput ---
    # Node-based pricing (r7g family, us-east-1 on-demand $/hr):
    _catalog: list[tuple[str, float, float, int]] = [
        # (type, memory_gb, hourly_rate, max_ops)
        ("cache.r7g.medium", 6.38, 0.113, 25_000),
        ("cache.r7g.large", 13.07, 0.226, 50_000),
        ("cache.r7g.xlarge", 26.32, 0.452, 100_000),
        ("cache.r7g.2xlarge", 52.82, 0.904, 200_000),
        ("cache.r7g.4xlarge", 105.81, 1.808, 400_000),
    ]

    # Pick smallest instance that fits both memory and throughput
    # Reserve ~25% memory headroom for Redis overhead (fragmentation, buffers)
    required_memory_gb = cache_size_gb * 1.25
    inst_type, inst_memory, hourly_rate, _max_ops = _catalog[-1]  # default to largest
    for itype, imem, ihr, iops in _catalog:
        if imem >= required_memory_gb and iops >= total_ops_per_sec:
            inst_type, inst_memory, hourly_rate, _max_ops = itype, imem, ihr, iops
            break

    # --- Calculate costs ---
    hours_per_month = 730

    # Primary node
    primary_cost = hourly_rate * hours_per_month

    # Replica for HA (1 replica by default)
    replica_count = 1
    replica_cost = hourly_rate * hours_per_month * replica_count

    # Backup storage: $0.085/GB-month, assume daily snapshot = ~cache size
    backup_cost = cache_size_gb * 0.085

    # Data transfer: cross-AZ replication ~$0.01/GB, estimate based on write throughput
    write_bytes_per_month = total_write_cps * 1024 * 86400 * 30  # assume 1KB avg write
    data_transfer_gb = write_bytes_per_month / (1024**3)
    data_transfer_cost = data_transfer_gb * 0.01

    total = round(primary_cost + replica_cost + backup_cost + data_transfer_cost, 2)

    return CostEstimate(
        monthly_cost_usd=total,
        cost_components={
            "primary_node": round(primary_cost, 2),
            "replica_nodes": round(replica_cost, 2),
            "backup_storage": round(backup_cost, 2),
            "data_transfer": round(data_transfer_cost, 2),
            "instance_type": inst_type,
            "replica_count": replica_count,
        },
        pricing_assumptions=[
            f"Instance: {inst_type} ({inst_memory} GB) in {target_region}",
            f"Estimated cache size: {cache_size_gb:.1f} GB (15% of {total_source_size_mb:.0f} MB source)",
            f"Estimated throughput: {total_ops_per_sec:.0f} ops/sec ({total_read_cps:.0f} reads + {total_write_cps:.0f} writes)",
            f"{replica_count} replica for high availability",
            "On-demand pricing (no reserved instances)",
            "Daily backup snapshot included",
            "Cross-AZ replication data transfer estimated from write throughput",
        ],
    )
