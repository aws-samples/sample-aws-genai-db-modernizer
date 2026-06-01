"""
DocumentDB Analysis Tools

Detection, scoring, cost estimation, embedding analysis, and decision trace
for Amazon DocumentDB migration assessment.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

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
from src.tools.analysis.documentdb_pattern_catalog import (
    DOCUMENTDB_ANTI_PATTERNS,
    DOCUMENTDB_PATTERNS,
    DOCUMENTDB_SCORE_WEIGHTS,
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
# Regex helpers
# ---------------------------------------------------------------------------

_AGGREGATION_RE = re.compile(
    r"\b(group\s+by|having|count\s*\(|sum\s*\(|avg\s*\(|min\s*\(|max\s*\()\b", re.I
)
_WINDOW_FN_RE = re.compile(r"\b(row_number|rank|dense_rank|ntile|lag|lead|over\s*\()\b", re.I)
_NEGATION_RE = re.compile(r"\b(not\s+in|not\s+exists|<>|!=)\b", re.I)
_CORRELATED_SUB_RE = re.compile(
    r"\(\s*select\b.*?\bwhere\b.*?\b\w+\.\w+\s*=\s*\w+\.\w+", re.I | re.S
)
_JSON_EXTRACT_RE = re.compile(r"\b(json_extract|json_unquote|json_value|->>|->)\b", re.I)
_RECURSIVE_RE = re.compile(r"\bwith\s+recursive\b", re.I)
_SESSION_RE = re.compile(r"\b(session|token|sess_id|session_id|login|auth)\b", re.I)


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
            is_junction = _is_junction_table(child_t)
            rel_type = (
                "many_to_many"
                if is_junction
                else "one_to_one"
                if avg_children <= 1.1
                else "one_to_many"
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


def _is_junction_table(t: dict) -> bool:
    fks = t.get("foreign_keys") or []
    pk = t.get("primary_key") or []
    return len(fks) >= 2 and len(pk) >= 2


def detect_polymorphic_tables(collector_output: dict) -> list[str]:
    """Detect tables with >30% nullable columns or type discriminator columns."""
    tables = (collector_output.get("database_schema") or {}).get("tables") or []
    result: list[str] = []
    type_keywords = {"type", "kind", "category", "class", "discriminator", "entity_type"}
    for t in tables:
        cols = t.get("columns") or []
        if not cols:
            continue
        nullable_count = sum(1 for c in cols if c.get("nullable", False))
        has_discriminator = any(c.get("column_name", "").lower() in type_keywords for c in cols)
        if nullable_count / len(cols) > 0.3 or has_discriminator:
            result.append(t.get("table_id", t.get("table_name", "")))
    return result


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
        "trade_offs": "Rule-based — LLM advisor was disabled or failed",
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
    risk_level: Literal["low", "medium", "high"] = "low"


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
        return (
            "Analyze the following database workload and produce DocumentDB embedding "
            "vs referencing decisions.\n\n"
            f"## Deterministic Results\n{deterministic_results}\n\n"
            f"## Schema\n{schema}\n\n"
            f"## Query Patterns\n{queries}\n\n"
            f"## Embedding Candidates\n{cand_data}\n\n"
            "For each candidate, decide: embed, reference, or hybrid."
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

    if not tables or not queries:
        return WorkloadAnalysis(patterns_detected=[], anti_patterns_detected=None)

    # Build lookup structures
    table_row_counts: dict[str, int] = {}
    table_col_counts: dict[str, int] = {}
    table_nullable_ratio: dict[str, float] = {}
    table_has_json: dict[str, bool] = {}
    table_fk_count: dict[str, int] = {}
    self_ref_tables: set[str] = set()

    for t in tables:
        tid = t.get("table_id", "")
        table_row_counts[tid] = t.get("row_count") or 0
        cols = t.get("columns") or []
        table_col_counts[tid] = len(cols)
        nullable = sum(1 for c in cols if c.get("nullable", False))
        table_nullable_ratio[tid] = nullable / max(len(cols), 1)
        table_has_json[tid] = any(
            (c.get("data_type") or "").lower() in ("json", "jsonb") for c in cols
        )
        fks = t.get("foreign_keys") or []
        table_fk_count[tid] = len(fks)
        for fk in fks:
            if fk.get("referenced_table", "") in (tid, t.get("table_name", "")):
                self_ref_tables.add(tid)

    polymorphic = set(detect_polymorphic_tables(collector_output))

    # Pattern accumulators
    content_mgmt: list[dict] = []
    product_catalog: list[dict] = []
    polymorphic_qs: list[dict] = []
    nested_doc: list[dict] = []
    agg_pipeline: list[dict] = []
    flexible_schema: list[dict] = []
    extended_ref: list[dict] = []
    write_time_agg: list[dict] = []

    # Anti-pattern accumulators
    cross_joins: list[dict] = []
    negation: list[dict] = []
    faceted: list[dict] = []
    window_fn: list[dict] = []
    correlated_sub: list[dict] = []
    graph_hierarchy: list[dict] = []

    for q in queries:
        text = q.get("query_text", "")
        text_lower = text.lower()
        qt = q.get("query_type", "").upper()
        accessed = q.get("tables_accessed") or []
        join_count = q.get("join_count") or 0
        has_joins = q.get("has_joins", False)
        cps = q.get("calls_per_second") or 0

        # --- Pattern matching (if, not elif — multi-match) ---

        # content-management: polymorphic table access
        for tid in accessed:
            if tid in polymorphic and table_col_counts.get(tid, 0) > 8:
                content_mgmt.append(q)
                break

        # product-catalog: many columns, many nullable, or JSON
        for tid in accessed:
            if table_col_counts.get(tid, 0) > 15 or table_has_json.get(tid, False):
                product_catalog.append(q)
                break

        # polymorphic-data: EAV or sparse columns
        for tid in accessed:
            if table_nullable_ratio.get(tid, 0) > 0.3:
                polymorphic_qs.append(q)
                break

        # nested-document: simple FK join (1-2 tables), bounded child
        if has_joins and join_count <= 2 and qt == "SELECT":
            for tid in accessed:
                if table_fk_count.get(tid, 0) >= 1:
                    nested_doc.append(q)
                    break

        # aggregation-pipeline: GROUP BY + agg functions
        if _AGGREGATION_RE.search(text) and not has_joins:
            agg_pipeline.append(q)

        # flexible-schema: JSON columns or JSON_EXTRACT
        if _JSON_EXTRACT_RE.search(text):
            flexible_schema.append(q)
        else:
            for tid in accessed:
                if table_has_json.get(tid, False) and qt == "SELECT":
                    flexible_schema.append(q)
                    break

        # extended-reference: join with small lookup table
        if has_joins and len(accessed) >= 2:
            has_small = any(table_row_counts.get(tid, 999999) < 1000 for tid in accessed)
            has_large = any(table_row_counts.get(tid, 0) > 10000 for tid in accessed)
            if has_small and has_large:
                extended_ref.append(q)

        # write-time-aggregation: multiple agg functions + JOIN
        if _AGGREGATION_RE.search(text) and has_joins:
            agg_count = len(_AGGREGATION_RE.findall(text))
            if agg_count >= 2:
                write_time_agg.append(q)

        # --- Anti-pattern matching ---

        # heavy cross-collection joins
        if has_joins and join_count >= 3:
            cross_joins.append(q)

        # negation-heavy
        if _NEGATION_RE.search(text) and cps >= 0.1:
            negation.append(q)

        # faceted analytics
        if "group by" in text_lower and text_lower.count(",") >= 2 and _AGGREGATION_RE.search(text):
            faceted.append(q)

        # window functions
        if _WINDOW_FN_RE.search(text):
            window_fn.append(q)

        # correlated subqueries
        if _CORRELATED_SUB_RE.search(text):
            correlated_sub.append(q)

        # graph/hierarchy traversal
        if _RECURSIVE_RE.search(text):
            graph_hierarchy.append(q)
        elif has_joins:
            for tid in accessed:
                if tid in self_ref_tables:
                    graph_hierarchy.append(q)
                    break

    patterns = _build_patterns_from_matches(
        [
            (DOCUMENTDB_PATTERNS[0], content_mgmt),
            (DOCUMENTDB_PATTERNS[1], product_catalog),
            (DOCUMENTDB_PATTERNS[2], polymorphic_qs),
            (DOCUMENTDB_PATTERNS[3], nested_doc),
            (DOCUMENTDB_PATTERNS[4], agg_pipeline),
            (DOCUMENTDB_PATTERNS[5], flexible_schema),
            (DOCUMENTDB_PATTERNS[6], extended_ref),
            (DOCUMENTDB_PATTERNS[7], write_time_agg),
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
        table_ids = list({t for q in matched_queries for t in (q.get("tables_accessed") or [])})
        query_ids = [q.get("query_id") for q in matched_queries if q.get("query_id")]
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
        table_ids = list({t for q in matched_queries for t in (q.get("tables_accessed") or [])})
        query_ids = [q.get("query_id") for q in matched_queries if q.get("query_id")]
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

    # Strong pattern bonuses
    if "content-management" in types:
        pattern += 12
    if "product-catalog" in types:
        pattern += 10
    if "nested-document" in types:
        pattern += 12
    if "write-time-aggregation" in types:
        pattern += 15
    if "extended-reference" in types:
        pattern += 10
    if "flexible-schema" in types:
        pattern += 8
    if "aggregation-pipeline" in types:
        pattern += 8

    # Schema flexibility reduces migration complexity
    if "polymorphic-data" in types or "flexible-schema" in types:
        complexity += 10

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
        parts.append(f"{profile.foreign_key_count} FK(s) — embedding candidate for DocumentDB.")
    if profile.has_joins and profile.max_join_count <= 2:
        parts.append("Simple joins can be eliminated via denormalization.")
    if profile.size_mb <= 100:
        parts.append("Small table — low storage cost on DocumentDB.")
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
        concerns.append("Large table (>10GB) — monitor DocumentDB storage I/O costs.")
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
    total_read_cps = 0.0
    total_write_cps = 0.0
    for q in queries:
        cps = q.get("calls_per_second") or 0
        qt = q.get("query_type", "").upper()
        if qt == "SELECT":
            total_read_cps += cps
        elif qt in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            total_write_cps += cps

    # DocumentDB pricing (us-east-1): db.r6g.large
    instance_hourly = 0.348
    instance_monthly = instance_hourly * 730  # ~$254

    read_ios_per_month = total_read_cps * 86400 * 30
    write_ios_per_month = total_write_cps * 86400 * 30
    io_cost = ((read_ios_per_month + write_ios_per_month) / 1_000_000) * 0.20

    storage_cost = max(total_size_gb, 0.01) * 0.10

    total = round(instance_monthly + io_cost + storage_cost, 2)

    return CostEstimate(
        monthly_cost_usd=total,
        cost_components={
            "instance": round(instance_monthly, 2),
            "io_requests": round(io_cost, 2),
            "storage_gb": round(storage_cost, 2),
            "instance_type": "db.r6g.large",
        },
        pricing_assumptions=[
            "Single db.r6g.large instance (no replicas)",
            f"Region: {target_region}",
            "I/O: $0.20 per million read/write requests",
            "Storage: $0.10 per GB-month",
            "No backups, snapshots, or data transfer included",
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
    embedding_candidates: list[EmbeddingCandidate] | None = None,
    polymorphic_tables: list[str] | None = None,
    denorm_strategies: list[dict] | None = None,
    llm_status: str = "skipped",
    llm_duration: float = 0.0,
    llm_attempts: int = 0,
) -> dict:
    """Build decision trace artifact for specialist calibration."""
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

    return {
        "trace_version": "1.0",
        "agent": "documentdb-analysis-agent",
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
        "embedding_candidates": [asdict(c) for c in embedding_candidates]
        if embedding_candidates
        else [],
        "polymorphic_tables": polymorphic_tables or [],
        "denormalization_strategies": denorm_strategies or [],
        "llm_advisor": {
            "status": llm_status,
            "duration_seconds": llm_duration,
            "attempts": llm_attempts,
        },
        "documentdb_compatibility": {
            "target_version": "8.0",
            "unsupported_features_detected": _detect_unsupported_features(queries),
        },
    }


def _detect_unsupported_features(queries: list[dict]) -> list[str]:
    """Detect SQL patterns that map to unsupported DocumentDB features."""
    features: set[str] = set()
    for q in queries:
        text = q.get("query_text", "")
        if _WINDOW_FN_RE.search(text):
            features.add("$setWindowFields (window functions)")
        if _RECURSIVE_RE.search(text):
            features.add("$graphLookup (recursive/hierarchical queries)")
        if _CORRELATED_SUB_RE.search(text):
            features.add("$lookup with let/pipeline (correlated subqueries)")
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
