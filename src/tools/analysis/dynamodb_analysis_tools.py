"""
DynamoDB Analysis Tools

Pattern detection, scoring adjustments, and cost estimation for DynamoDB.
Uses the specialist-curated pattern catalog for detection and scoring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from pathlib import Path

from pydantic import BaseModel, Field

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
from src.tools.analysis.dynamodb_pattern_catalog import (
    DYNAMODB_ANTI_PATTERNS,
    DYNAMODB_PATTERNS,
    DYNAMODB_SCORE_WEIGHTS,
    PATTERN_BY_ID,
    CatalogAntiPattern,
    CatalogPattern,
)
from src.tools.analysis.llm_advisor_base import LlmAdvisorBase
from src.tools.analysis.scoring import (
    TableProfile,
    build_table_profiles,
    compute_base_scores,
    compute_confidence,
)

# ---------------------------------------------------------------------------
# LLM Advisor Pydantic models (structured LLM response parsing only)
# ---------------------------------------------------------------------------


class AggregateKeyDesign(BaseModel):
    """Recommended DynamoDB key design for an aggregate."""

    aggregate_id: str
    partition_key: str
    sort_key: str | None = None
    rationale: str
    supporting_access_patterns: list[str] = Field(
        default_factory=list,
        description="query_ids from the input that justify this key design",
    )


class DenormStrategy(BaseModel):
    """Denormalization strategy recommendation for a detected opportunity."""

    opportunity_id: str  # references DenormalizationOpportunity
    strategy: str  # e.g., "embed child items", "adjacency list with GSI"
    rationale: str
    supporting_access_patterns: list[str] = Field(
        default_factory=list,
        description="query_ids from the input that justify this strategy",
    )


class LlmAdvisorOutput(BaseModel):
    """Structured output from the LLM advisor one-shot call."""

    aggregate_recommendations: list[AggregateKeyDesign]
    denormalization_strategies: list[DenormStrategy]


# ---------------------------------------------------------------------------
# Skill file loading
# ---------------------------------------------------------------------------

_SKILL_FILE = Path(__file__).resolve().parents[2] / "skills" / "dynamodb-analysis-advisor.md"


def load_advisor_prompt(skill_path: Path | None = None) -> str:
    """Load the DynamoDB analysis advisor system prompt from its skill file."""
    path = skill_path or _SKILL_FILE
    if not path.exists():
        raise FileNotFoundError(f"DynamoDB analysis advisor skill file not found: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# LLM Advisor class
# ---------------------------------------------------------------------------


class LlmAdvisor(LlmAdvisorBase):
    """DynamoDB-specific LLM advisor using the generic base class.

    Produces key design recommendations and denormalization strategies.
    For large workloads (> 30 queries), automatically splits into groups.
    """

    def __init__(
        self,
        system_prompt: str | None = None,
        enabled: bool | None = None,
    ):
        prompt = system_prompt if system_prompt is not None else load_advisor_prompt()
        super().__init__(system_prompt=prompt, enabled=enabled)

    def _output_model(self) -> type[BaseModel]:
        return LlmAdvisorOutput

    def _build_prompt(self, schema: dict, queries: list[dict], **kwargs) -> str:
        from dataclasses import asdict

        deterministic_results = kwargs.get("deterministic_results", {})
        aggregates = kwargs.get("aggregates", [])
        denorm_opportunities = kwargs.get("denorm_opportunities", [])

        agg_data = []
        for a in aggregates:
            if hasattr(a, "__dataclass_fields__"):
                agg_data.append(asdict(a))
            elif isinstance(a, dict):
                agg_data.append(a)

        denorm_data = []
        for d in denorm_opportunities:
            if hasattr(d, "__dataclass_fields__"):
                denorm_data.append(asdict(d))
            elif isinstance(d, dict):
                denorm_data.append(d)

        return (
            "Analyze the following database workload and produce DynamoDB key design "
            "recommendations and denormalization strategies.\n\n"
            f"## Deterministic Analysis Results\n{deterministic_results}\n\n"
            f"## Database Schema\n{schema}\n\n"
            f"## Query Patterns\n{queries}\n\n"
            f"## Identified Aggregates\n{agg_data}\n\n"
            f"## Denormalization Opportunities\n{denorm_data}\n\n"
            "Produce a key design (partition_key, optional sort_key, rationale) for each "
            "aggregate, and a denormalization strategy for each opportunity."
        )

    def _parse_result(self, result) -> LlmAdvisorOutput | None:
        output = getattr(result, "structured_output", None)
        if isinstance(output, LlmAdvisorOutput):
            return output
        return None

    def _merge_results(self, results: list) -> LlmAdvisorOutput | None:
        all_agg: list = []
        all_denorm: list = []
        for r in results:
            all_agg.extend(r.aggregate_recommendations)
            all_denorm.extend(r.denormalization_strategies)
        if not all_agg and not all_denorm:
            return None
        return LlmAdvisorOutput(
            aggregate_recommendations=all_agg,
            denormalization_strategies=all_denorm,
        )

    def _filter_kwargs_for_group(
        self, group_queries: list[dict], referenced_tables: set[str], **kwargs
    ) -> dict:
        from dataclasses import asdict

        aggregates = kwargs.get("aggregates", [])
        # Filter aggregates to those overlapping this group's tables
        filtered_agg = []
        for a in aggregates:
            if hasattr(a, "__dataclass_fields__"):
                agg_dict = asdict(a)
            elif isinstance(a, dict):
                agg_dict = a
            else:
                filtered_agg.append(a)
                continue
            member_tables = agg_dict.get("member_tables", [])
            if not member_tables or set(member_tables) & referenced_tables:
                filtered_agg.append(a)

        return {
            "deterministic_results": kwargs.get("deterministic_results", {}),
            "aggregates": filtered_agg,
            "denorm_opportunities": kwargs.get("denorm_opportunities", []),
        }

    def advise(  # type: ignore[override]
        self,
        deterministic_results: dict,
        schema: dict,
        queries: list[dict],
        aggregates: list,
        denorm_opportunities: list,
        timeout_seconds: int = 60,
    ) -> LlmAdvisorOutput | None:
        """Run LLM advisor. Delegates to base class with kwargs."""
        result = super().advise(
            schema=schema,
            queries=queries,
            deterministic_results=deterministic_results,
            aggregates=aggregates,
            denorm_opportunities=denorm_opportunities,
        )
        return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

_TIMESTAMP_KEYWORDS = re.compile(
    r"\b(created_at|updated_at|timestamp|date|time|expires?_at|ttl|last_\w+_at)\b", re.I
)
_SESSION_KEYWORDS = re.compile(r"\b(session|token|sess_id|session_id|login|auth)\b", re.I)
_AGGREGATION_RE = re.compile(r"\b(group\s+by|having|count\s*\(|sum\s*\(|avg\s*\()\b", re.I)


def _is_single_row_pk_lookup(q: dict, pk_columns: set[str]) -> bool:
    """Detect single-row lookups by primary key equality."""
    if q.get("query_type", "").upper() != "SELECT":
        return False
    rows_avg = q.get("rows_returned_avg", 0)
    if rows_avg > 2:
        return False
    # Check if filter columns include a PK column
    filters = q.get("filter_columns") or []
    return bool(pk_columns & {f.lower() for f in filters})


def _has_range_condition(q: dict) -> bool:
    text = q.get("query_text", "").lower()
    return bool(re.search(r"\b(between|>=?|<=?)\b", text)) and "order by" in text


def _is_write_query(q: dict) -> bool:
    return q.get("query_type", "").upper() in ("INSERT", "UPDATE", "DELETE", "REPLACE")


def _is_high_frequency(q: dict, threshold: float = 1.0) -> bool:
    return (q.get("calls_per_second") or 0) >= threshold


def _scan_frequency_per_minute(q: dict) -> float:
    """Estimate scans per minute for a query without WHERE clause."""
    cps = q.get("calls_per_second") or 0
    return cps * 60


# ---------------------------------------------------------------------------
# Composite PK classification
# ---------------------------------------------------------------------------


@dataclass
class PkClassification:
    """Classification of a table's primary key into DynamoDB key candidates.

    DynamoDB requires a partition key and optionally a sort key. This classifier
    maps relational PK structures to DynamoDB key candidates:
    - Single-column PK → natural partition key
    - Two-column PK → partition key + sort key (common for item collections)
    - Three+ columns → needs manual redesign (no automatic mapping)
    - No PK → flagged for attention (DynamoDB requires a key)
    """

    table_id: str
    pk_columns: list[str]
    partition_key_candidate: str | None
    sort_key_candidate: str | None
    needs_redesign: bool  # True when PK has 3+ columns
    no_pk: bool  # True when table has no PK


def classify_primary_keys(collector_output: dict) -> dict[str, PkClassification]:
    """Classify every table's PK into partition/sort key candidates.

    Rules:
    - 0 columns → no_pk=True, partition_key_candidate=None
    - 1 column  → partition_key_candidate=col, sort_key_candidate=None
    - 2 columns → partition_key_candidate=col[0], sort_key_candidate=col[1]
    - 3+ columns → needs_redesign=True, all columns recorded
    """
    tables = collector_output.get("database_schema", {}).get("tables", [])
    result: dict[str, PkClassification] = {}

    for t in tables:
        table_id = t.get("table_id", "")
        pk_columns = t.get("primary_key") or []

        count = len(pk_columns)
        match count:
            case 0:
                classification = PkClassification(
                    table_id=table_id,
                    pk_columns=[],
                    partition_key_candidate=None,
                    sort_key_candidate=None,
                    needs_redesign=False,
                    no_pk=True,
                )
            case 1:
                classification = PkClassification(
                    table_id=table_id,
                    pk_columns=list(pk_columns),
                    partition_key_candidate=pk_columns[0],
                    sort_key_candidate=None,
                    needs_redesign=False,
                    no_pk=False,
                )
            case 2:
                classification = PkClassification(
                    table_id=table_id,
                    pk_columns=list(pk_columns),
                    partition_key_candidate=pk_columns[0],
                    sort_key_candidate=pk_columns[1],
                    needs_redesign=False,
                    no_pk=False,
                )
            case _:
                classification = PkClassification(
                    table_id=table_id,
                    pk_columns=list(pk_columns),
                    partition_key_candidate=None,
                    sort_key_candidate=None,
                    needs_redesign=True,
                    no_pk=False,
                )

        result[table_id] = classification

    return result


# ---------------------------------------------------------------------------
# GSI candidate detection
# ---------------------------------------------------------------------------


@dataclass
class GsiCandidate:
    """A composite column group that is frequently queried but not part of the PK.

    DynamoDB supports multi-attribute composite keys in GSIs — up to 4 partition
    key attributes and 4 sort key attributes per GSI.
    """

    table_id: str
    partition_key_columns: list[str]  # 1–4 co-queried filter columns (DynamoDB GSI limit)
    sort_key_columns: list[str]  # 0–4 co-queried sort columns (DynamoDB GSI limit)
    total_frequency_per_hour: float
    query_ids: list[str]
    existing_index_name: str | None  # non-null if source DB already has a covering index
    is_sparse: bool  # True if population rate < 30%
    estimated_population_rate: float | None  # 0.0-1.0, None if unknown


def detect_gsi_candidates(
    collector_output: dict,
    pk_classifications: dict[str, PkClassification],
) -> list[GsiCandidate]:
    """Detect composite column groups that are frequently queried but not part of the PK.

    Leverages DynamoDB multi-key GSI support (up to 4 PK attrs + 4 SK attrs per GSI).

    Steps:
    1. For each query, collect filter_columns and sort_columns.
    2. Exclude columns that are part of the table's PK.
    3. Group co-occurring filter columns per query into a composite partition key
       candidate (capped at 4 columns per DynamoDB GSI limit).
    4. Group co-occurring sort columns per query into a composite sort key
       candidate (capped at 4 columns per DynamoDB GSI limit).
    5. Accumulate frequency_per_hour per (table_id, frozenset(filter_cols), tuple(sort_cols)).
    6. Threshold: combined frequency > 100 calls/hour.
    7. Check source indexes for existing coverage.
    8. Estimate population rate from nullable columns and column cardinality
       (if available in collector output). Flag sparse if < 30%.
    9. Rank by total_frequency_per_hour descending.
    """
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    tables = collector_output.get("database_schema", {}).get("tables", [])

    if not queries or not tables:
        return []

    # Build lookup structures

    # Build index lookup: table_id → list of {index_name, columns_set, is_primary}
    index_lookup: dict[str, list[dict]] = {}
    for t in tables:
        tid = t.get("table_id", "")
        idx_list = []
        for idx in t.get("indexes") or []:
            idx_list.append(
                {
                    "index_name": idx.get("index_name", ""),
                    "columns": {c.lower() for c in idx.get("columns", [])},
                    "is_primary": idx.get("is_primary", False),
                }
            )
        index_lookup[tid] = idx_list

    # Build column info lookup: table_id → column_name → {nullable, cardinality, row_count}
    column_info: dict[str, dict[str, dict]] = {}
    for t in tables:
        tid = t.get("table_id", "")
        row_count = t.get("row_count", 0)
        col_map: dict[str, dict] = {}
        for col in t.get("columns") or []:
            cname = col.get("column_name", "").lower()
            col_map[cname] = {
                "nullable": col.get("nullable", False),
                "cardinality": col.get("cardinality"),
                "row_count": row_count,
            }
        column_info[tid] = col_map

    # Accumulate: (table_id, frozenset(filter_cols), tuple(sorted_sort_cols))
    #   → {frequency, query_ids}
    accumulator: dict[tuple[str, frozenset[str], tuple[str, ...]], dict] = {}

    for q in queries:
        filter_cols = q.get("filter_columns") or []
        sort_cols = q.get("sort_columns") or []
        freq = q.get("frequency_per_hour", 0) or 0
        query_id = q.get("query_id", "")
        tables_accessed = q.get("tables_accessed") or []

        if not filter_cols:
            continue

        for tid in tables_accessed:
            pk_cls = pk_classifications.get(tid)
            pk_set = {c.lower() for c in (pk_cls.pk_columns if pk_cls else [])}

            # Exclude PK columns from filter and sort
            non_pk_filters = [c for c in filter_cols if c.lower() not in pk_set]
            non_pk_sorts = [c for c in sort_cols if c.lower() not in pk_set]

            if not non_pk_filters:
                continue

            # Cap at DynamoDB GSI limits (4 PK attrs, 4 SK attrs)
            capped_filters = non_pk_filters[:4]
            capped_sorts = non_pk_sorts[:4]

            key = (
                tid,
                frozenset(c.lower() for c in capped_filters),
                tuple(c.lower() for c in capped_sorts),
            )

            if key not in accumulator:
                accumulator[key] = {"frequency": 0.0, "query_ids": []}
            accumulator[key]["frequency"] += freq
            if query_id:
                accumulator[key]["query_ids"].append(query_id)

    # Apply threshold and build candidates
    candidates: list[GsiCandidate] = []
    for (tid, filter_fs, sort_t), info in accumulator.items():
        if info["frequency"] <= GSI_FREQUENCY_THRESHOLD:
            continue

        filter_list = sorted(filter_fs)
        sort_list = list(sort_t)

        # Check existing index coverage
        existing_idx = _find_covering_index(index_lookup.get(tid, []), filter_fs, sort_t)

        # Estimate population rate for sparse detection
        is_sparse, pop_rate = _estimate_sparsity(column_info.get(tid, {}), filter_fs)

        candidates.append(
            GsiCandidate(
                table_id=tid,
                partition_key_columns=filter_list,
                sort_key_columns=sort_list,
                total_frequency_per_hour=info["frequency"],
                query_ids=info["query_ids"],
                existing_index_name=existing_idx,
                is_sparse=is_sparse,
                estimated_population_rate=pop_rate,
            )
        )

    # Sort by total_frequency_per_hour descending
    candidates.sort(key=lambda c: c.total_frequency_per_hour, reverse=True)
    return candidates


def _find_covering_index(
    indexes: list[dict],
    filter_cols: frozenset[str],
    sort_cols: tuple[str, ...],  # tuple used as hashable key in accumulator dict
) -> str | None:
    """Check if any non-primary index covers the given filter+sort columns."""
    target_cols = filter_cols | set(sort_cols)
    for idx in indexes:
        if idx.get("is_primary"):
            continue
        if target_cols <= idx["columns"]:
            return str(idx["index_name"])
    return None


def _estimate_sparsity(
    col_info: dict[str, dict],
    filter_cols: frozenset[str],
) -> tuple[bool, float | None]:
    """Estimate population rate for filter columns. Flag sparse if < 30%.

    Uses cardinality / row_count as a proxy for population rate when available.
    NOTE: This is technically selectivity (distinct values / total rows), not true
    population rate (non-null rows / total rows). A boolean column with 2 distinct
    values across 1000 rows would show 0.002 selectivity despite 100% population.
    This approximation works well for high-cardinality columns (e.g., status enums
    where only "active" items exist) but can misclassify low-cardinality columns.
    A future improvement would use null_count from collector stats if available.

    Falls back to nullable as a hint: nullable columns *may* be sparse,
    but without stats we return None (unknown).
    """
    rates: list[float] = []
    for col_name in filter_cols:
        info = col_info.get(col_name)
        if not info:
            continue
        cardinality = info.get("cardinality")
        row_count = info.get("row_count", 0)
        if cardinality is not None and row_count and row_count > 0:
            rate = min(cardinality / row_count, 1.0)
            rates.append(rate)

    if rates:
        # Use the minimum population rate across filter columns
        min_rate = min(rates)
        return (min_rate < 0.30, min_rate)

    # No cardinality stats available — check nullable as a weak signal
    # but don't flag sparse without evidence
    return (False, None)


# ---------------------------------------------------------------------------
# Enhanced denormalization detection
# ---------------------------------------------------------------------------


# Detection thresholds — configurable knobs for tuning sensitivity.
# These are starting points calibrated via decision trace review.
BOUNDED_PARENT_CHILD_RATIO_THRESHOLD = 100  # child:parent row ratio
CO_ACCESS_FREQUENCY_THRESHOLD = 50.0  # calls/hour for co-accessed tables
GSI_FREQUENCY_THRESHOLD = 100.0  # calls/hour for GSI candidates
SECONDARY_INDEX_DOMINANCE_THRESHOLD = 0.5  # >50% frequency share


class DenormSubtype(str, Enum):
    """Denormalization sub-type classification.

    Each maps to a specific DynamoDB modeling strategy.
    """

    BOUNDED_PARENT_CHILD = "bounded-parent-child"
    MANY_TO_MANY_JUNCTION = "many-to-many-junction"
    CO_ACCESSED_TABLES = "co-accessed-tables"
    ADJACENCY_LIST = "adjacency-list"


@dataclass
class DenormalizationOpportunity:
    """A classified denormalization opportunity between related tables.

    Each opportunity maps to a specific DynamoDB modeling strategy:
    - bounded-parent-child → item collection or embedded document
    - many-to-many-junction → adjacency list pattern with GSI
    - co-accessed-tables → single-table design via denormalization
    - adjacency-list → PK/SK adjacency list with GSI for reverse lookups
    """

    subtype: DenormSubtype
    tables: list[str]
    cardinality_ratio: float | None  # child:parent ratio for bounded-parent-child
    co_access_frequency: float | None  # calls/hour for co-accessed-tables
    hierarchy_depth: int | None  # for adjacency-list
    is_cyclic: bool | None  # for adjacency-list (self-referential)
    evidence: dict  # supporting query_ids, FK info, etc.


def detect_denormalization_subtypes(
    collector_output: dict,
    relationships: list[dict],
) -> list[DenormalizationOpportunity]:
    """Classify denormalization opportunities into four sub-types.

    Replaces the single "denormalizable-relationship" pattern with four specific
    sub-classifications, each mapping to a different DynamoDB modeling strategy.

    Detection rules (evaluated in priority order — seen_pairs prevents double-counting):
    1. many-to-many-junction: exactly 2 FKs, PK = composite of both FK columns
       → DynamoDB: adjacency list pattern or GSI for bidirectional access
    2. adjacency-list: self-referential FK (table references itself)
       → DynamoDB: PK/SK adjacency list pattern with GSI for reverse lookups
    3. bounded-parent-child: FK exists, child_rows/parent_rows <= 100
       → DynamoDB: item collection (parent PK + child SK) or embedded items
    4. co-accessed-tables: two tables in same query, combined freq > 50 calls/hour
       → DynamoDB: denormalize into single table to avoid cross-table reads

    The ordering matters: more specific patterns (junction, adjacency-list) are
    checked before general ones (bounded-parent-child, co-accessed) so that a
    relationship is classified by its most specific characteristic.
    """
    tables = collector_output.get("database_schema", {}).get("tables", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])

    if not tables:
        return []

    # Build lookups
    table_by_id: dict[str, dict] = {}
    table_by_name: dict[str, dict] = {}
    for t in tables:
        table_by_id[t.get("table_id", "")] = t
        table_by_name[t.get("table_name", "")] = t

    row_counts: dict[str, int] = {t.get("table_id", ""): t.get("row_count", 0) for t in tables}

    opportunities: list[DenormalizationOpportunity] = []
    seen_pairs: set[frozenset[str]] = set()

    # --- 1. Detect many-to-many-junction from M:N relationships ---
    for rel in relationships:
        if rel.get("type") == "M:N":
            junction = rel.get("junction_table", "")
            table_a = rel.get("table_a", "")
            table_b = rel.get("table_b", "")
            # Resolve to table_ids
            resolved_a = _resolve_table_id(table_a, tables) or table_a
            resolved_b = _resolve_table_id(table_b, tables) or table_b
            pair_key = frozenset([junction, resolved_a, resolved_b])
            if pair_key not in seen_pairs:
                seen_pairs.add(pair_key)
                opportunities.append(
                    DenormalizationOpportunity(
                        subtype=DenormSubtype.MANY_TO_MANY_JUNCTION,
                        tables=[junction, resolved_a, resolved_b],
                        cardinality_ratio=None,
                        co_access_frequency=None,
                        hierarchy_depth=None,
                        is_cyclic=None,
                        evidence={
                            "junction_table": junction,
                            "table_a": resolved_a,
                            "table_b": resolved_b,
                            "columns_a": rel.get("columns_a", []),
                            "columns_b": rel.get("columns_b", []),
                        },
                    )
                )

    # --- 2. Detect adjacency-list (self-referential FK) ---
    for t in tables:
        tid = t.get("table_id", "")
        tname = t.get("table_name", "")
        for fk in t.get("foreign_keys") or []:
            ref_table = fk.get("referenced_table", "")
            resolved_ref = _resolve_table_id(ref_table, tables)
            # Self-referential: FK points back to the same table
            if resolved_ref == tid or ref_table == tname:
                pair_key = frozenset([tid, "self-ref"])
                if pair_key not in seen_pairs:
                    seen_pairs.add(pair_key)
                    opportunities.append(
                        DenormalizationOpportunity(
                            subtype=DenormSubtype.ADJACENCY_LIST,
                            tables=[tid],
                            cardinality_ratio=None,
                            co_access_frequency=None,
                            hierarchy_depth=None,
                            is_cyclic=True,
                            evidence={
                                "fk_columns": fk.get("columns", []),
                                "referenced_columns": fk.get("referenced_columns", []),
                                "reason": "self-referential FK",
                            },
                        )
                    )

    # --- 3. Detect bounded-parent-child from 1:M relationships ---
    for rel in relationships:
        if rel.get("type") != "1:M":
            continue
        child_tid = rel.get("from_table", "")
        parent_ref = rel.get("to_table", "")
        parent_tid = _resolve_table_id(parent_ref, tables) or parent_ref

        # Skip if either table is unknown
        if child_tid not in row_counts or parent_tid not in row_counts:
            continue

        pair_key = frozenset([child_tid, parent_tid])
        if pair_key in seen_pairs:
            continue

        parent_rows = row_counts.get(parent_tid, 0)
        child_rows = row_counts.get(child_tid, 0)

        if parent_rows > 0:
            ratio = child_rows / parent_rows
        else:
            ratio = 0.0

        if ratio <= BOUNDED_PARENT_CHILD_RATIO_THRESHOLD:
            seen_pairs.add(pair_key)
            opportunities.append(
                DenormalizationOpportunity(
                    subtype=DenormSubtype.BOUNDED_PARENT_CHILD,
                    tables=[parent_tid, child_tid],
                    cardinality_ratio=ratio,
                    co_access_frequency=None,
                    hierarchy_depth=None,
                    is_cyclic=None,
                    evidence={
                        "parent_table": parent_tid,
                        "child_table": child_tid,
                        "fk_columns": rel.get("from_columns", []),
                        "referenced_columns": rel.get("to_columns", []),
                        "parent_rows": parent_rows,
                        "child_rows": child_rows,
                    },
                )
            )

    # --- 4. Detect co-accessed-tables from query patterns ---
    # Accumulate co-access frequency per table pair
    pair_freq: dict[frozenset[str], float] = {}
    pair_queries: dict[frozenset[str], list[str]] = {}
    for q in queries:
        tables_accessed = q.get("tables_accessed") or []
        if len(tables_accessed) <= 1:
            continue
        freq = q.get("frequency_per_hour", 0.0)
        query_id = q.get("query_id", "")
        for pair in combinations(sorted(set(tables_accessed)), 2):
            key = frozenset(pair)
            pair_freq[key] = pair_freq.get(key, 0.0) + freq
            pair_queries.setdefault(key, []).append(query_id)

    for pair_key, total_freq in pair_freq.items():
        if total_freq <= CO_ACCESS_FREQUENCY_THRESHOLD:
            continue
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        pair_tables = sorted(pair_key)
        opportunities.append(
            DenormalizationOpportunity(
                subtype=DenormSubtype.CO_ACCESSED_TABLES,
                tables=pair_tables,
                cardinality_ratio=None,
                co_access_frequency=total_freq,
                hierarchy_depth=None,
                is_cyclic=None,
                evidence={
                    "query_ids": pair_queries.get(pair_key, []),
                    "total_frequency_per_hour": total_freq,
                },
            )
        )

    return opportunities


# ---------------------------------------------------------------------------
# Secondary index dominance detection
# ---------------------------------------------------------------------------


@dataclass
class SecondaryIndexDominance:
    """A table where the dominant access pattern uses a secondary index.

    When most queries hit a secondary index rather than the PK, the DynamoDB
    table design should consider using the secondary index columns as the
    primary key instead — this avoids paying for a GSI on every read.
    """

    table_id: str
    dominant_index_name: str
    dominant_index_columns: list[str]
    frequency_share: float  # 0.0-1.0, share of total table query frequency
    alternative_pk_candidate: str | None
    alternative_sk_candidate: str | None


def detect_secondary_index_dominance(
    collector_output: dict,
    pk_classifications: dict[str, PkClassification],
) -> list[SecondaryIndexDominance]:
    """Detect tables where >50% of query frequency uses a secondary index.

    When the dominant access pattern uses a secondary index rather than the PK,
    the DynamoDB key design should reflect actual query behavior — the secondary
    index columns become alternative PK/SK candidates.

    Steps:
    1. For each table, build a map: index_name → total calls/hour of queries
       whose filter_columns are a subset of that index's columns.
    2. Compare secondary index frequency to total table query frequency.
    3. If any secondary index has >50% share (strict), flag as dominant.
    4. Record the dominant index columns as alternative PK/SK candidates.
    5. Only the single most dominant index per table is reported.
    """
    tables = collector_output.get("database_schema", {}).get("tables", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])

    if not tables or not queries:
        return []

    # Build index lookup: table_id → list of {index_name, columns_set, is_primary}
    index_lookup: dict[str, list[dict]] = {}
    for t in tables:
        tid = t.get("table_id", "")
        idx_list = []
        for idx in t.get("indexes") or []:
            idx_list.append(
                {
                    "index_name": idx.get("index_name", ""),
                    "columns": {c.lower() for c in idx.get("columns", [])},
                    "columns_ordered": [c.lower() for c in idx.get("columns", [])],
                    "is_primary": idx.get("is_primary", False),
                }
            )
        index_lookup[tid] = idx_list

    # For each table, accumulate: index_name → total calls/hour
    # and total table query frequency
    table_total_freq: dict[str, float] = {}
    table_index_freq: dict[str, dict[str, float]] = {}

    for q in queries:
        filter_cols = q.get("filter_columns") or []
        if not filter_cols:
            continue
        freq = q.get("frequency_per_hour", 0) or 0
        if freq <= 0:
            continue
        tables_accessed = q.get("tables_accessed") or []
        filter_set = {c.lower() for c in filter_cols}

        for tid in tables_accessed:
            table_total_freq[tid] = table_total_freq.get(tid, 0.0) + freq

            for idx in index_lookup.get(tid, []):
                if idx["is_primary"]:
                    continue
                # Match: query filter_columns are a subset of or equal to index columns
                if filter_set and filter_set <= idx["columns"]:
                    idx_name = idx["index_name"]
                    if tid not in table_index_freq:
                        table_index_freq[tid] = {}
                    table_index_freq[tid][idx_name] = (
                        table_index_freq[tid].get(idx_name, 0.0) + freq
                    )

    # Find dominant secondary indexes (>50% share)
    results: list[SecondaryIndexDominance] = []

    for tid, idx_freqs in table_index_freq.items():
        total_freq = table_total_freq.get(tid, 0.0)
        if total_freq <= 0:
            continue

        for idx_name, idx_freq in idx_freqs.items():
            share = idx_freq / total_freq
            if share > SECONDARY_INDEX_DOMINANCE_THRESHOLD:
                # Find the index columns
                idx_info = None
                for idx in index_lookup.get(tid, []):
                    if idx["index_name"] == idx_name:
                        idx_info = idx
                        break

                if idx_info is None:
                    continue

                columns_ordered = idx_info["columns_ordered"]

                # Record alternative PK/SK candidates from index columns
                alt_pk = columns_ordered[0] if len(columns_ordered) >= 1 else None
                alt_sk = columns_ordered[1] if len(columns_ordered) >= 2 else None

                results.append(
                    SecondaryIndexDominance(
                        table_id=tid,
                        dominant_index_name=idx_name,
                        dominant_index_columns=list(columns_ordered),
                        frequency_share=share,
                        alternative_pk_candidate=alt_pk,
                        alternative_sk_candidate=alt_sk,
                    )
                )

    return results


# ---------------------------------------------------------------------------
# Pattern detection (per-query, catalog-driven)
# ---------------------------------------------------------------------------


def analyze_dynamodb_use_cases(collector_output: dict) -> WorkloadAnalysis:
    """Detect DynamoDB migration patterns using the specialist catalog.

    Matches patterns per-query (not per-table), then aggregates.
    Handles missing or partial collector output gracefully.
    """
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    tables = collector_output.get("database_schema", {}).get("tables", [])

    if not tables:
        return WorkloadAnalysis(patterns_detected=[], anti_patterns_detected=None)

    if not queries:
        # No queries — return empty patterns. Scoring will rely on structural signals only.
        return WorkloadAnalysis(patterns_detected=[], anti_patterns_detected=None)

    # Build PK column lookup per table
    pk_columns_by_table: dict[str, set[str]] = {}
    table_row_counts: dict[str, int] = {}
    for t in tables:
        tid = t.get("table_id", "")
        pk = t.get("primary_key") or []
        pk_columns_by_table[tid] = {c.lower() for c in pk}
        table_row_counts[tid] = t.get("row_count", 0)

    # All PK columns across all tables (for queries that don't specify table)
    all_pk_columns = set()
    for pks in pk_columns_by_table.values():
        all_pk_columns |= pks

    # Per-pattern query accumulators
    kv_queries: list[dict] = []
    range_queries: list[dict] = []
    write_queries: list[dict] = []
    low_freq_write_queries: list[dict] = []
    low_freq_read_queries: list[dict] = []
    timeseries_queries: list[dict] = []
    metadata_queries: list[dict] = []
    session_queries: list[dict] = []

    # Denormalization sub-type accumulators (replaces single join_queries)
    bounded_parent_child_queries: list[dict] = []
    many_to_many_junction_queries: list[dict] = []
    co_accessed_tables_queries: list[dict] = []
    adjacency_list_queries: list[dict] = []

    # Anti-pattern accumulators
    frequent_scan_queries: list[dict] = []
    complex_agg_queries: list[dict] = []
    unbounded_queries: list[dict] = []

    for q in queries:
        text = q.get("query_text", "")
        text_lower = text.lower()
        qt = q.get("query_type", "").upper()
        tables_accessed = q.get("tables_accessed") or []
        rows_avg = q.get("rows_returned_avg", 0)
        cps = q.get("calls_per_second") or 0

        # Resolve PK columns for this query's tables
        query_pk_cols = set()
        for tid in tables_accessed:
            query_pk_cols |= pk_columns_by_table.get(tid, set())
        if not query_pk_cols:
            query_pk_cols = all_pk_columns

        # --- Pattern matching (use if, not elif — queries can match multiple) ---

        # Key-value lookup
        if _is_single_row_pk_lookup(q, query_pk_cols):
            kv_queries.append(q)

        # Range query
        if qt == "SELECT" and _has_range_condition(q):
            range_queries.append(q)

        # Write-heavy
        if _is_write_query(q) and _is_high_frequency(q, threshold=0.5):
            write_queries.append(q)

        # Low-frequency writes (below write-heavy threshold but still need migration)
        if _is_write_query(q) and not _is_high_frequency(q, threshold=0.5):
            low_freq_write_queries.append(q)

        # Time-series / event log
        if _TIMESTAMP_KEYWORDS.search(text) and (
            qt == "INSERT" or (qt == "SELECT" and _has_range_condition(q))
        ):
            timeseries_queries.append(q)

        # Metadata/config store — small table, high reads, low writes
        if qt == "SELECT" and rows_avg <= 10 and cps >= 0.1:
            for tid in tables_accessed:
                if table_row_counts.get(tid, 0) <= 10000:
                    metadata_queries.append(q)
                    break

        # Session store
        if _SESSION_KEYWORDS.search(text):
            session_queries.append(q)

        # Denormalization sub-type detection (replaces single denormalizable-join)
        # These are coarse signal accumulators — the actual sub-type classification
        # happens in detect_denormalization_subtypes() using FK structure analysis.
        # A query can contribute to multiple accumulators here.
        if q.get("has_joins") and q.get("join_count", 0) <= 2:
            # Classify into sub-types based on query and table characteristics
            query_tables = q.get("tables_accessed") or []

            # Check for co-accessed tables (any multi-table query)
            if len(query_tables) > 1:
                co_accessed_tables_queries.append(q)

            # Check for bounded parent-child (simple FK join)
            if q.get("has_joins") and q.get("join_count", 0) == 1:
                bounded_parent_child_queries.append(q)

        # Many-to-many junction: queries accessing 3+ tables (junction + 2 referenced)
        if q.get("has_joins") and q.get("join_count", 0) >= 2:
            many_to_many_junction_queries.append(q)

        # Adjacency-list: self-join or hierarchical query patterns
        if q.get("has_joins") and q.get("tables_accessed"):
            accessed = q.get("tables_accessed") or []
            # Self-join: same table appears multiple times (detected via join on same table)
            if len(accessed) != len(set(accessed)):
                adjacency_list_queries.append(q)

        # --- Anti-pattern matching ---

        # Frequent full scan
        has_filter = bool(q.get("filter_columns"))
        if qt == "SELECT" and not has_filter and _scan_frequency_per_minute(q) > 1:
            frequent_scan_queries.append(q)

        # Complex aggregation — any GROUP BY / SUM / COUNT / AVG is an anti-pattern
        # for DynamoDB regardless of joins. DynamoDB has no server-side aggregation;
        # these need pre-computed counters or a different engine.
        if _AGGREGATION_RE.search(text):
            complex_agg_queries.append(q)

        # Unbounded result set
        if qt == "SELECT" and rows_avg > 10000 and "limit" not in text_lower:
            unbounded_queries.append(q)

        # Low-frequency reads — catch-all for SELECTs below frequency thresholds
        # that didn't match key-value, range, metadata, or session patterns
        if qt == "SELECT" and cps < 0.1:
            low_freq_read_queries.append(q)

    # Build Pattern objects from catalog
    patterns = _build_patterns_from_matches(
        [
            (DYNAMODB_PATTERNS[0], kv_queries),  # key-value-lookup
            (DYNAMODB_PATTERNS[1], range_queries),  # range-query
            (DYNAMODB_PATTERNS[2], write_queries),  # write-heavy-ingestion
            (DYNAMODB_PATTERNS[3], timeseries_queries),  # time-series-event-log
            (DYNAMODB_PATTERNS[4], metadata_queries),  # metadata-config-store
            (DYNAMODB_PATTERNS[5], session_queries),  # session-store
            (PATTERN_BY_ID["dynamodb-07a"], bounded_parent_child_queries),
            (PATTERN_BY_ID["dynamodb-07b"], many_to_many_junction_queries),
            (PATTERN_BY_ID["dynamodb-07c"], co_accessed_tables_queries),
            (PATTERN_BY_ID["dynamodb-07d"], adjacency_list_queries),
            (PATTERN_BY_ID["dynamodb-08"], low_freq_write_queries),
            (PATTERN_BY_ID["dynamodb-09"], low_freq_read_queries),
        ]
    )

    anti_patterns = _build_anti_patterns_from_matches(
        [
            (DYNAMODB_ANTI_PATTERNS[0], frequent_scan_queries),  # frequent-full-scan
            (DYNAMODB_ANTI_PATTERNS[1], complex_agg_queries),  # complex-aggregation
            (DYNAMODB_ANTI_PATTERNS[2], unbounded_queries),  # unbounded-result-set
        ]
    )

    return WorkloadAnalysis(
        patterns_detected=patterns,
        anti_patterns_detected=anti_patterns or None,
    )


def _build_patterns_from_matches(
    matches: list[tuple[CatalogPattern, list[dict]]],
) -> list[Pattern]:
    """Build Pattern objects from catalog patterns and matched queries."""
    patterns = []
    for catalog_pattern, matched_queries in matches:
        if not matched_queries:
            continue
        table_ids = list({t for q in matched_queries for t in (q.get("tables_accessed") or [])})
        query_ids = [str(q["query_id"]) for q in matched_queries if q.get("query_id")]

        # Map catalog base_score to Confidence enum for the output contract.
        # The contract uses HIGH/MEDIUM/LOW (not numeric) — this is the boundary
        # between internal numeric scoring and the contract's enum representation.
        # Numeric scores (0-100) stay in the decision trace; the enum goes to consumers.
        if catalog_pattern.base_score >= 85:
            confidence = Confidence.HIGH
        elif catalog_pattern.base_score >= 70:
            confidence = Confidence.MEDIUM
        else:
            confidence = Confidence.LOW

        patterns.append(
            Pattern(
                pattern_id=catalog_pattern.pattern_id,
                pattern_type=catalog_pattern.pattern_type,
                confidence=confidence,
                description=catalog_pattern.description,
                query_ids=query_ids,
                table_ids=table_ids,
                frequency_percent=None,  # Computed later if needed
            )
        )
    return patterns


def _build_anti_patterns_from_matches(
    matches: list[tuple[CatalogAntiPattern, list[dict]]],
) -> list[AntiPattern]:
    """Build AntiPattern objects from catalog anti-patterns and matched queries."""
    anti_patterns = []
    for catalog_ap, matched_queries in matches:
        if not matched_queries:
            continue
        table_ids = list({t for q in matched_queries for t in (q.get("tables_accessed") or [])})
        query_ids = [str(q["query_id"]) for q in matched_queries if q.get("query_id")]
        anti_patterns.append(
            AntiPattern(
                anti_pattern_id=catalog_ap.pattern_id,
                anti_pattern_type=catalog_ap.pattern_type,
                severity_weight=catalog_ap.severity_weight,
                description=catalog_ap.description,
                query_ids=query_ids,
                table_ids=table_ids,
                recommendation=catalog_ap.guidance,
            )
        )
    return anti_patterns


# ---------------------------------------------------------------------------
# Scoring adjustments (DynamoDB-specific)
# ---------------------------------------------------------------------------


def analyze_dynamodb_patterns(
    collector_output: dict, workload_analysis: WorkloadAnalysis
) -> list[TableRecommendation]:
    """Generate per-table recommendations using shared scoring + DynamoDB adjustments."""
    profiles = build_table_profiles(collector_output, workload_analysis)
    table_groups = build_table_groups(collector_output)

    # Reverse lookup: table_id → group members
    table_to_group: dict[str, list[str]] = {}
    for _root, members in table_groups.items():
        if len(members) > 1:
            for tid in members:
                table_to_group[tid] = [m for m in members if m != tid]

    # Build pattern/anti-pattern lookup by table
    table_patterns: dict[str, list[Pattern]] = {}
    for p in workload_analysis.patterns_detected:
        for tid in p.table_ids or []:
            table_patterns.setdefault(tid, []).append(p)

    table_anti_patterns: dict[str, list[AntiPattern]] = {}
    for ap in workload_analysis.anti_patterns_detected or []:
        for tid in ap.table_ids or []:
            table_anti_patterns.setdefault(tid, []).append(ap)

    recommendations = []
    for table_id, profile in profiles.items():
        # Use shared scoring for structural dimensions (complexity, performance, cost)
        base_scores = compute_base_scores(profile)

        # Override pattern_match_score with catalog-driven score:
        # weighted average of matched pattern base_scores, weighted by query frequency
        tp = table_patterns.get(table_id, [])
        tap = table_anti_patterns.get(table_id, [])
        catalog_pattern_score = _compute_catalog_pattern_score(tp, collector_output)

        scores = ScoreBreakdown(
            pattern_match_score=max(0, min(catalog_pattern_score, 100)),
            complexity_score=base_scores.complexity_score,
            performance_score=base_scores.performance_score,
            cost_score=base_scores.cost_score,
        )
        scores = _apply_dynamodb_adjustments(scores, profile)
        confidence = compute_confidence(scores, weights=DYNAMODB_SCORE_WEIGHTS)

        recommendations.append(
            TableRecommendation(
                table_id=table_id,
                confidence_score=confidence,
                rationale=_build_rationale(profile, tp),
                score_breakdown=scores,
                supporting_patterns=[p.pattern_id for p in tp],
                concerns=_build_concerns(profile, tap, table_to_group.get(table_id)),
                migration_complexity=_assess_complexity(profile, tap),
            )
        )

    return recommendations


def _compute_catalog_pattern_score(table_patterns: list[Pattern], collector_output: dict) -> int:
    """Compute pattern_match_score from catalog base scores weighted by query frequency.

    For each pattern matching this table, look up the catalog base_score and
    weight it by the total calls_per_second of the matched queries.
    """
    if not table_patterns:
        return 0

    from src.tools.analysis.dynamodb_pattern_catalog import PATTERN_BY_ID

    queries = collector_output.get("queries", {}).get("query_patterns", [])
    query_cps: dict[str, float] = {
        q.get("query_id", ""): q.get("calls_per_second", 0) for q in queries
    }

    weighted_sum = 0.0
    total_weight = 0.0

    for p in table_patterns:
        catalog = PATTERN_BY_ID.get(p.pattern_id)
        if not catalog:
            continue
        # Weight = total calls/sec of queries in this pattern for this table
        pattern_cps = sum(query_cps.get(qid, 0) for qid in (p.query_ids or []))
        weight = max(pattern_cps, 0.01)  # Minimum weight so zero-traffic patterns still count
        weighted_sum += catalog.base_score * weight
        total_weight += weight

    if total_weight == 0:
        return 0
    return int(weighted_sum / total_weight)


def _apply_dynamodb_adjustments(scores: ScoreBreakdown, profile: TableProfile) -> ScoreBreakdown:
    """Apply DynamoDB-specific scoring adjustments."""
    pattern = scores.pattern_match_score
    complexity = scores.complexity_score
    performance = scores.performance_score
    cost = scores.cost_score

    types = set(profile.pattern_types)

    # Strong bonuses for DynamoDB sweet spots
    if "key-value-lookup" in types:
        pattern += 15
    if "range-query" in types:
        pattern += 10
    if "metadata-config-store" in types:
        pattern += 10
    if "session-store" in types:
        pattern += 12
    if "time-series-event-log" in types:
        pattern += 10
    if "write-heavy-ingestion" in types:
        pattern += 8

    # Denormalizable joins — moderate bonus (needs design work)
    denorm_types = {
        "bounded-parent-child",
        "many-to-many-junction",
        "co-accessed-tables",
        "adjacency-list",
    }
    if types & denorm_types:
        pattern += 5
        complexity -= 10  # More migration effort

    # DynamoDB loves simple PKs
    if profile.has_primary_key and profile.column_count <= 10:
        complexity += 5

    # High write ratio is a DynamoDB strength (vs Redis which is read-heavy)
    if profile.read_ratio < 0.5 and profile.total_calls_per_second >= 1:
        performance += 10

    # On-demand pricing favors spiky workloads — small tables are cheap
    if profile.size_mb <= 100:
        cost += 10
    elif profile.size_mb > 10000:
        cost -= 10  # Large tables get expensive

    # Many foreign keys = complex denormalization
    if profile.foreign_key_count > 3:
        complexity -= 15

    return ScoreBreakdown(
        pattern_match_score=max(0, min(pattern, 100)),
        complexity_score=max(0, min(complexity, 100)),
        performance_score=max(0, min(performance, 100)),
        cost_score=max(0, min(cost, 100)),
    )


def _build_rationale(profile: TableProfile, table_patterns: list[Pattern]) -> str:
    """Build human-readable rationale for the recommendation."""
    if not table_patterns:
        return f"No strong DynamoDB patterns detected for {profile.table_id}."

    pattern_names = [p.pattern_type.replace("-", " ") for p in table_patterns]
    parts = [f"Detected patterns: {', '.join(pattern_names)}."]

    if profile.total_calls_per_second >= 10:
        parts.append(
            f"High throughput ({profile.total_calls_per_second:.0f} calls/sec) suits DynamoDB on-demand."
        )
    if profile.has_primary_key:
        parts.append("Has primary key — maps to DynamoDB partition key.")
    if profile.size_mb <= 100:
        parts.append("Small table — cost-effective on DynamoDB.")

    return " ".join(parts)


def _build_concerns(
    profile: TableProfile,
    table_anti_patterns: list[AntiPattern],
    related_tables: list[str] | None = None,
) -> list[str]:
    """Build list of concerns for the recommendation."""
    concerns = []
    for ap in table_anti_patterns:
        concerns.append(
            f"[source db] {ap.anti_pattern_type}: {ap.recommendation or ap.description}"
        )

    if related_tables:
        concerns.append(
            f"FK cluster: migrates together with {', '.join(related_tables)}. "
            "Consider single-table design or denormalization."
        )
    elif profile.foreign_key_count > 0:
        concerns.append(
            f"{profile.foreign_key_count} foreign key(s) — requires denormalization or GSI design."
        )
    if profile.has_joins and profile.max_join_count > 2:
        concerns.append(
            "Complex joins (>2) — needs careful single-table design or separate queries."
        )
    return concerns


def _assess_complexity(
    profile: TableProfile, table_anti_patterns: list[AntiPattern]
) -> MigrationComplexity:
    """Assess migration complexity for DynamoDB."""
    if profile.foreign_key_count > 3 or profile.max_join_count > 3:
        return MigrationComplexity.HIGH
    if profile.foreign_key_count > 0 or profile.has_joins or table_anti_patterns:
        return MigrationComplexity.MEDIUM
    return MigrationComplexity.LOW


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def estimate_dynamodb_costs(
    collector_output: dict,
    target_region: str = "us-east-1",
    analysis_options=None,
) -> CostEstimate:
    """Estimate DynamoDB monthly costs based on workload characteristics.

    Uses on-demand pricing as the default (simpler, no capacity planning).
    It is a very very high level and basic cost estimate
    """
    tables = collector_output.get("database_schema", {}).get("tables", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])

    # Aggregate storage
    total_size_gb = sum(t.get("size_mb", 0) for t in tables) / 1024

    # Aggregate throughput
    total_read_cps = 0.0
    total_write_cps = 0.0
    for q in queries:
        cps = q.get("calls_per_second", 0)
        qt = q.get("query_type", "").upper()
        if qt == "SELECT":
            total_read_cps += cps
        elif qt in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            total_write_cps += cps

    # On-demand pricing (us-east-1)
    # Read: $1.25 per million RRUs, Write: $1.25 per million WRUs
    # Storage: $0.25 per GB-month
    read_units_per_month = total_read_cps * 86400 * 30
    write_units_per_month = total_write_cps * 86400 * 30

    read_cost = (read_units_per_month / 1_000_000) * 0.125
    write_cost = (write_units_per_month / 1_000_000) * 0.625
    storage_cost = max(total_size_gb, 0.01) * 0.25  # Minimum for small DBs

    total = round(read_cost + write_cost + storage_cost, 2)

    return CostEstimate(
        monthly_cost_usd=total,
        cost_components={
            "read_request_units": round(read_cost, 2),
            "write_request_units": round(write_cost, 2),
            "storage_gb": round(storage_cost, 2),
            "pricing_mode": "on-demand",
        },
        pricing_assumptions=[
            "On-demand capacity mode (no provisioned throughput)",
            f"Region: {target_region}",
            "Eventually consistent reads (1 RRU per 4KB)",
            "Standard write units (1 WRU per 1KB)",
            "No DAX, backups, or global tables included",
        ],
    )


# ---------------------------------------------------------------------------
# Decision trace
# ---------------------------------------------------------------------------


def build_decision_trace(
    collector_output: dict,
    workload_analysis: WorkloadAnalysis,
    table_recommendations: list[TableRecommendation],
    *,
    pk_classifications: dict | None = None,
    aggregates: list | None = None,
    gsi_candidates: list | None = None,
    denorm_opportunities: list | None = None,
    si_dominance: list | None = None,
    llm_status: str = "skipped",
    llm_duration: float = 0.0,
    llm_attempts: int = 0,
    llm_output: LlmAdvisorOutput | None = None,
) -> dict:
    """Build the decision trace artifact for specialist calibration.

    This is a separate S3 artifact — not part of the agent-to-agent contract.
    Trace version 1.1 adds: pk_classifications, aggregates, gsi_candidates,
    denormalization_opportunities, secondary_index_dominance, llm_advisor.
    """
    from dataclasses import asdict

    queries = collector_output.get("queries", {}).get("query_patterns", [])

    # Build FK-connected table groups
    table_groups = build_table_groups(collector_output)

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
            }
        )

    # Per-pattern summaries
    from src.tools.analysis.dynamodb_pattern_catalog import PATTERN_BY_ID

    pattern_summaries = []
    for p in workload_analysis.patterns_detected:
        catalog = PATTERN_BY_ID.get(p.pattern_id)
        pattern_summaries.append(
            {
                "pattern_id": p.pattern_id,
                "pattern_type": p.pattern_type,
                "catalog_base_score": catalog.base_score if catalog else None,
                "queries_matched_count": len(p.query_ids or []),
                "tables_involved": p.table_ids or [],
            }
        )

    # Per-table recommendation derivations
    derivations = []
    for rec in table_recommendations:
        derivations.append(
            {
                "table_id": rec.table_id,
                "segments_contributing": rec.supporting_patterns or [],
                "score_breakdown": {
                    "pattern_match": rec.score_breakdown.pattern_match_score,
                    "complexity": rec.score_breakdown.complexity_score,
                    "performance": rec.score_breakdown.performance_score,
                    "cost": rec.score_breakdown.cost_score,
                },
                "weights_used": DYNAMODB_SCORE_WEIGHTS,
                "weighted_confidence": rec.confidence_score,
            }
        )

    trace = {
        "trace_version": "1.1",
        "agent": "dynamodb-analysis-agent",
        "summary": {
            "queries_analyzed": len(queries),
            "queries_matched": len(matched_qids),
            "queries_unmatched": len(queries) - len(matched_qids),
            "patterns_detected": len(workload_analysis.patterns_detected),
            "anti_patterns_detected": len(workload_analysis.anti_patterns_detected or []),
        },
        "query_matches": query_matches,
        "pattern_summaries": pattern_summaries,
        "recommendation_derivations": derivations,
        "table_groups": [
            {"group_root": root, "tables": members}
            for root, members in table_groups.items()
            if len(members) > 1
        ],
    }

    # --- New trace sections (v1.1) ---
    trace["pk_classifications"] = (
        [asdict(pk) for pk in pk_classifications.values()] if pk_classifications else []
    )
    trace["aggregates"] = [asdict(a) for a in aggregates] if aggregates else []
    trace["gsi_candidates"] = [asdict(g) for g in gsi_candidates] if gsi_candidates else []
    trace["denormalization_opportunities"] = (
        [asdict(d) for d in denorm_opportunities] if denorm_opportunities else []
    )
    trace["secondary_index_dominance"] = [asdict(s) for s in si_dominance] if si_dominance else []
    trace["llm_advisor"] = {
        "status": llm_status,
        "duration_seconds": llm_duration,
        "attempts": llm_attempts,
        "aggregate_key_designs": (
            [r.model_dump() for r in llm_output.aggregate_recommendations] if llm_output else []
        ),
        "denormalization_strategies": (
            [s.model_dump() for s in llm_output.denormalization_strategies] if llm_output else []
        ),
    }

    return trace


# ---------------------------------------------------------------------------
# Table grouping (FK clusters)
# ---------------------------------------------------------------------------


def build_table_groups(collector_output: dict) -> dict[str, list[str]]:
    """Build FK-connected table clusters.

    Returns a dict mapping group_id (root table) → list of table_ids in the cluster.
    Tables with no FK relationships are their own single-member group.
    """
    tables = collector_output.get("database_schema", {}).get("tables", [])

    # Build adjacency list from FK relationships
    adjacency: dict[str, set[str]] = {}
    all_table_ids: set[str] = set()

    for t in tables:
        tid = t.get("table_id", "")
        all_table_ids.add(tid)
        adjacency.setdefault(tid, set())
        for fk in t.get("foreign_keys") or []:
            ref_table = fk.get("referenced_table", "")
            # referenced_table might be just the name, not the full table_id
            # Try to find the matching table_id
            ref_tid = _resolve_table_id(ref_table, tables)
            if ref_tid:
                adjacency.setdefault(tid, set()).add(ref_tid)
                adjacency.setdefault(ref_tid, set()).add(tid)

    # Find connected components via BFS
    visited: set[str] = set()
    groups: dict[str, list[str]] = {}

    for tid in all_table_ids:
        if tid in visited:
            continue
        # BFS from this table
        cluster: list[str] = []
        queue = [tid]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            cluster.append(current)
            for neighbor in adjacency.get(current, set()):
                if neighbor not in visited:
                    queue.append(neighbor)

        # Use the first table alphabetically as the group root
        root = sorted(cluster)[0]
        groups[root] = sorted(cluster)

    return groups


def _resolve_table_id(ref_table: str, tables: list[dict]) -> str | None:
    """Resolve a referenced table name to its full table_id."""
    for t in tables:
        tid: str = t.get("table_id", "")
        tname: str = t.get("table_name", "")
        if tid == ref_table or tname == ref_table:
            return tid
    return None


# ---------------------------------------------------------------------------
# Relationship detection + Mermaid diagram
# ---------------------------------------------------------------------------


def detect_relationships(collector_output: dict) -> list[dict]:
    """Detect 1:1, 1:M, and M:N relationships from FK and index data.

    Returns a list of relationship dicts with type, tables, and columns.
    """
    tables = collector_output.get("database_schema", {}).get("tables", [])

    # Build lookup: table_name → table dict
    table_by_name: dict[str, dict] = {}
    for t in tables:
        table_by_name[t.get("table_name", "")] = t
        table_by_name[t.get("table_id", "")] = t

    # Build unique column sets per table (from unique indexes)
    unique_columns: dict[str, set[str]] = {}
    for t in tables:
        tid = t.get("table_id", "")
        uq: set[str] = set()
        for idx in t.get("indexes") or []:
            if idx.get("is_unique") and len(idx.get("columns", [])) == 1:
                uq.add(idx["columns"][0].lower())
        pk = t.get("primary_key") or []
        for c in pk:
            uq.add(c.lower())
        unique_columns[tid] = uq
        unique_columns[t.get("table_name", "")] = uq

    relationships = []

    for t in tables:
        tid = t.get("table_id", "")
        tname = t.get("table_name", "")
        fks = t.get("foreign_keys") or []

        # Detect M:N junction table: exactly 2 FKs and PK is composite of both FK columns
        if len(fks) == 2:
            pk = {c.lower() for c in (t.get("primary_key") or [])}
            fk_cols = set()
            for fk in fks:
                for c in fk.get("columns", []):
                    fk_cols.add(c.lower())
            if pk and pk == fk_cols:
                relationships.append(
                    {
                        "type": "M:N",
                        "junction_table": tid,
                        "table_a": fks[0].get("referenced_table", ""),
                        "table_b": fks[1].get("referenced_table", ""),
                        "columns_a": fks[0].get("columns", []),
                        "columns_b": fks[1].get("columns", []),
                        "description": (
                            f"Many-to-many via {tname}: "
                            f"{fks[0].get('referenced_table')} ←→ {fks[1].get('referenced_table')}"
                        ),
                    }
                )
                continue

        # Classify each FK as 1:1 or 1:M
        for fk in fks:
            fk_columns = [c.lower() for c in fk.get("columns", [])]
            ref_table = fk.get("referenced_table", "")
            ref_columns = fk.get("referenced_columns", [])

            # 1:1 if FK column has a unique constraint
            is_one_to_one = len(fk_columns) == 1 and fk_columns[0] in unique_columns.get(
                tname, set()
            )

            rel_type = "1:1" if is_one_to_one else "1:M"
            relationships.append(
                {
                    "type": rel_type,
                    "from_table": tid,
                    "to_table": ref_table,
                    "from_columns": fk.get("columns", []),
                    "to_columns": ref_columns,
                    "description": (
                        f"{rel_type}: {tname}.{','.join(fk.get('columns', []))} → "
                        f"{ref_table}.{','.join(ref_columns)}"
                    ),
                }
            )

    return relationships


def generate_mermaid_er_diagram(collector_output: dict, relationships: list[dict]) -> str:
    """Generate a Mermaid ER diagram string from schema and relationships."""
    tables = collector_output.get("database_schema", {}).get("tables", [])

    lines = ["erDiagram"]

    # Table definitions with columns
    for t in tables:
        tname = t.get("table_name", "unknown")
        lines.append(f"    {tname} {{")
        pk_cols = {c.lower() for c in (t.get("primary_key") or [])}
        for col in (t.get("columns") or [])[:15]:  # Cap at 15 columns for readability
            cname = col.get("column_name", "")
            ctype = col.get("data_type", "unknown")
            marker = " PK" if cname.lower() in pk_cols else ""
            lines.append(f"        {ctype} {cname}{marker}")
        lines.append("    }")

    # Relationships
    for rel in relationships:
        if rel["type"] == "M:N":
            ta = rel.get("table_a", "")
            tb = rel.get("table_b", "")
            jt = rel.get("junction_table", "").split(".")[-1]
            lines.append(f'    {ta} }}|--|{{ {jt} : ""')
            lines.append(f'    {tb} }}|--|{{ {jt} : ""')
        elif rel["type"] == "1:1":
            from_t = rel.get("from_table", "").split(".")[-1]
            to_t = rel.get("to_table", "")
            lines.append(f'    {to_t} ||--|| {from_t} : ""')
        else:  # 1:M
            from_t = rel.get("from_table", "").split(".")[-1]
            to_t = rel.get("to_table", "")
            lines.append(f'    {to_t} ||--|{{ {from_t} : ""')

    return "\n".join(lines)
