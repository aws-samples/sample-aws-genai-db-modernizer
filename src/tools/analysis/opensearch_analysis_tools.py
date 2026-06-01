"""
OpenSearch Analysis Tools

Pattern detection, workload classification, scoring, recommendation generation,
cost estimation, and decision trace for OpenSearch use case analysis.
Uses the specialist-curated pattern catalog for detection.

Implements Tasks 1-6:
- WorkloadType enum (SEARCH, TIMESERIES)
- analyze_opensearch_use_cases(collector_output) -> WorkloadAnalysis
- _build_pattern() helper
- Single-pass query scan for all 7 patterns + 3 anti-patterns
- classify_table_workload() + _is_timeseries()
- _compute_catalog_pattern_score(), _apply_opensearch_search_adjustments()
- _apply_opensearch_timeseries_adjustments()
- analyze_opensearch_patterns(), estimate_opensearch_costs()
- _build_rationale(), _build_concerns(), _assess_migration_complexity()
- build_opensearch_decision_trace()

Anti-patterns use severity_weight (float from catalog), NOT severity (enum).
"""

from __future__ import annotations

from enum import Enum

from src.contracts.analysis_output import (
    AntiPattern,
    Confidence,
    CostEstimate,
    MigrationComplexity,
    Pattern,
    RecommendationLevel,
    ScoreBreakdown,
    TableRecommendation,
    WorkloadAnalysis,
)
from src.tools.analysis.opensearch_pattern_catalog import (
    ANTI_PATTERN_BY_ID,
    FULLTEXT_KEYWORDS,
    FUZZY_KEYWORDS,
    HIGH_INGEST_CPS_THRESHOLD,
    OPENSEARCH_SEARCH_WEIGHTS,
    OPENSEARCH_TIMESERIES_WEIGHTS,
    PATTERN_BY_ID,
    REGEX_KEYWORDS,
    SEARCH_PATTERN_TYPES,
    STALENESS_TABLE_KEYWORDS,
    TIME_AGG_KEYWORDS,
    TIME_RANGE_KEYWORDS,
    TIMESERIES_PATTERN_TYPES,
    TIMESTAMP_KEYWORDS,
    WILDCARD_KEYWORDS,
)
from src.tools.analysis.scoring import (
    TableProfile,
    build_table_profiles,
    compute_base_scores,
    compute_confidence,
)

# ==========================================================================
# WorkloadType enum
# ==========================================================================


class WorkloadType(str, Enum):
    SEARCH = "SEARCH"
    TIMESERIES = "TIMESERIES"


# ==========================================================================
# Internal helpers
# ==========================================================================


def _build_pattern(
    pattern_id: str,
    confidence: Confidence,
    query_ids: list[str],
    table_ids: list[str],
    total_cps: float,
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


def _text_contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    """Case-insensitive substring check for any keyword in the tuple."""
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _has_timestamp_keyword(text: str) -> bool:
    return _text_contains_any(text, TIMESTAMP_KEYWORDS)


def _has_time_range_operator(text: str) -> bool:
    return _text_contains_any(text, TIME_RANGE_KEYWORDS)


def _table_name_is_staleness(table_id: str) -> bool:
    """Check if a table name contains staleness indicator keywords."""
    lower = table_id.lower()
    return any(kw in lower for kw in STALENESS_TABLE_KEYWORDS)


# ==========================================================================
# Main detection function
# ==========================================================================


def analyze_opensearch_use_cases(collector_output: dict) -> WorkloadAnalysis:
    """Single-pass scan of all queries producing WorkloadAnalysis.

    Detects all 7 patterns (os-01 through os-07) and 3 anti-patterns
    (os-ap-01 through os-ap-03) in a single pass over the query list.

    For high-ingest: INSERT CPS is accumulated per table; pattern is emitted
    for each table meeting the threshold.

    For audit-timestamp: timestamp equality vs range usage is tracked per table;
    anti-pattern emitted for tables with equality-only usage.

    Anti-patterns use severity_weight (float) from the catalog.
    """
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    total_query_count = len(queries)

    # --- Accumulators for patterns (query_ids, table_ids, cps) ---
    # Search patterns
    ft_query_ids: list[str] = []
    ft_table_ids: list[str] = []
    ft_cps: float = 0.0

    wc_query_ids: list[str] = []
    wc_table_ids: list[str] = []
    wc_cps: float = 0.0

    rx_query_ids: list[str] = []
    rx_table_ids: list[str] = []
    rx_cps: float = 0.0

    fz_query_ids: list[str] = []
    fz_table_ids: list[str] = []
    fz_cps: float = 0.0

    # Time-series patterns
    tr_query_ids: list[str] = []
    tr_table_ids: list[str] = []
    tr_cps: float = 0.0

    ta_query_ids: list[str] = []
    ta_table_ids: list[str] = []
    ta_cps: float = 0.0

    # High-ingest: per-table INSERT CPS accumulator
    # table_id -> {query_ids, total_cps}
    ingest_per_table: dict[str, dict] = {}

    # Anti-pattern accumulators
    # os-ap-01 multi-index-joins
    jn_query_ids: list[str] = []
    jn_table_ids: list[str] = []

    # os-ap-02 acid-transactions
    tx_query_ids: list[str] = []
    tx_table_ids: list[str] = []

    # os-ap-03 audit-columns-only
    # Per table: track whether we've seen timestamp in range context vs equality only
    # {table_id: {"has_range": bool, "has_equality_only": bool, "query_ids": list}}
    ts_usage: dict[str, dict] = {}

    # --- Single pass ---
    for q in queries:
        qid = q.get("query_id", "")
        qt = (q.get("query_type") or "").upper()
        text = q.get("query_text") or ""
        text_lower = text.lower()
        tables = q.get("tables_accessed") or []
        cps = float(q.get("calls_per_second") or 0.0)
        has_joins = bool(q.get("has_joins", False))
        join_count = int(q.get("join_count") or 0)

        # ---- Search pattern detection (uses `if`, not `elif`) ----

        # os-01 full-text-search
        if _text_contains_any(text_lower, FULLTEXT_KEYWORDS):
            ft_query_ids.append(qid)
            ft_table_ids.extend(tables)
            ft_cps += cps

        # os-02 wildcard-search
        if _text_contains_any(text_lower, WILDCARD_KEYWORDS):
            wc_query_ids.append(qid)
            wc_table_ids.extend(tables)
            wc_cps += cps

        # os-03 regex-search
        if _text_contains_any(text_lower, REGEX_KEYWORDS):
            rx_query_ids.append(qid)
            rx_table_ids.extend(tables)
            rx_cps += cps

        # os-04 fuzzy-search
        if _text_contains_any(text_lower, FUZZY_KEYWORDS):
            fz_query_ids.append(qid)
            fz_table_ids.extend(tables)
            fz_cps += cps

        # ---- Time-series pattern detection ----

        # os-05 time-range-query: timestamp keyword + range operator
        if _has_timestamp_keyword(text_lower) and _has_time_range_operator(text_lower):
            tr_query_ids.append(qid)
            tr_table_ids.extend(tables)
            tr_cps += cps

        # os-06 time-aggregation: time function keywords
        if _text_contains_any(text_lower, TIME_AGG_KEYWORDS):
            ta_query_ids.append(qid)
            ta_table_ids.extend(tables)
            ta_cps += cps

        # os-07 high-ingest: accumulate INSERT CPS per table
        if qt == "INSERT":
            for tid in tables:
                if tid not in ingest_per_table:
                    ingest_per_table[tid] = {"query_ids": [], "total_cps": 0.0}
                ingest_per_table[tid]["query_ids"].append(qid)
                ingest_per_table[tid]["total_cps"] += cps

        # ---- Anti-pattern detection ----

        # os-ap-01 multi-index-joins: has_joins AND join_count >= 2
        if has_joins and join_count >= 2:
            jn_query_ids.append(qid)
            jn_table_ids.extend(tables)

        # os-ap-02 acid-transactions: UPDATE or DELETE at CPS > 1
        if qt in {"UPDATE", "DELETE"} and cps > 1.0:
            tx_query_ids.append(qid)
            tx_table_ids.extend(tables)

        # os-ap-03 audit-columns-only tracking
        # Detect timestamp keyword in query text; classify as range or equality
        if _has_timestamp_keyword(text_lower):
            has_range = _has_time_range_operator(text_lower)
            for tid in tables:
                if tid not in ts_usage:
                    ts_usage[tid] = {"has_range": False, "query_ids": []}
                ts_usage[tid]["query_ids"].append(qid)
                if has_range:
                    ts_usage[tid]["has_range"] = True

    # ==========================================================================
    # Assemble detected patterns
    # ==========================================================================
    patterns: list[Pattern] = []

    if ft_query_ids:
        patterns.append(
            _build_pattern(
                "os-01", Confidence.HIGH, ft_query_ids, ft_table_ids, ft_cps, total_query_count
            )
        )

    if wc_query_ids:
        patterns.append(
            _build_pattern(
                "os-02", Confidence.MEDIUM, wc_query_ids, wc_table_ids, wc_cps, total_query_count
            )
        )

    if rx_query_ids:
        patterns.append(
            _build_pattern(
                "os-03", Confidence.MEDIUM, rx_query_ids, rx_table_ids, rx_cps, total_query_count
            )
        )

    if fz_query_ids:
        patterns.append(
            _build_pattern(
                "os-04", Confidence.HIGH, fz_query_ids, fz_table_ids, fz_cps, total_query_count
            )
        )

    if tr_query_ids:
        patterns.append(
            _build_pattern(
                "os-05", Confidence.MEDIUM, tr_query_ids, tr_table_ids, tr_cps, total_query_count
            )
        )

    if ta_query_ids:
        patterns.append(
            _build_pattern(
                "os-06", Confidence.HIGH, ta_query_ids, ta_table_ids, ta_cps, total_query_count
            )
        )

    # os-07 high-ingest: emit one pattern per qualifying table
    for tid, data in ingest_per_table.items():
        if data["total_cps"] >= HIGH_INGEST_CPS_THRESHOLD:
            catalog = PATTERN_BY_ID["os-07"]
            frequency_pct = (
                round(len(data["query_ids"]) / total_query_count * 100.0, 2)
                if total_query_count > 0
                else None
            )
            patterns.append(
                Pattern(
                    pattern_id="os-07",
                    pattern_type=catalog.pattern_type,
                    confidence=Confidence.HIGH,
                    description=catalog.description,
                    query_ids=data["query_ids"],
                    table_ids=[tid],
                    frequency_percent=frequency_pct,
                )
            )

    # ==========================================================================
    # Assemble detected anti-patterns
    # ==========================================================================
    anti_patterns: list[AntiPattern] = []

    if jn_query_ids:
        anti_patterns.append(_build_anti_pattern("os-ap-01", jn_query_ids, jn_table_ids))

    if tx_query_ids:
        anti_patterns.append(_build_anti_pattern("os-ap-02", tx_query_ids, tx_table_ids))

    # os-ap-03 audit-columns-only: tables with timestamp usage but NO range operators
    at_query_ids: list[str] = []
    at_table_ids: list[str] = []
    for tid, data in ts_usage.items():
        if not data["has_range"]:
            # Timestamp used only in equality contexts for this table
            at_query_ids.extend(data["query_ids"])
            at_table_ids.append(tid)

    if at_query_ids:
        anti_patterns.append(_build_anti_pattern("os-ap-03", at_query_ids, at_table_ids))

    return WorkloadAnalysis(
        patterns_detected=patterns,
        anti_patterns_detected=anti_patterns if anti_patterns else None,
    )


# ==========================================================================
# Task 3: Workload classification
# ==========================================================================


def _is_timeseries(
    table_id: str,
    table_patterns: list[Pattern],
    table_schema: dict,
    table_queries: list[dict],
) -> bool:
    """Return True if ALL three time-series criteria are met for this table.

    Criteria (ALL required):
    1. Timestamp range patterns present (time-range-query or time-aggregation).
    2. INSERT CPS >= 10 with INSERT_CPS:UPDATE_DELETE_CPS >= 5:1.
    3. Staleness indicators in table or column names.
    """
    # --- Criterion 1: timestamp range patterns ---
    ts_pattern_types = {p.pattern_type for p in table_patterns}
    has_timestamp_patterns = bool(
        ts_pattern_types & {"time-range-query", "time-aggregation", "high-ingest"}
    )
    if not has_timestamp_patterns:
        return False

    # --- Criterion 2: INSERT CPS >= 10, INSERT:UPDATE+DELETE >= 5:1 ---
    insert_cps = 0.0
    update_delete_cps = 0.0
    for q in table_queries:
        qt = (q.get("query_type") or "").upper()
        cps = float(q.get("calls_per_second") or 0.0)
        if qt == "INSERT":
            insert_cps += cps
        elif qt in {"UPDATE", "DELETE"}:
            update_delete_cps += cps

    if insert_cps < HIGH_INGEST_CPS_THRESHOLD:
        return False

    # Ratio check: INSERT:UPDATE_DELETE >= 5:1
    # If no UPDATE/DELETE, ratio is infinite — passes automatically
    if update_delete_cps > 0 and (insert_cps / update_delete_cps) < 5.0:
        return False

    # --- Criterion 3: staleness indicators in table or column names ---
    table_name = table_schema.get("table_name") or table_id
    has_staleness = _table_name_is_staleness(table_name) or _table_name_is_staleness(table_id)
    if not has_staleness:
        # Check column names too
        for col in table_schema.get("columns") or []:
            col_name = col.get("column_name") or ""
            if any(kw in col_name.lower() for kw in STALENESS_TABLE_KEYWORDS):
                has_staleness = True
                break

    return has_staleness


def classify_table_workload(
    table_id: str,
    workload_analysis: WorkloadAnalysis,
    table_schema: dict,
    queries: list[dict],
) -> WorkloadType | None:
    """Classify a table as TIMESERIES, SEARCH, or None (not suitable).

    Time-series is checked first (stricter ALL criteria).
    Search is a broader net (ANY criterion met).
    Returns None when neither classification applies.
    """
    # Patterns for this specific table
    table_patterns = [
        p for p in workload_analysis.patterns_detected if table_id in (p.table_ids or [])
    ]
    pattern_types = {p.pattern_type for p in table_patterns}

    # Queries touching this table
    table_queries = [q for q in queries if table_id in (q.get("tables_accessed") or [])]

    # --- Time-series first (ALL criteria must be met) ---
    if _is_timeseries(table_id, table_patterns, table_schema, table_queries):
        return WorkloadType.TIMESERIES

    # --- Search: ANY search pattern type detected ---
    if pattern_types & SEARCH_PATTERN_TYPES:
        return WorkloadType.SEARCH

    return None


# ==========================================================================
# Task 4: Scoring pipeline + recommendations
# ==========================================================================


def _compute_catalog_pattern_score(
    table_patterns: list[Pattern],
    collector_output: dict,
) -> int:
    """Compute pattern_match_score from catalog base scores weighted by query frequency.

    For each pattern matching this table, look up the catalog base_score and
    weight it by the total calls_per_second of the matched queries.
    """
    if not table_patterns:
        return 0

    queries = collector_output.get("queries", {}).get("query_patterns", [])
    query_cps: dict[str, float] = {
        q.get("query_id", ""): float(q.get("calls_per_second") or 0) for q in queries
    }

    weighted_sum = 0.0
    total_weight = 0.0

    for p in table_patterns:
        catalog = PATTERN_BY_ID.get(p.pattern_id)
        if not catalog:
            continue
        # Weight = total CPS of queries in this pattern for this table
        pattern_cps = sum(query_cps.get(qid, 0) for qid in (p.query_ids or []))
        weight = max(pattern_cps, 0.01)  # Minimum so zero-traffic patterns still count
        weighted_sum += catalog.base_score * weight
        total_weight += weight

    if total_weight == 0:
        return 0
    return int(weighted_sum / total_weight)


_TEXT_DATA_TYPES: frozenset[str] = frozenset(
    {"text", "varchar", "character varying", "longtext", "clob", "nvarchar"}
)


def _compute_text_ratio(table_schema: dict) -> float:
    """Compute fraction of columns with text-like data types."""
    columns = table_schema.get("columns") or []
    if not columns:
        return 0.0
    text_count = sum(
        1
        for col in columns
        if any(tt in (col.get("data_type") or "").lower() for tt in _TEXT_DATA_TYPES)
    )
    return text_count / len(columns)


def _apply_opensearch_search_adjustments(
    scores: ScoreBreakdown,
    profile: TableProfile,
    table_schema: dict,
) -> ScoreBreakdown:
    """Apply search-workload-specific scoring adjustments."""
    pattern = scores.pattern_match_score
    complexity = scores.complexity_score
    performance = scores.performance_score
    cost = scores.cost_score

    pattern_types = set(profile.pattern_types)

    # pattern_match bonuses
    if "full-text-search" in pattern_types:
        pattern += 15
    if "wildcard-search" in pattern_types or "fuzzy-search" in pattern_types:
        pattern += 10
    if "regex-search" in pattern_types:
        pattern += 5

    # complexity: text ratio >= 0.3 → +15
    text_ratio = _compute_text_ratio(table_schema)
    if text_ratio >= 0.3:
        complexity += 15

    # complexity: no foreign keys → +10
    if profile.foreign_key_count == 0:
        complexity += 10

    # performance: search latency >= 10ms → +10
    if profile.avg_execution_time_ms >= 10.0:
        performance += 10

    # cost: size > 10GB → -10
    if profile.size_mb > 10_000:
        cost -= 10

    return ScoreBreakdown(
        pattern_match_score=max(0, min(pattern, 100)),
        complexity_score=max(0, min(complexity, 100)),
        performance_score=max(0, min(performance, 100)),
        cost_score=max(0, min(cost, 100)),
    )


def _apply_opensearch_timeseries_adjustments(
    scores: ScoreBreakdown,
    profile: TableProfile,
) -> ScoreBreakdown:
    """Apply time-series-workload-specific scoring adjustments."""
    pattern = scores.pattern_match_score
    complexity = scores.complexity_score
    performance = scores.performance_score
    cost = scores.cost_score

    pattern_types = set(profile.pattern_types)

    # pattern_match bonuses
    if "time-aggregation" in pattern_types:
        pattern += 15
    if "time-range-query" in pattern_types:
        pattern += 10
    if "high-ingest" in pattern_types:
        pattern += 15

    # complexity: write ratio >= 0.7 (read_ratio <= 0.3) → +15
    write_ratio = 1.0 - profile.read_ratio
    if write_ratio >= 0.7:
        complexity += 15

    # complexity: no foreign keys → +10
    if profile.foreign_key_count == 0:
        complexity += 10

    # performance: CPS >= 10 → +15
    if profile.total_calls_per_second >= 10.0:
        performance += 15

    # performance: aggregation latency >= 50ms → +10
    if profile.avg_execution_time_ms >= 50.0:
        performance += 10

    # cost: size > 1GB → +10
    if profile.size_mb > 1_000:
        cost += 10
    # cost: size < 10MB → -10
    elif profile.size_mb < 10:
        cost -= 10

    return ScoreBreakdown(
        pattern_match_score=max(0, min(pattern, 100)),
        complexity_score=max(0, min(complexity, 100)),
        performance_score=max(0, min(performance, 100)),
        cost_score=max(0, min(cost, 100)),
    )


def _build_rationale(
    profile: TableProfile,
    table_patterns: list[Pattern],
    workload_type: WorkloadType | None,
) -> str:
    """Build human-readable rationale with workload prefix."""
    if workload_type is None:
        return (
            f"[NOT_SUITABLE] No OpenSearch patterns detected for {profile.table_id}. "
            "This table does not exhibit search or time-series access patterns."
        )

    prefix = f"[{workload_type.value}]"
    if not table_patterns:
        return f"{prefix} No strong patterns detected for {profile.table_id}."

    pattern_names = [p.pattern_type.replace("-", " ") for p in table_patterns]
    parts = [f"{prefix} Detected patterns: {', '.join(pattern_names)}."]

    if workload_type == WorkloadType.SEARCH:
        if profile.total_calls_per_second >= 10:
            parts.append(
                f"High query throughput ({profile.total_calls_per_second:.0f} calls/sec) "
                "suits OpenSearch search scalability."
            )
    elif workload_type == WorkloadType.TIMESERIES:
        write_ratio = 1.0 - profile.read_ratio
        if write_ratio >= 0.7:
            parts.append("Append-heavy write pattern suits OpenSearch data streams.")
        if profile.total_calls_per_second >= 10:
            parts.append(
                f"High ingest throughput ({profile.total_calls_per_second:.0f} calls/sec) "
                "matches OpenSearch bulk indexing."
            )

    return " ".join(parts)


def _build_concerns(
    profile: TableProfile,
    table_anti_patterns: list[AntiPattern],
    workload_type: WorkloadType | None,
) -> list[str]:
    """Build list of concerns for the recommendation."""
    concerns = []
    for ap in table_anti_patterns:
        concerns.append(f"{ap.anti_pattern_type}: {ap.recommendation or ap.description}")

    if profile.foreign_key_count > 0:
        concerns.append(
            f"{profile.foreign_key_count} foreign key(s) — OpenSearch does not support "
            "cross-index joins; denormalization required."
        )
    if profile.has_joins and profile.max_join_count >= 2:
        concerns.append(
            "Complex joins detected — must be handled in application layer or denormalized."
        )
    if workload_type == WorkloadType.TIMESERIES:
        concerns.append(
            "Index lifecycle management (ILM) policy required for data retention and rollover."
        )

    return concerns


def _assess_migration_complexity(
    profile: TableProfile,
    table_anti_patterns: list[AntiPattern],
    workload_type: WorkloadType | None,
) -> MigrationComplexity:
    """Assess migration complexity, workload-aware."""
    anti_pattern_count = len(table_anti_patterns)

    if anti_pattern_count >= 2 or profile.foreign_key_count > 3:
        return MigrationComplexity.HIGH

    if anti_pattern_count >= 1 or profile.foreign_key_count > 0 or profile.has_joins:
        return MigrationComplexity.MEDIUM

    return MigrationComplexity.LOW


def _confidence_to_recommendation(confidence: int) -> str:
    """Map confidence score to recommendation label."""
    if confidence >= 75:
        return RecommendationLevel.HIGHLY_SUITABLE.value
    if confidence >= 50:
        return RecommendationLevel.SUITABLE.value
    if confidence >= 25:
        return RecommendationLevel.MARGINAL.value
    return RecommendationLevel.NOT_SUITABLE.value


def analyze_opensearch_patterns(
    collector_output: dict,
    workload_analysis: WorkloadAnalysis,
) -> tuple[list[TableRecommendation], dict[str, WorkloadType | None], dict[str, dict[str, float]]]:
    """Produce per-table recommendations using workload classification + catalog scoring.

    Returns:
        (table_recommendations, classifications, weights_used)
        - classifications: {table_id -> WorkloadType | None}
        - weights_used: {table_id -> weight dict used for confidence}
    """
    tables = (collector_output.get("database_schema") or {}).get("tables") or []
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []

    # Build table_id -> table_dict lookup for schema access
    table_schema_lookup: dict[str, dict] = {t["table_id"]: t for t in tables if t.get("table_id")}

    profiles = build_table_profiles(collector_output, workload_analysis)

    # per-table pattern/anti-pattern lists
    table_patterns_map: dict[str, list[Pattern]] = {}
    for p in workload_analysis.patterns_detected:
        for tid in p.table_ids or []:
            table_patterns_map.setdefault(tid, []).append(p)

    table_anti_patterns_map: dict[str, list[AntiPattern]] = {}
    for ap in workload_analysis.anti_patterns_detected or []:
        for tid in ap.table_ids or []:
            table_anti_patterns_map.setdefault(tid, []).append(ap)

    recommendations: list[TableRecommendation] = []
    classifications: dict[str, WorkloadType | None] = {}
    weights_used: dict[str, dict[str, float]] = {}

    for table_id, profile in profiles.items():
        table_schema = table_schema_lookup.get(table_id, {})
        tp = table_patterns_map.get(table_id, [])
        tap = table_anti_patterns_map.get(table_id, [])

        workload_type = classify_table_workload(table_id, workload_analysis, table_schema, queries)
        classifications[table_id] = workload_type

        if workload_type is None:
            # NOT_SUITABLE: zero all scores
            scores = ScoreBreakdown(
                pattern_match_score=0,
                complexity_score=0,
                performance_score=0,
                cost_score=0,
            )
            confidence = 0
            w = OPENSEARCH_SEARCH_WEIGHTS  # fallback, doesn't matter since all zeros
        else:
            # Catalog-driven pattern_match override
            catalog_score = _compute_catalog_pattern_score(tp, collector_output)
            base_scores = compute_base_scores(profile)
            scores = ScoreBreakdown(
                pattern_match_score=max(0, min(catalog_score, 100)),
                complexity_score=base_scores.complexity_score,
                performance_score=base_scores.performance_score,
                cost_score=base_scores.cost_score,
            )

            if workload_type == WorkloadType.SEARCH:
                scores = _apply_opensearch_search_adjustments(scores, profile, table_schema)
                w = OPENSEARCH_SEARCH_WEIGHTS
            else:  # TIMESERIES
                scores = _apply_opensearch_timeseries_adjustments(scores, profile)
                w = OPENSEARCH_TIMESERIES_WEIGHTS

            confidence = compute_confidence(scores, weights=w)

        weights_used[table_id] = w

        recommendations.append(
            TableRecommendation(
                table_id=table_id,
                confidence_score=confidence,
                rationale=_build_rationale(profile, tp, workload_type),
                score_breakdown=scores,
                supporting_patterns=[p.pattern_id for p in tp] if tp else None,
                concerns=_build_concerns(profile, tap, workload_type) or None,
                migration_complexity=_assess_migration_complexity(profile, tap, workload_type),
            )
        )

    return recommendations, classifications, weights_used


def estimate_opensearch_costs(
    collector_output: dict,
    target_region: str,
    analysis_options=None,
) -> CostEstimate:
    """Simplified OpenSearch cost estimation.

    Short-circuits with a zero estimate when perform_cost_estimation is False.
    """
    if analysis_options is not None and not analysis_options.perform_cost_estimation:
        return CostEstimate(
            monthly_cost_usd=0.0,
            cost_components={},
            pricing_assumptions=["Cost estimation disabled."],
        )

    tables = (collector_output.get("database_schema") or {}).get("tables") or []

    total_size_gb = sum((t.get("size_mb") or 0.0) for t in tables) / 1024.0

    # r6g.large.search: ~$0.167/hr, 2 data nodes, gp3 EBS: $0.024/GB/month
    instance_cost = 2 * 0.167 * 24 * 30  # 2 nodes × hourly × hours/day × days/month
    storage_cost = max(total_size_gb * 1.2, 20.0) * 0.024  # 1.2x overhead, min 20GB, gp3 rate

    monthly = instance_cost + storage_cost

    return CostEstimate(
        monthly_cost_usd=round(monthly, 2),
        cost_components={
            "instance_cost_usd": round(instance_cost, 2),
            "storage_cost_usd": round(storage_cost, 2),
            "instance_type": "r6g.large.search",
            "data_nodes": 2,
            "storage_gb": round(max(total_size_gb * 1.2, 20.0), 2),
        },
        pricing_assumptions=[
            "2 r6g.large.search data nodes ($0.167/hr each)",
            "gp3 EBS storage at $0.024/GB/month",
            "20% storage overhead for OpenSearch index structures",
            "Minimum 20 GB storage assumed",
            f"Region: {target_region}",
        ],
    )


# ==========================================================================
# Task 5: Decision trace
# ==========================================================================


def build_opensearch_decision_trace(
    collector_output: dict,
    workload_analysis: WorkloadAnalysis,
    table_recommendations: list[TableRecommendation],
    classifications: dict[str, WorkloadType | None],
    weights_used: dict[str, dict[str, float]],
) -> dict:
    """Build the OpenSearch decision trace artifact for specialist calibration.

    This is a separate S3 artifact — not part of the agent-to-agent contract.
    Includes workload_classifications, signals in query_matches, and
    final_recommendation in derivations (OpenSearch-specific extensions).
    """
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []

    # Build per-query match lookup
    query_pattern_map: dict[str, list[str]] = {}
    query_anti_pattern_map: dict[str, list[str]] = {}
    for p in workload_analysis.patterns_detected:
        for qid in p.query_ids or []:
            query_pattern_map.setdefault(qid, []).append(p.pattern_id)
    for ap in workload_analysis.anti_patterns_detected or []:
        for qid in ap.query_ids or []:
            query_anti_pattern_map.setdefault(qid, []).append(ap.anti_pattern_id)

    matched_qids = set(query_pattern_map.keys()) | set(query_anti_pattern_map.keys())

    # Build signals for each query based on matched patterns
    def _signals_for_query(qid: str, q: dict) -> list[str]:
        signals = []
        matched_pids = query_pattern_map.get(qid, [])
        text = (q.get("query_text") or "").lower()
        for pid in matched_pids:
            catalog = PATTERN_BY_ID.get(pid)
            if catalog:
                signals.append(catalog.pattern_type.replace("-", "_"))
        # Add structural signals
        if q.get("has_joins") and (q.get("join_count") or 0) >= 2:
            signals.append("multi_table_join")
        if _text_contains_any(text, TIMESTAMP_KEYWORDS) and _has_time_range_operator(text):
            signals.append("timestamp_range_filter")
        return signals

    # Per-query matches
    query_matches = []
    for q in queries:
        qid = q.get("query_id", "")
        query_matches.append(
            {
                "query_id": qid,
                "query_text_preview": (q.get("query_text") or "")[:120],
                "matched_patterns": query_pattern_map.get(qid, []),
                "matched_anti_patterns": query_anti_pattern_map.get(qid, []),
                "signals": _signals_for_query(qid, q),
            }
        )

    # Per-pattern summaries (include total CPS from queries)
    query_cps: dict[str, float] = {
        q.get("query_id", ""): float(q.get("calls_per_second") or 0) for q in queries
    }
    pattern_summaries = []
    for p in workload_analysis.patterns_detected:
        catalog = PATTERN_BY_ID.get(p.pattern_id)
        total_cps = sum(query_cps.get(qid, 0) for qid in (p.query_ids or []))
        pattern_summaries.append(
            {
                "pattern_id": p.pattern_id,
                "pattern_type": p.pattern_type,
                "catalog_base_score": catalog.base_score if catalog else None,
                "queries_matched_count": len(p.query_ids or []),
                "tables_involved": p.table_ids or [],
                "total_calls_per_second": round(total_cps, 3),
            }
        )

    # Workload classification section (OpenSearch-specific)
    workload_classifications = []
    for table_id, wtype in classifications.items():
        # Determine search pattern count
        search_count = sum(
            1
            for p in workload_analysis.patterns_detected
            if table_id in (p.table_ids or []) and p.pattern_type in SEARCH_PATTERN_TYPES
        )

        # Determine which time-series criteria passed
        tables_list = (collector_output.get("database_schema") or {}).get("tables") or []
        table_schema: dict = next((t for t in tables_list if t.get("table_id") == table_id), {})
        all_queries = (collector_output.get("queries") or {}).get("query_patterns") or []
        table_queries = [q for q in all_queries if table_id in (q.get("tables_accessed") or [])]
        table_patterns_for_tid = [
            p for p in workload_analysis.patterns_detected if table_id in (p.table_ids or [])
        ]

        # Evaluate each sub-criterion for transparency
        ts_pattern_types = {p.pattern_type for p in table_patterns_for_tid}
        has_ts_patterns = bool(
            ts_pattern_types & {"time-range-query", "time-aggregation", "high-ingest"}
        )

        insert_cps = sum(
            float(q.get("calls_per_second") or 0)
            for q in table_queries
            if (q.get("query_type") or "").upper() == "INSERT"
        )
        has_high_ingest = insert_cps >= HIGH_INGEST_CPS_THRESHOLD

        table_name = table_schema.get("table_name") or table_id
        has_staleness = _table_name_is_staleness(table_name) or _table_name_is_staleness(table_id)
        if not has_staleness:
            for col in table_schema.get("columns") or []:
                if any(
                    kw in (col.get("column_name") or "").lower() for kw in STALENESS_TABLE_KEYWORDS
                ):
                    has_staleness = True
                    break

        if wtype is not None:
            reason = f"Matched {wtype.value.lower()} patterns: {', '.join(ts_pattern_types & (SEARCH_PATTERN_TYPES | TIMESERIES_PATTERN_TYPES))}"
        else:
            reason = "No search or time-series patterns detected — NOT_SUITABLE"

        workload_classifications.append(
            {
                "table_id": table_id,
                "workload_type": wtype.value if wtype is not None else "NOT_SUITABLE",
                "reason": reason,
                "search_pattern_count": search_count,
                "timeseries_criteria_met": {
                    "timestamp_range": has_ts_patterns,
                    "high_ingest": has_high_ingest,
                    "staleness": has_staleness,
                },
            }
        )

    # Per-table recommendation derivations
    derivations = []
    for rec in table_recommendations:
        w = weights_used.get(rec.table_id, OPENSEARCH_SEARCH_WEIGHTS)
        derivations.append(
            {
                "table_id": rec.table_id,
                "workload_type": (
                    wt.value
                    if (wt := classifications.get(rec.table_id)) is not None
                    else "NOT_SUITABLE"
                ),
                "score_breakdown": {
                    "pattern_match": rec.score_breakdown.pattern_match_score,
                    "complexity": rec.score_breakdown.complexity_score,
                    "performance": rec.score_breakdown.performance_score,
                    "cost": rec.score_breakdown.cost_score,
                },
                "weights_used": w,
                "weighted_confidence": rec.confidence_score,
                "final_recommendation": _confidence_to_recommendation(rec.confidence_score),
            }
        )

    # Summary counts
    tables_search = sum(1 for wt in classifications.values() if wt == WorkloadType.SEARCH)
    tables_timeseries = sum(1 for wt in classifications.values() if wt == WorkloadType.TIMESERIES)
    tables_not_suitable = sum(1 for wt in classifications.values() if wt is None)

    return {
        "trace_version": "1.0",
        "agent": "opensearch-analysis-agent",
        "summary": {
            "queries_analyzed": len(queries),
            "queries_matched": len(matched_qids),
            "queries_unmatched": len(queries) - len(matched_qids),
            "patterns_detected": len(workload_analysis.patterns_detected),
            "anti_patterns_detected": len(workload_analysis.anti_patterns_detected or []),
            "tables_search": tables_search,
            "tables_timeseries": tables_timeseries,
            "tables_not_suitable": tables_not_suitable,
        },
        "query_matches": query_matches,
        "pattern_summaries": pattern_summaries,
        "workload_classifications": workload_classifications,
        "recommendation_derivations": derivations,
    }
