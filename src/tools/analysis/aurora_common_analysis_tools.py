"""
Aurora Common Analysis Tools

Shared relational detection, scoring adjustments, and cost estimation used by
both Aurora PostgreSQL and Aurora MySQL analysis agents.

Implements:
- analyze_aurora_common_use_cases() -> (patterns, anti_patterns)
- _apply_aurora_common_adjustments() -> ScoreBreakdown
- estimate_aurora_costs() -> CostEstimate
"""

from __future__ import annotations

from src.contracts.analysis_output import (
    AntiPattern,
    Confidence,
    CostEstimate,
    MigrationComplexity,
    Pattern,
    ScoreBreakdown,
)
from src.tools.analysis.aurora_common_pattern_catalog import (
    AGGREGATION_KEYWORDS,
    ANTI_PATTERN_BY_ID,
    CACHE_READ_CPS_THRESHOLD,
    HIGH_FREQUENCY_PK_LOOKUP_CPS,
    HIGH_INGEST_CPS_THRESHOLD,
    HIGH_VOLUME_TEXT_SEARCH_CPS,
    IN_LIST_KEYWORDS,
    PAGINATION_KEYWORDS,
    PATTERN_BY_ID,
    SKIP_QUERY_TYPES,
    STANDARD_CRUD_MIN_CPS,
    SUBQUERY_RE,
    TEXT_COLUMN_RATIO_THRESHOLD,
    TEXT_DATA_TYPES,
    TEXT_SEARCH_KEYWORDS,
    TRANSACTION_WRITE_TYPES,
)
from src.tools.analysis.scoring import TableProfile

# ==========================================================================
# Internal helpers
# ==========================================================================


def _text_contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    """Case-insensitive substring check for any keyword."""
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _build_pattern(
    pattern_id: str,
    confidence: Confidence,
    query_ids: list[str],
    table_ids: list[str],
    total_queries: int,
) -> Pattern:
    """Create a Pattern from a catalog entry + matched query data."""
    catalog = PATTERN_BY_ID[pattern_id]
    frequency_pct = None
    if total_queries > 0:
        frequency_pct = min(100.0, len(query_ids) / total_queries * 100.0)
    return Pattern(
        pattern_id=pattern_id,
        pattern_type=catalog.pattern_type,
        confidence=confidence,
        description=catalog.description,
        query_ids=query_ids if query_ids else None,
        table_ids=sorted(set(table_ids)) if table_ids else None,
        frequency_percent=round(frequency_pct, 2) if frequency_pct is not None else None,
    )


def _build_anti_pattern(
    pattern_id: str,
    query_ids: list[str],
    table_ids: list[str],
) -> AntiPattern:
    """Create an AntiPattern from a catalog entry + matched query data."""
    catalog = ANTI_PATTERN_BY_ID[pattern_id]
    return AntiPattern(
        anti_pattern_id=pattern_id,
        anti_pattern_type=catalog.pattern_type,
        severity_weight=catalog.severity_weight,
        description=catalog.description,
        query_ids=query_ids if query_ids else None,
        table_ids=sorted(set(table_ids)) if table_ids else None,
        recommendation=catalog.guidance if catalog.guidance else None,
    )


def _is_single_row_pk_lookup(query: dict) -> bool:
    """Detect single-row PK lookup: SELECT, no joins, single table, rows_returned_avg <= 1."""
    qt = (query.get("query_type") or "").upper()
    if qt != "SELECT":
        return False
    if query.get("has_joins", False):
        return False
    tables = query.get("tables_accessed") or []
    if len(tables) != 1:
        return False
    rows_avg = query.get("rows_returned_avg") or 0.0
    return rows_avg <= 1.0


# ==========================================================================
# Main detection function
# ==========================================================================


def analyze_aurora_common_use_cases(
    collector_output: dict,
) -> tuple[list[Pattern], list[AntiPattern]]:
    """Single-pass scan of all queries producing common relational patterns and anti-patterns.

    Returns:
        Tuple of (patterns_list, anti_patterns_list) for shared relational detection.
    """
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    tables_schema = (collector_output.get("database_schema") or {}).get("tables") or []
    total_query_count = len(queries)

    # --- Accumulators for patterns ---
    # aurora-common-01: complex-join
    cj_query_ids: list[str] = []
    cj_table_ids: list[str] = []

    # aurora-common-02: aggregation-analytics
    ag_query_ids: list[str] = []
    ag_table_ids: list[str] = []

    # aurora-common-03: transactional-write
    tw_query_ids: list[str] = []
    tw_table_ids: list[str] = []

    # aurora-common-04: referential-integrity (table-level, not query-level)
    # Detected from schema, not query text

    # aurora-common-05: reporting-query
    rp_query_ids: list[str] = []
    rp_table_ids: list[str] = []

    # aurora-common-06: correlated-subquery
    cs_query_ids: list[str] = []
    cs_table_ids: list[str] = []

    # aurora-common-07: multi-row-mutation
    mm_query_ids: list[str] = []
    mm_table_ids: list[str] = []

    # aurora-common-08: pagination
    pg_query_ids: list[str] = []
    pg_table_ids: list[str] = []

    # aurora-common-09: simple-join (2 tables)
    sj_query_ids: list[str] = []
    sj_table_ids: list[str] = []

    # aurora-common-10: standard-crud (per-table accumulator)
    crud_per_table: dict[str, dict] = {}

    # aurora-common-11: batch-in-list
    bl_query_ids: list[str] = []
    bl_table_ids: list[str] = []

    # --- Anti-pattern accumulators ---
    # aurora-anti-01: high-frequency-pk-lookup
    pk_query_ids: list[str] = []
    pk_table_ids: list[str] = []

    # aurora-anti-02: simple-cache-read
    cr_query_ids: list[str] = []
    cr_table_ids: list[str] = []

    # aurora-anti-03: full-text-search-candidate
    ft_query_ids: list[str] = []
    ft_table_ids: list[str] = []

    # aurora-anti-04: unbounded-time-series-ingest (per-table)
    ingest_per_table: dict[str, dict] = {}

    # aurora-anti-06: single-access-pattern-table (per-table)
    access_pattern_per_table: dict[str, dict] = {}

    # aurora-anti-07: high-volume-text-search (per-table)
    text_search_per_table: dict[str, dict] = {}

    # --- Single pass ---
    for q in queries:
        qid = q.get("query_id", "")
        qt = (q.get("query_type") or "").upper()
        text = q.get("query_text") or ""
        text_lower = text.lower()
        tables = q.get("tables_accessed") or []
        cps = float(q.get("calls_per_second") or 0.0)
        has_joins = bool(q.get("has_joins", False))

        # Skip admin/metadata queries (SHOW, DESCRIBE, EXPLAIN, SET)
        if qt in SKIP_QUERY_TYPES:
            continue
        join_count = int(q.get("join_count") or 0)
        rows_avg = float(q.get("rows_returned_avg") or 0.0)

        # ---- Pattern detection ----

        # aurora-common-01: complex-join (3+ tables or join_count >= 3)
        if has_joins and (join_count >= 3 or len(tables) >= 3):
            cj_query_ids.append(qid)
            cj_table_ids.extend(tables)

        # aurora-common-02: aggregation-analytics
        if _text_contains_any(text_lower, AGGREGATION_KEYWORDS):
            ag_query_ids.append(qid)
            ag_table_ids.extend(tables)

        # aurora-common-03: transactional-write (multi-table writes or FK-constrained)
        if qt in TRANSACTION_WRITE_TYPES and (len(tables) > 1 or has_joins):
            tw_query_ids.append(qid)
            tw_table_ids.extend(tables)

        # aurora-common-05: reporting-query (large result sets + aggregation or date range)
        if rows_avg > 100 and _text_contains_any(text_lower, AGGREGATION_KEYWORDS):
            rp_query_ids.append(qid)
            rp_table_ids.extend(tables)

        # aurora-common-06: correlated-subquery
        if SUBQUERY_RE.search(text):
            cs_query_ids.append(qid)
            cs_table_ids.extend(tables)

        # aurora-common-07: multi-row-mutation
        if qt in {"UPDATE", "DELETE"} and not _is_single_row_pk_lookup(q):
            if has_joins or rows_avg > 1 or len(tables) > 1:
                mm_query_ids.append(qid)
                mm_table_ids.extend(tables)

        # aurora-common-08: pagination
        if _text_contains_any(text_lower, PAGINATION_KEYWORDS):
            pg_query_ids.append(qid)
            pg_table_ids.extend(tables)

        # aurora-common-09: simple-join (exactly 2 tables, not already caught by complex-join)
        if has_joins and join_count >= 1 and not (join_count >= 3 or len(tables) >= 3):
            sj_query_ids.append(qid)
            sj_table_ids.extend(tables)

        # aurora-common-10: standard-crud (single-table read/write at moderate frequency)
        if (
            qt in {"SELECT", "INSERT", "UPDATE", "DELETE"}
            and not has_joins
            and cps >= STANDARD_CRUD_MIN_CPS
        ):
            for tid in tables:
                if tid not in crud_per_table:
                    crud_per_table[tid] = {"query_ids": [], "total_cps": 0.0}
                crud_per_table[tid]["query_ids"].append(qid)
                crud_per_table[tid]["total_cps"] += cps

        # aurora-common-11: batch-in-list (WHERE col IN (...))
        if _text_contains_any(text_lower, IN_LIST_KEYWORDS) and qt == "SELECT":
            bl_query_ids.append(qid)
            bl_table_ids.extend(tables)

        # ---- Anti-pattern detection ----

        # aurora-anti-01: high-frequency-pk-lookup
        if _is_single_row_pk_lookup(q) and cps > HIGH_FREQUENCY_PK_LOOKUP_CPS:
            pk_query_ids.append(qid)
            pk_table_ids.extend(tables)

        # aurora-anti-02: simple-cache-read (high freq, small result, SELECT, no joins)
        if (
            qt == "SELECT"
            and not has_joins
            and cps > CACHE_READ_CPS_THRESHOLD
            and rows_avg <= 10
            and len(tables) == 1
        ):
            # Exclude single-row PK lookups that already qualify for anti-01
            is_pk_lookup = _is_single_row_pk_lookup(q) and cps > HIGH_FREQUENCY_PK_LOOKUP_CPS
            if not is_pk_lookup:
                cr_query_ids.append(qid)
                cr_table_ids.extend(tables)

        # aurora-anti-03: full-text-search-candidate
        if "like '%" in text_lower or "like N'%" in text_lower or "ilike '%" in text_lower:
            ft_query_ids.append(qid)
            ft_table_ids.extend(tables)

        # aurora-anti-04: unbounded-time-series-ingest (per table)
        if qt == "INSERT":
            for tid in tables:
                if tid not in ingest_per_table:
                    ingest_per_table[tid] = {"query_ids": [], "total_cps": 0.0, "has_reads": False}
                ingest_per_table[tid]["query_ids"].append(qid)
                ingest_per_table[tid]["total_cps"] += cps

        # aurora-anti-06: track access patterns per table
        if not has_joins and len(tables) == 1:
            for tid in tables:
                if tid not in access_pattern_per_table:
                    access_pattern_per_table[tid] = {"query_ids": set(), "all_single_table": True}
                access_pattern_per_table[tid]["query_ids"].add(qid)
        else:
            for tid in tables:
                if tid in access_pattern_per_table:
                    access_pattern_per_table[tid]["all_single_table"] = False
                else:
                    access_pattern_per_table[tid] = {"query_ids": {qid}, "all_single_table": False}

        # aurora-anti-07: track text search volume per table
        if _text_contains_any(text_lower, TEXT_SEARCH_KEYWORDS):
            for tid in tables:
                if tid not in text_search_per_table:
                    text_search_per_table[tid] = {"query_ids": [], "total_cps": 0.0}
                text_search_per_table[tid]["query_ids"].append(qid)
                text_search_per_table[tid]["total_cps"] += cps

    # Check ingest tables for complex reads (if no reads, it's an anti-pattern)
    for q in queries:
        qt = (q.get("query_type") or "").upper()
        if qt == "SELECT" and (
            q.get("has_joins")
            or _text_contains_any((q.get("query_text") or "").lower(), AGGREGATION_KEYWORDS)
        ):
            for tid in q.get("tables_accessed") or []:
                if tid in ingest_per_table:
                    ingest_per_table[tid]["has_reads"] = True

    # --- Detect aurora-common-04 from schema (FK density > 2) ---
    ri_table_ids: list[str] = []
    for table in tables_schema:
        tid = table.get("table_id", "")
        fks = table.get("foreign_keys") or []
        if len(fks) > 2:
            ri_table_ids.append(tid)

    # ==========================================================================
    # Assemble detected patterns
    # ==========================================================================
    patterns: list[Pattern] = []

    if cj_query_ids:
        patterns.append(
            _build_pattern(
                "aurora-common-01", Confidence.HIGH, cj_query_ids, cj_table_ids, total_query_count
            )
        )

    if ag_query_ids:
        patterns.append(
            _build_pattern(
                "aurora-common-02", Confidence.HIGH, ag_query_ids, ag_table_ids, total_query_count
            )
        )

    if tw_query_ids:
        patterns.append(
            _build_pattern(
                "aurora-common-03", Confidence.HIGH, tw_query_ids, tw_table_ids, total_query_count
            )
        )

    if ri_table_ids:
        patterns.append(
            Pattern(
                pattern_id="aurora-common-04",
                pattern_type="referential-integrity",
                confidence=Confidence.HIGH,
                description=PATTERN_BY_ID["aurora-common-04"].description,
                query_ids=None,
                table_ids=sorted(set(ri_table_ids)),
                frequency_percent=None,
            )
        )

    if rp_query_ids:
        patterns.append(
            _build_pattern(
                "aurora-common-05", Confidence.MEDIUM, rp_query_ids, rp_table_ids, total_query_count
            )
        )

    if cs_query_ids:
        patterns.append(
            _build_pattern(
                "aurora-common-06", Confidence.MEDIUM, cs_query_ids, cs_table_ids, total_query_count
            )
        )

    if mm_query_ids:
        patterns.append(
            _build_pattern(
                "aurora-common-07", Confidence.MEDIUM, mm_query_ids, mm_table_ids, total_query_count
            )
        )

    if pg_query_ids:
        patterns.append(
            _build_pattern(
                "aurora-common-08", Confidence.HIGH, pg_query_ids, pg_table_ids, total_query_count
            )
        )

    if sj_query_ids:
        patterns.append(
            _build_pattern(
                "aurora-common-09", Confidence.MEDIUM, sj_query_ids, sj_table_ids, total_query_count
            )
        )

    # aurora-common-10: standard-crud — emit per-table for tables with active CRUD
    for tid, data in crud_per_table.items():
        if data["total_cps"] >= STANDARD_CRUD_MIN_CPS:
            frequency_pct = (
                round(len(data["query_ids"]) / total_query_count * 100.0, 2)
                if total_query_count > 0
                else None
            )
            patterns.append(
                Pattern(
                    pattern_id="aurora-common-10",
                    pattern_type="standard-crud",
                    confidence=Confidence.MEDIUM,
                    description=PATTERN_BY_ID["aurora-common-10"].description,
                    query_ids=data["query_ids"],
                    table_ids=[tid],
                    frequency_percent=frequency_pct,
                )
            )

    if bl_query_ids:
        patterns.append(
            _build_pattern(
                "aurora-common-11", Confidence.MEDIUM, bl_query_ids, bl_table_ids, total_query_count
            )
        )

    # ==========================================================================
    # Assemble detected anti-patterns
    # ==========================================================================
    anti_patterns: list[AntiPattern] = []

    if pk_query_ids:
        anti_patterns.append(_build_anti_pattern("aurora-anti-01", pk_query_ids, pk_table_ids))

    if cr_query_ids:
        anti_patterns.append(_build_anti_pattern("aurora-anti-02", cr_query_ids, cr_table_ids))

    if ft_query_ids:
        anti_patterns.append(_build_anti_pattern("aurora-anti-03", ft_query_ids, ft_table_ids))

    # aurora-anti-04: per-table high ingest with no complex reads
    anti04_query_ids: list[str] = []
    anti04_table_ids: list[str] = []
    for tid, data in ingest_per_table.items():
        if data["total_cps"] >= HIGH_INGEST_CPS_THRESHOLD and not data["has_reads"]:
            anti04_query_ids.extend(data["query_ids"])
            anti04_table_ids.append(tid)

    if anti04_query_ids:
        anti_patterns.append(
            _build_anti_pattern("aurora-anti-04", anti04_query_ids, anti04_table_ids)
        )

    # aurora-anti-05: no-relational-need
    # Table with 0 FKs AND no query for this table has joins or multi-table access
    anti05_query_ids: list[str] = []
    anti05_table_ids: list[str] = []
    tables_with_joins: set[str] = set()
    for q in queries:
        if q.get("has_joins") or len(q.get("tables_accessed") or []) > 1:
            for tid in q.get("tables_accessed") or []:
                tables_with_joins.add(tid)

    for table in tables_schema:
        tid = table.get("table_id", "")
        fks = table.get("foreign_keys") or []
        if len(fks) > 0:
            continue
        if tid in tables_with_joins:
            continue
        # Must have at least some queries (don't flag unused tables)
        table_qs = [q for q in queries if tid in (q.get("tables_accessed") or [])]
        if not table_qs:
            continue
        anti05_query_ids.extend(q.get("query_id", "") for q in table_qs)
        anti05_table_ids.append(tid)

    if anti05_query_ids:
        anti_patterns.append(
            _build_anti_pattern("aurora-anti-05", anti05_query_ids, anti05_table_ids)
        )

    # aurora-anti-06: single-access-pattern-table
    # <=2 queries, all single-table, no FKs
    anti06_query_ids: list[str] = []
    anti06_table_ids: list[str] = []
    for tid, data in access_pattern_per_table.items():
        if not data["all_single_table"]:
            continue
        if len(data["query_ids"]) > 2:
            continue
        # Also check table has 0 FKs
        table_def = next((t for t in tables_schema if t.get("table_id") == tid), None)
        if table_def and len(table_def.get("foreign_keys") or []) > 0:
            continue
        anti06_query_ids.extend(data["query_ids"])
        anti06_table_ids.append(tid)

    if anti06_query_ids:
        anti_patterns.append(
            _build_anti_pattern("aurora-anti-06", anti06_query_ids, anti06_table_ids)
        )

    # aurora-anti-07: high-volume-text-search
    # Text search at >10 CPS + text columns >40% of schema
    anti07_query_ids: list[str] = []
    anti07_table_ids: list[str] = []
    for tid, data in text_search_per_table.items():
        if data["total_cps"] < HIGH_VOLUME_TEXT_SEARCH_CPS:
            continue
        table_def = next((t for t in tables_schema if t.get("table_id") == tid), None)
        if not table_def:
            continue
        columns = table_def.get("columns") or []
        if not columns:
            continue
        text_count = sum(
            1
            for col in columns
            if any(tt in (col.get("data_type") or "").lower() for tt in TEXT_DATA_TYPES)
        )
        text_ratio = text_count / len(columns)
        if text_ratio < TEXT_COLUMN_RATIO_THRESHOLD:
            continue
        anti07_query_ids.extend(data["query_ids"])
        anti07_table_ids.append(tid)

    if anti07_query_ids:
        anti_patterns.append(
            _build_anti_pattern("aurora-anti-07", anti07_query_ids, anti07_table_ids)
        )

    return patterns, anti_patterns


# ==========================================================================
# Scoring adjustments
# ==========================================================================


def _apply_aurora_common_adjustments(
    scores: ScoreBreakdown,
    profile: TableProfile,
) -> ScoreBreakdown:
    """Apply common relational scoring adjustments.

    Key inversion from NoSQL agents: high FK density and join complexity
    INCREASE the score (Aurora's strength).
    """
    pattern = scores.pattern_match_score
    complexity = scores.complexity_score
    performance = scores.performance_score
    cost = scores.cost_score

    # --- pattern_match bonuses ---
    # FK density is GOOD for Aurora
    if profile.foreign_key_count > 2:
        pattern += 15
    elif profile.foreign_key_count > 0:
        pattern += 10

    # Join complexity is GOOD for Aurora
    if profile.max_join_count >= 3:
        pattern += 15
    elif profile.has_joins:
        pattern += 10

    # --- complexity ---
    # For Aurora, "complexity" means how well the workload fits Aurora's model.
    # High FK count = complex relational model = GOOD fit
    if profile.foreign_key_count > 2:
        complexity += 20
    elif profile.foreign_key_count > 0:
        complexity += 10

    # Joins are natural for Aurora
    if profile.has_joins:
        complexity += 15

    # Mixed read/write is natural for Aurora (OLTP)
    if 0.3 <= profile.read_ratio <= 0.8:
        complexity += 10

    # --- performance ---
    # High query count suggests the table is actively used — Aurora benefits
    if profile.total_query_count >= 5:
        performance += 10

    # Moderate latency (not extreme) — Aurora handles well
    if 5.0 <= profile.avg_execution_time_ms <= 500.0:
        performance += 10

    # --- cost ---
    # Larger datasets favor Aurora (vs DynamoDB which gets expensive at scale with complex queries)
    if profile.size_mb >= 1000:
        cost += 15
    elif profile.size_mb >= 100:
        cost += 10

    # --- Penalties ---
    # Very high CPS with no joins and single-row reads → probably better on DynamoDB
    if (
        profile.total_calls_per_second > HIGH_FREQUENCY_PK_LOOKUP_CPS
        and not profile.has_joins
        and profile.max_rows_returned_avg <= 1
    ):
        pattern -= 20
        performance -= 15

    return ScoreBreakdown(
        pattern_match_score=max(0, min(pattern, 100)),
        complexity_score=max(0, min(complexity, 100)),
        performance_score=max(0, min(performance, 100)),
        cost_score=max(0, min(cost, 100)),
    )


# ==========================================================================
# Cost estimation
# ==========================================================================

# Aurora Serverless v2 pricing (us-east-1)
ACU_PRICE_PER_HOUR = 0.12
STORAGE_PRICE_PER_GB_MONTH = 0.10  # I/O-optimized

# Aurora Provisioned pricing (us-east-1)
INSTANCE_PRICES: dict[str, dict[str, float]] = {
    "db.r6g.large": {"postgresql": 0.24, "mysql": 0.22},
    "db.r6g.xlarge": {"postgresql": 0.48, "mysql": 0.44},
}


def _estimate_acu_from_load(total_cps: float, avg_query_count: int) -> tuple[float, float]:
    """Estimate min/max ACU from query load.

    Returns (min_acu, max_acu) tuple.
    """
    # Rough heuristic: 1 ACU handles ~50 simple queries/sec
    # Complex queries (joins, aggregations) use ~2-3x more ACU
    base_acu = total_cps / 50.0
    min_acu = max(0.5, base_acu * 0.5)  # Floor at 0.5 ACU
    max_acu = max(2.0, base_acu * 3.0)  # At least 2 ACU max
    return round(min_acu, 1), round(max_acu, 1)


def estimate_aurora_costs(
    collector_output: dict,
    target_region: str,
    analysis_options=None,
    engine: str = "postgresql",
) -> CostEstimate:
    """Estimate Aurora costs for both Serverless v2 and Provisioned configurations.

    Args:
        collector_output: Raw collector output dict.
        target_region: AWS region.
        analysis_options: Optional AnalysisOptions.
        engine: "postgresql" or "mysql".

    Returns:
        CostEstimate with both options shown in cost_components.
    """
    if analysis_options is not None and not analysis_options.perform_cost_estimation:
        return CostEstimate(
            monthly_cost_usd=0.0,
            cost_components={},
            pricing_assumptions=["Cost estimation disabled."],
        )

    tables = (collector_output.get("database_schema") or {}).get("tables") or []
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []

    total_size_gb = sum((t.get("size_mb") or 0.0) for t in tables) / 1024.0
    total_cps = sum(float(q.get("calls_per_second") or 0.0) for q in queries)
    query_count = len(queries)

    # --- Serverless v2 estimate ---
    min_acu, max_acu = _estimate_acu_from_load(total_cps, query_count)
    # Average ACU usage (assume 60% of max during peak, 30% during off-peak)
    avg_acu = (min_acu + max_acu) / 2.0
    serverless_compute_monthly = avg_acu * ACU_PRICE_PER_HOUR * 24 * 30
    serverless_storage_monthly = max(total_size_gb, 1.0) * STORAGE_PRICE_PER_GB_MONTH
    serverless_total = serverless_compute_monthly + serverless_storage_monthly

    # --- Provisioned estimate ---
    # Select instance size based on load
    if total_cps > 100 or total_size_gb > 50:
        instance_type = "db.r6g.xlarge"
    else:
        instance_type = "db.r6g.large"

    instance_price = INSTANCE_PRICES[instance_type][engine]
    provisioned_compute_monthly = instance_price * 24 * 30
    provisioned_storage_monthly = max(total_size_gb, 20.0) * STORAGE_PRICE_PER_GB_MONTH
    provisioned_total = provisioned_compute_monthly + provisioned_storage_monthly

    # Use the lower cost as the primary estimate
    monthly = min(serverless_total, provisioned_total)

    return CostEstimate(
        monthly_cost_usd=round(monthly, 2),
        cost_components={
            "serverless_v2": {
                "compute_monthly_usd": round(serverless_compute_monthly, 2),
                "storage_monthly_usd": round(serverless_storage_monthly, 2),
                "total_monthly_usd": round(serverless_total, 2),
                "min_acu": min_acu,
                "max_acu": max_acu,
                "avg_acu": round(avg_acu, 1),
            },
            "provisioned": {
                "compute_monthly_usd": round(provisioned_compute_monthly, 2),
                "storage_monthly_usd": round(provisioned_storage_monthly, 2),
                "total_monthly_usd": round(provisioned_total, 2),
                "instance_type": instance_type,
            },
            "recommended": (
                "serverless_v2" if serverless_total <= provisioned_total else "provisioned"
            ),
        },
        pricing_assumptions=[
            f"Aurora {engine.title()} — {target_region}",
            f"Serverless v2: ${ACU_PRICE_PER_HOUR}/ACU-hr, {min_acu}-{max_acu} ACU range",
            f"Provisioned: {instance_type} at ${instance_price}/hr",
            f"I/O-Optimized storage: ${STORAGE_PRICE_PER_GB_MONTH}/GB/month",
            f"Total data size: {total_size_gb:.2f} GB",
            f"Total query throughput: {total_cps:.1f} calls/second",
        ],
    )


# ==========================================================================
# Recommendation helpers
# ==========================================================================


def _count_inbound_fks(table_id: str, collector_output: dict) -> int:
    """Count how many other tables have FKs referencing this table."""
    tables = (collector_output.get("database_schema") or {}).get("tables") or []
    count = 0
    for t in tables:
        if t.get("table_id") == table_id:
            continue
        for fk in t.get("foreign_keys") or []:
            if fk.get("referenced_table") == table_id:
                count += 1
    return count


def compute_relational_need_score(
    fk_outbound_count: int,
    fk_inbound_count: int,
    join_query_count: int,
    multi_table_write_count: int,
    total_query_count: int,
) -> int:
    """Compute a graduated baseline reflecting how much a table NEEDS a relational engine.

    Replaces flat BASELINE_PATTERN_MATCH_FLOOR. Returns 15-65.
    """
    score = 20  # Minimum: Aurora can serve anything

    # FK outbound: this table depends on relational context (max +30)
    if fk_outbound_count >= 3:
        score += 30
    elif fk_outbound_count >= 1:
        score += 10

    # FK inbound: other tables reference this one — can't easily extract (max +25)
    if fk_inbound_count >= 3:
        score += 25
    elif fk_inbound_count >= 1:
        score += 8

    # Join participation ratio (max +20)
    if total_query_count > 0:
        join_ratio = join_query_count / total_query_count
        if join_ratio >= 0.5:
            score += 20
        elif join_ratio >= 0.2:
            score += 13
        elif join_query_count >= 1:
            score += 6

    # Multi-table write participation (max +15)
    if multi_table_write_count >= 3:
        score += 15
    elif multi_table_write_count >= 1:
        score += 7

    return min(score, 65)


def compute_catalog_pattern_score(
    table_patterns: list[Pattern],
    collector_output: dict,
    pattern_by_id: dict,
) -> int:
    """Compute pattern_match_score from catalog base scores weighted by query frequency."""
    if not table_patterns:
        return 0

    queries = collector_output.get("queries", {}).get("query_patterns", [])
    query_cps: dict[str, float] = {
        q.get("query_id", ""): float(q.get("calls_per_second") or 0) for q in queries
    }

    weighted_sum = 0.0
    total_weight = 0.0

    for p in table_patterns:
        catalog = pattern_by_id.get(p.pattern_id)
        if not catalog:
            continue
        pattern_cps = sum(query_cps.get(qid, 0) for qid in (p.query_ids or []))
        weight = max(pattern_cps, 0.01)
        weighted_sum += catalog.base_score * weight
        total_weight += weight

    if total_weight == 0:
        return 0
    return int(weighted_sum / total_weight)


def build_rationale(
    profile: TableProfile,
    table_patterns: list[Pattern],
    engine_label: str,
) -> str:
    """Build human-readable rationale for Aurora recommendation."""
    if not table_patterns:
        return (
            f"No strong relational patterns detected for {profile.table_id}. "
            f"This table may not benefit significantly from {engine_label}."
        )

    pattern_names = [p.pattern_type.replace("-", " ") for p in table_patterns]
    parts = [f"Detected relational patterns: {', '.join(pattern_names)}."]

    if profile.has_joins and profile.max_join_count >= 3:
        parts.append(
            f"Complex joins ({profile.max_join_count} tables) are a core {engine_label} strength."
        )
    if profile.foreign_key_count > 2:
        parts.append(
            f"High FK density ({profile.foreign_key_count} FKs) requires relational integrity enforcement."
        )
    if profile.total_calls_per_second >= 10:
        parts.append(
            f"Active workload ({profile.total_calls_per_second:.0f} calls/sec) "
            f"benefits from {engine_label} query optimizer."
        )

    return " ".join(parts)


def build_concerns(
    profile: TableProfile,
    table_anti_patterns: list[AntiPattern],
) -> list[str]:
    """Build list of concerns for the recommendation."""
    concerns = []
    for ap in table_anti_patterns:
        concerns.append(f"{ap.anti_pattern_type}: {ap.recommendation or ap.description}")

    if profile.total_calls_per_second > HIGH_FREQUENCY_PK_LOOKUP_CPS and not profile.has_joins:
        concerns.append(
            "Very high frequency simple lookups may benefit from a caching layer or DynamoDB."
        )

    return concerns


def assess_migration_complexity(
    profile: TableProfile,
    table_anti_patterns: list[AntiPattern],
) -> MigrationComplexity:
    """Assess migration complexity for Aurora.

    For Aurora targets, migration from an existing relational source is typically LOW
    since it's relational-to-relational. Complexity increases with anti-patterns.
    """
    anti_count = len(table_anti_patterns)

    if anti_count >= 2:
        return MigrationComplexity.HIGH
    if anti_count >= 1:
        return MigrationComplexity.MEDIUM
    return MigrationComplexity.LOW
