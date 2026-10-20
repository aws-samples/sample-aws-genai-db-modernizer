"""
DocumentDB Analysis Tools

Detection, scoring, cost estimation, embedding analysis, and decision trace
for Amazon DocumentDB migration assessment.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter

from src.agents.prompt_framing import frame_untrusted
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
from src.contracts.decision_trace_base import (
    LlmAdvisorTrace,
    PatternSummary,
    QueryMatch,
    RecommendationDerivation,
    Relationship,
    TableGroup,
    TraceSummary,
)
from src.contracts.documentdb_decision_trace import (
    DenormalizationStrategyTrace,
    DocumentDBCompatibilityTrace,
    DocumentDBDecisionTrace,
    EmbeddingCandidateTrace,
)
from src.shared import catalog
from src.shared.catalog import patterns as catalog_patterns
from src.shared.catalog.prices import HOURS_PER_MONTH, SECONDS_PER_MONTH, price_basis
from src.shared.source_cache import (
    NO_READS_BASIS,
    CollectionCache,
    HitRatioBasis,
    bound_fields,
    combined_basis,
    read_hit_ratio,
    read_io_basis_text,
)
from src.tools.analysis.documentdb_pattern_catalog import (
    DOCUMENTDB_ANTI_PATTERNS,
    DOCUMENTDB_PATTERNS,
    DOCUMENTDB_SCORE_WEIGHTS,
    PATTERN_BY_ID,
    CatalogAntiPattern,
    CatalogPattern,
)
from src.tools.analysis.key_access import KeyAccessReader, is_junction_table
from src.tools.analysis.llm_advisor_base import LlmAdvisorBase
from src.tools.analysis.scoring import (
    TableProfile,
    build_table_profiles,
    compute_base_scores,
    compute_confidence,
)
from src.tools.analysis.variant_attributes import (
    ATTRIBUTE_ROWS,
    ENTITY_WITH_ATTRIBUTE_ROWS,
    TYPED_DOCUMENT,
    VariantAttributeSchema,
)
from src.tools.sql.query_facts import query_facts, source_engine_of

# ---------------------------------------------------------------------------
# Target version. The text detectors (aggregate calls, window functions,
# negation, correlated subqueries, JSON extraction, WITH RECURSIVE) are
# engine-neutral query facts in ``src.tools.sql.query_facts`` (#491).
# ---------------------------------------------------------------------------

# The DocumentDB version and cluster type the analysis (and the PE reviewer)
# judge compatibility against: a patch level, not just the major version, so
# "needs 8.0.2+" rules read unambiguously against it (8.0.2 is the patch level
# the supported-APIs page names for $facet, $setWindowFields and correlated
# $lookup).
DOCUMENTDB_TARGET_VERSION = "8.0.2"
DOCUMENTDB_TARGET_CLUSTER = "instance-based"


# ---------------------------------------------------------------------------
# Embedding candidate detection
# ---------------------------------------------------------------------------


@dataclass
class EmbeddingCandidate:
    parent_table: str
    child_table: str
    relationship_type: str  # one_to_one, one_to_many, many_to_many
    avg_children_per_parent: float
    co_access_ratio: float
    independent_child_reads: int
    child_write_frequency: float
    estimated_doc_size_kb: float
    exceeds_16mb: bool


def detect_embedding_candidates(collector_output: dict) -> list[EmbeddingCandidate]:
    """Analyze FK relationships for embed vs reference decisions."""
    tables = (collector_output.get("database_schema") or {}).get("tables") or []
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []

    table_map: dict[str, dict] = {}
    for t in tables:
        tid = t.get("table_id", "")
        table_map[tid] = t
        table_map[t.get("table_name", "")] = t

    # Query access index
    table_queries: dict[str, list[dict]] = {}
    for q in queries:
        for tid in q.get("tables_accessed") or []:
            table_queries.setdefault(tid, []).append(q)

    candidates: list[EmbeddingCandidate] = []
    for t in tables:
        parent_id = t.get("table_id", "")
        for fk in t.get("foreign_keys") or []:
            ref = fk.get("referenced_table", "")
            ref_table = table_map.get(ref)
            if not ref_table:
                continue
            ref_id = ref_table.get("table_id", ref)

            # Determine parent/child (the table with FK is the child)
            child_id, real_parent_id = parent_id, ref_id
            child_t, parent_t = t, ref_table

            child_rows = child_t.get("row_count") or 0
            parent_rows = parent_t.get("row_count") or 1
            avg_children = child_rows / max(parent_rows, 1)

            # Co-access: % of child queries that also access parent
            child_qs = table_queries.get(child_id, [])
            co_access = sum(
                1 for q in child_qs if real_parent_id in (q.get("tables_accessed") or [])
            )
            co_access_ratio = co_access / max(len(child_qs), 1)

            # Independent child reads
            independent = sum(
                1
                for q in child_qs
                if q.get("query_type", "").upper() == "SELECT"
                and real_parent_id not in (q.get("tables_accessed") or [])
            )

            # Child write frequency
            child_writes = sum(
                q.get("calls_per_second") or 0
                for q in child_qs
                if q.get("query_type", "").upper() in ("INSERT", "UPDATE", "DELETE")
            )

            # Size estimate
            child_size_kb = (child_t.get("size_mb") or 0) * 1024
            parent_size_kb = (parent_t.get("size_mb") or 0) * 1024
            est_doc_kb = (parent_size_kb / max(parent_rows, 1)) + (
                child_size_kb / max(parent_rows, 1)
            )

            # Relationship type
            is_junction = is_junction_table(child_t)
            rel_type = (
                "many_to_many"
                if is_junction
                else "one_to_one" if avg_children <= 1.1 else "one_to_many"
            )

            candidates.append(
                EmbeddingCandidate(
                    parent_table=real_parent_id,
                    child_table=child_id,
                    relationship_type=rel_type,
                    avg_children_per_parent=round(avg_children, 2),
                    co_access_ratio=round(co_access_ratio, 2),
                    independent_child_reads=independent,
                    child_write_frequency=round(child_writes, 2),
                    estimated_doc_size_kb=round(est_doc_kb, 2),
                    exceeds_16mb=est_doc_kb > 16000,
                )
            )
    return candidates


def detect_polymorphic_tables(collector_output: dict) -> list[str]:
    """The tables that hold an entity with variable attributes, from the same schema facts
    the variant-attributes pattern reads (``src.tools.analysis.variant_attributes``, #967):
    an entity-attribute-value table, or a typed entity (a discriminator column) with a JSON
    column. Nullability and a column merely named ``type`` are not evidence (#920)."""
    schema = VariantAttributeSchema.from_collector(collector_output)
    return sorted(set(schema.entity_columns) | (schema.typed & schema.documents))


# ---------------------------------------------------------------------------
# Fallback embedding strategies (when LLM disabled/fails)
# ---------------------------------------------------------------------------


def _fallback_embedding_strategy(c: EmbeddingCandidate) -> dict:
    if c.exceeds_16mb:
        strategy, risk = "reference", "low"
    elif c.relationship_type == "many_to_many":
        strategy, risk = "reference", "low"
    elif (
        c.avg_children_per_parent <= 100
        and c.co_access_ratio >= 0.5
        and c.child_write_frequency < 1.0
    ):
        strategy, risk = "embed", "low"
    else:
        strategy, risk = "reference", "low"
    return {
        "parent_table": c.parent_table,
        "child_table": c.child_table,
        "strategy": strategy,
        "rationale": f"Fallback rule: {c.relationship_type}, "
        f"avg_children={c.avg_children_per_parent}, co_access={c.co_access_ratio}",
        "trade_offs": "Rule-based: LLM advisor was disabled or failed",
        "risk_level": risk,
    }


# ---------------------------------------------------------------------------
# LLM Advisor — embedding vs referencing trade-offs
# ---------------------------------------------------------------------------


class DenormalizationStrategy(BaseModel):
    parent_table: str
    child_table: str
    strategy: Literal["embed", "reference", "hybrid"]
    rationale: str
    trade_offs: str
    hybrid_embedded_fields: list[str] | None = None
    estimated_document_size_kb: float | None = None
    # Required: an enum with a default becomes a nullable-typed enum in the Bedrock
    # tool schema, which Bedrock refuses (#1281).
    risk_level: Literal["low", "medium", "high"] = Field(
        description="Migration risk of this strategy: low, medium or high"
    )


class LlmDocumentDBOutput(BaseModel):
    denormalization_strategies: list[DenormalizationStrategy]
    collection_design_notes: str = ""


# ---------------------------------------------------------------------------
# Skill file loading
# ---------------------------------------------------------------------------

_DOCDB_SKILL_FILE = (
    Path(__file__).resolve().parents[2] / "skills" / "documentdb-analysis-advisor.md"
)


def load_docdb_advisor_prompt(skill_path: Path | None = None) -> str:
    """Load the DocumentDB analysis advisor system prompt from its skill file."""
    path = skill_path or _DOCDB_SKILL_FILE
    if not path.exists():
        raise FileNotFoundError(f"DocumentDB analysis advisor skill file not found: {path}")
    return path.read_text(encoding="utf-8")


class LlmDocumentDBAdvisor(LlmAdvisorBase):
    """DocumentDB-specific LLM advisor using the generic base class.

    Produces embedding vs referencing decisions for parent/child relationships.
    For large workloads (> 30 queries), automatically splits into groups.
    """

    def __init__(self, system_prompt: str | None = None, enabled: bool | None = None):
        prompt = system_prompt if system_prompt is not None else load_docdb_advisor_prompt()
        super().__init__(system_prompt=prompt, enabled=enabled)

    def _output_model(self) -> type[BaseModel]:
        return LlmDocumentDBOutput

    def _build_prompt(self, schema: dict, queries: list[dict], **kwargs) -> str:
        deterministic_results = kwargs.get("deterministic_results", {})
        embedding_candidates = kwargs.get("embedding_candidates", [])
        cand_data = [asdict(c) for c in embedding_candidates]
        workload = (
            f"## Deterministic Results\n{deterministic_results}\n\n"
            f"## Schema\n{schema}\n\n"
            f"## Query Patterns\n{queries}\n\n"
            f"## Embedding Candidates\n{cand_data}"
        )
        return (
            "Analyze the following database workload and produce DocumentDB embedding "
            "vs referencing decisions.\n\n"
            + frame_untrusted(workload, label="the database workload to analyze")
            + "\n\nFor each candidate, decide: embed, reference, or hybrid."
        )

    def _parse_result(self, result) -> LlmDocumentDBOutput | None:
        output = getattr(result, "structured_output", None)
        if isinstance(output, LlmDocumentDBOutput):
            return output
        return None

    def _merge_results(self, results: list) -> LlmDocumentDBOutput | None:
        all_strategies: list = []
        for r in results:
            all_strategies.extend(r.denormalization_strategies)
        if not all_strategies:
            return None
        return LlmDocumentDBOutput(denormalization_strategies=all_strategies)

    def _filter_kwargs_for_group(
        self, group_queries: list[dict], referenced_tables: set[str], **kwargs
    ) -> dict:
        embedding_candidates = kwargs.get("embedding_candidates", [])
        group_candidates = [
            c
            for c in embedding_candidates
            if c.parent_table in referenced_tables or c.child_table in referenced_tables
        ]
        return {
            "deterministic_results": kwargs.get("deterministic_results", {}),
            "embedding_candidates": group_candidates,
        }

    def advise(  # type: ignore[override]
        self,
        deterministic_results: dict,
        schema: dict,
        queries: list[dict],
        embedding_candidates: list[EmbeddingCandidate],
    ) -> LlmDocumentDBOutput | None:
        """Run LLM advisor. Delegates to base class with kwargs."""
        result = super().advise(
            schema=schema,
            queries=queries,
            deterministic_results=deterministic_results,
            embedding_candidates=embedding_candidates,
        )
        return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Per-query pattern detection
# ---------------------------------------------------------------------------


def analyze_documentdb_use_cases(collector_output: dict) -> WorkloadAnalysis:
    """Detect DocumentDB migration patterns per-query against the specialist catalog."""
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    tables = (collector_output.get("database_schema") or {}).get("tables") or []
    source_engine = source_engine_of(collector_output)

    if not tables or not queries:
        return WorkloadAnalysis(patterns_detected=[], anti_patterns_detected=None)

    # Build lookup structures
    table_row_counts: dict[str, int] = {}
    table_fk_count: dict[str, int] = {}
    self_ref_tables: set[str] = set()

    for t in tables:
        tid = t.get("table_id", "")
        table_row_counts[tid] = t.get("row_count") or 0
        fks = t.get("foreign_keys") or []
        table_fk_count[tid] = len(fks)
        for fk in fks:
            if fk.get("referenced_table", "") in (tid, t.get("table_name", "")):
                self_ref_tables.add(tid)

    # The variable-attribute reads (#920): an engine-neutral fact about the query's own
    # access pattern, never a column count or nullability.
    variant = VariantAttributeSchema.from_collector(collector_output)

    key_reader = KeyAccessReader.from_collector(collector_output, source_engine)

    # Pattern accumulators
    variant_reads: list[dict] = []
    nested_doc: list[dict] = []
    agg_pipeline: list[dict] = []
    json_reads: list[dict] = []
    extended_ref: list[dict] = []

    # Anti-pattern accumulators
    cross_joins: list[dict] = []
    negation: list[dict] = []
    faceted: list[dict] = []
    window_fn: list[dict] = []
    correlated_sub: list[dict] = []
    graph_hierarchy: list[dict] = []

    for q in queries:
        facts = query_facts(q.get("query_text", ""), source_engine)
        qt = q.get("query_type", "").upper()
        accessed = q.get("tables_accessed") or []
        join_count = q.get("join_count") or 0
        has_joins = q.get("has_joins", False)
        cps = q.get("calls_per_second") or 0

        # --- Pattern matching (if, not elif — multi-match) ---

        # The variable-attribute patterns test the read their catalog entry describes:
        # a key-anchored read of an entity with variable attributes (#920).
        # variant-attributes: one entity's attribute rows, a typed entity read with its
        # attribute rows, or a typed entity with a JSON column (#967: one pattern).
        if variant.reads(q, source_engine) & {
            ATTRIBUTE_ROWS,
            ENTITY_WITH_ATTRIBUTE_ROWS,
            TYPED_DOCUMENT,
        }:
            variant_reads.append(q)

        # nested-document: simple FK join (1-2 tables), bounded child
        if has_joins and join_count <= 2 and qt == "SELECT":
            for tid in accessed:
                if table_fk_count.get(tid, 0) >= 1:
                    nested_doc.append(q)
                    break

        # aggregation-pipeline: a GROUP BY that does more than de-duplicate, or an
        # aggregate over rows no key anchors, with no join (#967). One key's COUNT or SUM
        # is an indexed read (countDocuments on the key), not a $group pipeline.
        # ``aggregation`` (a GROUP BY or an aggregate call), not ``needs_aggregation``,
        # which a window function or FOUND_ROWS() alone also sets (review of #1093).
        if (
            facts.aggregation
            and not facts.dedup_only_group_by
            and not has_joins
            and (facts.group_by or not key_reader.access(q).keyed)
        ):
            agg_pipeline.append(q)

        # json-document-query: the query reads or builds JSON (the json_access fact,
        # #967). A JSON column on a table the query reads is a schema fact, not this
        # access pattern.
        if facts.json_access:
            json_reads.append(q)

        # extended-reference: join with small lookup table
        if has_joins and len(accessed) >= 2:
            has_small = any(table_row_counts.get(tid, 999999) < 1000 for tid in accessed)
            has_large = any(table_row_counts.get(tid, 0) > 10000 for tid in accessed)
            if has_small and has_large:
                extended_ref.append(q)

        # write-time-aggregation is a cross-engine design note, not a DocumentDB fit (#967).

        # --- Anti-pattern matching ---

        # heavy cross-collection joins
        if has_joins and join_count >= 3:
            cross_joins.append(q)

        # negation-heavy
        if facts.negation_word and cps >= 0.1:
            negation.append(q)

        # faceted analytics: several groupings of one row set in one statement
        # (GROUPING SETS, ROLLUP, CUBE), which DocumentDB serves only with $facet. A
        # GROUP BY over several columns is one $group with a compound _id, native on
        # every version, so it is not this pattern (#968).
        if facts.grouping_sets and facts.aggregation:
            faceted.append(q)

        # window functions: the ones $setWindowFields serves on 8.0.2+ (ranking,
        # running aggregates and the rest). LAG and LEAD ($shift) and NTILE (no bucketing operator)
        # run on no version: that is the offset_window hard gate, not this bounded
        # limit (#968), so a query whose only window functions are those is not one.
        if facts.window_function and (
            facts.ranking_window_function
            or facts.windowed_aggregate
            or not facts.offset_window_function
        ):
            window_fn.append(q)

        # correlated subqueries
        if facts.correlated_subquery_shape:
            correlated_sub.append(q)

        # graph/hierarchy traversal
        if facts.recursive_cte:
            graph_hierarchy.append(q)
        elif has_joins:
            for tid in accessed:
                if tid in self_ref_tables:
                    graph_hierarchy.append(q)
                    break

    by_type = {p.pattern_type: p for p in DOCUMENTDB_PATTERNS}
    patterns = _build_patterns_from_matches(
        [
            (by_type["variant-attributes"], variant_reads),
            (by_type["nested-document"], nested_doc),
            (by_type["aggregation-pipeline"], agg_pipeline),
            (by_type["json-document-query"], json_reads),
            (by_type["extended-reference"], extended_ref),
        ]
    )

    anti_patterns = _build_anti_patterns_from_matches(
        [
            (DOCUMENTDB_ANTI_PATTERNS[0], cross_joins),
            (DOCUMENTDB_ANTI_PATTERNS[4], negation),
            (DOCUMENTDB_ANTI_PATTERNS[5], faceted),
            (DOCUMENTDB_ANTI_PATTERNS[6], window_fn),
            (DOCUMENTDB_ANTI_PATTERNS[7], correlated_sub),
            (DOCUMENTDB_ANTI_PATTERNS[3], graph_hierarchy),
        ]
    )

    return WorkloadAnalysis(
        patterns_detected=patterns,
        anti_patterns_detected=anti_patterns or None,
    )


def _build_patterns_from_matches(
    matches: list[tuple[CatalogPattern, list[dict]]],
) -> list[Pattern]:
    patterns: list[Pattern] = []
    for catalog_pattern, matched_queries in matches:
        if not matched_queries:
            continue
        table_ids = sorted({t for q in matched_queries for t in (q.get("tables_accessed") or [])})
        query_ids = [str(q["query_id"]) for q in matched_queries if q.get("query_id")]
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
                frequency_percent=None,
            )
        )
    return patterns


def _build_anti_patterns_from_matches(
    matches: list[tuple[CatalogAntiPattern, list[dict]]],
) -> list[AntiPattern]:
    anti_patterns: list[AntiPattern] = []
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
# Scoring — per-table recommendations
# ---------------------------------------------------------------------------


def analyze_documentdb_patterns(
    collector_output: dict, workload_analysis: WorkloadAnalysis
) -> list[TableRecommendation]:
    """Generate per-table recommendations using shared scoring + DocumentDB adjustments."""
    from src.tools.analysis.dynamodb_analysis_tools import build_table_groups

    profiles = build_table_profiles(collector_output, workload_analysis)
    table_groups = build_table_groups(collector_output)

    table_to_group: dict[str, list[str]] = {}
    for _root, members in table_groups.items():
        if len(members) > 1:
            for tid in members:
                table_to_group[tid] = [m for m in members if m != tid]

    table_patterns: dict[str, list[Pattern]] = {}
    for p in workload_analysis.patterns_detected:
        for tid in p.table_ids or []:
            table_patterns.setdefault(tid, []).append(p)

    table_anti_patterns: dict[str, list[AntiPattern]] = {}
    for ap in workload_analysis.anti_patterns_detected or []:
        for tid in ap.table_ids or []:
            table_anti_patterns.setdefault(tid, []).append(ap)

    recommendations: list[TableRecommendation] = []
    for table_id, profile in profiles.items():
        base_scores = compute_base_scores(profile)
        tp = table_patterns.get(table_id, [])
        tap = table_anti_patterns.get(table_id, [])
        catalog_score = _compute_catalog_pattern_score(tp, collector_output)

        scores = ScoreBreakdown(
            pattern_match_score=max(0, min(catalog_score, 100)),
            complexity_score=base_scores.complexity_score,
            performance_score=base_scores.performance_score,
            cost_score=base_scores.cost_score,
        )
        scores = _apply_documentdb_adjustments(scores, profile)
        conf = compute_confidence(scores, weights=DOCUMENTDB_SCORE_WEIGHTS)

        recommendations.append(
            TableRecommendation(
                table_id=table_id,
                confidence_score=conf,
                rationale=_build_rationale(profile, tp),
                score_breakdown=scores,
                supporting_patterns=[p.pattern_id for p in tp],
                concerns=_build_concerns(profile, tap, table_to_group.get(table_id)),
                migration_complexity=_assess_complexity(profile, tap),
            )
        )
    return recommendations


def _compute_catalog_pattern_score(table_patterns: list[Pattern], collector_output: dict) -> int:
    if not table_patterns:
        return 0
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    query_cps: dict[str, float] = {
        q.get("query_id", ""): q.get("calls_per_second") or 0 for q in queries
    }
    weighted_sum = 0.0
    total_weight = 0.0
    for p in table_patterns:
        catalog = PATTERN_BY_ID.get(p.pattern_id)
        if not catalog:
            continue
        pattern_cps = sum(query_cps.get(qid, 0) for qid in (p.query_ids or []))
        weight = max(pattern_cps, 0.01)
        weighted_sum += catalog.base_score * weight
        total_weight += weight
    return int(weighted_sum / total_weight) if total_weight > 0 else 0


def _apply_documentdb_adjustments(scores: ScoreBreakdown, profile: TableProfile) -> ScoreBreakdown:
    pattern = scores.pattern_match_score
    complexity = scores.complexity_score
    performance = scores.performance_score
    cost = scores.cost_score
    types = set(profile.pattern_types)

    # FKs are BONUS for DocumentDB (embedding opportunity) — opposite of DynamoDB
    if profile.foreign_key_count >= 1:
        pattern += 10
    if profile.foreign_key_count >= 3:
        pattern += 5  # More FKs = richer document model

    # JOINs are neutral/slight bonus ($lookup works for simple cases)
    if profile.has_joins and profile.max_join_count <= 2:
        complexity += 5

    # The per-pattern bonuses, and schema flexibility's lower migration complexity, are
    # the fit entries' table_bonus / complexity_feature in src/catalog/patterns/*.toml
    # with the engine file's complexity_by_feature_count (#921).
    bonus, complexity_bonus = catalog_patterns.table_bonuses("documentdb", types)
    pattern += bonus
    complexity += complexity_bonus + catalog_patterns.feature_complexity_bonus("documentdb", types)

    # Large tables are fine in DocumentDB (instance-based, not per-item pricing)
    if profile.size_mb > 1000:
        cost += 5

    # Complex joins (3+) are a concern
    if profile.max_join_count > 2:
        complexity -= 15

    return ScoreBreakdown(
        pattern_match_score=max(0, min(pattern, 100)),
        complexity_score=max(0, min(complexity, 100)),
        performance_score=max(0, min(performance, 100)),
        cost_score=max(0, min(cost, 100)),
    )


def _build_rationale(profile: TableProfile, table_patterns: list[Pattern]) -> str:
    if not table_patterns:
        return f"No strong DocumentDB patterns detected for {profile.table_id}."
    names = [p.pattern_type.replace("-", " ") for p in table_patterns]
    parts = [f"Detected patterns: {', '.join(names)}."]
    if profile.foreign_key_count >= 1:
        parts.append(f"{profile.foreign_key_count} FK(s): embedding candidate for DocumentDB.")
    if profile.has_joins and profile.max_join_count <= 2:
        parts.append("Simple joins can be eliminated via denormalization.")
    if profile.size_mb <= 100:
        parts.append("Small table: low storage cost on DocumentDB.")
    return " ".join(parts)


def _build_concerns(
    profile: TableProfile,
    table_anti_patterns: list[AntiPattern],
    related_tables: list[str] | None = None,
) -> list[str]:
    concerns: list[str] = []
    for ap in table_anti_patterns:
        concerns.append(f"[{ap.anti_pattern_type}] {ap.recommendation or ap.description}")
    if related_tables:
        concerns.append(
            f"FK cluster: migrates together with {', '.join(related_tables)}. "
            "Consider embedding related tables into a single document."
        )
    if profile.size_mb > 10000:
        concerns.append("Large table (>10GB): monitor DocumentDB storage I/O costs.")
    return concerns


def _assess_complexity(
    profile: TableProfile, table_anti_patterns: list[AntiPattern]
) -> MigrationComplexity:
    high_severity = any(ap.severity_weight >= 0.7 for ap in table_anti_patterns)
    if high_severity or profile.max_join_count > 3:
        return MigrationComplexity.HIGH
    if profile.foreign_key_count > 0 or profile.has_joins or table_anti_patterns:
        return MigrationComplexity.MEDIUM
    return MigrationComplexity.LOW


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def estimate_documentdb_costs(
    collector_output: dict,
    target_region: str = "us-east-1",
    analysis_options=None,
) -> CostEstimate:
    """Estimate DocumentDB monthly costs. Instance-based + I/O + storage."""
    tables = (collector_output.get("database_schema") or {}).get("tables") or []
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []

    total_size_gb = sum(t.get("size_mb") or 0 for t in tables) / 1024
    # A read is billed an I/O only when it misses the buffer cache (the engine file's
    # ``reads_billed_on_cache_miss_only``): each read is estimated with its own hit
    # ratio, else the collection's, scaled down by this instance's memory over the
    # source's, else billed; at least 1% of every read is billed (#1197,
    # ``src.shared.source_cache``).
    engine_id = "documentdb"
    prices = catalog.pricing(engine_id)
    instance_type = prices.estimate_instance_class
    if instance_type is None:
        raise ValueError("src/catalog/engines/documentdb.toml sets no estimate_instance_class")
    target_memory_gb = prices.estimate_instance_memory_gb
    collection = CollectionCache.from_collector(collector_output)
    miss_only = prices.reads_billed_on_cache_miss_only
    total_read_cps = 0.0
    total_write_cps = 0.0
    bases: list[HitRatioBasis] = []
    for q in queries:
        cps = q.get("calls_per_second") or 0
        qt = (q.get("query_type") or "").upper()
        if qt == "SELECT":
            hit, basis = (
                read_hit_ratio(q, collection, target_memory_gb) if miss_only else (0.0, "none")
            )
            bases.append(basis)
            total_read_cps += cps * (1.0 - hit / 100.0)
        elif qt in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            total_write_cps += cps
    hit_basis = combined_basis(bases) if bases else NO_READS_BASIS
    bound = bound_fields(collection, target_memory_gb)

    # Every price is the price table's (src/catalog/prices.toml, #1196, #1208), through the
    # engine file's configuration (Standard storage), for the engine file's estimate instance.
    if instance_type not in prices.instance_hourly:
        raise ValueError(f"the price table has no {instance_type} price for {engine_id}")
    instance_hourly = prices.instance_hourly[instance_type]
    instance_monthly = instance_hourly * HOURS_PER_MONTH

    read_ios_per_month = total_read_cps * SECONDS_PER_MONTH
    write_ios_per_month = total_write_cps * SECONDS_PER_MONTH
    # This estimate charges one I/O a write and a read that misses the cache.
    io_price = catalog.required_price(
        engine_id, "read_unit_price_per_million", prices.read_unit_price_per_million
    )
    io_cost = ((read_ios_per_month + write_ios_per_month) / 1_000_000) * io_price

    storage_price = catalog.required_price(
        engine_id, "storage_price_per_gb_month", prices.storage_price_per_gb_month
    )
    storage_cost = max(total_size_gb, 0.01) * storage_price

    total = round(instance_monthly + io_cost + storage_cost, 2)

    return CostEstimate(
        monthly_cost_usd=total,
        cost_components={
            "instance": round(instance_monthly, 2),
            "io_requests": round(io_cost, 2),
            "storage_gb": round(storage_cost, 2),
            "instance_type": instance_type,
            # #1196: the price-table configuration every figure above is priced on.
            "list_prices": prices.list_prices,
            # #1196: the price table this estimate was priced from, by date.
            "price_list": price_basis(),
            # #1197: where the hit ratio the reads were billed with came from, and the
            # bound applied to the collection's.
            "read_io_hit_ratio_basis": hit_basis,
            **bound,
        },
        pricing_assumptions=[
            f"Single {instance_type} instance (no replicas) at ${instance_hourly}/hr",
            f"Region: {target_region}",
            f"{prices.configuration} storage I/O: ${io_price} per million read/write requests",
            read_io_basis_text(hit_basis, bound, has_reads=bool(bases)),
            f"Storage: ${storage_price} per GB-month",
            "No backups, snapshots, or data transfer included",
        ],
    )


# ---------------------------------------------------------------------------
# Decision trace
# ---------------------------------------------------------------------------

_RELATIONSHIP: TypeAdapter[Relationship] = TypeAdapter(Relationship)


def build_decision_trace(
    collector_output: dict,
    workload_analysis: WorkloadAnalysis,
    table_recommendations: list[TableRecommendation],
    *,
    embedding_candidates: list[EmbeddingCandidate] | None = None,
    polymorphic_tables: list[str] | None = None,
    denorm_strategies: list[dict] | None = None,
    llm_status: str = "skipped",
    llm_duration: float = 0.0,
    llm_attempts: int = 0,
    relationships: list[dict] | None = None,
) -> dict:
    """Build the decision trace artifact for specialist calibration.

    It is built as ``DocumentDBDecisionTrace`` (#1188), so a section the contract
    refuses fails here, and returned as the dict the one writer stores
    (``src/agents/analysis/decision_trace.py``).
    """
    from src.tools.analysis.dynamodb_analysis_tools import build_table_groups

    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    table_groups = build_table_groups(collector_output)

    # Per-query match lookup
    query_pattern_map: dict[str, list[str]] = {}
    query_anti_pattern_map: dict[str, list[str]] = {}
    for p in workload_analysis.patterns_detected:
        for qid in p.query_ids or []:
            query_pattern_map.setdefault(qid, []).append(p.pattern_id)
    for ap in workload_analysis.anti_patterns_detected or []:
        for qid in ap.query_ids or []:
            query_anti_pattern_map.setdefault(qid, []).append(ap.anti_pattern_id)

    matched_qids = set(query_pattern_map.keys()) | set(query_anti_pattern_map.keys())

    query_matches = [
        {
            "query_id": q.get("query_id", ""),
            "query_text_preview": (q.get("query_text") or "")[:120],
            "matched_patterns": query_pattern_map.get(q.get("query_id", ""), []),
            "matched_anti_patterns": query_anti_pattern_map.get(q.get("query_id", ""), []),
        }
        for q in queries
    ]

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

    derivations = [
        {
            "table_id": rec.table_id,
            "segments_contributing": rec.supporting_patterns or [],
            "score_breakdown": {
                "pattern_match": rec.score_breakdown.pattern_match_score,
                "complexity": rec.score_breakdown.complexity_score,
                "performance": rec.score_breakdown.performance_score,
                "cost": rec.score_breakdown.cost_score,
            },
            "weights_used": DOCUMENTDB_SCORE_WEIGHTS,
            "weighted_confidence": rec.confidence_score,
        }
        for rec in table_recommendations
    ]

    trace = DocumentDBDecisionTrace(
        summary=TraceSummary(
            queries_analyzed=len(queries),
            queries_matched=len(matched_qids),
            queries_unmatched=len(queries) - len(matched_qids),
            patterns_detected=len(workload_analysis.patterns_detected),
            anti_patterns_detected=len(workload_analysis.anti_patterns_detected or []),
        ),
        query_matches=[QueryMatch.model_validate(m) for m in query_matches],
        pattern_summaries=[PatternSummary.model_validate(p) for p in pattern_summaries],
        recommendation_derivations=[
            RecommendationDerivation.model_validate(d) for d in derivations
        ],
        table_groups=[
            TableGroup(group_root=root, tables=members)
            for root, members in table_groups.items()
            if len(members) > 1
        ],
        embedding_candidates=[
            EmbeddingCandidateTrace.model_validate(asdict(c)) for c in embedding_candidates or []
        ],
        polymorphic_tables=polymorphic_tables or [],
        denormalization_strategies=[
            DenormalizationStrategyTrace.model_validate(d) for d in denorm_strategies or []
        ],
        llm_advisor=LlmAdvisorTrace(
            status=llm_status, duration_seconds=llm_duration, attempts=llm_attempts
        ),
        documentdb_compatibility=DocumentDBCompatibilityTrace(
            target_version=DOCUMENTDB_TARGET_VERSION,
            cluster_type=DOCUMENTDB_TARGET_CLUSTER,
            version_dependent_features_detected=_detect_version_dependent_features(
                queries, source_engine_of(collector_output)
            ),
            unsupported_features_detected=_detect_unsupported_features(
                queries, source_engine_of(collector_output)
            ),
        ),
        relationships=[_RELATIONSHIP.validate_python(r) for r in relationships or []],
    )
    return trace.model_dump(mode="json")


def _detect_version_dependent_features(queries: list[dict], source_engine: str = "") -> list[str]:
    """Detect SQL patterns whose DocumentDB operator needs a recent 8.0 release.

    Each needs a DocumentDB 8.0 instance-based cluster (not 5.0 or earlier,
    not elastic clusters), per the AWS DocumentDB supported-APIs page.
    """
    features: set[str] = set()
    for q in queries:
        facts = query_facts(q.get("query_text", ""), source_engine)
        if facts.ranking_window_function or facts.windowed_aggregate:
            features.add("$setWindowFields (ranking and window aggregates): needs 8.0.2+")
        elif facts.window_function and not facts.offset_window_function:
            # A window function neither list names (FIRST_VALUE, PERCENT_RANK,
            # a nested aggregate ...): still flagged as the anti-pattern, so it
            # still gets an entry rather than silently none.
            features.add("window function: check $setWindowFields support (needs 8.0.2+)")
        if facts.recursive_cte:
            features.add("$graphLookup (recursive/hierarchical queries): needs 8.0")
        if facts.correlated_subquery_shape:
            features.add("$lookup with let/pipeline (correlated subqueries): needs 8.0.2+")
    return sorted(features)


def _detect_unsupported_features(queries: list[dict], source_engine: str = "") -> list[str]:
    """Detect SQL patterns DocumentDB has no operator for on any version.

    LAG/LEAD need ``$shift`` and NTILE has no equivalent; the supported-APIs
    page lists neither on any version or cluster type.
    """
    features: set[str] = set()
    for q in queries:
        if query_facts(q.get("query_text", ""), source_engine).offset_window_function:
            features.add("LAG/LEAD/NTILE (offset and bucketing window functions)")
    return sorted(features)


# ---------------------------------------------------------------------------
# Mermaid ER diagram with embedding annotations
# ---------------------------------------------------------------------------


def generate_mermaid_er_diagram(
    collector_output: dict,
    embedding_candidates: list[EmbeddingCandidate] | None = None,
) -> str:
    """Generate Mermaid ER diagram annotated with embedding decisions."""
    from src.tools.analysis.dynamodb_analysis_tools import detect_relationships

    relationships = detect_relationships(collector_output)
    tables = (collector_output.get("database_schema") or {}).get("tables") or []

    # Build embed lookup
    embed_pairs: set[tuple[str, str]] = set()
    if embedding_candidates:
        for c in embedding_candidates:
            if (
                c.co_access_ratio >= 0.5
                and not c.exceeds_16mb
                and c.relationship_type != "many_to_many"
            ):
                embed_pairs.add((c.parent_table, c.child_table))

    lines = ["erDiagram"]
    for t in tables:
        tid = t.get("table_id", "")
        safe = tid.replace(".", "_").replace("-", "_")
        cols = t.get("columns") or []
        lines.append(f"    {safe} {{")
        for col in cols[:10]:
            cname = col.get("column_name", "")
            dtype = (col.get("data_type") or "string")[:20]
            lines.append(f"        {dtype} {cname}")
        if len(cols) > 10:
            lines.append(f"        string _plus_{len(cols) - 10}_more")
        lines.append("    }")

    for rel in relationships:
        src = rel.get("source_table", "").replace(".", "_").replace("-", "_")
        tgt = rel.get("target_table", "").replace(".", "_").replace("-", "_")
        rel_type = rel.get("relationship_type", "1:N")
        is_embed = (rel.get("source_table", ""), rel.get("target_table", "")) in embed_pairs or (
            rel.get("target_table", ""),
            rel.get("source_table", ""),
        ) in embed_pairs

        arrow = "||--o{" if rel_type == "1:N" else "||--||" if rel_type == "1:1" else "}o--o{"
        label = "embeds" if is_embed else "references"
        lines.append(f"    {src} {arrow} {tgt} : {label}")

    return "\n".join(lines)
