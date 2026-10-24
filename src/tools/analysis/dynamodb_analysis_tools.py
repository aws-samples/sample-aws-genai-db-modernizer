"""
DynamoDB Analysis Tools

Pattern detection, scoring adjustments, and cost estimation for DynamoDB.
Uses the specialist-curated pattern catalog for detection and scoring.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from src.agents.prompt_framing import frame_untrusted
from src.agents.referee.access_pattern import (
    KEY_SCOPED_COUNT_FACT,
    SMALL_TABLE_MAX_ROWS,
    UNPARSED,
    UTILITY,
    SchemaKeys,
    query_access,
)
from src.agents.referee.cache_overlay import is_locking_read
from src.agents.referee.query_cost import row_widths
from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.transaction_shape import BOUNDED, transaction_shape
from src.agents.referee.write_groups import written_tables
from src.contracts.analysis_output import (
    AntiPattern,
    Confidence,
    CostEstimate,
    KeyAnchor,
    KeyScopedCount,
    KeyScopedCountKey,
    KeyScopedWrite,
    MigrationComplexity,
    Pattern,
    ScoreBreakdown,
    TableRecommendation,
    WorkloadAnalysis,
)
from src.contracts.dynamodb_decision_trace import (
    AggregateKeyDesign,
    AggregateTrace,
    DenormalizationOpportunityTrace,
    DenormStrategy,
    DenormSubtype,
    DynamoDBDecisionTrace,
    GsiCandidateTrace,
    LlmAdvisorTrace,
    PatternSummary,
    PkClassificationTrace,
    QueryMatch,
    RecommendationDerivation,
    Relationship,
    SecondaryIndexDominanceTrace,
    TableGroup,
    TimestampColumnsTrace,
    TraceSummary,
    TransactionShapeTrace,
)
from src.shared import catalog
from src.shared.catalog import patterns as catalog_patterns
from src.shared.catalog.prices import SECONDS_PER_MONTH, price_basis
from src.tools.analysis.dynamodb_pattern_catalog import (
    ANTI_PATTERN_BY_ID,
    DYNAMODB_ANTI_PATTERNS,
    DYNAMODB_PATTERNS,
    DYNAMODB_SCORE_WEIGHTS,
    PATTERN_BY_ID,
    CatalogAntiPattern,
    CatalogPattern,
)
from src.tools.analysis.key_access import KeyAccessReader
from src.tools.analysis.llm_advisor_base import LlmAdvisorBase
from src.tools.analysis.scoring import (
    TableProfile,
    build_table_profiles,
    compute_base_scores,
    compute_confidence,
)
from src.tools.sql.query_facts import (
    ColumnTypes,
    query_facts,
    source_engine_of,
    timestamp_column_trace,
)

# ---------------------------------------------------------------------------
# LLM Advisor Pydantic models (structured LLM response parsing only)
# ---------------------------------------------------------------------------


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

        workload = (
            f"## Deterministic Analysis Results\n{deterministic_results}\n\n"
            f"## Database Schema\n{schema}\n\n"
            f"## Query Patterns\n{queries}\n\n"
            f"## Identified Aggregates\n{agg_data}\n\n"
            f"## Denormalization Opportunities\n{denorm_data}"
        )
        return (
            "Analyze the following database workload and produce DynamoDB key design "
            "recommendations and denormalization strategies.\n\n"
            + frame_untrusted(workload, label="the database workload to analyze")
            + "\n\nProduce a key design (partition_key, optional sort_key, rationale) for "
            "each aggregate, and a denormalization strategy for each opportunity."
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

# The text detectors (timestamp and session words, aggregation calls, range
# conditions, LIMIT) are engine-neutral query facts in
# ``src.tools.sql.query_facts`` (#491).


# Statements with no parsed access facts: nothing is known about their predicates.
_UNREAD_CLASSES = frozenset({UNPARSED, UTILITY})


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
    source_engine = source_engine_of(collector_output)
    scope = SourceScope.from_collector(collector_output)
    column_types = ColumnTypes.from_collector(collector_output)

    if not tables:
        return WorkloadAnalysis(patterns_detected=[], anti_patterns_detected=None)

    if not queries:
        # No queries — return empty patterns. Scoring will rely on structural signals only.
        return WorkloadAnalysis(patterns_detected=[], anti_patterns_detected=None)

    table_row_counts: dict[str, int] = {
        t.get("table_id", ""): t.get("row_count", 0) or 0 for t in tables
    }
    # How each query reaches its rows by key, from the SQL facts (#920): the
    # collector's filter_columns is empty on every bundled sample query.
    reader = KeyAccessReader.from_collector(collector_output, source_engine)

    # Per-pattern query accumulators
    kv_queries: list[dict] = []
    range_queries: list[dict] = []
    write_queries: list[dict] = []
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
    ordered_scan_queries: list[dict] = []
    complex_agg_queries: list[dict] = []
    unbounded_queries: list[dict] = []
    wide_transaction_queries: list[dict] = []

    for q in queries:
        facts = query_facts(q.get("query_text", ""), source_engine)
        qt = q.get("query_type", "").upper()
        tables_accessed = q.get("tables_accessed") or []
        rows_avg = q.get("rows_returned_avg", 0)
        cps = q.get("calls_per_second") or 0
        access = reader.access(q)

        # --- Pattern matching (use if, not elif — queries can match multiple) ---

        # Key-value lookup: a read by a full primary or unique key (GetItem, or
        # BatchGetItem for an IN-list of keys).
        if access.key_lookup_read:
            kv_queries.append(q)

        # Range query: a partition-key equality plus a range on another column of the
        # same table (Query with a KeyConditionExpression), not any range.
        if qt == "SELECT" and access.partition_range:
            range_queries.append(q)

        # Write-heavy: a frequent write that addresses its items by key (PutItem,
        # UpdateItem, DeleteItem). A frequent write alone is a call rate, not an access
        # pattern (#920): an unkeyed or joined write is not this pattern.
        if _is_write_query(q) and _is_high_frequency(q, threshold=0.5) and access.keyed:
            write_queries.append(q)

        # Time-series / event log: an INSERT into a table the workload only appends
        # to, or an entity-anchored time range read (a partition key plus a range).
        if facts.timestamp_column(column_types) and (
            (qt == "INSERT" and all(reader.append_only(t) for t in tables_accessed))
            or (qt == "SELECT" and access.partition_range)
        ):
            timeseries_queries.append(q)

        # Metadata/config store: a keyed read, with no aggregate, of a small table the
        # workload reads far more often than it writes.
        if (
            qt == "SELECT"
            and access.keyed
            and not facts.aggregation
            and rows_avg <= 10
            and cps >= 0.1
            and tables_accessed
            and all(
                table_row_counts.get(t, 0) <= SMALL_TABLE_MAX_ROWS and reader.read_mostly(t)
                for t in tables_accessed
            )
        ):
            metadata_queries.append(q)

        # Session store: a session or token statement that addresses its item by key
        # (GetItem/PutItem on the session key), not any statement naming a token (#920).
        if facts.session_key and access.keyed:
            session_queries.append(q)

        # Denormalization sub-types: a key-anchored join (every table reached by key
        # from a pinned one), not a join count. detect_denormalization_subtypes() then
        # classifies them from the FK structure.
        if access.anchored_join:
            joined = set(tables_accessed)
            if len(joined) == 2:
                bounded_parent_child_queries.append(q)
            if 2 <= len(joined) <= 3 and cps * 3600 >= CO_ACCESS_FREQUENCY_THRESHOLD:
                co_accessed_tables_queries.append(q)
            if any(reader.is_junction(t) for t in joined) and len(joined) >= 3:
                many_to_many_junction_queries.append(q)

        # Adjacency-list: self-join or hierarchical query patterns
        if q.get("has_joins") and q.get("tables_accessed"):
            accessed = q.get("tables_accessed") or []
            # Self-join: same table appears multiple times (detected via join on same table)
            if len(accessed) != len(set(accessed)):
                adjacency_list_queries.append(q)

        # --- Anti-pattern matching ---

        # Frequent full scan: a SELECT that pins no column to a value in any scope, so no
        # partition key value can address it whatever the key design (a Scan), run more
        # than once a minute. Read from the SQL facts, not the collector's filter_columns
        # (#920); a statement the facade cannot parse is not called a scan.
        pins_nothing = access.access_class not in _UNREAD_CLASSES and not access.pins_value
        if qt == "SELECT" and pins_nothing and _scan_frequency_per_minute(q) > 1:
            frequent_scan_queries.append(q)

        # Ordered scan: the first rows of a whole table in some order, at any call rate.
        # A Scan returns no order, so the application reads and sorts every item (#920).
        if qt == "SELECT" and pins_nothing and facts.top_n:
            ordered_scan_queries.append(q)

        # Complex aggregation: a GROUP BY, or an aggregate over rows no key anchors or
        # over a join. DynamoDB has no server-side aggregation; these need pre-computed
        # aggregates or a different engine. An aggregate over one key's rows of one
        # table with no GROUP BY (COUNT(*) WHERE post_id = ?) is a Query on that key
        # with Select = COUNT, or a sum over its item collection: no redesign, so not
        # this pattern (#968). The read itself is the key-scoped class.
        if facts.aggregation and (facts.group_by or not access.keyed or facts.join_count > 0):
            complex_agg_queries.append(q)

        # Unbounded result set
        if qt == "SELECT" and rows_avg > 10000 and not facts.row_limit:
            unbounded_queries.append(q)

        # Wide transaction (ADR-031 Decision 3, #493): DynamoDB runs a write of up to
        # 100 items as one transaction; only a write whose shape one transaction
        # cannot hold (more items, or a set the statement does not bound) needs a
        # redesign. The shape is the engine-neutral fact in transaction_shape.
        shape = transaction_shape(q, scope, reader.keys)
        if shape is not None and shape.needs_redesign:
            wide_transaction_queries.append(q)

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
        ]
    )

    anti_patterns = _build_anti_patterns_from_matches(
        [
            (DYNAMODB_ANTI_PATTERNS[0], frequent_scan_queries),  # frequent-full-scan
            (ANTI_PATTERN_BY_ID["dynamodb-ap-05"], ordered_scan_queries),
            (DYNAMODB_ANTI_PATTERNS[1], complex_agg_queries),  # complex-aggregation
            (DYNAMODB_ANTI_PATTERNS[2], unbounded_queries),  # unbounded-result-set
            (ANTI_PATTERN_BY_ID["dynamodb-ap-04"], wide_transaction_queries),
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
        table_ids = sorted({t for q in matched_queries for t in (q.get("tables_accessed") or [])})
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
        table_ids = sorted({t for q in matched_queries for t in (q.get("tables_accessed") or [])})
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

    # The per-pattern bonuses (DynamoDB's sweet spots, and a denormalizable join's
    # moderate bonus and migration effort) are each fit entry's table_bonus /
    # complexity_bonus in src/catalog/patterns/*.toml (#921).
    bonus, complexity_bonus = catalog_patterns.table_bonuses("dynamodb", types)
    pattern += bonus
    complexity += complexity_bonus

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
        parts.append("Has primary key: maps to DynamoDB partition key.")
    if profile.size_mb <= 100:
        parts.append("Small table: cost-effective on DynamoDB.")

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
            f"{profile.foreign_key_count} foreign key(s): requires denormalization or GSI design."
        )
    if profile.has_joins and profile.max_join_count > 2:
        concerns.append(
            "Complex joins (>2): needs careful single-table design or separate queries."
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

# The DynamoDB on-demand request-unit rules (#1476), adapted from the AWS documentation
# (https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/read-write-operations.html;
# the transactional rate: .../developerguide/transaction-apis.html, every item in a
# transaction is read or written twice). A read unit covers 4 KB: one strongly consistent
# read, two eventually consistent reads, or half a transactional read. A Query is billed
# on the total size of the items it reads, rounded up to 4 KB once. A write unit covers
# 1 KB of one item, two when transactional; each item is rounded up on its own, and an
# update is billed on the larger of the item before and after it. The unit sizes are the
# engine file's (``src/catalog/engines/dynamodb.toml``), the prices the price table's.
EVENTUAL = "eventually consistent"
STRONG = "strongly consistent"
TRANSACTIONAL = "transactional"
STANDARD = "standard"
READ_UNITS_PER_BLOCK = {EVENTUAL: 0.5, STRONG: 1.0, TRANSACTIONAL: 2.0}
WRITE_UNITS_PER_BLOCK = {STANDARD: 1.0, TRANSACTIONAL: 2.0}

# The maintainer decision on #1000 (2026-10-24): stated whenever a read is priced
# eventually consistent because the collection can't tell what it is.
CONSISTENCY_FOOTNOTE = (
    "DynamoDB reads are priced eventually consistent, except transactions, which are "
    "priced at transactional rates."
)

_READ_TYPES = frozenset({"SELECT"})
_WRITE_TYPES = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})


def _blocks(nbytes: float, unit_bytes: int) -> int:
    """Whole units of ``unit_bytes`` that ``nbytes`` needs, at least one."""
    return max(1, math.ceil(max(0.0, nbytes) / unit_bytes - 1e-9))


def read_request_units(call_bytes: float, kind: str, unit_bytes: int) -> float:
    """Read request units one call bills: its bytes rounded up once to ``unit_bytes``
    (4 KB), at the rate of ``kind`` (``EVENTUAL``, ``STRONG`` or ``TRANSACTIONAL``)."""
    return _blocks(call_bytes, unit_bytes) * READ_UNITS_PER_BLOCK[kind]


def write_request_units(item_bytes: float, kind: str, unit_bytes: int, items: float = 1.0) -> float:
    """Write request units one call bills: each of its ``items`` rounded up to
    ``unit_bytes`` (1 KB) on its own, at the rate of ``kind`` (``STANDARD`` or
    ``TRANSACTIONAL``)."""
    return items * _blocks(item_bytes, unit_bytes) * WRITE_UNITS_PER_BLOCK[kind]


def _rows(query: dict, *fields: str) -> float:
    for name in fields:
        value = query.get(name)
        if isinstance(value, int | float) and value > 0:
            return max(1.0, float(value))
    return 1.0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def estimate_dynamodb_costs(
    collector_output: dict,
    target_region: str = "us-east-1",
    analysis_options=None,
) -> CostEstimate:
    """Estimate DynamoDB monthly costs based on workload characteristics.

    On-demand pricing (no capacity planning), on average load: each statement's call
    rate times the request units one call bills (#1476).

    - A read (``SELECT``) bills its bytes: the rows it returns (``rows_returned_avg``,
      at least one) times the average row of its tables (each table's ``size_mb`` over
      its ``row_count``, the per-query cost model's rule), rounded up to 4 KB once. A
      locking read (``SELECT ... FOR UPDATE``, read in the source dialect) reads the
      item it is about to write, so it is priced strongly consistent; every other read
      eventually consistent, with :data:`CONSISTENCY_FOOTNOTE` (the maintainer decision
      on #1000). The collection records no transaction boundaries, so no read is
      priced transactional.
    - A write bills each item it changes (``rows_affected_avg``, else
      ``rows_returned_avg``, at least one) at the average row of the tables it writes,
      rounded up to 1 KB per item. A write that changes a bounded set of items (2 to
      100, ``transaction_shape`` BOUNDED) keeps its atomicity as one
      ``TransactWriteItems`` call and is priced transactional; any other write at the
      standard rate.
    - Global secondary indexes are not modelled, so their write fan-out and storage
      are not included.

    A missing price or unit size raises (#1395).
    """
    tables = collector_output.get("database_schema", {}).get("tables", [])
    queries = collector_output.get("queries", {}).get("query_patterns", [])

    # Aggregate storage
    total_size_gb = sum(t.get("size_mb", 0) for t in tables) / 1024

    # On-demand pricing: the request unit and storage prices are the price table's
    # (``src/catalog/prices.toml``, #1196), through the engine file's configuration.
    engine_id = "dynamodb"
    prices = catalog.pricing(engine_id)
    read_price = catalog.required_price(
        engine_id, "read_unit_price_per_million", prices.read_unit_price_per_million
    )
    write_price = catalog.required_price(
        engine_id, "write_unit_price_per_million", prices.write_unit_price_per_million
    )
    storage_price = catalog.required_price(
        engine_id, "storage_price_per_gb_month", prices.storage_price_per_gb_month
    )
    read_bytes = int(catalog.required_price(engine_id, "read_unit_bytes", prices.read_unit_bytes))
    write_bytes = int(
        catalog.required_price(engine_id, "write_unit_bytes", prices.write_unit_bytes)
    )

    source_engine = source_engine_of(collector_output)
    scope = SourceScope.from_collector(collector_output)
    keys = SchemaKeys.from_collector(collector_output)
    widths = row_widths(collector_output)

    def width(names) -> float:
        return sum(widths.get(str(t), 0.0) for t in names or [])

    read_units = 0.0
    write_units = 0.0
    reads = {EVENTUAL: 0, STRONG: 0}
    writes = {STANDARD: 0, TRANSACTIONAL: 0}
    for q in queries:
        cps = float(q.get("calls_per_second") or 0.0)
        qt = str(q.get("query_type") or "").upper()
        if qt in _READ_TYPES:
            kind = STRONG if is_locking_read(q, source_engine) else EVENTUAL
            reads[kind] += 1
            call_bytes = _rows(q, "rows_returned_avg") * width(q.get("tables_accessed"))
            read_units += cps * read_request_units(call_bytes, kind, read_bytes)
        elif qt in _WRITE_TYPES:
            shape = transaction_shape(q, scope, keys)
            kind = TRANSACTIONAL if shape is not None and shape.shape == BOUNDED else STANDARD
            writes[kind] += 1
            items = _rows(q, "rows_affected_avg", "rows_returned_avg")
            item_bytes = width(written_tables(q, source_engine, scope))
            write_units += cps * write_request_units(item_bytes, kind, write_bytes, items)

    read_units_per_month = read_units * SECONDS_PER_MONTH
    write_units_per_month = write_units * SECONDS_PER_MONTH
    read_cost = (read_units_per_month / 1_000_000) * read_price
    write_cost = (write_units_per_month / 1_000_000) * write_price
    storage_cost = max(total_size_gb, 0.01) * storage_price

    total = round(read_cost + write_cost + storage_cost, 2)

    read_kb, write_kb = read_bytes // 1024, write_bytes // 1024
    rates: list[str] = []
    if reads[EVENTUAL]:
        rates.append(
            f"{_plural(reads[EVENTUAL], 'read')} priced eventually consistent "
            f"({READ_UNITS_PER_BLOCK[EVENTUAL]:g} RRU per {read_kb} KB)"
        )
    if reads[STRONG]:
        rates.append(
            f"{_plural(reads[STRONG], 'locking read')} (a read of an item it then writes) "
            f"priced strongly consistent ({READ_UNITS_PER_BLOCK[STRONG]:g} RRU per {read_kb} KB)"
        )
    if writes[STANDARD]:
        rates.append(
            f"{_plural(writes[STANDARD], 'write')} priced at the standard rate "
            f"({WRITE_UNITS_PER_BLOCK[STANDARD]:g} WRU per {write_kb} KB per item)"
        )
    if writes[TRANSACTIONAL]:
        rates.append(
            f"{_plural(writes[TRANSACTIONAL], 'multi-item write')} (2 to 100 items, kept "
            "atomic as one TransactWriteItems call) priced transactional "
            f"({WRITE_UNITS_PER_BLOCK[TRANSACTIONAL]:g} WRU per {write_kb} KB per item)"
        )
    return CostEstimate(
        monthly_cost_usd=total,
        cost_components={
            "read_request_units": round(read_cost, 2),
            "write_request_units": round(write_cost, 2),
            "storage_gb": round(storage_cost, 2),
            "read_request_units_per_month": round(read_units_per_month, 2),
            "write_request_units_per_month": round(write_units_per_month, 2),
            "pricing_mode": "on-demand",
            "list_prices": prices.list_prices,
            # #1196: the price table this estimate was priced from, by date.
            "price_list": price_basis(),
            # #1476: the lines the TCO states with this figure (``build_tco_analysis``).
            "footnotes": [CONSISTENCY_FOOTNOTE] if reads[EVENTUAL] else [],
        },
        pricing_assumptions=[
            "On-demand capacity mode (no provisioned throughput), on average load",
            f"Region: {target_region}",
            f"Reads billed on the bytes each call returns (rows times the table's average "
            f"row size), rounded up to {read_kb} KB",
            f"Writes billed on each item's size (the table's average row), rounded up to "
            f"{write_kb} KB; an update's new size is not collected, so the larger of the "
            "item before and after it is taken as that average",
            *rates,
            "Global secondary indexes are not modelled: their write and storage costs "
            "are not included",
            "No DAX, backups, or global tables included",
            *([CONSISTENCY_FOOTNOTE] if reads[EVENTUAL] else []),
        ],
    )


# ---------------------------------------------------------------------------
# Key-scoped writes (the DynamoDB schema designer's input, #1061)
# ---------------------------------------------------------------------------


def key_scoped_writes(collector_output: dict) -> list[KeyScopedWrite]:
    """Every key-scoped write of the collection and the key it needs (#915, #1061).

    The same engine-neutral ``transaction_shape`` the wide-transaction check and the
    decision trace read (``query_matches[].transaction_shape.schema_note``), over the
    whole collection, so the designer is given exactly what routing scored. The
    analysis contract carries it, so every designer input path (the external request,
    the Bedrock projection and each group's input) reads the same list."""
    scope = SourceScope.from_collector(collector_output)
    keys = SchemaKeys.from_collector(collector_output)
    columns_by_table = {
        str(t.get("table_id") or ""): {
            str(c.get("column_name") or "").lower(): c for c in t.get("columns") or []
        }
        for t in (collector_output.get("database_schema") or {}).get("tables") or []
    }
    out: list[KeyScopedWrite] = []
    for q in collector_output.get("queries", {}).get("query_patterns", []):
        shape = transaction_shape(q, scope, keys)
        if shape is None or shape.schema_note is None or shape.table_id is None:
            continue
        qid = str(q.get("query_id") or "")
        table_keys = keys.tables.get(shape.table_id)
        anchors = []
        for column in shape.anchors:
            cardinality = (
                columns_by_table.get(shape.table_id, {}).get(column, {}).get("cardinality")
            )
            lookups = table_keys.lookup_queries(column) if table_keys else ()
            anchors.append(
                KeyAnchor(
                    column=column,
                    cardinality=cardinality if isinstance(cardinality, int) else "unknown",
                    lookup_query_ids=[x for x in lookups if x != qid],
                )
            )
        out.append(
            KeyScopedWrite(
                query_id=qid,
                table_id=shape.table_id,
                columns=list(shape.anchors),
                schema_note=shape.schema_note,
                anchors=anchors,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Decision trace
# ---------------------------------------------------------------------------

_RELATIONSHIP: TypeAdapter[Relationship] = TypeAdapter(Relationship)


# ---------------------------------------------------------------------------
# Key-scoped counts (the DynamoDB schema designer's input, #377)
# ---------------------------------------------------------------------------

KEY_SCOPED_COUNT_NOTE = (
    "Served as a Query on this key with Select = COUNT, which reads and bills every "
    "matching item (1 MB per page, summed over the pages): give the key a partition key "
    "of an item collection (the base table's or a GSI's); for a low- or unknown-"
    "cardinality key, or a hot count, prefer a maintained counter item."
)


def key_scoped_counts(collector_output: dict) -> list[KeyScopedCount]:
    """Every key-scoped count of the collection (the ``key_scoped_count`` class fact,
    ADR-031 Decision 2 Amendment 12, #377), the key columns it pins and each one's
    collected cardinality (``"unknown"`` with no statistics).

    Routing may place such a count on DynamoDB when its table's writes are there; the
    analysis lists every one, and each design call keeps the ones it serves
    (``key_scoped_counts_in_scope``), so the designer is told the count's key and how
    selective it is, never routing's cardinality gate (there is none)."""
    from src.tools.sql.query_facts import source_engine_of

    keys = SchemaKeys.from_collector(collector_output)
    source_engine = source_engine_of(collector_output)
    columns_by_table = {
        str(t.get("table_id") or ""): {
            str(c.get("column_name") or "").lower(): c for c in t.get("columns") or []
        }
        for t in (collector_output.get("database_schema") or {}).get("tables") or []
    }
    out: list[KeyScopedCount] = []
    for q in collector_output.get("queries", {}).get("query_patterns", []):
        access = query_access(q, keys, source_engine)
        if KEY_SCOPED_COUNT_FACT not in access.classification.facts or len(access.scopes) != 1:
            continue
        (scope,) = access.scopes
        if len(scope.tables) != 1:
            continue
        (table_id,) = scope.tables
        pinned = sorted(
            scope.pinned.get(table_id, frozenset()) & scope.key_columns.get(table_id, frozenset())
        )
        if not pinned:
            continue
        stats = columns_by_table.get(table_id, {})
        out.append(
            KeyScopedCount(
                query_id=str(q.get("query_id") or ""),
                table_id=table_id,
                columns=pinned,
                keys=[
                    KeyScopedCountKey(
                        column=c,
                        cardinality=(
                            card
                            if isinstance(card := stats.get(c, {}).get("cardinality"), int)
                            else "unknown"
                        ),
                    )
                    for c in pinned
                ],
                schema_note=KEY_SCOPED_COUNT_NOTE,
            )
        )
    return out


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
    relationships: list[dict] | None = None,
    llm_status: str = "skipped",
    llm_duration: float = 0.0,
    llm_attempts: int = 0,
    llm_output: LlmAdvisorOutput | None = None,
) -> dict:
    """Build the decision trace artifact for specialist calibration.

    This is a separate artifact, not part of the agent-to-agent contract
    (ADR-017 Decision 3). It is built as ``DynamoDBDecisionTrace`` (#973), so
    every section is typed and the dict returned carries the contract's
    ``contract_version``; writers write it through the model again
    (``src/agents/analysis/decision_trace.py``).
    """
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    scope = SourceScope.from_collector(collector_output)
    keys = SchemaKeys.from_collector(collector_output)

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
        # Which transaction shape a write matched and why (ADR-031 Decision 3).
        shape = transaction_shape(q, scope, keys)
        query_matches.append(
            QueryMatch(
                query_id=qid,
                query_text_preview=(q.get("query_text") or "")[:120],
                matched_patterns=query_pattern_map.get(qid, []),
                matched_anti_patterns=query_anti_pattern_map.get(qid, []),
                transaction_shape=(
                    TransactionShapeTrace.model_validate(shape.as_trace())
                    if shape is not None
                    else None
                ),
            )
        )

    # Per-pattern summaries
    pattern_summaries = []
    for p in workload_analysis.patterns_detected:
        catalog_pattern = PATTERN_BY_ID.get(p.pattern_id)
        pattern_summaries.append(
            PatternSummary(
                pattern_id=p.pattern_id,
                pattern_type=p.pattern_type,
                catalog_base_score=catalog_pattern.base_score if catalog_pattern else None,
                queries_matched_count=len(p.query_ids or []),
                tables_involved=p.table_ids or [],
            )
        )

    # Per-table recommendation derivations
    derivations = [
        RecommendationDerivation.model_validate(
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
        for rec in table_recommendations
    ]

    trace = DynamoDBDecisionTrace(
        summary=TraceSummary(
            queries_analyzed=len(queries),
            queries_matched=len(matched_qids),
            queries_unmatched=len(queries) - len(matched_qids),
            patterns_detected=len(workload_analysis.patterns_detected),
            anti_patterns_detected=len(workload_analysis.anti_patterns_detected or []),
        ),
        query_matches=query_matches,
        pattern_summaries=pattern_summaries,
        recommendation_derivations=derivations,
        table_groups=[
            TableGroup(group_root=root, tables=members)
            for root, members in table_groups.items()
            if len(members) > 1
        ],
        # How the timestamp-column fact was read (#935): by type, or by name and why.
        timestamp_columns=TimestampColumnsTrace.model_validate(
            timestamp_column_trace(collector_output)
        ),
        # The v1.1 sections, from the detectors' dataclasses.
        pk_classifications=[
            PkClassificationTrace.model_validate(asdict(pk))
            for pk in (pk_classifications or {}).values()
        ],
        aggregates=[AggregateTrace.model_validate(asdict(a)) for a in aggregates or []],
        gsi_candidates=[GsiCandidateTrace.model_validate(asdict(g)) for g in gsi_candidates or []],
        denormalization_opportunities=[
            DenormalizationOpportunityTrace.model_validate(asdict(d))
            for d in denorm_opportunities or []
        ],
        secondary_index_dominance=[
            SecondaryIndexDominanceTrace.model_validate(asdict(s)) for s in si_dominance or []
        ],
        llm_advisor=LlmAdvisorTrace(
            status=llm_status,
            duration_seconds=llm_duration,
            attempts=llm_attempts,
            aggregate_key_designs=list(llm_output.aggregate_recommendations) if llm_output else [],
            denormalization_strategies=(
                list(llm_output.denormalization_strategies) if llm_output else []
            ),
        ),
        relationships=[_RELATIONSHIP.validate_python(r) for r in relationships or []],
    )
    return trace.model_dump(mode="json")


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

    for tid in sorted(all_table_ids):
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
