"""
Synthesis report builder — transforms pipeline artifacts into RefereeOutputContract.

Deterministic logic for:
- Architecture recommendation (single/multi/hybrid based on engine selection)
- Table mappings (from schema design source_tables → target engine)
- TCO analysis (aggregate cost estimates from analysis outputs)
- Risk assessment (from anti-patterns, migration notes, unsupported patterns)
- Query group summary (from schema design pattern_groups)

The executive summary narrative is generated separately (LLM or template).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Collection, Iterable
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from src.agents.prompt_framing import SYSTEM_PROMPT_DATA_DIRECTIVE, frame_untrusted
from src.agents.referee.assigned_cost import (
    CACHE_MISS_ASSUMPTION,
    CACHE_OVERLAY,
    sized_cost_estimate,
    storage_gb,
    unqueried_assumption,
    unqueried_tables,
)
from src.agents.referee.assignment_resolver import retained_engine_for
from src.agents.referee.aurora_choice import source_database_engine
from src.agents.referee.cache_overlay import (
    CACHE_OVERLAY_ENGINES,
    PATTERN_LABELS,
    candidate_summary,
    overlay_summary,
)
from src.agents.referee.engine_scope import SCOPE_BASIS
from src.agents.referee.routed_confidence import RoutedFit, routed_fits
from src.agents.referee.schema_fields import field_list
from src.agents.referee.substring_match import substring_match_risks
from src.agents.referee.synthesis_grounding import (
    display_name,
    engine_mentions,
    ground_risks,
    recommends_engine,
)
from src.agents.referee.table_resolution import PSEUDO_TABLES, SourceScope, TableNameResolver
from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.agents.referee.utility_statements import is_utility_statement
from src.agents.schema_design.dynamodb_merge import OVERLAP_PREFIX, merge_overlaps, overlap_home
from src.agents.schema_design.scope import covered_query_ids
from src.contracts.executive_summary_response import (
    ExecutiveSummaryResponse,
    parse_executive_summary_response,
)
from src.contracts.model_flag import validation_error_note
from src.shared.bedrock_guardrail import GuardrailConfigError, GuardrailIntervened
from src.shared.capability_labels import CAPABILITY_DISPLAY_NAMES
from src.shared.design_text import severe_risk_strategy
from src.shared.engine_capabilities import PATTERN_ONLY_ENGINES
from src.shared.engine_names import (
    SOURCE_ENGINE_DISPLAY_NAMES,
    heterogeneous_source_clause,
    scoped_source_clause,
)
from src.shared.engines_in_use import retired_in_use_statement
from src.shared.migration_risk_rules import risk_sentence
from src.shared.migration_wave_engines import cache_front_description
from src.shared.models import Role, build_bedrock_model, resolve_model_id
from src.shared.progress import say
from src.shared.ranking import PARTIAL_NOTE, SIGNAL_ONLY_NOTE, evidence_note, fit_text
from src.shared.report_facts import (
    analysis_overview_facts,
    mean_table_fit,
    table_fit_score,
)
from src.shared.risk_severity import overall_risk_level
from src.shared.signal_labels import signal_noun
from src.shared.skill_sections import skill_sections, skill_subsections
from src.shared.source_cache import cost_basis_fields
from src.shared.unsupported_pattern import (
    is_dedup_only_group_by,
    unsupported_pattern_ids,
    unsupported_pattern_label,
    unsupported_pattern_mitigation,
    unsupported_pattern_text,
)
from src.shared.workload_fit_labels import PINNED_UTILITY_LABEL
from src.tools.analysis.aurora_common_pattern_catalog import (
    CATALOG_ENGINE_GUIDANCE,
    CATALOG_ENGINE_HINTS,
    FULL_TEXT_SEARCH_GUIDANCE,
    HIGH_VOLUME_TEXT_SEARCH_GUIDANCE,
    TIME_SERIES_INGEST_GUIDANCE,
)
from src.tools.sql.query_facts import source_engine_of

if TYPE_CHECKING:
    from src.agents.referee.synthesis_data import SynthesisData

logger = logging.getLogger(__name__)

AURORA_ENGINES = frozenset({"aurora_mysql", "aurora_postgresql"})

# #478: a query the collector could not name a real table for
# comes back with ``tables_accessed: ["unknown"]`` -- a placeholder, not a
# fact, since the collector bug behind it (REFRESH MATERIALIZED VIEW,
# information_schema reads it currently misses) is tracked separately. Two
# distinct groups replace the generic "unknown"/"(no source table)" label a
# query like this would otherwise land in: a real utility/session/DDL
# statement (``utility_statements.is_utility_statement``, already routed to
# the relational engine by the assignment -- SHOW/SET/EXPLAIN, catalog
# introspection) gets its own readable group, and a query that is not one of
# those but still names only pseudo tables (``table_resolution.PSEUDO_TABLES``
# -- "unknown", MySQL's dummy ``DUAL``) gets a group that says the collector,
# not the query, is why. Neither is a real access pattern a reader should see
# ranked as a "busiest" group -- every consumer that picks top/leading groups
# (the deterministic summary, the grounding fallback summary,
# ``build_effective_architecture``) excludes both exact strings.
UTILITY_GROUP_LABEL = "Utility and session statements"
UNRESOLVED_TABLE_GROUP_LABEL = "Table not identified by the collector"

_RELATIONAL_REASON_MAX_CHARS = 180
# The collector's own placeholder for a table it could not name (#483: some
# of those statements do have a real table it missed) -- replaced with a
# readable phrase wherever it surfaces inside copied assignment-reason
# prose, matching word boundaries so it never touches a real identifier
# that merely contains "unknown" as a substring.
_UNKNOWN_WORD_RE = re.compile(r"\bunknown\b", re.IGNORECASE)


def _dedupe_reason_clauses(reason: str) -> str:
    """Drop redundant clauses from an assignment reason (#478).

    A query's own ``assignment_reason`` can state the same base reason twice
    -- once alone, once again as the prefix of a fuller clause (seen on the
    real wordpress sample: "highest fit for Aurora MySQL; highest fit for
    Aurora MySQL | excluded from ElastiCache: it joins more than one table,
    and a key-value store can't serve joins"), from how the
    assignment resolver accumulates reasons across co-dependency
    propagation. Split on "; " (how the resolver joins
    separate reasons); drop a clause that exactly repeats an earlier one, or
    that is only a strict prefix of a later, fuller one -- keeping the
    fuller clause, not the redundant short one. Order preserved.
    """
    if not reason:
        return reason
    clauses = reason.split("; ")
    kept: list[str] = []
    for i, clause in enumerate(clauses):
        if clause in kept:
            continue
        if any(later != clause and later.startswith(clause) for later in clauses[i + 1 :]):
            continue
        kept.append(clause)
    return "; ".join(kept)


# The utility pin's reason as the assignment resolver writes it (#327, #710).
_UTILITY_PIN_REASON_RE = re.compile(
    r"utility/metadata statement, kept on the (source-compatible|chosen) relational engine "
    r"\(([^()]+)\)"
)


def _readable_utility_reason(match: re.Match[str]) -> str:
    """The utility pin's reason in plain words (#1220): what the statement is and why
    it stays, not the rule's label."""
    which = "source-compatible" if match.group(1) == "source-compatible" else "chosen relational"
    return f"administration or metadata statement, kept on {match.group(2)} as the {which} engine"


def _clean_relational_reason(
    reason: str | None, max_chars: int = _RELATIONAL_REASON_MAX_CHARS
) -> str:
    """A relational engine's per-query reason, ready to sit next to its excerpt.

    Deduplicated (see above), the collector's "unknown" table placeholder
    replaced by a readable phrase, the utility pin's label worded as a sentence
    (#1220), and capped by dropping whole exclusion clauses (never mid-sentence, #951).
    """
    cleaned = _dedupe_reason_clauses(reason or "")
    cleaned = _UNKNOWN_WORD_RE.sub("an unresolved table", cleaned)
    cleaned = _UTILITY_PIN_REASON_RE.sub(_readable_utility_reason, cleaned)
    if len(cleaned) <= max_chars:
        return cleaned
    # #951: never cut mid-sentence. The base reason (before " | ") says why the
    # query is on this engine and is kept whole; the other engines' exclusions
    # after it are dropped whole, from the end, and counted.
    base, sep, rest = cleaned.partition(" | ")
    if not sep:
        return cleaned
    clauses = rest.split("; ")
    kept = list(clauses)
    while kept and len(f"{base} | {'; '.join(kept)}") > max_chars:
        kept.pop()
    if not kept:
        # Not even one exclusion fits: the base reason alone, with no count that
        # would read as more reasons for this engine.
        return base
    dropped = len(clauses) - len(kept)
    more = f" (+{dropped} more)" if dropped else ""
    return f"{base} | {'; '.join(kept)}{more}"


def reason_text_for(group: dict, access_pattern: dict) -> str:
    """An access pattern's reason text, wherever it actually lives.

    A relational engine's entry (``build_query_groups``'s relational branch,
    #478) carries ``reason_index`` into the group's own ``reasons`` list
    instead of a copy of the string -- 146 distinct reasons backed 1281
    access patterns on the discourse sample, so copying it onto every one of
    them was pure duplication. The generic loop's entries (every other
    engine) still carry the string directly as ``description``, read
    straight from that engine's own schema design. Every consumer reads
    through this function rather than ``access_pattern.get("description")``
    so neither shape has to know about the other.
    """
    reason_index = access_pattern.get("reason_index")
    if isinstance(reason_index, int):
        reasons = group.get("reasons") or []
        if 0 <= reason_index < len(reasons):
            # A ``None`` entry (shouldn't happen, but never worth a literal
            # "None" in a reader-facing cell) resolves the same way the JS
            # ``reasonTextFor`` already does.
            value = reasons[reason_index]
            return str(value) if value else ""
        return ""
    return str(access_pattern.get("description") or "")


def schema_table_defs(engine: str, schema: dict) -> list[dict]:
    """Return an engine's schema output as ``table_definitions``-shaped dicts.

    Each entry has at least ``table_name``, ``source_tables`` and
    ``aggregate_pattern``. DynamoDB already emits this shape. The other engines
    use their own containers:

    - OpenSearch: ``index_designs`` + ``data_stream_designs``
    - DocumentDB: ``collections``
    - ElastiCache: ``key_designs`` (one entry per key pattern)
    - Aurora: ``table_definitions`` without ``source_tables``, because tables
      carry over 1:1. Each maps back to ``<source_database>.<table_name>``, the
      ``table_id`` form analysis uses.

    Never mutates ``schema``.
    """
    table_defs = list(schema.get("table_definitions") or [])

    if engine in AURORA_ENGINES:
        source_db = schema.get("source_database", "")
        return [
            {
                **t,
                "source_tables": t.get("source_tables")
                or [f"{source_db}.{t['table_name']}" if source_db else t["table_name"]],
                "aggregate_pattern": "relational_table",
            }
            for t in table_defs
        ]
    if table_defs:
        return table_defs

    if engine == "opensearch":
        return [
            {
                "table_name": idx.get("index_name", ""),
                "source_tables": field_list(idx, "source_tables"),
                "aggregate_pattern": "search_index",
            }
            for idx in (schema.get("index_designs") or [])
        ] + [
            {
                "table_name": ds.get("data_stream_name", ""),
                "source_tables": field_list(ds, "source_tables"),
                "aggregate_pattern": "data_stream",
            }
            for ds in (schema.get("data_stream_designs") or [])
        ]
    if engine == "documentdb":
        return [
            {
                "table_name": coll.get("collection_name", ""),
                "source_tables": field_list(coll, "source_tables"),
                "aggregate_pattern": "document_collection",
            }
            for coll in (schema.get("collections") or [])
        ]
    if engine == "elasticache":
        return [
            {
                "table_name": kd.get("key_pattern", ""),
                "source_tables": field_list(kd, "source_tables"),
                "aggregate_pattern": kd.get("data_type", "unknown"),
            }
            for kd in (schema.get("key_designs") or [])
        ]
    return []


def _compute_assignment_distribution(data: SynthesisData) -> dict:
    """Compute per-engine workload distribution from assignment data.

    Returns a dict keyed by engine name with:
      query_count, in_scope_count, workload_percent, top_reasons
    Returns empty dict if no assignment exists.

    #562: this used to also count each engine's table-level ``primary_engine``
    plurality (``primary_table_count`` -> ``ranking[].primary_tables``), but
    nothing ever displayed that count -- the query is the unit of
    assignment (ADR-031 Decision 1), and this field was exactly the
    table-level summary the migration-waves fix replaced. Dropped rather
    than recomputed, so nothing can display it later; a per-engine table
    count a reader actually needs is ``migration_waves[].table_count``,
    scoped by the engine's own queries.
    """
    if not data.assignment:
        return {}

    query_assignments = data.assignment.get("query_assignments", [])
    total_queries = len(query_assignments)
    if total_queries == 0:
        return {}

    # Build per-engine stats
    engine_stats: dict[str, dict] = {}
    for qa in query_assignments:
        eng = qa.get("assigned_engine", "")
        if eng not in engine_stats:
            engine_stats[eng] = {
                "query_count": 0,
                "in_scope_count": 0,
                "reasons": [],
            }
        engine_stats[eng]["query_count"] += 1
        if qa.get("in_scope", True):
            engine_stats[eng]["in_scope_count"] += 1
        reason = qa.get("assignment_reason", "")
        if reason:
            engine_stats[eng]["reasons"].append(reason)

    result = {}
    for eng, stats in engine_stats.items():
        # Top reasons by frequency
        reason_counts: dict[str, int] = {}
        for r in stats["reasons"]:
            reason_counts[r] = reason_counts.get(r, 0) + 1
        top_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:3]

        result[eng] = {
            "query_count": stats["query_count"],
            "in_scope_count": stats["in_scope_count"],
            "workload_percent": round(stats["query_count"] / total_queries * 100, 1),
            "top_reasons": [r[0] for r in top_reasons],
        }

    return result


def build_cache_overlay(data: SynthesisData) -> dict | None:
    """The cache layer's view of the workload (#296), or None when nothing is cached.

    ElastiCache owns no query, so the owner distribution (``workload_percent``) never
    counts it. This is the separate view: the in-scope queries it fronts, their share
    of calls, their owner engines, and the notes of the post-schema safety net.

    #459 review: ``notes`` is the full ``cache_notes`` audit trail (safety-net
    drops, customer-edit notes with full query hashes, the legacy-migration
    note) -- internal, for the engineering report only. ``safety_net_notes`` is
    the customer-facing subset: only the safety net's own notes, the ones every
    other deliverable (the Results page, chat) must read instead.
    """
    if not data.assignment:
        return None
    qas = data.assignment.get("query_assignments", [])
    summary = overlay_summary(qas, data.source_queries)
    # Drops and notes persist on the assignment (cache_dropped, cache_notes,
    # cache_safety_net_notes), so a re-run of synthesis reports them too, not
    # only the run that dropped them.
    dropped = list(
        dict.fromkeys(
            [
                *data.cache_overlay_dropped,
                *(qa["query_id"] for qa in qas if qa.get("cache_dropped")),
            ]
        )
    )
    notes = list(
        dict.fromkeys([*(data.assignment.get("cache_notes") or []), *data.cache_overlay_notes])
    )
    safety_net_notes = list(
        dict.fromkeys(
            [*(data.assignment.get("cache_safety_net_notes") or []), *data.cache_overlay_notes]
        )
    )
    if summary is None and not notes and not dropped:
        return None
    out = dict(summary or {})
    # #1252: the reads routing put under the cache (the gate's figure), apart from
    # ``query_count``, the reads the cache's design covers.
    out["candidate_query_count"], out["candidate_call_share_percent"] = candidate_summary(
        qas, data.source_queries
    )
    out["dropped_query_ids"] = dropped
    out["notes"] = notes
    out["safety_net_notes"] = safety_net_notes
    return out


def is_cache_layer(entry: dict) -> bool:
    """True for a ranking entry that is the cache layer (owns no query, #296)."""
    return bool(entry.get("role") == "cache_layer")


def routed_fits_of(data: SynthesisData) -> dict[str, RoutedFit]:
    """Each engine's routed fit from the effective assignment (#152, #1325).

    The one call every consumer shares: the ranking in ``report.json`` and the analysis
    API, which serves the cache layer's figure from it, so they cannot differ.
    """
    return routed_fits(
        data.assignment,
        data.triage,
        data.source_queries,
        {e: a.analysis or {} for e, a in data.engines.items()},
        source_tables=[str(t["table_id"]) for t in data.source_tables if t.get("table_id")],
        source_engine=source_engine_of(data.collector),
        schema_tables=data.source_tables,
    )


def build_ranking(data: SynthesisData) -> list[dict]:
    """Rank target engines by the share of the workload routed to them (#152).

    Each entry keeps ``analysis_confidence`` (suitability averaged over every table
    the engine analyzed, also as ``confidence_score``) and ``weight`` for audit, and
    carries ``routed_confidence``: the mean fit of the queries the effective
    assignment routes to it. Owners come first, largest
    workload share first (weight, then name, break ties); without an assignment the
    weight order stands.

    The cache layer (#296) owns no query: its ``workload_percent`` is 0, it carries
    ``cache_overlay_queries`` / ``cache_call_share_percent`` instead, its routed
    confidence is measured on the reads it fronts, and it ranks after every owner
    engine so it is never read as the main recommendation.
    """
    ranking = []
    assignment_dist = _compute_assignment_distribution(data)
    overlay = build_cache_overlay(data) or {}
    fits = routed_fits_of(data)

    for engine, artifacts in data.engines.items():
        analysis = artifacts.analysis or {}
        table_recs = analysis.get("table_recommendations") or []
        workload = analysis.get("workload_analysis", {})
        aggregates = analysis.get("aggregate_recommendations") or []

        # One definition of the engine's overview figures, shared with the analysis API (#1312).
        overview = analysis_overview_facts(analysis)
        avg_confidence = mean_table_fit(analysis)

        patterns = workload.get("patterns_detected") or []
        anti_patterns = workload.get("anti_patterns_detected") or []

        complexities = [t.get("migration_complexity", "MEDIUM") for t in table_recs]
        complexity_counts = {c: complexities.count(c) for c in set(complexities)}
        most_common = (
            max(complexity_counts, key=lambda k: complexity_counts[k])
            if complexity_counts
            else "MEDIUM"
        )

        monthly_cost = overview["monthly_cost_usd"]
        pattern_score = min(len(patterns) / 5, 1.0)
        anti_penalty = min(len(anti_patterns) * 0.1, 0.3)
        weight = round(
            (avg_confidence / 100) * 0.6 + pattern_score * 0.25 - anti_penalty + 0.15,
            3,
        )
        weight = max(0.0, min(1.0, weight))

        # Schema design stats — handle engine-specific formats
        schema = artifacts.schema_design or {}
        schema_tables = schema_table_defs(engine, schema)
        access_patterns = field_list(schema, "access_patterns")
        pattern_groups: dict[str, list] = {}
        for ap in access_patterns:
            g = ap.get("pattern_group", "ungrouped")
            pattern_groups.setdefault(g, []).append(ap)

        fit = fits.get(engine)
        entry = {
            "target": engine,
            "confidence_score": overview["avg_confidence_score"],  # backward compat
            "analysis_confidence": overview["avg_confidence_score"],
            "routed_confidence": fit.confidence if fit else None,
            "weight": weight,
            "monthly_cost_usd": monthly_cost,
            "tables_analyzed": overview["tables_analyzed"],
            "tables_highly_suitable": sum(1 for t in table_recs if table_fit_score(t) >= 80),
            "tables_suitable": sum(1 for t in table_recs if 60 <= table_fit_score(t) < 80),
            "tables_marginal": sum(1 for t in table_recs if 40 <= table_fit_score(t) < 60),
            "tables_not_suitable": sum(1 for t in table_recs if table_fit_score(t) < 40),
            "patterns_detected": overview["patterns_detected"],
            "anti_patterns_detected": overview["anti_patterns_detected"],
            "migration_complexity_avg": most_common,
            "aggregate_count": len(aggregates),
            "schema_design_available": bool(schema_tables),
            "target_tables": len(schema_tables),
            "access_patterns": len(access_patterns),
            "pattern_groups": len(pattern_groups),
        }
        if fit is not None:
            entry["routed_confidence_basis"] = fit.basis
            entry["routed_queries"] = fit.queries
            entry["routed_tables"] = fit.tables
            entry["routed_lead"] = fit.lead_signal
            entry["routed_lead_count"] = fit.lead_count
            entry["routed_confidence_evidence"] = fit.evidence
            entry["routed_queries_without_table_evidence"] = fit.unbacked_queries
            entry["routed_pinned_queries"] = fit.pinned_queries

        # Enrich with assignment distribution when available
        if assignment_dist and engine in assignment_dist:
            dist = assignment_dist[engine]
            entry["assigned_queries"] = dist["query_count"]
            entry["assigned_queries_in_scope"] = dist["in_scope_count"]
            entry["workload_percent"] = dist["workload_percent"]
            entry["assignment_reason_summary"] = dist["top_reasons"]
        elif assignment_dist:
            # Engine exists but got no assignments
            entry["assigned_queries"] = 0
            entry["assigned_queries_in_scope"] = 0
            entry["workload_percent"] = 0.0
            entry["assignment_reason_summary"] = []

        if engine in CACHE_OVERLAY_ENGINES and overlay.get("engine") == engine:
            entry["role"] = "cache_layer"
            entry["cache_overlay_queries"] = overlay.get("query_count", 0)
            entry["cache_call_share_percent"] = overlay.get("call_share_percent", 0.0)
            entry["cache_overlay_owners"] = overlay.get("owners", {})
            # The overlay's own eligibility floor (#304/#296) and the combined
            # traffic that cleared it, carried through so the "Why" cell can
            # justify the cache's cost from these facts (#375 review) rather
            # than stating a dollar figure with no explanation beside it.
            entry["cache_calls_per_second"] = overlay.get("calls_per_second", 0.0)
            entry["cache_min_calls_per_second"] = overlay.get("min_calls_per_second", 0.0)

        ranking.append(entry)

    ranking.sort(
        key=lambda r: (
            is_cache_layer(r),
            -float(r.get("workload_percent") or 0),
            -r["weight"],
            r["target"],
        )
    )
    # Every engine carries its rationale, so the reports can show it for an engine
    # recommended_architecture.databases leaves out (the retained engine, #152)
    for entry in ranking:
        entry["rationale"] = _engine_rationale(data, entry)
    return ranking


def build_table_mappings(data: SynthesisData) -> list[dict]:
    """Build source table → target engine mappings from schema design outputs.

    Each source table gets mapped to the engine(s) whose schema design
    includes it. A table can map to multiple engines (e.g., payment stays
    in Aurora for writes but gets a DynamoDB read replica).

    A schema design's own ``source_tables`` is free text a model wrote (#380):
    it has named a Postgres sequence (``public.badge_groupings_id_seq``) as the
    "source" for a DynamoDB id-generator table, with no real analysis behind
    it (``table_confidence`` then defaults to 0), which inflated the migration
    map with objects nothing actually collected. Every ``source_table`` is
    resolved against the collector's own schema (:class:`TableNameResolver`,
    the same canonical check ``migration_waves`` and assignment resolution
    use, #225/#316) before it is kept: a name that does not resolve to a real
    table or view the collector saw is dropped, never counted as a "mapped"
    table. A name that resolves under a different spelling (a bare name the
    collector recorded schema-qualified) is carried under the canonical id,
    so it is not double-counted under two spellings.
    """
    resolver = TableNameResolver.from_collector(data.collector)

    def _canonical(source_table: str) -> str | None:
        if resolver is None:
            # No collector schema to check against (#380's guard only has teeth
            # when there is one): fail open and keep every name, the pre-#380
            # behavior, rather than treat a known-good caller as all noise.
            return source_table
        return resolver.resolve(str(source_table))

    # Collect all source_table → engine mappings from schema designs
    table_to_engines: dict[str, list[dict]] = {}

    for engine, artifacts in data.engines.items():
        schema = artifacts.schema_design or {}
        analysis = artifacts.analysis or {}

        # Get per-table confidence from analysis
        table_confidence = {
            t["table_id"]: table_fit_score(t) for t in (analysis.get("table_recommendations") or [])
        }

        table_defs = schema_table_defs(engine, schema)

        for table_def in table_defs:
            target_table_name = table_def.get("table_name", "")
            aggregate_pattern = table_def.get("aggregate_pattern", "separate")

            for raw_source_table in field_list(table_def, "source_tables"):
                source_table = _canonical(raw_source_table)
                if source_table is None:
                    continue
                if source_table not in table_to_engines:
                    table_to_engines[source_table] = []

                table_to_engines[source_table].append(
                    {
                        "engine": engine,
                        "target_table": target_table_name,
                        "aggregate_pattern": aggregate_pattern,
                        "confidence_score": table_confidence.get(
                            source_table, table_confidence.get(raw_source_table, 0)
                        ),
                    }
                )

    # Build mappings — primary = highest confidence, others = alternatives
    mappings = []
    for source_table, targets in sorted(table_to_engines.items()):
        targets.sort(key=lambda t: t["confidence_score"], reverse=True)
        primary = targets[0]
        alternatives = targets[1:] if len(targets) > 1 else []

        mappings.append(
            {
                "source_table": source_table,
                "recommended_database": primary["engine"],
                "target_table": primary["target_table"],
                "aggregate_pattern": primary["aggregate_pattern"],
                "confidence_score": primary["confidence_score"],
                "alternatives": [
                    {
                        "database": alt["engine"],
                        "target_table": alt["target_table"],
                        "confidence_score": alt["confidence_score"],
                    }
                    for alt in alternatives
                ],
            }
        )

    return mappings


def build_query_groups(data: SynthesisData) -> list[dict]:
    """Build unified query group view across all engines.

    Groups access patterns by their explicit pattern_group or source tables,
    enriched with the original source query data from the collector.
    This is the primary organizing structure for the UI — similar to
    Leo's query classification approach.

    The owners' groups are a partition of the queries they serve (#226): the cache
    layer (``CACHE_OVERLAY_ENGINES``) owns no query, so its patterns make groups of
    their own (``cache_layer``: True), keyed apart from an owner's group of the same
    name, and each query they list is a ``cached_read``, never a served query.
    """
    source_queries = {q["query_id"]: q for q in data.source_queries}
    source_scope = SourceScope.from_collector(data.collector)
    # Keyed by (group name, cache layer): a cache pattern grouped by its tables (or a
    # pattern_group that happens to match an owner's) never joins the owner's group.
    groups: dict[tuple[str, bool], dict] = {}

    # Reason text repeats far more than it varies (#478): a consolidation
    # reason is written once per *event*, not per query, so on a large
    # workload (discourse: 1281 relational access patterns) the distinct
    # text is a small fraction of the total (146) -- storing it once per
    # group and indexing into it, instead of copying the full string onto
    # every access pattern, cut report.json from 1.28 MB to well under that.
    # Keyed by group name (not nested in ``groups`` itself) so it survives
    # across this whole function's passes without becoming a serialized
    # field a consumer could mistake for real data. Shared by the main loop
    # below (an Aurora engine with a real, #157-derived access pattern) and
    # the relational branch further down (one still awaiting its design), so
    # the same table's reasons dedupe together regardless of which branch
    # built the group first.
    reason_indices: dict[tuple[str, bool], dict[str, int]] = {}
    # query_id -> the assignment's own routing reason (#157 review, I3): an
    # Aurora access pattern's own ``description`` is structural ("SELECT on
    # t; filtered by id"), not why the query is on Aurora at all -- that
    # fact lives only in the assignment, same source the relational branch
    # below already reads for the not-yet-designed case.
    qid_to_assignment_reason = {
        qa["query_id"]: qa.get("assignment_reason")
        for qa in (data.assignment or {}).get("query_assignments", [])
        if qa.get("query_id")
    }

    for engine, artifacts in data.engines.items():
        schema = artifacts.schema_design or {}
        for ap in field_list(schema, "access_patterns"):
            source_tables = sorted(set(ap.get("source_tables") or []))
            clean_tables = [t for t in source_tables if t not in _PLACEHOLDER_TABLES]
            if ap.get("pattern_group"):
                group_name = ap["pattern_group"]
            elif source_tables and not clean_tables:
                # #157 review, C2: every name in source_tables is a
                # placeholder ("unknown", the collector's own catch-all for a
                # query it could not resolve a table for) -- Aurora's own
                # access patterns have no pattern_group to fall back on, so
                # without this a literal "unknown" leaked into the group name
                # instead of one of the two blame labels the relational
                # branch below already uses for exactly this fact.
                group_name = (
                    UTILITY_GROUP_LABEL
                    if is_utility_statement(ap.get("sql_text"), source_tables, source_scope)
                    else UNRESOLVED_TABLE_GROUP_LABEL
                )
            else:
                group_name = ", ".join(clean_tables) or "ungrouped"
            query_ids = _access_pattern_query_ids(ap)
            cached = engine in CACHE_OVERLAY_ENGINES
            key = (group_name, cached)

            if key not in groups:
                groups[key] = {
                    "group_name": group_name,
                    "engines": [],
                    "access_patterns": [],
                    "source_queries": [],
                    "total_design_rps": 0,
                }
                if cached:
                    groups[key]["cache_layer"] = True

            if engine not in groups[key]["engines"]:
                groups[key]["engines"].append(engine)

            entry = {
                "pattern_id": ap.get("pattern_id"),
                "engine": engine,
                "operation": ap.get("operation"),
                "table_name": ap.get("table_name"),
                "key_condition": ap.get("key_condition"),
                "design_rps": ap.get("design_rps", 0),
                "description": ap.get("description"),
                "in_scope": ap.get("in_scope", True),
                "query_ids": query_ids,
            }
            if engine in AURORA_ENGINES:
                # #157 review, I3: the assignment's own routing reason, via
                # the same reasons/reason_index mechanism the relational
                # branch below already uses -- ``description`` above stays
                # the structural "SELECT on t; filtered by id" text; this is
                # why the query is on Aurora at all, which every consumer
                # (the engineering report's "Reason" column,
                # ``auroraDesign.js``'s "Queries on Aurora" tab) already reads
                # through ``reason_text_for``/``reasonTextFor``.
                reason_text = _clean_relational_reason(
                    qid_to_assignment_reason.get(query_ids[0]) if query_ids else None
                )
                group_reasons = groups[key].setdefault("reasons", [])
                idx_by_reason = reason_indices.setdefault(key, {})
                reason_index = idx_by_reason.get(reason_text)
                if reason_index is None:
                    reason_index = len(group_reasons)
                    idx_by_reason[reason_text] = reason_index
                    group_reasons.append(reason_text)
                entry["reason_index"] = reason_index
            groups[key]["access_patterns"].append(entry)

            groups[key]["total_design_rps"] += ap.get("design_rps", 0)

            # Link back to source queries
            for qid in query_ids:
                if qid in source_queries:
                    # Check if already added
                    existing = next(
                        (sq for sq in groups[key]["source_queries"] if sq["query_id"] == qid),
                        None,
                    )
                    if existing:
                        # Add this pattern to the existing query's linked patterns
                        if ap.get("pattern_id") not in existing["linked_patterns"]:
                            existing["linked_patterns"].append(ap.get("pattern_id"))
                    else:
                        sq = source_queries[qid]
                        groups[key]["source_queries"].append(
                            {
                                "query_id": qid,
                                "query_text": sq.get("query_text", "")[:200],
                                "query_type": sq.get("query_type"),
                                "frequency_per_hour": sq.get("frequency_per_hour", 0),
                                "execution_time_ms_avg": sq.get("execution_time_ms_avg"),
                                "tables_accessed": sq.get("tables_accessed", []),
                                "linked_patterns": [ap.get("pattern_id")],
                                # #226: a read the cache layer fronts, served by its owner.
                                **({"cached_read": True} if cached else {}),
                            }
                        )

    # Relational branch (#478): a query is the atomic unit synthesis groups
    # by, so a query the assignment routed to an Aurora engine belongs here
    # like any other. Built from the assignment, not ``data.engines``, so a
    # group exists even when Aurora has no schema design at all yet (only the
    # design-dependent fields -- DDL, table definitions -- are unavailable
    # then, not the groups; the queries still run on Aurora). Since #157,
    # Aurora's schema design derives real access_patterns once it has run; the
    # step-aside check just below lets the main loop above take over as soon
    # as it does, so this branch only ever covers an Aurora engine that has
    # not been designed yet.
    # Sorted, not frozenset iteration order: a frozenset's iteration order is
    # stable within one process but not guaranteed across runs (hash
    # randomization), so which Aurora engine's entry "wins" a shared dict
    # key below should not depend on it. ``reason_indices`` (declared at the
    # top of this function, shared with the main loop above) keeps its own
    # per-group dedup regardless of which branch builds a group's "reasons"
    # list first.
    for engine in sorted(AURORA_ENGINES):
        aurora_artifacts = data.engines.get(engine)
        schema = (aurora_artifacts.schema_design if aurora_artifacts else None) or {}
        if schema.get("access_patterns"):
            # This engine's schema design has run and derived real
            # access_patterns, so the main loop above already covers it --
            # stepping aside here avoids double-building (and
            # double-counting) its groups.
            continue

        # Some assignments carry more than one query_assignments row for the
        # same query_id (one per table a multi-table query touches, seen on
        # the AdventureWorks sample: 657 rows, 79 distinct queries).
        # ``_rows_per_query`` merges them into one entry per query (#1522);
        # the per-group set below still guards a repeated pattern id.
        seen_pattern_ids: dict[str, set[str]] = {}
        # #1522: one entry per query, its rows' tables together, so a query with one
        # row per table it touches is listed by one group, named by all of them.
        for qa, row_tables in _rows_per_query(data.assignment, engine):
            qid = str(qa["query_id"])
            src_query = source_queries.get(qid)
            raw_tables = row_tables or (src_query.get("tables_accessed") if src_query else []) or []
            clean_tables = sorted({t for t in raw_tables if t and t not in PSEUDO_TABLES})
            if clean_tables:
                group_name = ", ".join(clean_tables)
            elif is_utility_statement(
                src_query.get("query_text") if src_query else None, raw_tables, source_scope
            ):
                group_name = UTILITY_GROUP_LABEL
            else:
                group_name = UNRESOLVED_TABLE_GROUP_LABEL
            # The query's own id: a short, truncated id
            # risks two different queries silently merging under the same
            # prefix (real on a non-cryptographic id scheme, e.g.
            # AdventureWorks' sequential "0x...FEEE" ids); correctness over
            # the few bytes a truncated id would have saved. Deterministic
            # (same query_id -> same pattern_id every time), so a duplicate
            # assignment row for the same query within the same group is
            # recognized as the same pattern, not counted twice.
            pattern_id = f"relational-{engine}-{qid}"
            key = (group_name, False)

            if key not in groups:
                groups[key] = {
                    "group_name": group_name,
                    "engines": [],
                    "access_patterns": [],
                    "source_queries": [],
                    "total_design_rps": 0,
                }

            if engine not in groups[key]["engines"]:
                groups[key]["engines"].append(engine)

            # A query naming the same tables an owner engine's own access
            # pattern already grouped under this exact name reuses that group
            # (never the cache layer's, #226: it is keyed apart; the loop above may have created it first, with no
            # "reasons" key at all -- only this branch ever needs one).
            # ``setdefault``, not a plain assignment: that existing group's
            # own entries have no "reason_index" and keep reading their
            # reason from "description" directly, unaffected either way.
            groups[key].setdefault("reasons", [])

            if pattern_id in seen_pattern_ids.setdefault(group_name, set()):
                continue
            seen_pattern_ids[group_name].add(pattern_id)

            design_rps = src_query.get("calls_per_second", 0) if src_query else 0
            # Deduplicated, "unknown" replaced and capped on a word boundary:
            # a co-dependency reason can repeat the same clause twice and
            # lists every excluding engine, so it can run long -- compact,
            # not reproduced, and never a bare "unknown" (that placeholder
            # names a real table the collector missed for some queries,
            # #483).
            reason_text = _clean_relational_reason(qa.get("assignment_reason"))
            group_reasons = groups[key]["reasons"]
            idx_by_reason = reason_indices.setdefault(key, {})
            reason_index = idx_by_reason.get(reason_text)
            if reason_index is None:
                reason_index = len(group_reasons)
                idx_by_reason[reason_text] = reason_index
                group_reasons.append(reason_text)
            groups[key]["access_patterns"].append(
                {
                    "pattern_id": pattern_id,
                    "engine": engine,
                    "operation": (src_query.get("query_type") if src_query else None) or "QUERY",
                    "design_rps": design_rps,
                    # The reason text itself lives once in this group's own
                    # "reasons" list (above) -- this is only its index into
                    # that list, not a copy of the string (146 distinct
                    # reasons backed 1281 access patterns on discourse).
                    # ``reason_text_for`` resolves this back to the string;
                    # every consumer goes through it rather than reading
                    # ``description`` directly on a relational entry.
                    "reason_index": reason_index,
                    "in_scope": qa.get("in_scope", True),
                    "query_ids": [qid],
                }
            )
            # ``table_name``/``key_condition`` omitted: the former duplicates
            # this group's own name (tables carry over 1:1, #157 adds a real
            # target-table mapping only once Aurora has real access
            # patterns), and the latter is always None -- neither is read by
            # anything that renders this group.
            groups[key]["total_design_rps"] += design_rps

            if src_query and not any(s["query_id"] == qid for s in groups[key]["source_queries"]):
                groups[key]["source_queries"].append(
                    {
                        "query_id": qid,
                        "query_text": src_query.get("query_text", "")[:200],
                        "query_type": src_query.get("query_type"),
                        "frequency_per_hour": src_query.get("frequency_per_hour", 0),
                        "execution_time_ms_avg": src_query.get("execution_time_ms_avg"),
                        # ``tables_accessed``/``linked_patterns`` omitted:
                        # redundant here -- this group's own name already
                        # names the tables, and a query has exactly one
                        # access pattern in this branch (one
                        # query_assignments row per query per group), unlike
                        # the generic loop above where a query can be served
                        # by several different access patterns.
                    }
                )

    _list_each_query_once(groups, data.assignment)
    # Sort by total RPS descending
    result = sorted(groups.values(), key=lambda g: g["total_design_rps"], reverse=True)
    return result


def _rows_per_query(assignment: dict | None, engine: str) -> list[tuple[dict[str, Any], list[str]]]:
    """``engine``'s ``query_assignments`` rows, one per query (its first row), with the
    tables of all its rows in first-seen order (#1522). A row with no query id is left
    out: no query to list."""
    first: dict[str, dict[str, Any]] = {}
    tables: dict[str, list[str]] = {}
    for qa in (assignment or {}).get("query_assignments", []):
        if qa.get("assigned_engine") != engine or not qa.get("query_id"):
            continue
        qid = str(qa["query_id"])
        first.setdefault(qid, qa)
        merged = tables.setdefault(qid, [])
        merged += [t for t in qa.get("source_tables") or [] if t not in merged]
    return [(qa, tables[qid]) for qid, qa in first.items()]


def _list_each_query_once(groups: dict[tuple[str, bool], dict], assignment: dict | None) -> None:
    """Each query is listed in one owner group's ``source_queries`` (#1522).

    A query two groups list (two ``pattern_group`` values of one design citing it, or a
    design citing a query the assignment routed to another engine) stays listed by the
    first group of its assigned engine, else by the first group that lists it, so it is
    never dropped. The other groups' patterns still cite it in ``query_ids``; the kept
    listing links to every pattern of its engine that does. The cache layer's groups
    list cached reads, never served queries, and are left as they are (#226).
    """
    owner = {
        qa["query_id"]: qa.get("assigned_engine")
        for qa in reversed((assignment or {}).get("query_assignments", []))
        if qa.get("query_id")
    }
    listings: dict[str, list[dict]] = {}
    for (_, cached), group in groups.items():
        if not cached:
            for sq in group["source_queries"]:
                listings.setdefault(sq["query_id"], []).append(group)
    for qid, listed in listings.items():
        if len(listed) < 2:
            continue
        home = next((g for g in listed if owner.get(qid) in g["engines"]), listed[0])
        engine = owner.get(qid) if owner.get(qid) in home["engines"] else None
        kept = next(sq for sq in home["source_queries"] if sq["query_id"] == qid)
        for group in listed:
            if group is home:
                continue
            group["source_queries"] = [
                sq for sq in group["source_queries"] if sq["query_id"] != qid
            ]
            if "linked_patterns" not in kept:
                continue
            for ap in group["access_patterns"]:
                if (
                    qid in (ap.get("query_ids") or [])
                    and (engine is None or ap.get("engine") == engine)
                    and ap.get("pattern_id") not in kept["linked_patterns"]
                ):
                    kept["linked_patterns"].append(ap.get("pattern_id"))


def build_tco_analysis(
    data: SynthesisData,
    retired_costs: dict[str, float] | None = None,
    wave_one_engine: str | None = None,
    retired_bases: dict[str, dict] | None = None,
) -> dict:
    """Aggregate cost estimates from all analysis outputs.

    ``wave_one_engine`` (#1378) is wave 1's Aurora engine (``wave_retained_engine``):
    wave 1 moves the whole source database, so the tables no in-scope query reads are
    stored on it, priced with its own tables. ``unqueried_tables`` states how many there
    are, their size and the engine that stores them (None when no kept engine is wave 1's,
    so no figure includes them); None when every table is read, with no assignment and
    with no query in scope.

    ``retired_costs`` (#380, #949) is each engine the source runs today that the
    plan retires, mapped to its own analysed ``cost_estimate.monthly_cost_usd`` --
    the figure a recommendation sentence names as a saving (e.g. "$271.80/mo")
    when it recommends retiring that engine's cluster. It is carried into
    ``retired_engine_savings`` here so that figure is traceable in the TCO facts
    the report publishes, not only in prose elsewhere; omitted entirely (not an
    empty list) when there is nothing to report. An engine the plan analysed as
    a candidate and dropped is never in it: the customer does not run it, so
    dropping it saves nothing (``considered_not_recommended`` names it instead).
    ``eliminated_engine_costs``, which listed those candidates before contract
    1.25, is no longer written. ``retired_bases`` (#1197) is each retired engine's
    read I/O basis (:func:`src.shared.source_cache.cost_basis_fields`), carried onto
    its row as each kept engine's is.
    """
    target_costs = []
    total_projected = 0.0
    unqueried = unqueried_tables(data.collector, data.assignment)
    stored_on = wave_one_engine if unqueried and wave_one_engine in data.engines else None
    # #1476: a line an engine's estimate asks the TCO to state with its figure (the
    # DynamoDB read-consistency footnote), each once, in engine order.
    footnotes: list[str] = []

    for engine, artifacts in data.engines.items():
        analysis = artifacts.analysis or {}
        cost = analysis.get("cost_estimate") or {}
        components = cost.get("cost_components") or {}
        monthly = cost.get("monthly_cost_usd", 0)
        sizing: dict | None = None
        # #1195: the analysis estimate sized the engine for the whole collection; the
        # TCO prices it for the queries the effective assignment gives it. An analysis
        # that priced nothing (no cost components: cost estimation turned off) keeps its
        # figure, and so does a job with no assignment.
        sized = (
            sized_cost_estimate(
                engine,
                data.collector,
                data.assignment,
                unqueried if engine == stored_on else None,
            )
            if components
            else None
        )
        if sized is not None:
            estimate, sizing = sized
            monthly = estimate.monthly_cost_usd
            components = estimate.cost_components
        footnotes += [str(f) for f in components.get("footnotes") or [] if str(f) not in footnotes]
        target_costs.append(
            {
                "database": engine,
                "monthly_cost_usd": monthly,
                "pricing_mode": components.get("pricing_mode", "on-demand"),
                # #1195: what the figure was sized for (None: the analysis estimate).
                "sizing": sizing,
                # #1196: the price table the figure was priced from (ADR-032 Decision 6).
                "price_list": components.get("price_list"),
                # #1197: the read I/O basis, for an estimate that bills cache misses only.
                **cost_basis_fields({"cost_components": components}),
            }
        )
        total_projected += monthly
    unqueried_summary = (
        {"table_count": len(unqueried), "storage_gb": storage_gb(unqueried), "stored_on": stored_on}
        if unqueried
        else None
    )

    # Source cost estimate from collector metrics (if available)
    rds_meta = (
        data.collector.get("metadata", {})
        .get("source_database", {})
        .get("rds_instance_metadata", {})
    )
    # #380: whether there is a real source baseline to compare against at all,
    # not just whether the rough estimate happened to come out to zero -- a
    # customer reading "$0.00 current / 0% savings" cannot tell "no instance
    # metadata was collected" from "the source genuinely costs nothing", and
    # the second never happens. Renderers must show "source cost not
    # provided" instead of the figure when this is ``False``.
    current_cost_known = bool(rds_meta)
    # Rough RDS cost estimate based on instance class
    current_monthly = _estimate_rds_cost(rds_meta) if rds_meta else 0

    savings_pct = (
        round((1 - total_projected / current_monthly) * 100, 1) if current_monthly > 0 else 0
    )

    result = {
        "current_monthly_cost": current_monthly,
        "current_cost_known": current_cost_known,
        "projected_monthly_cost": round(total_projected, 2),
        "savings_percent": savings_pct,
        "cost_breakdown": target_costs,
        "unqueried_tables": unqueried_summary,
        "assumptions": [
            "On-demand capacity mode for all target databases",
            "Region: us-east-1",
            # #1247: only a known baseline was estimated; with none, the note is
            # left out like the current-cost and savings lines (#949).
            *(
                ["Current RDS cost estimated from instance class (actual billing may differ)"]
                if current_cost_known
                else []
            ),
            "Does not include data transfer, backups, or global tables",
            # #1195: a cache layer sized on its fronted reads leaves those reads billed on
            # their owners too, every one counted as a miss; said so, not left to look
            # like the same traffic counted twice.
            *(
                [CACHE_MISS_ASSUMPTION]
                if any(
                    (row.get("sizing") or {}).get("basis") == CACHE_OVERLAY
                    and (row.get("sizing") or {}).get("sized_query_count")
                    for row in target_costs
                )
                else []
            ),
            # #1378: which engine stores the tables no in-scope query reads.
            *([unqueried_assumption(unqueried_summary)] if unqueried_summary else []),
            *footnotes,
        ],
    }
    if retired_costs:
        result["retired_engine_savings"] = [
            {
                "database": engine,
                "monthly_cost_usd": cost,
                # #1197: the read I/O basis of the retired engine's own estimate.
                **(retired_bases or {}).get(engine, {}),
            }
            for engine, cost in sorted(retired_costs.items())
        ]
    return result


def _estimate_rds_cost(rds_meta: dict) -> float:
    """Rough monthly cost estimate for an RDS instance based on class."""
    # Simplified pricing — actual costs vary by region, reserved vs on-demand
    instance_costs = {
        "db.t3.micro": 15,
        "db.t3.small": 30,
        "db.t3.medium": 65,
        "db.t3.large": 130,
        "db.t3.xlarge": 260,
        "db.t3.2xlarge": 520,
        "db.r5.large": 175,
        "db.r5.xlarge": 350,
        "db.r5.2xlarge": 700,
        "db.r5.4xlarge": 1400,
        "db.r5.8xlarge": 2800,
        "db.r6g.large": 160,
        "db.r6g.xlarge": 320,
        "db.r6g.2xlarge": 640,
    }
    instance_class = rds_meta.get("instance_class", "")
    base = instance_costs.get(instance_class, 200)  # Default $200/mo
    if rds_meta.get("multi_az"):
        base *= 2
    return base


def _engines_with_assigned_queries(data: SynthesisData) -> set[str]:
    """Engines the assignment actually routed in-scope queries to.

    ``data.engines`` is keyed on *triage's* selections and is never narrowed by the
    assignment, so it still holds engines the assignment eliminated outright. Risks are
    generated per engine during analysis, while every triage-selected engine is still a
    candidate, so carrying a dropped engine's risks forward inflates the counts -- and
    because ``overall_risk_level`` is the highest open severity, one HIGH risk on an
    engine that carries no workload would raise the headline rating to HIGH.

    Fail-open: an empty set means "cannot tell". The caller keeps every risk in that case
    rather than silently emptying the register, which would be far worse than the defect.
    """
    if not data.assignment:
        return set()
    return {
        qa["assigned_engine"]
        for qa in data.assignment.get("query_assignments", [])
        if qa.get("in_scope", True) and qa.get("assigned_engine")
    }


def build_risk_assessment(
    data: SynthesisData,
    eliminated: dict[str, str | None] | None = None,
    lifted: Collection[tuple[str, str, str]] = frozenset(),
) -> dict:
    """Compile risks from anti-patterns, migration notes, and unsupported patterns.

    Anti-patterns are cross-referenced against schema design access patterns:
    if all of an anti-pattern's query_ids are covered by in-scope access
    patterns, the risk is considered resolved and downgraded to a note.

    Anti-pattern risks follow their queries (#221): the ``[engine]`` risk covers only the
    queries the effective assignment keeps on that engine. Queries moved to another
    engine are resolved by the move only when that engine's design serves them with an
    in-scope access pattern (an unsupported pattern there, itself a MEDIUM risk, counts
    only for risks no more severe than MEDIUM), or when they moved to Aurora, which runs
    the source SQL. Otherwise the risk is re-attributed to the new engine with its
    original severity. When the anti-pattern's own advice was to move the queries to the
    engine they landed on, the anti-pattern is resolved and the queries that engine's
    design does not serve become one coverage-gap risk per engine. Every resolved risk is
    recorded in ``resolved_risks`` (grounded like the risks).

    Only engines the assignment routed queries to contribute risks. An engine triage
    selected but the assignment then dropped is not part of the target architecture, so its
    anti-patterns describe a design that will never be built.

    ``lifted``: ``(query_id, engine, anti_pattern_type)`` findings a redesign removes
    (#1231), whichever engine serves the query. Routing did not penalise them, and the
    serving engine's design is given the keys (``design.redesign`` raises it where the
    plan relies on them), so an anti-pattern risk leaves those queries out: one finding
    raises at most one risk.

    ``eliminated`` maps engines the reality check removed to the engine that absorbed
    them (see ``synthesis_grounding.eliminated_engines``). Surviving engines' risk text
    written while those engines were still candidates is rewritten so no risk or
    mitigation recommends an eliminated engine, and the mitigation strategies only name
    engines in the effective architecture (#202).
    """
    risks = []
    resolved: list[dict] = []
    risk_id = 0
    assigned = _engines_with_assigned_queries(data)
    # Effective engine of every in-scope query (built first: anti-pattern risks follow
    # their queries to this engine, #221).
    query_engine = {
        qa["query_id"]: qa["assigned_engine"]
        for qa in (data.assignment or {}).get("query_assignments", [])
        if qa.get("in_scope", True) and qa.get("assigned_engine") and qa.get("query_id")
    }
    # Every in-scope query's own assignment_reason (#375 review): the capability
    # gate and the utility pin already explain a hard-pinned query there, reused
    # by _capability_pin_reason so a DynamoDB-alternative risk on the same table
    # can name the reason instead of contradicting the routing outright.
    assignment_reason = {
        qa["query_id"]: qa.get("assignment_reason", "")
        for qa in (data.assignment or {}).get("query_assignments", [])
        if qa.get("in_scope", True) and qa.get("query_id")
    }
    assignment_ids = {
        qa.get("query_id") for qa in (data.assignment or {}).get("query_assignments", [])
    }
    query_tables = {
        q["query_id"]: set(q.get("tables_accessed") or [])
        for q in data.source_queries
        if q.get("query_id")
    }
    # Normalised tables per query for risks built from query ids (unsupported patterns,
    # coverage gaps): ``tables_accessed``, else the assignment's ``source_tables``.
    normalise = _table_normaliser(data)
    assigned_tables = {
        qa["query_id"]: qa.get("source_tables") or []
        for qa in (data.assignment or {}).get("query_assignments", [])
        if qa.get("query_id")
    }
    risk_tables = {
        q: normalise(query_tables.get(q) or assigned_tables.get(q) or [])
        for q in set(query_tables) | set(assigned_tables)
    }
    query_text = {
        q["query_id"]: str(q.get("query_text") or "")
        for q in data.source_queries
        if q.get("query_id")
    }
    source_engine = source_database_engine(data.collector)
    covered_by = {
        engine: covered_query_ids(engine, artifacts.schema_design or {})
        for engine, artifacts in data.engines.items()
    }
    unsupported_by = {
        engine: _unsupported_query_ids(artifacts.schema_design or {})
        for engine, artifacts in data.engines.items()
    }
    # target engine -> {query id: severity} for queries an anti-pattern's advice moved to
    # that engine but its design does not serve (reported as one gap risk per engine).
    coverage_gaps: dict[str, dict[str, str]] = {}
    # #949: engines whose coverage gap is "not checked", because no design ran.
    undesigned: set[str] = set()

    for engine, artifacts in data.engines.items():
        # ``assigned`` empty => no readable assignment => keep every risk (fail-open).
        if assigned and engine not in assigned:
            logger.info(
                "Risk assessment: skipping %s (assignment routed it no in-scope queries)",
                engine,
            )
            continue
        analysis = artifacts.analysis or {}
        schema = artifacts.schema_design or {}

        # Every table's in-scope queries on THIS engine (#375 review): a
        # DynamoDB-alternative anti-pattern's own flagged queries can be simple
        # key-value reads with no capability need of their own, while a
        # *different* query on the same table is why the table stays here --
        # _capability_pin_reason below looks at every one of them, not just
        # the flagged queries, to find that reason.
        table_to_qids: dict[str, set[str]] = {}
        for qid, qid_engine in query_engine.items():
            if qid_engine != engine:
                continue
            for table in normalise(query_tables.get(qid) or assigned_tables.get(qid) or []):
                table_to_qids.setdefault(table, set()).add(qid)

        # Anti-patterns from analysis — only include if NOT resolved by schema design
        # Source database anti-patterns (full scans, slow queries) are migration
        # motivation, not destination risks. Skip them entirely — the schema design
        # already addresses them with proper access patterns.
        # Only include anti-patterns that affect the TARGET database design.
        for ap in analysis.get("workload_analysis", {}).get("anti_patterns_detected") or []:
            ap_type = ap.get("anti_pattern_type", "")

            # Skip source-database-only anti-patterns
            if ap_type in ("frequent-full-scan", "queries-without-index"):
                continue

            flagged_ids = set(field_list(ap, "query_ids"))
            ap_query_ids = {q for q in flagged_ids if (q, engine, ap_type) not in lifted}
            if flagged_ids and not ap_query_ids:
                continue  # a redesign removes every flagged query's finding
            severity = "HIGH" if ap.get("severity_weight", 0) >= 0.7 else "MEDIUM"
            description = ap.get("description", ap.get("anti_pattern_type", "Unknown"))
            ap_tables = [t for t in ap.get("table_ids", []) if t and t not in _PLACEHOLDER_TABLES]

            # Split the flagged queries by the engine the assignment routed them to
            # (#221). Fail-open: when none of them is in the assignment, keep the
            # analysis attribution.
            if query_engine and ap_query_ids & assignment_ids:
                on_engine: dict[str, set[str]] = {}
                for q in ap_query_ids:
                    if q in query_engine:
                        on_engine.setdefault(query_engine[q], set()).add(q)
            else:
                on_engine = {engine: ap_query_ids}

            if not on_engine:
                resolved.append(
                    _resolved_risk(
                        engine,
                        severity,
                        description,
                        ap_tables,
                        ap_query_ids,
                        None,
                        "every flagged query is out of scope in the assignment",
                    )
                )
                continue

            for target in sorted(on_engine, key=lambda e: (e != engine, e)):
                ids = on_engine[target]
                tables = _tables_for(ids, ap_tables, query_tables)
                if target == engine:
                    # #157 review, I2: Aurora's own access patterns are a
                    # mechanical projection of every query it owns (carry_over
                    # runs the source SQL as-is), not a design decision -- so
                    # "covered" by them says nothing about whether this
                    # anti-pattern (a missing index, a key-value table forced
                    # relational, ...) was actually addressed. Counting it as
                    # schema-design coverage would silently resolve every such
                    # risk just because Aurora derives a pattern for every
                    # query it owns. An Aurora risk still resolves below, via
                    # the deliberate-routing/pin_reason path (a sibling query's
                    # own capability or utility/DDL pin on the same table),
                    # which is a real decision rather than an artifact of
                    # derivation.
                    covered = (
                        covered_by.get(engine, set()) if engine not in AURORA_ENGINES else set()
                    )
                    # If all flagged queries are covered by the schema design, it's resolved
                    if ids and ids.issubset(covered):
                        resolved.append(
                            _resolved_risk(
                                engine,
                                severity,
                                description,
                                tables,
                                ids,
                                engine,
                                "the schema design covers every flagged query",
                            )
                        )
                        continue
                    # Partially resolved — note which queries are still uncovered
                    uncovered = ids - covered
                    text = _without_hedge(description)
                    if uncovered and ids:
                        pct_covered = round((1 - len(uncovered) / len(ids)) * 100)
                        text += (
                            f" ({pct_covered}% of queries resolved by schema design, "
                            f"{len(uncovered)} remaining)"
                        )
                    mitigation = _assigned_engine_mitigation(
                        ap.get("recommendation"), engine, len(ids)
                    )
                    if ap_type in _DYNAMODB_ALTERNATIVE_ANTI_PATTERNS:
                        sibling_qids: set[str] = set()
                        for t in tables:
                            sibling_qids |= table_to_qids.get(t, set())
                        pin_reason = _capability_pin_reason(
                            assignment_reason.get(q, "") for q in sibling_qids
                        )
                        if pin_reason:
                            # #380 review: this anti-pattern's own flagged queries
                            # look like simple key-value access, but a different
                            # query on the same table is why it stays on this
                            # engine -- the routing is deliberate, not an open
                            # risk, so it is resolved (not raised) rather than
                            # kept as a MEDIUM risk whose own "mitigation" is "no
                            # action needed" (not a mitigation at all, and a
                            # restatement of a decision the report already made
                            # elsewhere). This is also how two anti-patterns
                            # flagging the same already-pinned table (e.g. "no
                            # foreign keys" and "at most 2 patterns") stop
                            # duplicating each other as separate open risks over
                            # largely the same tables: both resolve here instead.
                            #
                            # #393: the resolved entry states the resolution ("kept
                            # on <engine> because <reason>") instead of reusing the
                            # anti-pattern's own open-risk wording (``text``), which
                            # names the table a candidate for "a simpler engine" --
                            # true while the risk was open, false (and
                            # self-contradicting) once it is resolved and kept here.
                            engine_name = display_name(engine)
                            resolved.append(
                                _resolved_risk(
                                    engine,
                                    severity,
                                    f"Kept on {engine_name} because of {pin_reason} on this table.",
                                    tables,
                                    ids,
                                    engine,
                                    f"{engine_name} is also required for {pin_reason} on "
                                    "this table, so keeping it there is a deliberate "
                                    "routing decision, not an open risk; revisit "
                                    "DynamoDB for this table's simple key-value reads "
                                    "in a later wave if those other queries are "
                                    "retired or move too",
                                )
                            )
                            continue
                    risk_id += 1
                    risks.append(
                        {
                            "risk_id": f"RISK-{risk_id:03d}",
                            "risk_type": "PERFORMANCE_DEGRADATION",
                            "severity": severity,
                            "description": f"[{engine}] {text}",
                            "affected_tables": tables,
                            "mitigation": mitigation,
                            "query_ids": sorted(ids),
                        }
                    )
                    continue

                # Queries moved to another engine. Aurora serves the SQL relationally, so
                # a non-relational engine's limitation does not follow them there.
                if target in AURORA_ENGINES and engine not in AURORA_ENGINES:
                    resolved.append(
                        _resolved_risk(
                            engine,
                            severity,
                            description,
                            tables,
                            ids,
                            target,
                            f"the queries run as SQL on {display_name(target)}",
                        )
                    )
                    continue
                # Only an in-scope access pattern on the new engine resolves a query. An
                # unsupported pattern there is its own (MEDIUM) risk, so it stands in only
                # for risks no more severe than that.
                addressed = set(covered_by.get(target, set()))
                if _SEVERITY_ORDER.get(severity, 9) >= _SEVERITY_ORDER["MEDIUM"]:
                    addressed |= unsupported_by.get(target, set())
                remaining = ids - addressed
                recommendation = ap.get("recommendation") or ""
                if recommends_engine(f"{description} {recommendation}", target):
                    # The analysis advised moving these queries to ``target`` and the
                    # assignment did: the anti-pattern itself is resolved. Queries the new
                    # design does not serve are a coverage gap on ``target``, reported once
                    # per engine in their own words below.
                    resolved.append(
                        _resolved_risk(
                            engine,
                            severity,
                            _moved_description(description, target),
                            tables,
                            ids,
                            target,
                            f"the {display_name(engine)} analysis recommended "
                            f"{display_name(target)} for these queries and the assignment "
                            "moved them there",
                        )
                    )
                    gap = coverage_gaps.setdefault(target, {})
                    for q in remaining:
                        if _SEVERITY_ORDER.get(severity, 9) < _SEVERITY_ORDER.get(
                            gap.get(q, "LOW"), 9
                        ):
                            gap[q] = severity
                        else:
                            gap.setdefault(q, severity)
                    continue
                if not remaining:
                    resolved.append(
                        _resolved_risk(
                            engine,
                            severity,
                            _moved_description(description, target),
                            tables,
                            ids,
                            target,
                            f"the queries moved to {display_name(target)}, whose schema "
                            "design serves all of them",
                        )
                    )
                    continue
                if len(remaining) > BROAD_FLAGGED_QUERIES:
                    # #297: a flag over this many queries is no bounded risk with its
                    # own fix. The queries the design does not serve join the one gap
                    # risk of the engine (or its "not checked" risk), which names the
                    # tables to start with.
                    gap = coverage_gaps.setdefault(target, {})
                    for q in remaining:
                        if _SEVERITY_ORDER.get(severity, 9) < _SEVERITY_ORDER.get(
                            gap.get(q, "LOW"), 9
                        ):
                            gap[q] = severity
                        else:
                            gap.setdefault(q, severity)
                    continue
                risk_id += 1
                pct_covered = round((1 - len(remaining) / len(ids)) * 100)
                n_rem = len(remaining)
                mitigation = (
                    f"Cover the {n_rem} remaining {'query' if n_rem == 1 else 'queries'} in "
                    f"the {display_name(target)} schema design or route "
                    f"{'it' if n_rem == 1 else 'them'} to an engine that serves "
                    f"{'it' if n_rem == 1 else 'them'}."
                )
                # The old engine's advice is kept only as background, and dropped when it
                # leans on that engine's own features (e.g. DynamoDB Streams for queries
                # now on ElastiCache).
                if (
                    recommendation
                    and recommendation not in CATALOG_ENGINE_GUIDANCE
                    and engine not in {e for e, _, _ in engine_mentions(recommendation)}
                ):
                    mitigation += f" Background ({display_name(engine)} analysis): {recommendation}"
                n_ids = len(ids)
                risks.append(
                    {
                        "risk_id": f"RISK-{risk_id:03d}",
                        "risk_type": "PERFORMANCE_DEGRADATION",
                        "severity": severity,
                        # Attribution first; the standard "(N% ..., M remaining)"
                        # parenthetical last, which the deck parses for the count.
                        "description": (
                            f"[{target}] Flagged by the {display_name(engine)} analysis for "
                            f"{n_ids} {'query' if n_ids == 1 else 'queries'} now on "
                            f"{display_name(target)}: {_without_hedge(description)} ({pct_covered}% of "
                            f"queries resolved by schema design, {n_rem} remaining)"
                        ),
                        "affected_tables": tables,
                        "mitigation": mitigation,
                        "query_ids": sorted(ids),
                        "reattributed_from": engine,
                    }
                )

        # Unsupported patterns from schema design. The four schema-design
        # contracts disagree on field names (dynamodb/opensearch carry
        # pattern_type/recommendation; documentdb/elasticache carry
        # reason/workaround instead) -- read via the shared helper so every
        # engine's unsupported patterns become risks with real text, not
        # "[engine] unknown: " for the engines whose contract this code used
        # to not read (#210).
        for up in field_list(schema, "unsupported_patterns"):
            pattern_ids = unsupported_pattern_ids(up)
            tables = sorted({t for q in pattern_ids for t in risk_tables.get(q, ())})
            risk_id += 1
            # Dedup-only GROUP BY (#336): every one of this *aggregation* entry's
            # queries is a GROUP BY used only to de-duplicate rows (no aggregate
            # function, no HAVING, no window function, no ROLLUP/CUBE/GROUPING
            # SETS) -- an application-code fix, not a real blocking aggregation.
            # Gated on the entry's own category (``_is_aggregation_unsupported_
            # pattern``), not just the SQL shape: a non-aggregation entry (a join
            # or LIKE-pattern limitation, say) is never reclassified just because
            # its query also happens to have a dedup-only GROUP BY. Kept as an
            # open LOW risk, worded as the application-code fix it is -- the
            # issue's own alternative -- not resolved and not counted as covered:
            # no in-scope access pattern actually serves these queries.
            if (
                pattern_ids
                and _is_aggregation_unsupported_pattern(up)
                and all(
                    is_dedup_only_group_by(query_text.get(q, ""), source_engine)
                    for q in pattern_ids
                )
            ):
                risks.append(
                    {
                        "risk_id": f"RISK-{risk_id:03d}",
                        "risk_type": "MIGRATION_COMPLEXITY",
                        "severity": "LOW",
                        "description": f"[{engine}] {unsupported_pattern_label(up)}: "
                        f"{_unsupported_pattern_problem(engine, up, query_text)}",
                        "affected_tables": tables,
                        "mitigation": (
                            "Needs an application change: drop the GROUP BY and serve "
                            "it as a Query."
                        ),
                        "query_ids": sorted(pattern_ids),
                    }
                )
                continue
            risks.append(
                {
                    "risk_id": f"RISK-{risk_id:03d}",
                    "risk_type": "MIGRATION_COMPLEXITY",
                    "severity": "MEDIUM",
                    "description": f"[{engine}] {unsupported_pattern_label(up)}: "
                    f"{_unsupported_pattern_problem(engine, up, query_text)}",
                    "affected_tables": tables,
                    "mitigation": unsupported_pattern_mitigation(up),
                    "query_ids": sorted(pattern_ids),
                }
            )

        # Unresolved cross-group design conflicts from the DynamoDB merge (#426).
        # ``dynamodb_merge.independent_homes`` records a source table two design
        # groups each gave its own entity (not consolidated) as a review
        # trade-off instead of raising a validation failure -- that trade-off
        # was visible in the deliverables' trade-off list, but never became a
        # risk, so the report could say "review before migration" while the
        # risk register stayed empty. One MEDIUM risk per such trade-off; it
        # disappears with the trade-off once the overlap is consolidated (a
        # group's own trade-off naming the overlap no longer hides it, #333).
        risk_id, conflict_risks = _design_conflict_risks(risk_id, engine, schema, normalise)
        risks.extend(conflict_risks)

        # Queries --split left out of every design group because they touch no
        # source table (catalog/utility statements, #276/#369): a deterministic
        # out-of-scope note, same as an unsupported pattern, so they are visible
        # in the report rather than silently missing from both coverage and risks.
        excluded_queries = schema.get("excluded_queries") or []
        if excluded_queries:
            risk_id += 1
            excluded_ids = sorted(
                {str(e.get("query_id")) for e in excluded_queries if e.get("query_id")}
            )
            n_excluded = len(excluded_ids)
            risks.append(
                {
                    "risk_id": f"RISK-{risk_id:03d}",
                    "risk_type": "MIGRATION_COMPLEXITY",
                    "severity": "LOW",
                    "description": (
                        f"[{engine}] Not designed: {n_excluded} "
                        f"{'query' if n_excluded == 1 else 'queries'} touch no source "
                        "table (catalog/utility statements) and were left out of "
                        "schema design."
                    ),
                    "affected_tables": [],
                    "mitigation": (
                        "Review whether these statements should be routed to this " "engine at all."
                    ),
                    "query_ids": excluded_ids,
                }
            )

        # Migration notes from schema design
        # The description names the object; the logic to build is the mitigation (#252).
        for mn in field_list(schema, "migration_notes"):
            risk_id += 1
            object_label = str(mn.get("object_name") or "").strip()
            logic = str(mn.get("application_logic_required") or "").strip()
            risks.append(
                {
                    "risk_id": f"RISK-{risk_id:03d}",
                    "risk_type": "OPERATIONAL_RISK",
                    "severity": "MEDIUM",
                    "description": (
                        f"[{engine}] {mn.get('object_type') or 'migration note'}: "
                        f"{object_label or 'this object'} needs application-side logic on "
                        f"{display_name(engine)}."
                    ),
                    "affected_tables": sorted(normalise([mn.get("source_table") or ""])),
                    # #380: a MEDIUM+ risk always carries a concrete mitigation --
                    # when the schema design gave no logic to build, fall back to
                    # a reviewable action instead of leaving it ``None`` (silently
                    # no mitigation at all for a risk this severe).
                    "mitigation": (
                        f"Implement as application logic: {logic}"
                        if logic
                        else (
                            f"Review {object_label or 'this object'} "
                            f"({mn.get('object_type') or 'migration note'}) and implement its "
                            f"logic in the application before cutover to {display_name(engine)}."
                        )
                    ),
                    "object_type": str(mn.get("object_type") or ""),
                    "object_name": str(mn.get("object_name") or ""),
                }
            )

    # Leading-wildcard LIKE (#493): each final engine's reason, as a risk -- the scan a
    # scan engine keeps, the extension Aurora PostgreSQL needs, a word search's redesign --
    # and the lower score another candidate took for it. One source for the sentences:
    # substring_match, the same one the scorer and the decision trace read.
    for risk in substring_match_risks(data.collector, query_engine, data.engines):
        risk_id += 1
        risks.append({"risk_id": f"RISK-{risk_id:03d}", **risk})

    for target in sorted(coverage_gaps):
        gap = coverage_gaps[target]
        if not gap:
            continue
        risk_id += 1
        # #949: with no schema design for ``target``, every moved query "has no access
        # pattern" only because the design did not run.
        target_artifacts = data.engines.get(target)
        design_ran = bool(target_artifacts and target_artifacts.schema_design)
        if not design_ran:
            undesigned.add(target)
        risks.append(
            _coverage_gap_risk(
                f"RISK-{risk_id:03d}", target, gap, query_text, risk_tables, design_ran
            )
        )

    # Guard (#335): a query reality check moved onto an engine whose own schema
    # design lists it as unsupported. #338 fixes #335's root cause (a missing
    # aggregation/complex-joins capability check in the reality check's
    # serviceability gate) at the source; this is a narrow invariant guard in
    # case a gap like it slips through again, not the fix itself. Every such id
    # already has an open risk by construction (the per-pattern unsupported-
    # pattern risk above, at minimum), so this never adds a parallel risk: it
    # raises/annotates the existing one, and only falls back to a new one for
    # the (should not happen) case where none names the id.
    risk_id = _raise_moved_onto_unsupported_risks(
        risk_id, data, unsupported_by, risks, risk_tables, query_text
    )

    risks = [_without_repeated_mitigation(r) for r in risks]
    risks = ground_risks(risks, eliminated or {}, query_engine)
    resolved = ground_risks(resolved, eliminated or {}, query_engine)
    for r in resolved:
        logger.info(
            "Risk assessment: %s risk from %s resolved on %s (%s)",
            r["severity"],
            r["engine"],
            r["resolved_on"],
            r["reason"],
        )

    return {
        "overall_risk_level": overall_risk_level(risks),
        "risks": risks,
        "mitigation_strategies": _build_mitigation_strategies(
            risks, assigned or set(data.engines), undesigned
        ),
        # Anti-pattern risks the effective assignment resolved (#221): kept for the
        # audit trail so no risk, HIGH or otherwise, disappears without a record.
        "resolved_risks": resolved,
    }


_SQL_EXCERPT_CHARS = 120


def _sql_excerpt(sql: str) -> str:
    """``sql`` on one line without backticks, clipped to ``_SQL_EXCERPT_CHARS``.

    Backticks (MySQL identifier quotes) would open code spans in the Markdown engineering
    report, whose text escaping leaves them alone.
    """
    one_line = " ".join(sql.replace("`", "").split())
    if len(one_line) <= _SQL_EXCERPT_CHARS:
        return one_line
    return one_line[: _SQL_EXCERPT_CHARS - 1].rstrip() + "…"


def _unsupported_pattern_problem(engine: str, up: dict, query_text: dict[str, str]) -> str:
    """What an unsupported pattern's risk is about, without its fix (#252).

    The ``reason`` when the contract has one (documentdb, elasticache, opensearch). The
    dynamodb contract carries only ``pattern_type`` and ``recommendation`` -- the fix,
    which is the risk's mitigation -- so the problem is stated from the queries instead,
    quoting the first one's SQL.
    """
    reason = str(up.get("reason") or "").strip()
    if reason:
        return reason
    ids = unsupported_pattern_ids(up)
    # OpenSearch patterns carry the SQL themselves (``source_query``).
    sql = next((query_text[q] for q in ids if query_text.get(q, "").strip()), "") or str(
        up.get("source_query") or ""
    )
    head = f"{display_name(engine)} has no native equivalent for"
    if len(ids) > 1:
        return f"{head} these {len(ids)} queries" + (f", e.g. {_sql_excerpt(sql)}" if sql else ".")
    subject = "this query" if ids else "this pattern"
    return f"{head} {subject}" + (f": {_sql_excerpt(sql)}" if sql else ".")


def _without_repeated_mitigation(risk: dict) -> dict:
    """``risk`` with ``mitigation`` set to None when the description already contains it.

    A mitigation is never a copy of the description (#252); renderers omit an empty one.
    Compared case-insensitively with whitespace collapsed.
    """
    mitigation = " ".join(str(risk.get("mitigation") or "").split()).casefold()
    description = " ".join(str(risk.get("description") or "").split()).casefold()
    if mitigation and mitigation in description:
        return {**risk, "mitigation": None}
    return risk


# Table ids analysis emits when it cannot attribute a query to a table
# (e.g. ``SELECT FOUND_ROWS()``).
_PLACEHOLDER_TABLES = frozenset({"unknown", "UNKNOWN", "None", "null"})
# Names that are never a source table in a risk's ``affected_tables``.
_PSEUDO_TABLES = _PLACEHOLDER_TABLES | {"", "DUAL", "dual"}
_PSEUDO_TABLES_LOWER = frozenset(t.lower() for t in _PSEUDO_TABLES)


def _table_normaliser(data: SynthesisData) -> Callable[[Iterable[str]], set[str]]:
    """Map table names to the collector's source-table ids.

    Source-table ids are qualified (``<db>.table`` for MySQL, ``<schema>.table`` for
    PostgreSQL, SQL Server and Oracle) while query ``tables_accessed`` may be bare or
    differently cased. A name that is a known id is kept; otherwise its last dotted
    segment is matched case-insensitively against the known ids' last segments and a
    unique match gives the known id. A name that matches nothing (or several) is kept as
    given (fail-open). Only pseudo-tables (``unknown``, ``DUAL``) are dropped.
    """
    known = {str(t["table_id"]) for t in data.source_tables if t.get("table_id")}
    by_lower = {k.lower(): k for k in known}
    by_segment: dict[str, set[str]] = {}
    for k in known:
        by_segment.setdefault(k.rsplit(".", 1)[-1].lower(), set()).add(k)

    def resolve(name: str) -> str:
        if name in known:
            return name
        if name.lower() in by_lower:
            return by_lower[name.lower()]
        matches = by_segment.get(name.rsplit(".", 1)[-1].lower(), set())
        return next(iter(matches)) if len(matches) == 1 else name

    def normalise(names: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for raw in names:
            name = str(raw or "").strip()
            if name.lower() in _PSEUDO_TABLES_LOWER:
                continue
            out.add(resolve(name))
        return out

    return normalise


# Public name for the normaliser (#950: the migration risks match tables the same way).
table_normaliser = _table_normaliser


def _access_pattern_query_ids(ap: dict) -> list[str]:
    """Source query ids of an access pattern.

    DynamoDB/OpenSearch key them ``query_ids``; DocumentDB/ElastiCache use
    ``source_query_ids`` (the same split as their unsupported patterns, #210).
    """
    return list(ap.get("query_ids") or ap.get("source_query_ids") or [])


def _unsupported_query_ids(schema: dict) -> set[str]:
    """Query ids the schema design lists as unsupported (each is its own MEDIUM risk)."""
    ids: set[str] = set()
    for up in field_list(schema, "unsupported_patterns"):
        ids.update(unsupported_pattern_ids(up))
    return ids


def _is_aggregation_unsupported_pattern(up: dict) -> bool:
    """True when ``up``'s own category is aggregation, however its contract spells it (#336).

    Only DynamoDB's contract carries a real ``pattern_type`` (one of
    ``UNSUPPORTED_PATTERN_TYPES``); DocumentDB, ElastiCache and OpenSearch have
    no equivalent structured field, only free-text ``reason``/``workaround``
    (``unsupported_pattern_label`` falls back to the generic "unsupported
    pattern" for all three). Gating the dedup-only GROUP BY check (#336) on
    "this entry's own category is aggregation" -- not "this entry's queries
    happen to have a GROUP BY somewhere in their SQL" -- matters: a DynamoDB
    entry whose actual pattern_type is a multi-table join, or an ElastiCache
    entry about LIKE pattern matching, must not be reclassified just because
    one of its flagged queries also has a dedup-only GROUP BY elsewhere in its
    text.
    """
    pattern_type = str(up.get("pattern_type") or "").strip().strip("*").lower()
    if pattern_type:
        return pattern_type == "aggregation"
    return "aggregat" in unsupported_pattern_text(up).lower()


def _moved_query_ids(data: SynthesisData) -> dict[str, set[str]]:
    """Query ids Reality Check actually moved to a different engine, keyed by
    the CURRENT (post-move) engine (#335).

    Compares the effective assignment against the one Reality Check started
    from (``data.pre_reality_check_assignment``, loaded via
    ``resolve_reality_check_input_version`` -- see ``synthesis_data.py``): a
    query whose ``assigned_engine`` differs between the two was moved. This is
    not the same as "an anti-pattern on a different engine also names this
    query": an anti-pattern's own query_ids are routinely served by whichever
    engine the *initial* assignment resolver already picked -- most queries
    aurora_mysql's analysis flags, say, are assigned to dynamodb from the
    start, never "moved" there by anything. ``Consolidation``
    (``src/contracts/reality_check_output.py``) itself carries no per-query
    ids (only a per-engine-pair ``query_count``), so the only precise signal is
    this before/after diff.

    ``None`` for either assignment (no Reality Check run on record for this
    lineage, or the pre-run version could not be read) means nothing was
    moved -- fail safe, not "everything was moved".
    """
    if not data.assignment or not data.pre_reality_check_assignment:
        return {}
    before = {
        qa["query_id"]: qa.get("assigned_engine")
        for qa in data.pre_reality_check_assignment.get("query_assignments", [])
        if qa.get("query_id")
    }
    moved: dict[str, set[str]] = {}
    for qa in data.assignment.get("query_assignments", []):
        query_id = qa.get("query_id")
        after = qa.get("assigned_engine")
        if not query_id or not after:
            continue
        before_engine = before.get(query_id)
        if before_engine and before_engine != after:
            moved.setdefault(after, set()).add(query_id)
    return moved


def _relational_aurora_engine(data: SynthesisData) -> str:
    """The Aurora engine this architecture keeps as its relational core (#335's
    guard mitigation names it; the cache-layer "Why" cell fronts it).

    Source-compatible only for a homogeneous source (the source's own Aurora
    dialect, ``SOURCE_ENGINE_TO_AURORA``). For a heterogeneous source (SQL Server,
    Oracle, Db2) it is the Aurora engine ADR-031 Decision 6 chose, which is not
    source-compatible (Amendment of 2026-10-14, #656, #710): callers must never say
    it runs the source SQL as-is (``_runs_source_sql_as_is``).

    Delegates to ``table_assignments.retained_engine_for`` (#583/#586 review):
    the source's own dialect when it has one; else the resolver's recorded
    ``aurora_engine_choice``; else whichever Aurora engine ``query_assignments``
    shows actually in use, breaking a genuine tie through the shared ADR-031
    Decision 2/12 placeholder rather than an alphabetical pick -- the same
    helper ``migration_waves.build_migration_waves`` uses for wave 1, so this
    "Why" cell and risk note cannot name a different engine than wave 1 does
    for the same assignment.

    Falls back to whichever Aurora engine is already part of this architecture
    (``data.engines``) only when ``retained_engine_for`` has nothing to go on
    (no assignment yet); then to mapping the collector's source database engine
    (mysql/mariadb -> aurora_mysql, postgresql/postgres -> aurora_postgresql),
    defaulting to aurora_mysql when even that is unknown -- never wrong about
    there being an Aurora safety net, only about which flavour's name to print.
    """
    assignment = data.assignment or {}
    engine = retained_engine_for(
        data.collector,
        assignment.get("query_assignments"),
        assignment.get("aurora_engine_choice"),
    )
    if engine:
        return engine
    for engine in sorted(AURORA_ENGINES):
        if engine in data.engines:
            return engine
    source_engine = source_database_engine(data.collector)
    return SOURCE_ENGINE_TO_AURORA.get(source_engine, "aurora_mysql")


def _runs_source_sql_as_is(data: SynthesisData, engine: str) -> bool:
    """Whether ``engine`` is the source's own Aurora dialect, so it runs the source
    SQL unchanged (ADR-031 Decision 6). A heterogeneous or unknown source has no
    such engine (#710)."""
    return SOURCE_ENGINE_TO_AURORA.get(source_database_engine(data.collector)) == engine


def _raise_moved_onto_unsupported_risks(
    risk_id: int,
    data: SynthesisData,
    unsupported_by: dict[str, set[str]],
    risks: list[dict],
    risk_tables: dict[str, set[str]],
    query_text: dict[str, str],
) -> int:
    """Raise to HIGH, and annotate, the risk(s) for a query reality check moved
    onto an engine whose own schema design lists it as unsupported (#335's
    actual invariant, narrowed by review).

    #338 (``feat/engines-earn-their-place``) fixes #335's root cause -- a
    missing aggregation/complex-joins capability check in the reality check's
    serviceability gate, ``src/agents/referee/capability_registry.py`` -- at
    the source. This module does not decide routing, so it stays a narrow
    invariant guard rather than the fix: it reports a query that still ends up
    moved onto an engine that cannot serve it, in case a capability gap like
    #335's slips through again (#338's own gate has at least one known gap:
    it does not block a single-table ``COUNT(*)`` move).

    Never adds a risk in parallel with an existing one for the same id: every
    unsupported id already has an open risk by construction (at minimum, the
    per-pattern "unsupported_patterns" risk every such id gets above, whatever
    its severity -- MEDIUM, or LOW for a #336 dedup-only ``GROUP BY``). This
    raises that risk's severity to HIGH (never down -- a query already flagged
    CRITICAL or already HIGH for an unrelated reason keeps that) and appends
    one sentence naming the Aurora fallback, rather than creating a second
    entry for the same gap. Only when (by construction, should not happen) no
    existing risk names the id does this add a new dedicated one, so the gap
    is never silently dropped.

    Not CRITICAL: Aurora serves any SQL query relationally (#221): as-is on the
    source's own Aurora dialect, after conversion for a heterogeneous source
    (#710). So this is never "no engine serves it"; it is "stuck on the wrong
    engine rather than Aurora until that engine's schema design (or an
    application change) catches up", the same severity register as every other
    reattribution risk above.
    The note is phrased as advice ("keep it on Aurora until ..."), not as a
    statement of where the query is ("stays on Aurora"): the assignment still
    names the engine it was moved to, not Aurora.
    """
    moved = _moved_query_ids(data)
    aurora_engine = _relational_aurora_engine(data)
    aurora_name = display_name(aurora_engine)

    ids_by_engine = {
        engine: ids
        for engine in sorted(moved)
        if engine not in AURORA_ENGINES
        for ids in [moved[engine] & unsupported_by.get(engine, set())]
        if ids
    }
    if not ids_by_engine:
        return risk_id

    mitigation_addendum = (
        f"{aurora_name} runs the source SQL as-is."
        if _runs_source_sql_as_is(data, aurora_engine)
        else f"{aurora_name} is the relational fallback."
    )
    for engine, ids in ids_by_engine.items():
        # Grouped by the risk object itself (by risk_id, not by query id): several
        # of this engine's moved-and-unsupported ids can share one pre-existing
        # risk (e.g. one unsupported_patterns entry naming all of them), and each
        # such risk is annotated once, with its own it/them wording.
        claimed: dict[str, dict] = {}
        unclaimed: set[str] = set()
        for q in sorted(ids):
            target_risk = next((r for r in risks if q in (r.get("query_ids") or [])), None)
            if target_risk is None:
                unclaimed.add(q)
            else:
                claimed[target_risk["risk_id"]] = target_risk

        for target_risk in claimed.values():
            # This risk's own share of the moved-and-unsupported ids -- not every
            # id moved onto this engine necessarily sits in this particular risk.
            overlap = ids & set(target_risk.get("query_ids") or [])
            pronoun = "it" if len(overlap) == 1 else "them"
            note = (
                f" Moved onto {display_name(engine)}, which lists {pronoun} as "
                f"unsupported: keep {pronoun} on {aurora_name} until "
                f"{display_name(engine)}'s schema design (or an application "
                f"change) can serve {pronoun}."
            )
            if _SEVERITY_ORDER.get(target_risk.get("severity", "LOW"), 9) > _SEVERITY_ORDER["HIGH"]:
                target_risk["severity"] = "HIGH"
            if note not in target_risk["description"]:
                target_risk["description"] += note
            mitigation = target_risk.get("mitigation") or ""
            if aurora_name not in mitigation:
                target_risk["mitigation"] = (
                    f"{mitigation} {mitigation_addendum}".strip()
                    if mitigation
                    else mitigation_addendum
                )

        if not unclaimed:
            continue
        # Should not happen (every unsupported id has an unsupported_patterns risk
        # above), but a gap is never silently dropped if it ever does.
        ids_sorted = sorted(unclaimed)
        n = len(ids_sorted)
        pronoun = "it" if n == 1 else "them"
        tables = sorted({t for q in ids_sorted for t in risk_tables.get(q, ())})
        texts = [_sql_excerpt(query_text.get(q) or q[:12]) for q in ids_sorted[:3]]
        listed = "; ".join(texts) + (f"; and {n - 3} more" if n > 3 else "")
        risk_id += 1
        risks.append(
            {
                "risk_id": f"RISK-{risk_id:03d}",
                "risk_type": "MIGRATION_COMPLEXITY",
                "severity": "HIGH",
                "description": (
                    f"[{engine}] {n} {'query' if n == 1 else 'queries'} moved onto "
                    f"{display_name(engine)}, whose schema design lists {pronoun} as "
                    f"unsupported: {listed}. Keep {pronoun} on {aurora_name} until "
                    f"{display_name(engine)}'s schema design (or an application "
                    f"change) can serve {pronoun}."
                ),
                "affected_tables": tables,
                "mitigation": mitigation_addendum,
                "query_ids": ids_sorted,
            }
        )
    return risk_id


# #297: a flag over more queries than this is folded into its engine's one coverage
# gap risk instead of standing as a risk of its own.
BROAD_FLAGGED_QUERIES = 10
# How many tables a broad gap's mitigation names as the place to start.
_START_WITH_TABLES = 3


def _start_with(ids: list[str], query_tables: dict[str, set[str]]) -> str:
    """The "Start with ..." sentence naming the tables most of ``ids`` touch.

    For a gap broad enough that "these queries" is no bounded set (#297); empty for a
    small one."""
    if len(ids) <= BROAD_FLAGGED_QUERIES:
        return ""
    counts: dict[str, int] = {}
    for q in ids:
        for t in query_tables.get(q, set()) - _PLACEHOLDER_TABLES:
            counts[t] = counts.get(t, 0) + 1
    top = sorted(counts, key=lambda t: (-counts[t], t))[:_START_WITH_TABLES]
    if not top:
        return ""
    named = ", ".join(f"{t} ({counts[t]})" for t in top)
    return f" Start with the tables the most of them touch: {named}."


def _coverage_gap_risk(
    risk_id: str,
    engine: str,
    gap: dict[str, str],
    query_text: dict[str, str],
    query_tables: dict[str, set[str]],
    design_ran: bool = True,
) -> dict:
    """One risk for the queries assigned to ``engine`` that its design does not serve.

    ``design_ran`` False (#949): ``engine`` has no schema design, so its queries were
    never checked against one. The risk says so and is LOW: a phase that did not run
    never raises the overall level (``overall_risk_level``) on its own.
    """
    ids = sorted(gap)
    n = len(ids)
    severity = min(gap.values(), key=lambda s: _SEVERITY_ORDER.get(s, 9))
    texts = []
    for q in ids[:3]:
        t = " ".join(query_text.get(q, "").split()) or q[:12]
        texts.append(t if len(t) <= 80 else t[:77] + "...")
    listed = "; ".join(texts) + (f"; and {n - 3} more" if n > 3 else "")
    tables = sorted({t for q in ids for t in query_tables.get(q, set())} - _PLACEHOLDER_TABLES)
    name = display_name(engine)
    if not design_ran:
        return {
            "risk_id": risk_id,
            "risk_type": "MIGRATION_COMPLEXITY",
            "severity": "LOW",
            "description": (
                f"[{engine}] Not checked: schema design was not run for {name}, so "
                f"{n} {'query' if n == 1 else 'queries'} assigned to it "
                f"{'has' if n == 1 else 'have'} no access pattern to check against: {listed}"
            ),
            "affected_tables": tables,
            "mitigation": (
                f"Run the {name} schema design; any of these queries it does not serve "
                f"then becomes a schema design gap.{_start_with(ids, query_tables)}"
            ),
            "query_ids": ids,
            "coverage_gap": True,
        }
    return {
        "risk_id": risk_id,
        "risk_type": "MIGRATION_COMPLEXITY",
        "severity": severity,
        "description": (
            f"[{engine}] Schema design gap: {n} {'query' if n == 1 else 'queries'} assigned "
            f"to {name} {'has' if n == 1 else 'have'} no in-scope access "
            f"pattern: {listed} (0% of queries resolved by schema design, {n} remaining)"
        ),
        "affected_tables": tables,
        "mitigation": (
            f"Add in-scope access patterns for {'this query' if n == 1 else 'these queries'} "
            f"to the {name} schema design, or route "
            f"{'it' if n == 1 else 'them'} to an engine that serves "
            f"{'it' if n == 1 else 'them'}.{_start_with(ids, query_tables)}"
        ),
        "query_ids": ids,
        "coverage_gap": True,
    }


# aurora-anti-05 ("no-relational-need") and aurora-anti-06
# ("single-access-pattern-table"), aurora_common_pattern_catalog.py: both
# recommend DynamoDB for a table whose OWN flagged queries look like simple
# key-value access. That table can still have *other* in-scope queries that
# need a hard capability only Aurora offers (#338's capability gate) or are a
# utility/DDL statement (#327) -- the table then stays on Aurora for those
# other queries' sake, not because the flagged ones need it. A review of
# #375 found the risk reading as a flat contradiction of the routing in that
# case ("Consider DynamoDB" right next to an architecture that keeps the
# table on Aurora, with no reason given); it must instead name the reason
# the table stays, and offer DynamoDB as a later-wave opportunity for the
# flagged queries specifically, not a blanket recommendation.
_DYNAMODB_ALTERNATIVE_ANTI_PATTERNS = frozenset(
    {"no-relational-need", "single-access-pattern-table"}
)

# The capability gate (assignment_resolver.py, reality_check_rules.py) and the
# utility pin (#327) already explain themselves in a query's own
# assignment_reason text ("DynamoDB lacks required capability: aggregation,
# complex_joins", "utility/metadata statement -- kept on source-compatible
# relational engine") -- reused here rather than re-deriving the same decision
# from the query text a second time. #572: matches on the phrase alone, not a
# leading engine name/tag, since assignment_reason now names the engine with
# display_engine (a display name can be two words, e.g. "Aurora PostgreSQL"),
# never the raw id or an internal "[capability]"/rule_id tag.
_CAPABILITY_PIN_RE = re.compile(r"lacks required capability:\s*([^;]+)")
_UTILITY_PIN_RE = re.compile(r"utility/metadata statement")
# Display strings for every capability the "lacks required capability: ..." reason
# can name -- shared with assignment_resolver.py (#572 review suggestion 7), which
# builds that reason text, so the two can never show a different name for the same
# capability. See src.shared.capability_labels's own docstring for the coverage
# story (#393) that keeps this in sync with capability_registry.
_CAPABILITY_PIN_DISPLAY = CAPABILITY_DISPLAY_NAMES


def _capability_pin_reason(reasons: Iterable[str]) -> str | None:
    """The named reason (joins, aggregation, a utility statement, ...) that some
    query sharing ``reasons``' table is pinned to its engine by a hard
    capability, or ``None`` when none of ``reasons`` names one.

    Reads every "<engine> lacks required capability: ..." fragment in each
    reason with ``finditer``, not just the first (#393 review): different excluded
    engines can lack different, non-identical subsets of the required capabilities (an
    engine that already has aggregation but not joins lists only "complex_joins", while
    one with neither lists both), so only the first fragment under-reported the full set.
    """
    found: set[str] = set()
    for reason in reasons:
        reason = reason or ""
        if _UTILITY_PIN_RE.search(reason):
            found.add(_CAPABILITY_PIN_DISPLAY["sql_admin"])
        for match in _CAPABILITY_PIN_RE.finditer(reason):
            for cap in match.group(1).split(","):
                cap = cap.strip()
                if cap:
                    found.add(_CAPABILITY_PIN_DISPLAY.get(cap, cap))
    if not found:
        return None
    names = sorted(found)
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _aurora_capability_lead(data: SynthesisData, engine: str) -> tuple[int, str] | None:
    """Why ``engine``'s (an Aurora engine) queries stay relational (#393): the hard
    capability pins (joins, aggregation, a utility/DDL statement) named in each
    in-scope query's own ``assignment_reason`` -- the same source
    :func:`_capability_pin_reason` and the resolved-risk pin text (above) read, so
    the "Why" cell cannot contradict them with the leading workload signal, which
    is often key-value lookups riding along in the same co-dependency group, not
    the reason the table is relational at all.

    Returns ``(pinned_query_count, capability_names)``, or ``None`` when no in-scope
    query assigned to ``engine`` carries a capability pin -- the caller then falls
    back to the routed-workload signal.
    """
    pinned: set[str] = set()
    reasons: list[str] = []
    for qa in (data.assignment or {}).get("query_assignments", []):
        if not qa.get("in_scope", True) or qa.get("assigned_engine") != engine:
            continue
        if qa.get("pinned"):
            # A utility pin has its own note ("N more queries are pinned
            # (utility)", #1373), so it is not counted here too.
            continue
        reason = qa.get("assignment_reason") or ""
        if _UTILITY_PIN_RE.search(reason) or _CAPABILITY_PIN_RE.search(reason):
            qid = qa.get("query_id")
            if qid:
                pinned.add(qid)
            reasons.append(reason)
    if not pinned:
        return None
    caps = _capability_pin_reason(reasons)
    return (len(pinned), caps) if caps else None


def _tables_for(
    query_ids: set[str], ap_tables: list[str], query_tables: dict[str, set[str]]
) -> list[str]:
    """The anti-pattern's tables that the given queries touch (all of them if unknown)."""
    touched: set[str] = set()
    for q in query_ids:
        touched |= query_tables.get(q, set())
    narrowed = sorted(t for t in ap_tables if t in touched)
    return narrowed or sorted(ap_tables)


def _design_conflict_risks(
    risk_id: int,
    engine: str,
    schema: dict,
    normalise: Callable[[Iterable[str]], set[str]],
) -> tuple[int, list[dict]]:
    """One MEDIUM risk per unresolved cross-group design conflict (#426).

    ``dynamodb_merge.independent_homes`` adds a review trade-off (collected by
    :func:`merge_overlaps`) when a source table is the primary entity of two or
    more tables designed by different groups -- it never raises a validation failure, so the merge can succeed
    with the conflict still open. That trade-off is real (it is in the
    deliverables' trade-off list), but names a decision the migration still has
    to make, which makes it a risk too. ``affected_tables`` is the shared source
    tables (normalised like every other risk's); ``query_ids`` is every
    in-scope access pattern whose own ``source_tables`` actually touches one of
    those shared tables, read against the tables the overlap produced -- not
    every access pattern on those tables, which would also list queries against
    other entities an item-collection table happens to also hold. The risk
    disappears exactly when the trade-off does: once the tables are
    consolidated, a later merge stops emitting it (a group's own trade-off
    naming the overlap does not, #333).
    """
    conflicts = merge_overlaps(schema)
    if not conflicts:
        return risk_id, []

    risks: list[dict] = []
    for conflict in conflicts:
        risk_id += 1
        sources = normalise(conflict.get("source_tables") or [])
        targets = {t for t in conflict.get("target_tables") or [] if isinstance(t, str)}
        query_ids = sorted(
            {
                q
                for ap in schema.get("access_patterns") or []
                if isinstance(ap, dict)
                and str(ap.get("table_name")) in targets
                and normalise(ap.get("source_tables") or []) & sources
                for q in ap.get("query_ids") or []
            }
        )
        description = str(conflict["description"])[len(OVERLAP_PREFIX) :].strip()
        risks.append(
            {
                "risk_id": f"RISK-{risk_id:03d}",
                "risk_type": "DATA_CONSISTENCY",
                "severity": "MEDIUM",
                "description": f"[{engine}] {description}",
                "affected_tables": sorted(sources),
                "mitigation": _overlap_mitigation(
                    overlap_home(conflict, schema.get("access_patterns") or [])
                ),
                "query_ids": query_ids,
            }
        )
    return risk_id, risks


def _overlap_mitigation(home: tuple[str, list[str]] | None) -> str:
    """The step a DynamoDB overlap leaves to the customer (#1528): the merge already
    chose the authoritative home (#333), so the copies need their sync, or a GSI of the
    home instead."""
    if home is None:
        return (
            "Consolidate to one table per source table, or document the "
            "dual-write path and keep the copies in sync."
        )
    kept, copies = home
    shown = " and ".join([", ".join(copies[:-1]), copies[-1]] if len(copies) > 1 else copies)
    return (
        f"Build the DynamoDB Streams consumer that keeps {shown} in sync from {kept}, the "
        f"authoritative home, or move the access patterns on {shown} onto a global "
        f"secondary index of {kept}."
    )


def _without_hedge(description: str) -> str:
    """``description`` without the catalog's engine-hint sentence (#649).

    The ``CATALOG_ENGINE_HINTS`` sentences (e.g. "...could run on a simpler, purpose-built
    engine (DynamoDB..., ElastiCache...)", "OpenSearch provides purpose-built inverted
    indexes...") are the analysis finding's advice naming a fixed engine; in the report
    they contradict the engine a risk is attributed to (a risk now on
    DocumentDB suggesting DynamoDB or ElastiCache) and, once resolved, hedges a decision
    already made. The analysis output and the catalog keep it.
    """
    # Remove each hint's exact text (not the sentences that contain it), so a hint that
    # grows to several sentences, or shares a sentence with other text, is still removed
    # whole and nothing of it is left behind.
    text = description
    for hint in CATALOG_ENGINE_HINTS:
        text = text.replace(hint, " ")
    text = " ".join(text.split())
    return text or description


def _assigned_engine_mitigation(
    recommendation: str | None, engine: str, n_queries: int
) -> str | None:
    """The risk's mitigation for deliverables, naming only the engine it is attributed to.

    A catalog ``guidance`` that is an alternative-engine suggestion ("Consider DynamoDB ...
    or ElastiCache") contradicts a table that stays on ``engine`` (#668); it is replaced by
    the load-test action the mitigation strategies already use. Any other recommendation
    (an analysis agent's own wording) is kept. ``analysis.json`` is not touched.
    """
    if (recommendation or "") not in CATALOG_ENGINE_GUIDANCE:
        return recommendation
    noun = "query" if n_queries == 1 else "queries"
    count = f"the {n_queries} flagged {noun}" if n_queries else "the flagged queries"
    name = display_name(engine)
    if recommendation == TIME_SERIES_INGEST_GUIDANCE:
        return (
            f"Set a retention rule on {name} for the rows written by {count}: partition the "
            "table by time and drop or archive old partitions (for example to Amazon S3), and "
            "size storage for the rows you keep."
        )
    if recommendation in (FULL_TEXT_SEARCH_GUIDANCE, HIGH_VOLUME_TEXT_SEARCH_GUIDANCE):
        return (
            f"Load-test the text search in {count} on {name} with production-scale data and "
            "review its text index and search configuration where latency or result quality "
            "misses the target."
        )
    return (
        f"Load-test {count} on {name} with production-scale data before "
        "cutover and tune indexes or query plans where the measured latency misses the target."
    )


def _moved_description(description: str, target: str) -> str:
    """Resolved-item text for queries the assignment moved to ``target``: the decision,
    then the finding's factual sentences."""
    return f"Moved to {display_name(target)}: {_without_hedge(description)}"


def _resolved_risk(
    engine: str,
    severity: str,
    description: str,
    tables: list[str],
    query_ids: set[str],
    resolved_on: str | None,
    reason: str,
) -> dict:
    return {
        "engine": engine,
        "severity": severity,
        "description": f"[{engine}] {_without_hedge(description)}",
        "affected_tables": tables,
        "query_ids": sorted(query_ids),
        "resolved_on": resolved_on,
        "reason": reason,
    }


def _complementary_services(effective_engines: set[str]) -> list[str]:
    """Where unsupported patterns can go: services in the effective architecture that
    take text search / aggregations, then application code."""
    services = []
    if "opensearch" in effective_engines:
        services.append(display_name("opensearch"))
    for engine in sorted(effective_engines & AURORA_ENGINES):
        services.append(f"{display_name(engine)} (full-text indexes, SQL aggregation)")
    services.append("application code")
    return services


_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
# Migration-note object types that are database-side code the application must absorb.
_DB_CODE_OBJECTS = ("procedure", "trigger", "view")


def _join_and(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _join_or(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} or {items[-1]}"


def _tagged_engine(risk: dict) -> str | None:
    text = str(risk.get("description") or "")
    if text.startswith("[") and "]" in text:
        return text[1 : text.index("]")]
    return None


def _engines_of(risks: list[dict]) -> list[str]:
    """Display names of the engines the risks are tagged with, in first-seen order."""
    seen: list[str] = []
    for r in risks:
        engine = _tagged_engine(r)
        if engine and engine not in seen:
            seen.append(engine)
    return [display_name(e) for e in seen]


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _risk_scope(risk: dict) -> str:
    tables = [t for t in risk.get("affected_tables") or [] if t]
    if tables:
        shown = ", ".join(tables[:3])
        return shown + (f" and {len(tables) - 3} more" if len(tables) > 3 else "")
    n = len(risk.get("query_ids") or [])
    return f"{n} {'query' if n == 1 else 'queries'}" if n else "no table recorded"


def _build_mitigation_strategies(
    risks: list[dict], effective_engines: set[str], undesigned: set[str] | None = None
) -> list[str]:
    """Mitigation strategies derived from the risks actually present (#222).

    ``undesigned`` (#949) are the engines with no schema design: their coverage gap
    is a check that did not run, so its strategy is to run the design.

    The CRITICAL/HIGH risks come first, each with its engine, scope and own
    mitigation; every other line names the risks, engines or objects it is about.
    Only engines in ``effective_engines`` (the post-reality-check architecture) are
    suggested as homes for unsupported patterns.

    Only risk-specific strategies: the list is empty when no risk is open (#645). The
    post-migration checks, one per migration wave, come from :func:`build_wave_checks`.
    """
    strategies: list[str] = []
    severe = sorted(
        (r for r in risks if r.get("severity") in ("CRITICAL", "HIGH")),
        key=lambda r: (_SEVERITY_ORDER.get(r["severity"], 9), r.get("risk_id", "")),
    )
    for r in severe:
        engine = _tagged_engine(r)
        head = r.get("risk_id", "Risk")
        label = f"{r['severity']}, {display_name(engine)}" if engine else r["severity"]
        mitigation = r.get("mitigation")
        body = (
            _sentence(mitigation)
            if mitigation
            else "no mitigation recorded; define one before cutover."
        )
        strategies.append(severe_risk_strategy(head, label, _risk_scope(r), body))

    by_type: dict[str, list[dict]] = {}
    for r in risks:
        by_type.setdefault(r.get("risk_type", ""), []).append(r)

    perf = by_type.get("PERFORMANCE_DEGRADATION", [])
    if perf:
        strategies.append(
            f"Load-test the queries behind {_join_and([r['risk_id'] for r in perf])} on "
            f"{_join_and(_engines_of(perf)) or 'the target databases'} with "
            "production-scale data before cutover."
        )

    complexity = by_type.get("MIGRATION_COMPLEXITY", [])
    unsupported = [r for r in complexity if not r.get("coverage_gap")]
    unchecked = [
        r for r in complexity if r.get("coverage_gap") and _tagged_engine(r) in (undesigned or ())
    ]
    gaps = [r for r in complexity if r.get("coverage_gap") and r not in unchecked]
    for r in unchecked:
        n = len(r.get("query_ids") or [])
        strategies.append(
            f"Run the {display_name(_tagged_engine(r) or '')} schema design to check the {n} "
            f"{'query' if n == 1 else 'queries'} assigned to it ({r['risk_id']})."
        )
    if gaps:
        n = sum(len(r.get("query_ids") or []) for r in gaps)
        strategies.append(
            f"Add in-scope access patterns for the {n} "
            f"{'query' if n == 1 else 'queries'} the {_join_and(_engines_of(gaps))} schema "
            f"{'design does' if len(gaps) == 1 else 'designs do'} not serve "
            f"({', '.join(r['risk_id'] for r in gaps)}), or route "
            f"{'it' if n == 1 else 'them'} to an engine that serves "
            f"{'it' if n == 1 else 'them'}."
        )
    if unsupported:
        n = len(unsupported)
        engines = _join_and(_engines_of(unsupported))
        strategies.append(
            f"Serve the {n} unsupported query {'pattern' if n == 1 else 'patterns'}"
            + (f" on {engines}" if engines else "")
            + f" ({', '.join(r['risk_id'] for r in unsupported)}) "
            + _join_or([f"in {s}" for s in _complementary_services(effective_engines)])
            + "."
        )

    notes = by_type.get("OPERATIONAL_RISK", [])
    db_code = [r for r in notes if str(r.get("object_type", "")).lower() in _DB_CODE_OBJECTS]
    other_notes = [r for r in notes if r not in db_code]
    if db_code:
        objects = _join_and(
            [f"{r['object_type'].lower()} '{r.get('object_name', '')}'" for r in db_code]
        )
        strategies.append(
            f"Re-implement the {objects} as application logic before migration "
            f"({', '.join(r['risk_id'] for r in db_code)})."
        )
    if other_notes:
        items = _join_and(
            [
                f"{r.get('object_name') or r.get('object_type') or 'migration note'} "
                f"({r['risk_id']})"
                for r in other_notes
            ]
        )
        strategies.append(
            f"Build the application-side {'replacement' if len(other_notes) == 1 else 'replacements'} "
            f"for {items} before cutover."
        )

    if severe:
        tables = sorted({t for r in severe for t in (r.get("affected_tables") or []) if t})
        strategies.append(
            "Use blue-green deployment with rollback for the tables behind the critical or "
            "high risks: " + ", ".join(tables) + "."
            if tables
            else "Use blue-green deployment with rollback for the migrations behind "
            + _join_and([r["risk_id"] for r in severe])
            + "."
        )

    return strategies


def _wave_label(wave: dict) -> str:
    engines = [display_name(e) for e in wave.get("engines") or [] if e]
    return (
        f"wave {wave.get('wave')} ({_join_and(engines)})" if engines else f"wave {wave.get('wave')}"
    )


def _wave_baseline_ms(query_ids: set[str], query_groups: list[dict]) -> float | None:
    """Call-weighted mean of the source ``execution_time_ms_avg`` of ``query_ids``."""
    seen: dict[str, tuple[float, float]] = {}
    for group in query_groups:
        for q in group.get("source_queries") or []:
            qid = q.get("query_id")
            ms = q.get("execution_time_ms_avg")
            if qid in query_ids and qid not in seen and isinstance(ms, (int, float)):
                weight = q.get("frequency_per_hour")
                seen[qid] = (float(ms), float(weight) if isinstance(weight, (int, float)) else 0.0)
    if not seen:
        return None
    total = sum(w for _, w in seen.values())
    if total <= 0:
        return sum(ms for ms, _ in seen.values()) / len(seen)
    return sum(ms * w for ms, w in seen.values()) / total


def _wave_design_rps(
    query_ids: set[str],
    query_groups: list[dict],
    engines: set[str] | None,
    skip_engines: set[str],
) -> float:
    """Design request rate of the in-scope access patterns that serve ``query_ids``.

    Each ``(engine, pattern_id)`` counts once. ``engines`` limits the patterns to a
    wave's own engines; ``None`` (the cutover wave, where every query moves) takes
    every engine's patterns except ``skip_engines`` (the cache layer, which owns none).
    """
    seen: dict[tuple[str, str], float] = {}
    for group in query_groups:
        for ap in group.get("access_patterns") or []:
            engine = ap.get("engine")
            if (
                (engine in engines if engines is not None else engine not in skip_engines)
                and ap.get("in_scope", True) is not False
                and isinstance(ap.get("design_rps"), (int, float))
                and query_ids & {str(q) for q in ap.get("query_ids") or []}
            ):
                seen.setdefault((str(engine), str(ap.get("pattern_id"))), float(ap["design_rps"]))
    return sum(seen.values())


def build_wave_checks(
    waves: list[dict] | None,
    query_groups: list[dict],
    query_assignments: list[dict],
    cache_overlay: dict | None,
) -> list[str]:
    """One post-migration check per migration wave (#645), from ``report.json`` facts.

    Each names the wave and its engines, the queries that move in it (a cutover wave
    carries ``cutover_query_count``, the whole workload), their source latency baseline
    (``execution_time_ms_avg`` weighted by ``frequency_per_hour``) and the design
    request rate of the same queries' in-scope access patterns; a cache wave names the
    hot reads it fronts and their share of calls. The check gates the next wave. Cache
    figures use the Engineering Report's formatter. No waves, no checks: nothing here is
    invented beyond what the report states.
    """
    from src.report.formatting import fmt_num  # the report's one number formatter

    waves = [w for w in waves or [] if isinstance(w, dict) and w.get("wave") is not None]
    overlay = cache_overlay or {}
    cache_engines = {str(w["engines"][0]) for w in waves if w.get("fronts") and w.get("engines")}
    cache_engines |= {str(overlay["engine"])} if overlay.get("engine") else set()
    checks: list[str] = []
    for i, wave in enumerate(waves):
        if wave.get("no_move_reason"):
            # #1383: a wave that moves nothing has no latency to compare after it.
            continue
        gate = f" before starting wave {waves[i + 1]['wave']}" if i + 1 < len(waves) else ""
        label = _wave_label(wave)
        if wave.get("fronts"):
            n = wave.get("query_count")
            reads = _count(n, "hot read", "hot reads") if n else "the hot reads"
            text = (
                f"After {label}: track the cache hit rate for the {reads} it fronts "
                f"on {display_name(wave['fronts'])}"
            )
            detail = []
            if overlay.get("call_share_percent") is not None:
                detail.append(f"{fmt_num(overlay['call_share_percent'], 1)}% of calls")
            if overlay.get("calls_per_second") is not None:
                detail.append(f"{fmt_num(overlay['calls_per_second'], 1)} calls/s")
            if detail:
                text += f" ({', '.join(detail)})"
            checks.append(text + f" and compare their latency with the source{gate}.")
            continue
        engines = {str(e) for e in wave.get("engines") or []}
        cutover = wave.get("cutover_query_count")
        n = cutover if cutover else wave.get("query_count")
        ids = {
            str(qa["query_id"])
            for qa in query_assignments
            if qa.get("query_id")
            and qa.get("in_scope", True) is not False
            and (cutover or qa.get("assigned_engine") in engines)
        }
        queries = _count(n, "query", "queries") if n else "its queries"
        baseline = _wave_baseline_ms(ids, query_groups)
        text = f"After {label}: compare the latency of {queries} with their source baseline"
        if baseline:
            text += f" (weighted average {fmt_num(baseline, 2)} ms)"
        rps = _wave_design_rps(ids, query_groups, None if cutover else engines, cache_engines)
        if rps > 0:
            text += (
                f" and check the engine sustains the design rate of {fmt_num(rps, 1)} requests/s"
            )
        checks.append(text + f"{gate}.")
    return checks


def designed_and_not_designed_engines(ranking: list[dict]) -> tuple[list[str], list[str]]:
    """Engines in the effective architecture, split by whether a schema design
    exists for them (#132 review, 370-1/370-2).

    A *versioned* entry (an assignment ran, so ``assigned_queries`` is present,
    even if ``0``) is limited to engines with workload (``assigned_queries``)
    or a cache overlay (``cache_overlay_queries``): an engine with neither was
    not part of the effective assignment at all (eliminated, or never routed
    anything), so it belongs in neither list -- there is nothing to say about
    it either way.

    An *unversioned* entry (no assignment artifact at all, so
    ``assigned_queries`` was never added) has no workload signal to gate on in
    the first place, so it is classified by ``schema_design_available`` alone
    (#370 recheck): gating it the same way an assignment-aware entry is gated
    treated "no assignment yet" the same as "assignment ran and routed
    nothing here", which emptied both lists even when an engine clearly had a
    design and made the executive-summary prompt fall back to the fully
    assertive branch with nothing to back it.

    The single source of truth for "has a design" everywhere this question is
    asked: the architecture rationale, the deterministic summary, and both the
    Bedrock and Claude Code (external) executive-summary prompts
    (``prepare_synthesis_llm_input``'s ``schema_design_status``), so they
    can't drift out of step (370-4).
    """
    designed: list[str] = []
    not_designed: list[str] = []
    for r in ranking:
        if "assigned_queries" in r:
            has_workload = r.get("assigned_queries", 0) > 0 or r.get("cache_overlay_queries", 0) > 0
            if not has_workload:
                continue
        if r.get("schema_design_available"):
            designed.append(r["target"])
        else:
            not_designed.append(r["target"])
    return designed, not_designed


def source_database_label(engine: str) -> str:
    """The source engine as the executive summary names it (e.g. "MySQL").

    An unknown or missing engine reads as "relational database" rather than
    guessing one.
    """
    if not engine:
        return "relational database"
    return SOURCE_ENGINE_DISPLAY_NAMES.get(engine, engine)


def schema_design_case(designed: list[str], not_designed: list[str]) -> str:
    """Which ``Schema Design Status`` case of ``src/skills/executive-summary.md``
    applies (#628): ``all_designed``, ``some_designed`` or ``none_designed``.

    Both lists empty is ``none_designed`` (#370 recheck: an empty
    ``not_designed`` is NOT by itself proof everything is designed). Both
    summary modes take the case from here, so neither has to infer it.
    """
    if designed and not not_designed:
        return "all_designed"
    if designed:
        return "some_designed"
    return "none_designed"


def build_architecture_recommendation(
    data: SynthesisData,
    ranking: list[dict],
    table_mappings: list[dict],
) -> dict:
    """Determine the recommended architecture type and database allocations."""
    # Count engines that have actual query assignments or schema designs
    engines_with_workload = [
        r["target"]
        for r in ranking
        if r.get("assigned_queries", 0) > 0
        or r.get("cache_overlay_queries", 0) > 0
        or r.get("schema_design_available")
    ]

    # Architecture type
    if len(engines_with_workload) <= 1:
        arch_type = "SINGLE_DATABASE"
    else:
        # Check if any engine is a cache (elasticache)
        has_cache = any("cache" in e.lower() for e in engines_with_workload)
        arch_type = "HYBRID_WITH_CACHE" if has_cache else "MULTI_DATABASE"

    # Database allocations
    engine_tables: dict[str, list[str]] = {}
    for mapping in table_mappings:
        engine = mapping["recommended_database"]
        engine_tables.setdefault(engine, []).append(mapping["source_table"])

    # When NO engine has a schema design (llm_mode=none, #281), table_mappings
    # are empty, so every engine with workload is listed with no tables; leaving
    # them all out made the rationale read "Hybrid architecture: .". In a run
    # where some engine was designed, an engine with workload but no design is
    # the retained engine: it stays out of databases, so the report renderers
    # call it "Retained" and the deck puts it in the no-migration Wave 1.
    designed_engines, _not_designed_engines = designed_and_not_designed_engines(ranking)
    no_schema_design = not designed_engines
    # #287: the broadened listing above (every workload engine, so the rationale
    # below is never empty) must not sweep in the retained engine too -- the one
    # whose dialect the source database already speaks, so it carries no data
    # migration of its own. Decided from the homogeneous mapping only
    # (SOURCE_ENGINE_TO_AURORA), never ``_relational_aurora_engine``'s
    # broader guess: that helper also returns whichever Aurora engine the
    # heterogeneous resolver (SQL Server/Oracle/DB2, #381) picked as a real
    # migration target when neither Aurora dialect matches the source, and
    # mapping a source engine ``SOURCE_ENGINE_TO_AURORA`` has no entry for
    # (every heterogeneous source) correctly yields ``None`` -- no retained
    # engine. Also ``None`` unless that engine actually carries workload here,
    # so a homogeneous source fully migrated off its native Aurora engine is
    # never named "retained" for work it no longer does. Excluded from
    # ``databases`` the same way the mixed-run case below already excludes an
    # undesigned retained engine, letting it fall through to the "Retained"
    # status check in ``report_facts.engine_role`` instead of reading as a
    # migration target.
    #
    # #949: the same holds when that engine has a schema design of its own (an Aurora
    # design: DDL and optimizations for the tables it keeps). It is still the retained
    # core, not a migration target: listed here, every deliverable said all of its
    # queries "move to a new engine".
    # In a designed run that needs routed queries: a design alone is no workload.
    candidate = SOURCE_ENGINE_TO_AURORA.get(source_database_engine(data.collector))
    routed = any(r["target"] == candidate and r.get("assigned_queries", 0) > 0 for r in ranking)
    retained_engine: str | None = (
        candidate if candidate in engines_with_workload and (no_schema_design or routed) else None
    )
    databases = []
    for r in ranking:
        engine = r["target"]
        tables = engine_tables.get(engine, [])
        if engine == retained_engine:
            continue
        if not tables and not r.get("schema_design_available"):
            if not (no_schema_design and engine in engines_with_workload):
                continue

        databases.append(
            {
                "service": engine,
                "table_count": len(tables),
                "rationale": r.get("rationale") or _engine_rationale(data, r),
                "tables": tables,
                "confidence_score": r["confidence_score"],
                "routed_confidence": r.get("routed_confidence"),
            }
        )

    rationale = _architecture_rationale(arch_type, databases, ranking, retained_engine)
    # #287 round 2: an Aurora-only run (``databases`` empty, everything retained)
    # still has not run schema design -- the note is about the run, not about
    # how many tables ``databases`` happens to list, so it belongs here too.
    if no_schema_design and (databases or retained_engine):
        rationale += " Schema design was not run, so no tables are allocated yet."
    return {
        "databases": databases,
        "architecture_type": arch_type,
        "rationale": rationale,
    }


def _routed_phrase(r: dict, noun: str, what: tuple[str, str]) -> str:
    """``90% mean fit across 98 queries (22 rated tables)`` (#152); empty without one.

    "Rated" because ``routed_tables`` counts only the source tables the engine's
    *analysis* rated (#312 review) -- not every table the routed queries touch,
    which the schema design's own table count already states elsewhere.

    Table-scored engines name rated tables and queries without table evidence,
    using the labels and threshold of ``src.shared.ranking``. Pattern-only
    engines name matched patterns and signal-only queries instead. A ``pattern``
    fit keeps "(each) matches a detected pattern".
    """
    fit = r.get("routed_confidence")
    if fit is None:
        return ""
    n = int(r.get("routed_queries") or 0)
    n_t = int(r.get("routed_tables") or 0)
    unbacked = int(r.get("routed_queries_without_table_evidence") or 0)
    evidence = r.get("routed_confidence_evidence")
    if evidence == "signal_only" or (evidence is None and not n_t):
        detail = evidence_note(r) or SIGNAL_ONLY_NOTE
    elif evidence == "pattern":
        # #859: a pattern-only engine (OpenSearch) never scores a table, so its
        # evidence is the query's own matched pattern, not a rated-table count.
        detail = "matches a detected pattern" if n == 1 else "each matches a detected pattern"
    elif evidence == "partial" and r.get("target") in PATTERN_ONLY_ENGINES:
        matched = n - unbacked
        verb = "matches" if matched == 1 else "match"
        detail = (
            f"{_count(matched, *what)} {verb} a detected pattern; "
            f"{_count(unbacked, *what)} signal only"
        )
    else:
        detail = _count(n_t, "rated table", "rated tables")
        if unbacked:
            detail += f"; {_count(unbacked, *what)} without table-level evidence"
            if evidence_note(r) == PARTIAL_NOTE:
                detail += f", {PARTIAL_NOTE}"
    return f"{fit}% {noun} across {_count(n, *what)} ({detail})"


def _pinned_note(r: dict, what: tuple[str, str]) -> str:
    """``; 13 more queries are pinned (utility), not in the mean`` (#1373); empty
    with none: the routed queries a pin, not their score, placed on the engine."""
    pinned = int(r.get("routed_pinned_queries") or 0)
    if not pinned:
        return ""
    verb = "is" if pinned == 1 else "are"
    return f"; {_count(pinned, *what).replace(' ', ' more ', 1)} {verb} {PINNED_UTILITY_LABEL}, not in the mean"


def _engine_rationale(data: SynthesisData, r: dict) -> str:
    """Generate a rationale string for an engine recommendation.

    It names the workload routed to the engine and how well the engine fits it
    (#152), e.g. "90% mean fit across 98 queries (22 rated tables), led by key-value
    lookups (36 of 98)". The analysis average over every analyzed table is the
    ``analysis_confidence`` field, not the rationale: it describes the tables the
    engine looked at, not the work it was given. A report without an assignment
    keeps that average, since nothing is routed yet.

    Access patterns are counted in scope, as in the summary (#255). The cache layer
    is described by the reads it fronts, not by an owner share (#296) -- via
    ``cache_front_description``, the one place every deliverable's
    cache-fronting sentence is built (#375 review), so this "Why" cell and the
    roadmap's own wave 2 card cannot name different engines.

    An Aurora engine never leads with the routed workload signal (#393): see
    ``_aurora_capability_lead``. It leads with the hard capability pins (joins,
    aggregation, a utility/DDL statement) instead, so the "Why" cell cannot say an
    engine the rest of the report frames as the relational core is "led by
    key-value lookups" while naming no relational reason at all.
    """
    if is_cache_layer(r):
        # #381: a source with no Aurora dialect of its own has no entry in
        # SOURCE_ENGINE_TO_AURORA, but the assignment resolver may still have picked
        # one of the two competing Aurora engines -- _relational_aurora_engine
        # already prefers whichever Aurora engine is actually part of this
        # architecture before falling back to that mapping, so it names the #381
        # choice here too instead of silently falling back to no engine at all.
        retained_engine = _relational_aurora_engine(data)
        owners = cache_front_description(retained_engine, r.get("cache_overlay_owners"))
        n = r.get("cache_overlay_queries", 0)
        lead = r.get("routed_lead")
        shape = f", mostly {PATTERN_LABELS.get(lead, lead.replace('_', ' '))}s" if lead else ""
        head = (
            f"Cache layer for {n} designed cached {'read' if n == 1 else 'reads'} "
            f"({r.get('cache_call_share_percent', 0)}% of calls){shape}, cache-aside in front "
            f"of {owners}; it owns no queries"
        )
        fit = _routed_phrase(r, "mean cache fit", ("cached read", "cached reads"))
        parts = [head + (f"; {fit}" if fit else "")]
        if r.get("target_tables", 0) > 0:
            parts.append(
                f"Schema design: {_count(r['target_tables'], 'key design', 'key designs')}, "
                + _in_scope_phrase(*_access_pattern_scope(data, r))
            )
        cps = r.get("cache_calls_per_second", 0.0)
        floor = r.get("cache_min_calls_per_second", 0.0)
        if floor:
            # Review of #375: the cache is justified by the overlay's own
            # eligibility floor (#304/#296) and the combined traffic that
            # cleared it. #951: the cost itself is the Cost table's, never a
            # fragment appended to the reason.
            parts.append(
                f"These reads total {cps:g} calls/s, above the {floor:g} calls/s hot-read floor"
            )
        return ". ".join(parts) + "."
    if r.get("routed_confidence") is not None:
        # Every routed query, pinned ones included: the capability pins below
        # count utility pins too (#1373).
        n = int(r.get("routed_queries") or 0) + int(r.get("routed_pinned_queries") or 0)
        engine = r.get("target", "")
        lead_clause = None
        if engine in AURORA_ENGINES:
            # #393: an Aurora engine's leading *workload* signal is often
            # key-value lookups riding along in the same co-dependency group as
            # the joins/aggregation/utility statements that actually pin the
            # table to a relational engine -- naming the workload signal here
            # instead reads as contradicting the "relational core" framing the
            # rest of the report gives Aurora. When any in-scope query routed
            # here carries one of those hard capability pins, lead with that
            # instead; only an Aurora engine with no pinned query at all (e.g. a
            # 1:1, not-yet-scored carry-over) falls back to the workload signal.
            pin = _aurora_capability_lead(data, engine)
            if pin:
                pinned, caps = pin
                # "Pinned" is the utility pin's word alone (#1373): the capability
                # lead says the queries are kept relational, out of every query
                # routed here, pins included.
                lead_clause = f"kept relational by {caps} ({pinned} of all {n} routed)"
        if lead_clause is None:
            lead = r.get("routed_lead")
            if lead:
                lead_clause = f"led by {signal_noun(lead)} ({r.get('routed_lead_count', 0)} of {n})"
        parts = [
            _routed_phrase(r, "mean fit", ("query", "queries"))
            + (f", {lead_clause}" if lead_clause else "")
            + _pinned_note(r, ("query", "queries"))
        ]
    elif r.get("routed_pinned_queries"):
        # Every routed query is pinned (#1373): no score to average.
        pinned = int(r["routed_pinned_queries"])
        verb = "is" if pinned == 1 else "are"
        parts = [f"{_count(pinned, 'query', 'queries')} routed, all {verb} {PINNED_UTILITY_LABEL}"]
    elif "assigned_queries" in r:
        parts = [
            f"No queries routed; {r['confidence_score']}% average fit across "
            f"{r['tables_analyzed']} analyzed tables"
        ]
    else:
        parts = [f"{r['confidence_score']}% average fit across {r['tables_analyzed']} tables"]
        if r["patterns_detected"] > 0:
            parts.append(f"{r['patterns_detected']} matching workload patterns")
    if r.get("target_tables", 0) > 0:
        parts.append(
            f"Schema design: {r['target_tables']} target tables, "
            + _in_scope_phrase(*_access_pattern_scope(data, r))
        )
    # #951: no "estimated $X/month." fragment here: every deliverable states each
    # engine's cost once, in its cost table.
    return ". ".join(parts) + "."


# A short functional label for an engine's role in the rationale (#287 round 2),
# used in place of "primary data storage" once the retained relational core
# already carries most of the workload -- at that point the other engine is a
# carve-out for a specific access pattern, not "the" primary store. Covers the
# engines ``build_architecture_recommendation`` can actually list alongside a
# retained engine; anything else falls back to a neutral phrase rather than a
# guess at what it is for.
_ENGINE_FUNCTION_PHRASE = {
    "dynamodb": "key-value access",
    "documentdb": "document workloads",
    "opensearch": "full-text search",
}


def _engine_function_phrase(engine: str) -> str:
    return _ENGINE_FUNCTION_PHRASE.get(engine, "secondary workload")


def _workload_share(ranking: list[dict], engine: str) -> float | None:
    """``workload_percent`` of ``engine`` in ``ranking``, or ``None`` if absent."""
    entry = next((r for r in ranking if r.get("target") == engine), None)
    value = entry.get("workload_percent") if entry else None
    return float(value) if value is not None else None


def _architecture_rationale(
    arch_type: str,
    databases: list[dict],
    ranking: list[dict],
    retained_engine: str | None = None,
) -> str:
    """Generate architecture-level rationale.

    ``retained_engine`` (#287) names the engine the source database already speaks,
    kept with no data migration of its own -- it never appears in ``databases``
    (``build_architecture_recommendation`` excludes it), so without this it went
    unnamed here even when it is the only, or the only non-cache, engine in the
    architecture ("Insufficient data to recommend a specific architecture." for an
    Aurora-only run that in fact recommends keeping Aurora, or a cache-only
    sentence for an Aurora-plus-cache run that silently dropped the Aurora side).

    #287 round 2: when the retained engine carries the most workload of any
    non-cache engine, the other engine is never called "primary data storage"
    (contradicting "retained as the relational core" in the same sentence) --
    it is named by function instead, and the retained engine's own share is
    quoted, e.g. "Aurora PostgreSQL retained as the relational core (77% of
    queries); dynamodb for key-value access; elasticache for caching hot data".
    """
    retained_share = _workload_share(ranking, retained_engine) if retained_engine else None
    retained_clause = ""
    if retained_engine:
        retained_clause = f"{display_name(retained_engine)} retained as the relational core"
        if retained_share:
            retained_clause += f" ({retained_share:.0f}% of queries)"
    if arch_type == "SINGLE_DATABASE":
        if databases:
            # The ranking entry carries the pin count (#1373); the database entry
            # is read when no ranking entry matches.
            entry = next(
                (r for r in ranking if r.get("target") == databases[0]["service"]), databases[0]
            )
            return (
                f"Workload analysis indicates {display_name(databases[0]['service'])} as the "
                f"primary target with {fit_text(entry, 'workload fit')}."
            )
        if retained_engine:
            return f"{retained_clause}, with no data migration."
        return "Insufficient data to recommend a specific architecture."
    elif arch_type == "HYBRID_WITH_CACHE":
        primary = [d for d in databases if "cache" not in d["service"].lower()]
        cache = [d for d in databases if "cache" in d["service"].lower()]
        primary_share = _workload_share(ranking, primary[0]["service"]) if primary else None
        retained_leads = (
            bool(retained_engine)
            and bool(primary)
            and (primary_share is None or (retained_share or 0) >= primary_share)
        )
        if retained_leads:
            parts = [
                retained_clause,
                f"{display_name(primary[0]['service'])} for "
                f"{_engine_function_phrase(primary[0]['service'])}",
            ]
            if cache:
                parts.append(f"{display_name(cache[0]['service'])} for caching hot data")
            return f"Hybrid architecture: {'; '.join(parts)}."
        parts = []
        if retained_engine:
            parts.append(retained_clause)
        if primary:
            parts.append(f"{display_name(primary[0]['service'])} for primary data storage")
        if cache:
            parts.append(f"{display_name(cache[0]['service'])} for caching hot data")
        return f"Hybrid architecture: {' and '.join(parts)}."
    else:
        services = [d["service"] for d in databases[:3]]
        if retained_engine:
            labeled = [f"{display_name(s)} for {_engine_function_phrase(s)}" for s in services]
            return (
                f"Multi-database architecture: {retained_clause}, with "
                f"{', '.join(labeled)} based on workload pattern analysis."
            )
        return (
            f"Multi-database architecture using "
            f"{', '.join(display_name(s) for s in services)} based on "
            "workload pattern analysis."
        )


# What one schema-design object is called per engine (for the summary, #219).
# Relational designs say "target table": the PDF report and the engineering
# report count the *source* tables mapped to each engine on the same page.
_OBJECT_NOUNS = {
    "elasticache": ("key design", "key designs"),
    "documentdb": ("collection", "collections"),
    "opensearch": ("index", "indexes"),
}


def _object_noun(engine: str) -> tuple[str, str]:
    return _OBJECT_NOUNS.get(engine, ("target table", "target tables"))


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _table_rps_weights(query_groups: list[dict]) -> dict[str, float]:
    """Each source table's own calls/s weight: the sum of ``total_design_rps``
    over every query group whose (comma-joined) name contains it.

    A relational group's own queries all touch every table in its join
    together -- there is no per-table split of calls/s within one group --
    so a table's weight is accumulated across every group it appears in,
    over the *whole* report, not just the handful of names a caller shortens
    at once. Used by ``_short_group_labels`` to pick which table actually
    represents a multi-table group (#478).
    """
    weights: dict[str, float] = {}
    for group in query_groups:
        rps = group.get("total_design_rps") or 0
        for table in str(group.get("group_name") or "").split(", "):
            weights[table] = weights.get(table, 0.0) + rps
    return weights


def _short_group_labels(
    group_names: list[str], table_weights: dict[str, float] | None = None
) -> list[str]:
    """Query group names, safe to join with ", " in prose (#478).

    A relational engine's group name is its (sorted, comma-joined) source
    tables -- a join across eight tables reads like eight separate groups
    once it sits in a comma-joined list of "top groups" (the deterministic
    summary, and the deck slide that reads straight from it). Shortened to
    its busiest table (by ``table_weights``, see ``_table_rps_weights``) plus
    how many more, the same "+N more" shape every other inline table list in
    this module already uses -- not the alphabetically first one, since
    alphabetical order has nothing to do with which table a reader would
    recognize the group by.

    Shortening each name on its own can still make two *different* groups
    look identical: on a real sample, two groups' table sets were a strict
    subset of one another ("a, b, c" and "a, b, c, d"), so their busiest
    tables are the same no matter how they are ranked. Resolved in the order
    given (the caller's own throughput order): the first group locks in its
    shortest form; each later one grows -- one more table at a time, picked
    by the same weight order -- until the exact *set* of tables it shows
    (ignoring the "+N more" count, which a plain string compare does not) no
    longer matches any already-finalized group's shown set, or it runs out
    of tables to add.
    """
    weights = table_weights or {}
    seen_shown: list[set[str]] = []
    labels: list[str] = []
    for name in group_names:
        tables = name.split(", ")
        if len(tables) <= 1:
            labels.append(name)
            seen_shown.append(set(tables))
            continue
        ranked = sorted(tables, key=lambda t: (-weights.get(t, 0.0), t))
        n = 1
        shown = set(ranked[:n])
        while shown in seen_shown and n < len(tables):
            n += 1
            shown = set(ranked[:n])
        if n >= len(tables):
            label = ", ".join(tables)
        else:
            label = f"{' + '.join(ranked[:n])} (+{len(tables) - n} more)"
        labels.append(label)
        seen_shown.append(shown)
    return labels


def _access_pattern_scope(data: SynthesisData, rank: dict) -> tuple[int, int]:
    """``(in_scope, out_of_scope)`` access patterns of an engine's schema design.

    The ranking counts every pattern; without the design itself all of them are
    taken as in scope (nothing is known to be out of scope).
    """
    artifacts = data.engines.get(rank["target"])
    if artifacts is None or artifacts.schema_design is None:
        return int(rank.get("access_patterns", 0)), 0
    patterns = field_list(artifacts.schema_design, "access_patterns")
    n_in = sum(1 for ap in patterns if ap.get("in_scope", True))
    return n_in, len(patterns) - n_in


def _in_scope_phrase(n_in: int, n_out: int) -> str:
    """``47 in-scope access patterns (plus 4 out of scope)`` (#255).

    The Engineering Report heading counts every pattern, so the summary says that
    it counts in-scope ones and how many it leaves out.
    """
    text = _count(n_in, "in-scope access pattern", "in-scope access patterns")
    return text + (f" (plus {n_out} out of scope)" if n_out else "")


# The summary's risk sentence; one copy, which the render-time backfill rewrites too (#1148).
_risk_sentence = risk_sentence


def source_table_count(data: SynthesisData) -> int:
    """The collected schema's own tables-only total (no views, #530 review).

    ``build_summary``'s "Analyzed N source tables ..." opening line was the
    only place this number existed, forcing a renderer that needed it
    (``src.report.architecture._n_tables_mapped``, when no schema design has
    run) to parse it back out of that prose with a regex. Giving it its own
    contract field (``SynthesisOutputContract.source_table_count``) makes it
    a first-class, structured figure instead; this helper is the one place
    that still computes it, so the field and the summary sentence can never
    disagree.
    """
    return len(data.source_tables)


def build_summary(
    data: SynthesisData,
    ranking: list[dict],
    table_mappings: list[dict],
    tco: dict,
    risks: dict,
    query_groups: list[dict],
    eliminated: dict[str, str | None] | None = None,
    reality_check_settled: set[str] | None = None,
    retired_in_use: list[dict] | None = None,
) -> str:
    """Build a comprehensive executive summary.

    With assignment data the summary describes the whole workload split: schema-design
    totals over every engine that carries queries, and "other targets" are only the
    engines that carry none (left empty by the assignment, or eliminated, see
    ``synthesis_grounding.eliminated_engines``). With an assignment the ranking is
    ordered by workload share (#152); without one ``ranking[0]`` is the
    analysis-weight leader.

    ``reality_check_settled`` (``synthesis_grounding.reality_check_settled_engines``,
    #235) names which of ``eliminated``'s engines the reality check itself
    settled, so only those get "by the reality check" in prose -- an engine
    ``eliminated`` reports only because a *later* customer edit emptied it (the
    reality check itself kept it) gets neutral wording instead. ``None`` treats
    every ``eliminated`` engine as reality-check wording.
    """
    if not ranking:
        return "No analysis results available."

    top = ranking[0]
    total_source_tables = source_table_count(data)
    total_queries = len(data.source_queries)

    parts = []

    # Opening
    parts.append(
        f"Analyzed {total_source_tables} source tables and {total_queries} query patterns "
        f"across {len(ranking)} target database(s)."
    )

    # #381 review: the resolver's heterogeneous Aurora engine choice (a source with no
    # Aurora dialect of its own -- SQL Server, Oracle, DB2 -- where Aurora MySQL and
    # Aurora PostgreSQL competed on the collected workload) reached wave 1's rationale
    # only; name it here too, with display names, so it is visible without reading the
    # migration roadmap.
    aurora_choice = (data.assignment or {}).get("aurora_engine_choice")
    source_engine = (aurora_choice or {}).get("source_engine")
    # #381 review round 2, #743: the opening clause comes from
    # heterogeneous_source_clause (shared with the wave rationale), which names the
    # source with display_source_database. source_engine is required to say anything
    # at all; without it the sentence is skipped, same as with no recorded choice.
    if aurora_choice and aurora_choice.get("engine") and source_engine:
        chosen_name = display_name(aurora_choice["engine"])
        if aurora_choice.get("basis") == SCOPE_BASIS:
            # #1327: the customer's engine scope chose; nothing competed.
            parts.append(
                f"{scoped_source_clause(source_engine, str(aurora_choice.get('reason') or ''))} "
                "(a cross-engine, dialect-and-schema conversion, not a 1:1 carry-over)."
            )
        else:
            parts.append(
                f"{heterogeneous_source_clause(source_engine)}, and "
                f"{chosen_name} was selected to serve it (a cross-engine, dialect-and-schema "
                "conversion, not a 1:1 carry-over)."
            )

    has_assignment = any("assigned_queries" in r for r in ranking)
    with_workload = sorted(
        (r for r in ranking if r.get("assigned_queries", 0) > 0),
        key=lambda r: r.get("assigned_queries", 0),
        reverse=True,
    )
    if has_assignment:
        # Workload split view, largest share first
        if with_workload:
            engine_parts = [
                f"{display_name(r['target'])} handles {r['assigned_queries']} queries "
                f"({r.get('workload_percent', 0)}%)"
                for r in with_workload
            ]
            parts.append(f"Workload split: {', '.join(engine_parts)}.")
        # #1329: the queries no engine in the customer's engine scope can serve have no
        # engine, so the split above leaves them out; say how many, and why.
        unservable = len((data.assignment or {}).get("unservable_queries") or [])
        if unservable:
            parts.append(
                f"{unservable} of the {total_queries} queries "
                f"{'has' if unservable == 1 else 'have'} no engine: no engine in the "
                "customer's engine scope can serve "
                f"{'it' if unservable == 1 else 'them'}, so "
                f"{'it is' if unservable == 1 else 'they are'} listed as a blocking risk "
                "and left out of the waves and the schema designs."
            )
        cache = [r for r in ranking if is_cache_layer(r) and r.get("cache_overlay_queries")]
        for r in cache:
            n = r["cache_overlay_queries"]
            parts.append(
                f"Cache layer: {display_name(r['target'])} fronts {n} designed cached "
                f"{'read' if n == 1 else 'reads'} ({r.get('cache_call_share_percent', 0)}% of "
                "calls) cache-aside and owns no queries."
            )
        designed_engines, _ = designed_and_not_designed_engines(ranking)
        designed = [r for r in with_workload + cache if r["target"] in designed_engines]
        if designed:
            engines = {r["target"] for r in designed}
            relevant_groups = [g for g in query_groups if engines & set(g.get("engines") or [])]
            groups = len(relevant_groups)
            # #478: before #157, a relational engine's queries got their own
            # query_groups entries (built from the assignment, since the
            # engine's schema design had no access_patterns of its own) but
            # carried no access pattern, so "N access patterns across M query
            # groups" could read M > N once a relational engine's groups
            # counted toward M but never toward N. Queries and groups are
            # still counted on one footing here -- every distinct in-scope
            # query id these groups carry, for any designed engine -- which
            # also still works correctly once an Aurora engine's groups do
            # carry real access patterns (#157): ``in_scope`` on each one
            # mirrors the assignment scope, so this set is exactly the queries
            # the assignment routed to a designed engine, regardless of
            # whether that engine's own entries are routing records or real
            # designed patterns.
            query_ids: set[str] = set()
            for g in relevant_groups:
                for ap in g.get("access_patterns") or []:
                    if ap.get("engine") in engines and ap.get("in_scope", True) is not False:
                        query_ids.update(ap.get("query_ids") or [])
            scope = {r["target"]: _access_pattern_scope(data, r) for r in designed}
            per_engine = "; ".join(
                f"{display_name(r['target'])}: "
                f"{_count(r.get('target_tables', 0), *_object_noun(r['target']))}"
                + (
                    f", {_count(scope[r['target']][0], 'in-scope access pattern', 'in-scope access patterns')}"
                    if scope[r["target"]][0]
                    else ""
                )
                for r in designed
            )
            total_in = sum(n for n, _ in scope.values())
            total_out = sum(n for _, n in scope.values())
            # "on non-relational engines" only still holds while no Aurora
            # engine actually contributes any access pattern to this total
            # (#157 review, C2): an Aurora engine with a schema design but no
            # access patterns of its own yet (schema_design_available from
            # table_definitions alone, before #157's derivation covered it)
            # keeps the qualifier; one that does contribute drops it rather
            # than being misnamed "non-relational".
            non_relational_only = not any(
                target in AURORA_ENGINES and (n_in or n_out)
                for target, (n_in, n_out) in scope.items()
            )
            ap_clause = (
                f"; {_in_scope_phrase(total_in, total_out)}"
                f"{' on non-relational engines' if non_relational_only else ''}"
                if (total_in or total_out)
                else ""
            )
            parts.append(
                f"Schema design produced "
                f"{sum(r.get('target_tables', 0) for r in designed)} target objects: "
                f"{_count(len(query_ids), 'query', 'queries')} in "
                f"{_count(groups, 'query group', 'query groups')}"
                f"{ap_clause} ({per_engine})."
            )
    else:
        parts.append(
            f"Top recommendation: {display_name(top['target'])} with "
            f"{top['confidence_score']}% average fit."
        )
        # Schema design summary
        if top.get("schema_design_available"):
            parts.append(
                f"Schema design produced {top['target_tables']} target tables with "
                f"{_in_scope_phrase(*_access_pattern_scope(data, top))} across "
                f"{top['pattern_groups']} query groups."
            )

    # Table mapping summary
    if table_mappings:
        engines_used = sorted({display_name(m["recommended_database"]) for m in table_mappings})
        # #1169: say what it counts, the design's table map, not the tables an engine
        # serves or moves (the deliverables add those from the facts layer).
        parts.append(
            f"The schema design's table map lists "
            f"{_count(len(table_mappings), 'source table', 'source tables')} "
            f"({', '.join(engines_used)})."
        )

    # Cost
    if tco["projected_monthly_cost"] > 0:
        parts.append(
            f"Estimated monthly cost: ${tco['projected_monthly_cost']:.2f}"
            + (
                f" ({tco['savings_percent']}% savings vs current)."
                if tco["savings_percent"]
                else "."
            )
        )

    # Risks
    risk_count = len(risks.get("risks", []))
    if risk_count > 0:
        parts.append(
            _risk_sentence(
                risk_count,
                risks["overall_risk_level"],
                len(risks.get("resolved_risks") or []),
            )
        )

    # Query groups
    # #478: neither label is a real access pattern a reader should
    # see ranked as a "busiest" group -- excluded before taking the top 3,
    # not just skipped if picked.
    full_names = [
        g["group_name"]
        for g in query_groups
        if g.get("group_name") not in (UTILITY_GROUP_LABEL, UNRESOLVED_TABLE_GROUP_LABEL)
    ][:3]
    named_groups = _short_group_labels(full_names, _table_rps_weights(query_groups))
    if named_groups:
        parts.append(f"Top query groups by throughput: {', '.join(named_groups)}.")

    # #1076: an engine the customer runs today that the plan retires is stated on its own,
    # with its saving, and is not an "other target" the customer never had.
    retired_ids = {str(r["engine"]) for r in retired_in_use or []}
    if has_assignment and retired_in_use:
        statements = "; ".join(retired_in_use_statement(r) for r in retired_in_use)
        parts.append(f"Retired by this plan: {statements}.")

    # Other engines: evaluated but carrying no workload in the target
    if has_assignment:
        others = [
            f"{display_name(r['target'])} (no queries assigned)"
            for r in ranking
            if r.get("assigned_queries", 0) == 0
            and not is_cache_layer(r)
            and r["target"] not in retired_ids
        ]
        listed = {r["target"] for r in ranking}
        for engine, absorber in (eliminated or {}).items():
            if engine in listed or engine in retired_ids:
                continue
            settled = reality_check_settled is None or engine in reality_check_settled
            engine_name = display_name(engine)
            if absorber:
                absorber_name = display_name(absorber)
                others.append(
                    f"{engine_name} (consolidated into {absorber_name} by the reality check)"
                    if settled
                    else f"{engine_name} (consolidated into {absorber_name})"
                )
            else:
                others.append(
                    f"{engine_name} (eliminated by the reality check)"
                    if settled
                    else f"{engine_name} (removed from the assignment)"
                )
        if others:
            parts.append(f"Other targets evaluated: {', '.join(others)}.")
    elif len(ranking) > 1:
        others_txt = ", ".join(
            f"{display_name(r['target'])} ({r['confidence_score']}%)" for r in ranking[1:]
        )
        parts.append(f"Other targets evaluated: {others_txt}.")

    return " ".join(parts)


EXECUTIVE_SUMMARY_SKILL = "executive-summary.md"

# The sections of the shared skill the Bedrock prompt is built from, in order.
# ``/synthesize`` tells Claude Code to read the same file (#628).
_SUMMARY_PROMPT_SECTIONS = (
    "Task",
    "Guardrails",
    "Grounding",
    "Structure",
    "AWS Managed Integrations",
    "Style",
    "Response",
)


def _executive_summary_prompt_fragments(
    designed: list[str], not_designed: list[str]
) -> dict[str, str]:
    """The ``Schema Design Status`` case of ``src/skills/executive-summary.md``
    that applies, with the engine names filled in (#132 review, 370-1/370-3,
    #628).

    Returns ``premise``, ``rules`` and ``authority``. All designed: the fully
    assertive text. Some designed: only the designed engines are narrated as
    designed, and the rest are named as not designed yet. None designed
    (including both lists empty): the routing-only text, with no table/index
    count or "built" framing at all. The case text lives in the skill, so
    ``/synthesize`` reads exactly the same words.
    """
    case = schema_design_case(designed, not_designed)
    fields = {
        "designed": ", ".join(display_name(e) for e in designed),
        "not_designed": ", ".join(display_name(e) for e in not_designed),
        "it": "it" if len(not_designed) == 1 else "them",
    }
    parts = skill_subsections(EXECUTIVE_SUMMARY_SKILL, "Schema Design Status", case)
    return {
        "premise": parts["Premise"].format(**fields),
        "rules": parts["Rules"].format(**fields),
        "authority": parts["Authority"].format(**fields),
    }


def executive_summary_system_prompt(designed: list[str], not_designed: list[str]) -> str:
    """The Bedrock system prompt: the skill's Role plus the case's Authority."""
    fragments = _executive_summary_prompt_fragments(designed, not_designed)
    role = skill_sections(EXECUTIVE_SUMMARY_SKILL)["Role"]
    return role + "\n\n" + fragments["authority"] + "\n\n" + SYSTEM_PROMPT_DATA_DIRECTIVE


def executive_summary_prompt(
    designed: list[str],
    not_designed: list[str],
    context: dict,
    rejection: dict | None = None,
) -> str:
    """The Bedrock user prompt: the shared skill's sections, then the request.

    ``context`` is the ``prepare_synthesis_llm_input`` payload, the request
    ``/synthesize`` hands Claude Code (#628). It carries customer-derived
    strings (table and query-group names, risk/trade-off descriptions), so it
    is framed as untrusted data (R1).

    ``rejection`` (#214) is ``{"rejected_summary", "warnings"}`` for the one
    rewrite after the post-checks rejected a summary: the skill's ``Rewrite``
    section follows the request, then the rejected text and the warnings,
    framed as untrusted data too (model text quoting customer names).
    """
    fragments = _executive_summary_prompt_fragments(designed, not_designed)
    sections = skill_sections(EXECUTIVE_SUMMARY_SKILL)
    body = "\n\n".join(
        f"{heading.upper()}:\n{sections[heading]}" for heading in _SUMMARY_PROMPT_SECTIONS
    )
    return (
        fragments["premise"]
        + "\n\n"
        + body
        + "\n\nSCHEMA DESIGN STATUS:\n"
        + fragments["rules"]
        + "\n\n"
        # Compact JSON: the payload grows with the workload (#628 review).
        + frame_untrusted(
            json.dumps(context, separators=(",", ":"), sort_keys=True),
            label="assessment context",
        )
        + _rewrite_request(rejection)
        + "\n\nWrite the briefing now."
    )


def _rewrite_request(rejection: dict | None) -> str:
    """The prompt part asking for the one rewrite (#214); empty on the first call."""
    if not rejection:
        return ""
    rejected = {
        "rejected_summary": str(rejection.get("rejected_summary") or ""),
        "warnings": [str(w) for w in rejection.get("warnings") or []],
    }
    return (
        "\n\nREWRITE:\n"
        + skill_sections(EXECUTIVE_SUMMARY_SKILL)["Rewrite"]
        + "\n\n"
        + frame_untrusted(
            json.dumps(rejected, separators=(",", ":"), sort_keys=True),
            label="rejected summary and the post-check warnings",
        )
    )


def generate_executive_summary(
    llm_input: dict, rejection: dict | None = None
) -> ExecutiveSummaryResponse | None:
    """Generate the executive summary on Bedrock from the headless request payload.

    ``llm_input`` is :func:`~src.agents.referee.synthesis_handler.prepare_synthesis_llm_input`'s
    payload, the same request ``/synthesize`` hands Claude Code (payload parity,
    #628): the model sees the same data in both modes, and the instruction comes
    from the same skill (``src/skills/executive-summary.md``), with the
    ``Schema Design Status`` case the payload's ``schema_design_status.case``
    names. The answer is asked for as structured output against
    :class:`~src.contracts.executive_summary_response.ExecutiveSummaryResponse`,
    the contract the headless response is validated against too; a plain-text
    answer is read as the summary text.

    Returns the validated response, or ``None`` when no usable answer came back
    (Strands missing, a call error, or an answer off the contract): the caller
    keeps the deterministic summary. The caller post-checks the summary
    (``apply_synthesis_llm_output``) exactly as it does a headless one.

    ``rejection`` asks for the one rewrite of a rejected summary (#214); see
    :func:`executive_summary_prompt`.
    """
    try:
        from strands import Agent
    except ImportError:
        logger.warning("Strands not available — using deterministic summary")
        return None

    status = llm_input.get("schema_design_status") or {}
    designed = list(status.get("designed") or [])
    not_designed = list(status.get("not_designed") or [])
    prompt = executive_summary_prompt(designed, not_designed, llm_input, rejection)
    system_prompt = executive_summary_system_prompt(designed, not_designed)

    try:
        model = build_bedrock_model(Role.SUMMARY)
        agent = Agent(
            model=model,
            system_prompt=system_prompt,
            tools=[],
            structured_output_model=ExecutiveSummaryResponse,
            callback_handler=None,
        )

        say("[synthesis] Generating executive summary with LLM...")
        result = agent(prompt)
        structured = getattr(result, "structured_output", None)
        raw: object = (
            structured
            if isinstance(structured, ExecutiveSummaryResponse)
            else {"executive_summary": str(result).strip()}
        )
        response = parse_executive_summary_response(raw)
        say("[synthesis] Executive summary generated " f"({len(response.executive_summary)} chars)")
        return response

    except GuardrailConfigError:
        raise
    except GuardrailIntervened as exc:
        # Not retried: the deterministic summary stands.
        logger.error("%s", exc)
        say(f"[synthesis] ERROR: {exc} Falling back to the deterministic summary.")
    except ValidationError as exc:
        logger.error(
            "LLM executive summary is off the contract (%s) — using deterministic",
            validation_error_note(exc),
        )
        say("[synthesis] ERROR: LLM summary did not match the response contract")
    except Exception as exc:
        model_id = resolve_model_id(Role.SUMMARY)
        # The underlying error from an LLM call can carry prompt/response
        # fragments (#143): full text at DEBUG only.
        logger.debug(
            "LLM executive summary failed (model=%s): %s — using deterministic", model_id, exc
        )
        logger.error(
            "LLM executive summary failed (model=%s): %s — using deterministic",
            model_id,
            type(exc).__name__,
        )
        say(
            f"[synthesis] ERROR: LLM summary failed with model '{model_id}' "
            f"({type(exc).__name__})"
        )
        say(
            "[synthesis] Set SUMMARY_MODEL_ID env var to override. Falling back to deterministic summary."
        )

    return None
