"""
Referee-Triage — Workload pattern detection and analysis agent selection.

Reads collector output, detects workload signals from schema and query
patterns, and selects which analysis agents should run. Pure Python,
no LLM — deterministic pattern matching.

Signals use the same pattern vocabulary as the analysis agent catalogs
so that analysis agents can cross-reference triage signals in their
decision traces.

Query classification groups:
  Group 1: Key-value lookups (PK reads ≤5 rows, MAX/MIN) → DynamoDB, ElastiCache
  Group 2: Range queries (PK + sort key range, ORDER BY) → DynamoDB
  Group 3: Status-based filters (WHERE status=?, EXISTS, anti-joins) → Aurora
  Group 4: Write operations (INSERT/UPDATE/DELETE ≥5 cps) → DynamoDB, Keyspaces
  Group 5: Complex joins (3+ table JOINs) → DocumentDB, Aurora
  Group 6: Aggregations (SUM/COUNT/AVG + GROUP BY) → DocumentDB, OpenSearch
  Group 7: Large result sets (COUNT(*) on date ranges, bulk scans) → Aurora, OpenSearch
  Group 8: Time-series (timestamp in WHERE/ORDER BY, INSERT-dominant) → DynamoDB, Keyspaces, OpenSearch
  Group 9: Session store (session/token keywords, user-scoped) → DynamoDB, ElastiCache
  Group 10: Metadata/config (small table, high reads) → DynamoDB

Schema signals:
  JSON/JSONB columns → DocumentDB
  Junction tables (many-to-many) → Neptune, DynamoDB
  High FK density → Neptune
  EAV pattern → DynamoDB, DocumentDB
  Self-referential FK → Neptune, DynamoDB

Cross-cutting signals:
  Text search (LIKE/ILIKE, tsvector, MATCH AGAINST, REGEXP, pg_trgm) → OpenSearch
  Time range filters → DynamoDB, Keyspaces, OpenSearch
  Leaderboard (ORDER BY + LIMIT) → ElastiCache
  Subqueries → Aurora
  High-frequency reads (≥10 cps) → DynamoDB, ElastiCache
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Agents that run as ECS analysis tasks
ANALYSIS_AGENTS = frozenset(
    {
        "dynamodb",
        "documentdb",
        "opensearch",
        "elasticache",
        "aurora_postgresql",
        "aurora_mysql",
    }
)

# Aurora is the relational baseline — always selected based on source engine.
# The specific agent (aurora_postgresql or aurora_mysql) competes alongside
# NoSQL agents to score queries for relational fitness.
BASELINE = "aurora"

# Map source engine to the matching Aurora analysis agent
SOURCE_ENGINE_TO_AURORA: dict[str, str] = {
    "mysql": "aurora_mysql",
    "mariadb": "aurora_mysql",
    "postgresql": "aurora_postgresql",
    "postgres": "aurora_postgresql",
}

# Agents deferred to Phase 1 — signals tracked for Synthesis reporting
# but not dispatched as ECS tasks.
DEFERRED = frozenset({"keyspaces", "neptune"})

ALL_AGENTS = ANALYSIS_AGENTS | {BASELINE} | DEFERRED


@dataclass
class TriageSignal:
    """A detected workload signal with evidence and traceability."""

    signal: str
    targets: list[str]
    evidence: str
    query_ids: list[str] = field(default_factory=list)
    table_ids: list[str] = field(default_factory=list)
    query_count: int = 0


@dataclass
class TriageResult:
    """Output of triage analysis."""

    selected: dict[str, list[str]] = field(default_factory=dict)  # analysis agents to dispatch
    skipped: dict[str, str] = field(default_factory=dict)  # analysis agents not dispatched
    baseline: dict[str, list[str]] = field(default_factory=dict)  # aurora signals for synthesis
    deferred: dict[str, list[str]] = field(default_factory=dict)  # phase 1 agents (not dispatched)
    signals: list[TriageSignal] = field(default_factory=list)
    query_capabilities: dict[str, list[str]] = field(default_factory=dict)  # query_id -> [caps]


def triage(collector_output: dict) -> TriageResult:
    """Analyze collector output and decide which analysis agents to run."""
    signals = _detect_schema_signals(collector_output)
    signals.extend(_detect_query_signals(collector_output))

    # Determine which Aurora agent matches the source engine
    source_engine = (
        collector_output.get("metadata", {}).get("source_database", {}).get("engine", "")
    ).lower()
    aurora_agent = SOURCE_ENGINE_TO_AURORA.get(source_engine)

    result = _select_agents(signals, aurora_agent=aurora_agent)

    # Detect hard capability requirements per query
    result.query_capabilities = _detect_query_capabilities(collector_output, signals)

    return result


# ---------------------------------------------------------------------------
# Schema-level signal detection
# ---------------------------------------------------------------------------


def _detect_schema_signals(co: dict) -> list[TriageSignal]:
    signals: list[TriageSignal] = []
    tables = co.get("database_schema", {}).get("tables", [])
    if not tables:
        return signals

    # JSON/JSONB columns → DocumentDB
    json_tables: list[str] = []
    for t in tables:
        for c in t.get("columns") or []:
            if (c.get("data_type") or "").lower() in ("json", "jsonb"):
                json_tables.append(t.get("table_id") or t["table_name"])
                break
    if json_tables:
        signals.append(
            TriageSignal(
                signal="json_columns",
                targets=["documentdb"],
                evidence=f"JSON columns in: {', '.join(json_tables[:5])}",
                table_ids=json_tables,
            )
        )

    # Junction tables (2+ FKs, few own columns) → Neptune, DynamoDB
    junction: list[str] = []
    for t in tables:
        if len(t.get("foreign_keys") or []) >= 2 and len(t.get("columns") or []) <= 5:
            junction.append(t.get("table_id") or t["table_name"])
    if junction:
        signals.append(
            TriageSignal(
                signal="junction_tables",
                targets=["neptune", "dynamodb"],
                evidence=f"{len(junction)} junction tables: {', '.join(junction[:5])}",
                table_ids=junction,
            )
        )

    # High FK density → Neptune
    total_fks = sum(len(t.get("foreign_keys") or []) for t in tables)
    avg_fks = total_fks / len(tables)
    if avg_fks >= 2:
        signals.append(
            TriageSignal(
                signal="high_fk_density",
                targets=["neptune"],
                evidence=f"{total_fks} FKs across {len(tables)} tables (avg {avg_fks:.1f}/table)",
            )
        )

    # Self-referential FK → Neptune, DynamoDB (adjacency-list pattern)
    self_ref: list[str] = []
    for t in tables:
        tid = t.get("table_id") or t.get("table_name", "")
        tname = t.get("table_name", "")
        for fk in t.get("foreign_keys") or []:
            ref = fk.get("referenced_table", "")
            if ref in (tid, tname):
                self_ref.append(tid)
                break
    if self_ref:
        signals.append(
            TriageSignal(
                signal="self_referential_fk",
                targets=["neptune", "dynamodb"],
                evidence=f"Self-referential FK in: {', '.join(self_ref[:5])}",
                table_ids=self_ref,
            )
        )

    # EAV pattern → DynamoDB, DocumentDB
    eav_tables = [t.get("table_id") or t["table_name"] for t in tables if _is_eav_table(t)]
    if eav_tables:
        signals.append(
            TriageSignal(
                signal="eav_pattern",
                targets=["dynamodb", "documentdb"],
                evidence=f"EAV tables: {', '.join(eav_tables[:5])}",
                table_ids=eav_tables,
            )
        )

    return signals


def _is_eav_table(t: dict) -> bool:
    """Detect Entity-Attribute-Value pattern from table structure.

    NOTE: Name-based heuristic — may false-positive on tables like
    `user_metadata` that aren't true EAV. The compound check (name +
    entity column + key/value columns) reduces noise but isn't perfect.
    Revisit if calibration traces show false positives.
    """
    name = (t.get("table_name") or "").lower()
    col_names = {(c.get("column_name") or "").lower() for c in (t.get("columns") or [])}
    if any(kw in name for kw in ("metadata", "attribute", "property", "tag", "setting")):
        has_entity = any(c for c in col_names if "entity" in c or "object" in c or "parent" in c)
        has_key = any(
            c
            for c in col_names
            if c in ("key", "name", "attribute_name", "property_name", "tag_name")
        )
        has_value = any(
            c for c in col_names if c in ("value", "attribute_value", "property_value", "data")
        )
        return has_entity and (has_key or has_value)
    return False


# ---------------------------------------------------------------------------
# Query-level signal detection (SQL text analysis)
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(
    r"\b(created_at|updated_at|timestamp|event_time|open_date|close_date|"
    r"created_date|modified_date|log_time|event_date)\b",
    re.IGNORECASE,
)
_SESSION_RE = re.compile(r"\b(session|token|sess_id|session_id|csrf)\b", re.IGNORECASE)
_AGGREGATION_RE = re.compile(r"\b(sum|count|avg)\s*\(", re.IGNORECASE)


def _detect_query_signals(co: dict) -> list[TriageSignal]:
    signals: list[TriageSignal] = []
    queries = co.get("queries", {}).get("query_patterns", [])
    if not queries:
        return signals

    # Accumulators: list of query_ids per signal
    kv_lookups: list[str] = []
    range_queries: list[str] = []
    status_filters: list[str] = []
    write_heavy: list[str] = []
    low_freq_writes: list[str] = []
    low_freq_reads: list[str] = []
    complex_joins: list[str] = []
    aggregations: list[str] = []
    large_scans: list[str] = []
    timeseries: list[str] = []
    session_store: list[str] = []
    metadata_config: list[str] = []
    text_searches: list[str] = []
    time_ranges: list[str] = []
    leaderboards: list[str] = []
    subqueries: list[str] = []
    high_freq_reads: list[str] = []

    for q in queries:
        text = (q.get("query_text") or "").lower()
        qtype = (q.get("query_type") or "").upper()
        cps = q.get("calls_per_second") or 0
        rows_avg = q.get("rows_returned_avg") or 0
        join_count = q.get("join_count") or 0
        qid = q.get("query_id", "")

        # Group 1: Key-value lookups
        if qtype == "SELECT" and not q.get("has_joins") and rows_avg <= 5:
            kv_lookups.append(qid)
        if re.search(r"\b(max|min)\s*\(", text) and not q.get("has_joins"):
            kv_lookups.append(qid)

        # Group 2: Range queries (PK + sort key range)
        if qtype == "SELECT" and re.search(r"\b(between|>=?|<=?)\b", text) and "order by" in text:
            range_queries.append(qid)

        # Group 3: Status-based filters
        if re.search(r"\bstatus\s*=\s*[\?'\"]", text):
            status_filters.append(qid)
        if re.search(r"\b(exists|not\s+exists)\s*\(", text):
            status_filters.append(qid)
        if re.search(r"\bis\s+null\b", text) and q.get("has_joins"):
            status_filters.append(qid)

        # Group 4: Write operations
        if qtype in ("INSERT", "UPDATE", "DELETE") and cps >= 5:
            write_heavy.append(qid)

        # Group 4b: Low-frequency writes (still need migration support)
        if qtype in ("INSERT", "UPDATE", "DELETE") and cps < 5:
            low_freq_writes.append(qid)

        # Group 4c: Low-frequency reads (still need migration support)
        if qtype == "SELECT" and cps < 0.1:
            low_freq_reads.append(qid)

        # Group 5: Complex joins (3+ tables)
        if join_count >= 3 or (q.get("has_joins") and text.count(" join ") >= 3):
            complex_joins.append(qid)

        # Group 6: Aggregations
        if _AGGREGATION_RE.search(text) and re.search(r"\bgroup\s+by\b", text):
            aggregations.append(qid)
        elif re.search(r"\b(sum|count|avg|min|max)\s*\(", text) and q.get("has_joins"):
            aggregations.append(qid)

        # Group 7: Large result sets / bulk scans
        if re.search(r"\bcount\s*\(\s*[\*1]\s*\)", text) and q.get("has_time_range_filter"):
            large_scans.append(qid)
        if (q.get("full_table_scans") or 0) > 0 and rows_avg > 1000:
            large_scans.append(qid)

        # Group 8: Time-series / event log
        if _TIMESTAMP_RE.search(text) and (
            qtype == "INSERT" or (qtype == "SELECT" and "order by" in text)
        ):
            timeseries.append(qid)

        # Group 9: Session store
        if _SESSION_RE.search(text):
            session_store.append(qid)

        # Group 10: Metadata/config store (small result, high reads)
        if qtype == "SELECT" and rows_avg <= 10 and cps >= 1:
            metadata_config.append(qid)

        # Cross-cutting: Text search → OpenSearch
        # Check both the collector's has_text_search flag AND SQL text patterns
        if q.get("has_text_search"):
            text_searches.append(qid)
        elif re.search(r"\b(like|ilike)\s+['\"\?%]", text):
            text_searches.append(qid)
        elif re.search(r"\b(tsvector|tsquery|to_tsvector|plainto_tsquery|match\s+against)\b", text):
            text_searches.append(qid)
        elif re.search(r"\b(regexp|rlike)\b", text):
            text_searches.append(qid)
        elif re.search(r"\b(similarity\s*\(|pg_trgm)\b", text):
            text_searches.append(qid)

        # Cross-cutting: Time range filters
        if q.get("has_time_range_filter"):
            time_ranges.append(qid)

        # Cross-cutting: Leaderboard (ORDER BY + LIMIT)
        if re.search(r"order\s+by\b.*\blimit\b", text):
            leaderboards.append(qid)

        # Cross-cutting: Subqueries
        if q.get("has_subqueries") or text.count("select") > 1:
            subqueries.append(qid)

        # Cross-cutting: High-frequency reads
        if qtype == "SELECT" and cps >= 10:
            high_freq_reads.append(qid)

    # Build signals from non-empty groups (deduplicated query_ids)
    _signal = _build_signal

    if kv_lookups:
        signals.append(
            _signal(
                "key_value_lookups",
                ["dynamodb", "elasticache", "documentdb"],
                "key-value lookup queries (PK reads, ≤5 rows, MAX/MIN)",
                kv_lookups,
            )
        )
    if range_queries:
        signals.append(
            _signal(
                "range_queries",
                ["dynamodb", "documentdb"],
                "range queries (PK + sort key range with ORDER BY)",
                range_queries,
            )
        )
    if status_filters:
        signals.append(
            _signal(
                "status_filters",
                ["aurora", "documentdb"],
                "status-based filter queries (WHERE status=?, EXISTS, IS NULL)",
                status_filters,
            )
        )
    if write_heavy:
        signals.append(
            _signal(
                "write_heavy",
                ["dynamodb", "keyspaces", "documentdb"],
                "high-frequency write queries (≥5 cps)",
                write_heavy,
            )
        )
    if low_freq_writes:
        signals.append(
            _signal(
                "low_frequency_writes",
                ["dynamodb", "documentdb"],
                "low-frequency write queries (<5 cps) — infrequent but may be business-critical",
                low_freq_writes,
            )
        )
    if low_freq_reads:
        signals.append(
            _signal(
                "low_frequency_reads",
                ["dynamodb", "documentdb"],
                "low-frequency read queries (<0.1 cps) — admin, reporting, or health checks",
                low_freq_reads,
            )
        )
    if complex_joins:
        signals.append(
            _signal(
                "complex_joins",
                ["documentdb", "aurora"],
                "complex join queries (3+ tables)",
                complex_joins,
            )
        )
    if aggregations:
        signals.append(
            _signal(
                "aggregations",
                ["documentdb", "opensearch"],
                "aggregation queries (SUM/COUNT/AVG with GROUP BY or JOINs)",
                aggregations,
            )
        )
    if large_scans:
        signals.append(
            _signal(
                "large_scans",
                ["aurora", "opensearch"],
                "large scan / bulk count queries",
                large_scans,
            )
        )
    if timeseries:
        signals.append(
            _signal(
                "time_series",
                ["dynamodb", "keyspaces", "documentdb", "opensearch"],
                "time-series / event log queries (timestamp in WHERE/ORDER BY)",
                timeseries,
            )
        )
    if session_store:
        signals.append(
            _signal(
                "session_store",
                ["dynamodb", "elasticache", "documentdb"],
                "session store queries (session/token keywords)",
                session_store,
            )
        )
    if metadata_config:
        signals.append(
            _signal(
                "metadata_config",
                ["dynamodb", "documentdb"],
                "metadata/config store queries (small result, high reads)",
                metadata_config,
            )
        )
    if text_searches:
        signals.append(
            _signal(
                "text_search",
                ["opensearch"],
                "text search queries (LIKE, MATCH, tsvector)",
                text_searches,
            )
        )
    if time_ranges:
        signals.append(
            _signal(
                "time_range_filters",
                ["dynamodb", "keyspaces", "opensearch", "documentdb"],
                "time-range filter queries",
                time_ranges,
            )
        )
    if leaderboards:
        signals.append(
            _signal(
                "leaderboard_pattern",
                ["elasticache"],
                "leaderboard queries (ORDER BY + LIMIT)",
                leaderboards,
            )
        )
    if subqueries:
        signals.append(
            _signal(
                "subqueries",
                ["aurora"],
                "queries with subqueries (correlated or EXISTS)",
                subqueries,
            )
        )
    if high_freq_reads:
        signals.append(
            _signal(
                "high_frequency_reads",
                ["dynamodb", "elasticache", "documentdb"],
                "high-frequency read queries (≥10 cps)",
                high_freq_reads,
            )
        )

    return signals


def _build_signal(
    signal: str, targets: list[str], description: str, query_ids: list[str]
) -> TriageSignal:
    deduped = list(dict.fromkeys(query_ids))
    return TriageSignal(
        signal=signal,
        targets=targets,
        evidence=f"{len(deduped)} {description}",
        query_ids=deduped,
        query_count=len(deduped),
    )


# ---------------------------------------------------------------------------
# Hard capability detection
# ---------------------------------------------------------------------------


def _detect_query_capabilities(co: dict, signals: list[TriageSignal]) -> dict[str, list[str]]:
    """Detect hard capability requirements per query.

    Combines regex detection on SQL text with signal-based derivation.
    Only returns entries for queries that actually require capabilities.
    """
    from src.agents.referee.capability_registry import detect_required_capabilities

    queries = co.get("queries", {}).get("query_patterns", [])
    if not queries:
        return {}

    # Build signal map: query_id → list of signal names
    query_signal_map: dict[str, list[str]] = defaultdict(list)
    for sig in signals:
        for qid in sig.query_ids:
            query_signal_map[qid].append(sig.signal)

    result: dict[str, list[str]] = {}
    for q in queries:
        qid = q.get("query_id", "")
        query_text = q.get("query_text", "")
        sigs = query_signal_map.get(qid, [])
        caps = detect_required_capabilities(query_text, sigs)
        if caps:
            result[qid] = caps

    return result


# ---------------------------------------------------------------------------
# Agent selection
# ---------------------------------------------------------------------------


def _select_agents(signals: list[TriageSignal], aurora_agent: str | None = None) -> TriageResult:
    """Select analysis agents based on detected signals.

    Aurora signals are tracked separately in `baseline` for Synthesis —
    orchestration only dispatches agents in `selected`.
    Deferred agents (Phase 1) are tracked in `deferred` for reporting.
    """
    result = TriageResult(signals=signals)

    # Aurora baseline — always present, signals accumulated for Synthesis
    result.baseline[BASELINE] = ["Relational baseline — tables not claimed by purpose-built agents"]

    for sig in signals:
        for target in sig.targets:
            if target == BASELINE:
                result.baseline[BASELINE].append(f"{sig.signal}: {sig.evidence}")
            elif target in DEFERRED:
                if target not in result.deferred:
                    result.deferred[target] = []
                result.deferred[target].append(f"{sig.signal}: {sig.evidence}")
            else:
                if target not in result.selected:
                    result.selected[target] = []
                result.selected[target].append(f"{sig.signal}: {sig.evidence}")

    # Always select the matching Aurora agent (source engine is inherently relational)
    if aurora_agent and aurora_agent not in result.selected:
        result.selected[aurora_agent] = [
            "Source engine is relational — Aurora analysis provides baseline scoring"
        ]

    # The non-matching Aurora agent should be skipped (not both)
    non_matching_aurora = {"aurora_postgresql", "aurora_mysql"} - {aurora_agent or ""}
    for agent in ANALYSIS_AGENTS:
        if agent not in result.selected:
            if agent in non_matching_aurora:
                result.skipped[agent] = "Source engine does not match this Aurora variant"
            else:
                result.skipped[agent] = "No matching workload signals detected"

    return result
