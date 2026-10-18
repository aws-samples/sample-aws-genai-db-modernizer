"""
Referee-Triage — Workload pattern detection and analysis agent selection.

Reads collector output, detects workload signals from schema and query
patterns, and selects the analysis agents to run. Pure Python, no LLM —
deterministic pattern matching.

Every query-owning engine is selected (ADR-031 Decision 2 step 1, #726),
minus the future placeholders and the Aurora flavour the source rules out.
The signals below are evidence for scoring and reports, not a gate on which
owning engines are analysed. ElastiCache, a cache overlay that owns no
query, is still selected only when one of its signals fires.

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
  Leaderboard (hot SELECT with ORDER BY + LIMIT) → ElastiCache (cache-overlay hint only, #296)
  Subqueries → Aurora
  High-frequency reads (≥10 cps) → DynamoDB, ElastiCache
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from src.agents.referee.cache_overlay import CACHE_OVERLAY_ENGINES, HOT_READ_MIN_CALLS_PER_SECOND
from src.agents.referee.capability_registry import (
    detect_required_capabilities,
    requires_aggregation_capability,
)
from src.agents.referee.table_resolution import SourceScope
from src.shared import catalog
from src.tools.analysis.key_access import KeyAccessReader
from src.tools.sql.query_facts import ColumnTypes, query_facts, source_engine_of

logger = logging.getLogger(__name__)

# Agents that run as ECS analysis tasks: every engine routing can target (the catalog's
# active engines, ADR-031 Decision 8).
ANALYSIS_AGENTS = catalog.engine_ids(status=catalog.ACTIVE)

# Aurora is the relational baseline — always selected based on source engine.
# The specific agent (aurora_postgresql or aurora_mysql) competes alongside
# NoSQL agents to score queries for relational fitness. The id is the catalog's one
# legacy relational engine (the generic id older reports carry).
(BASELINE,) = catalog.engine_ids(status="legacy", role="relational")

# Map source engine to the matching Aurora analysis agent: each Aurora engine file's
# ``source_engines`` (#921).
SOURCE_ENGINE_TO_AURORA: dict[str, str] = catalog.source_engine_dialects()

# Relational source engines with no Aurora dialect of their own (#381). Neither
# Aurora engine is a native match, so instead of skipping both (the pre-#381
# behaviour), both compete on the collected workload and the assignment resolver
# picks exactly one (`src.agents.referee.aurora_choice.choose_heterogeneous_engine`).
# `scripts/collect-oracle.sql` and `scripts/collect-sqlserver.sql` exist; there is no
# DB2 collector yet, so a DB2 collection cannot reach this path in practice today --
# the entry is kept for when one exists, per the agreed design.
HETEROGENEOUS_SOURCE_ENGINES = frozenset({"sqlserver", "oracle", "db2"})

# Agents deferred to Phase 1 — signals tracked for Synthesis reporting
# but not dispatched as ECS tasks: the catalog's future placeholders (Decision 9).
DEFERRED = catalog.engine_ids(status="future")

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

    from src.agents.referee.aurora_choice import AURORA_ENGINES  # import cycle

    # Determine which Aurora agent(s) compete for this source engine: its own
    # dialect when it has one; both Aurora agents when it is a known relational
    # engine with no Aurora dialect of its own (#381) or when no source engine is
    # recorded (#726: the resolver picks one, as for a heterogeneous source);
    # neither for any other (unrecognised or non-relational) source engine.
    source_engine = (
        (collector_output.get("metadata") or {}).get("source_database", {}).get("engine") or ""
    ).lower()
    matched_aurora = SOURCE_ENGINE_TO_AURORA.get(source_engine)
    if matched_aurora:
        aurora_agents = [matched_aurora]
    elif source_engine in HETEROGENEOUS_SOURCE_ENGINES or not source_engine:
        aurora_agents = sorted(ANALYSIS_AGENTS & AURORA_ENGINES)
    else:
        aurora_agents = []

    result = _select_agents(
        signals, aurora_agents=aurora_agents, no_source_engine=not source_engine
    )

    # Detect hard capability requirements per query
    result.query_capabilities = detect_query_capabilities(collector_output, signals)

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
                targets=catalog.signal_targets("json_columns"),
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
                targets=catalog.signal_targets("junction_tables"),
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
                targets=catalog.signal_targets("high_fk_density"),
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
                targets=catalog.signal_targets("self_referential_fk"),
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
                targets=catalog.signal_targets("eav_pattern"),
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

# The text detectors below are engine-neutral query facts
# (``src.tools.sql.query_facts``, #491), read once per query in the source
# engine's dialect. The joins and subqueries are the collector's own facade
# facts (``join_count``, ``has_joins``, ``has_subqueries``, #796, #893).


def _detect_query_signals(co: dict) -> list[TriageSignal]:
    signals: list[TriageSignal] = []
    queries = co.get("queries", {}).get("query_patterns", [])
    if not queries:
        return signals
    source_engine = source_engine_of(co)
    key_reader = KeyAccessReader.from_collector(co, source_engine)
    column_types = ColumnTypes.from_collector(co)

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
    # Time-series queries whose timestamp column was read by name although the
    # collection has column types (its table could not be told, #935).
    timeseries_by_name: list[str] = []
    session_store: list[str] = []
    metadata_config: list[str] = []
    text_searches: list[str] = []
    time_ranges: list[str] = []
    leaderboards: list[str] = []
    subqueries: list[str] = []
    high_freq_reads: list[str] = []

    for q in queries:
        text = q.get("query_text") or ""
        facts = query_facts(text, source_engine)
        qtype = (q.get("query_type") or "").upper()
        cps = q.get("calls_per_second") or 0
        rows_avg = q.get("rows_returned_avg") or 0
        join_count = q.get("join_count") or 0
        qid = q.get("query_id", "")

        # Group 1: Key-value lookups
        if qtype == "SELECT" and not q.get("has_joins") and rows_avg <= 5:
            kv_lookups.append(qid)
        if facts.min_max_call and not q.get("has_joins"):
            kv_lookups.append(qid)

        # Group 2: Range queries: a partition-key equality plus a range on another column
        # of the same table (the key-access fact, #933's shape, the one range definition)
        if qtype == "SELECT" and key_reader.access(q).partition_range:
            range_queries.append(qid)

        # Group 3: Status-based filters
        if facts.status_equals_value:
            status_filters.append(qid)
        if facts.exists_or_in_subquery:
            status_filters.append(qid)
        if facts.is_null and q.get("has_joins"):
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

        # Group 5: Complex joins (3+ tables, i.e. 2+ JOIN clauses). join_count
        # >= 3 used to require 4+ tables -- a 3-table join (join_count == 2)
        # scored no signal at all and was free to move to DynamoDB (#338).
        if join_count >= 2:
            complex_joins.append(qid)

        # Group 6: Aggregations -- an aggregate function call needs real
        # aggregation capability on one table just as much as a joined one
        # (COUNT(*)/FOUND_ROWS() on a single table used to score no signal at
        # all). A GROUP BY with no aggregate function and no HAVING is a
        # dedup technique (#336), not an aggregation, so it is excluded here
        # the same way the capability gate excludes it.
        if requires_aggregation_capability(text, source_engine):
            aggregations.append(qid)

        # Group 7: Large result sets / bulk scans
        if facts.count_star_or_one and q.get("has_time_range_filter"):
            large_scans.append(qid)
        if (q.get("full_table_scans") or 0) > 0 and rows_avg > 1000:
            large_scans.append(qid)

        # Group 8: Time-series / event log
        timestamp = facts.timestamp_column(column_types)
        if timestamp and (qtype == "INSERT" or (qtype == "SELECT" and facts.order_by)):
            timeseries.append(qid)
            if timestamp.read_by_name and not timestamp.no_types:
                timeseries_by_name.append(qid)

        # Group 9: Session store. Matched on the original text, not the
        # lower-cased one, so a PascalCase [SessionToken] still splits (#663).
        if facts.session_key:
            session_store.append(qid)

        # Group 10: Metadata/config store (small result, high reads)
        if qtype == "SELECT" and rows_avg <= 10 and cps >= 1:
            metadata_config.append(qid)

        # Cross-cutting: Text search → OpenSearch. The collector's has_text_search
        # flag, or the text_search fact the capability gate reads too (#893).
        if q.get("has_text_search") or facts.text_search:
            text_searches.append(qid)

        # Cross-cutting: Time range filters
        if q.get("has_time_range_filter"):
            time_ranges.append(qid)

        # Cross-cutting: Leaderboard / top-N (ORDER BY + LIMIT, or T-SQL's
        # TOP (n) ... ORDER BY / FETCH FIRST|NEXT, #663; one pattern shared with
        # the cache overlay, the ``top_n`` query fact). Only a hot SELECT
        # counts: a Rails ``.first`` / ``find_by`` at 0.01 calls/s has the same
        # shape and is not cache material (#296). The signal is a cache-overlay
        # hint, never an ownership override.
        if qtype == "SELECT" and cps >= HOT_READ_MIN_CALLS_PER_SECOND and facts.top_n:
            leaderboards.append(qid)

        # Cross-cutting: Subqueries (the collector's facade fact)
        if q.get("has_subqueries"):
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
                "key-value lookup queries (PK reads, ≤5 rows, MAX/MIN)",
                kv_lookups,
            )
        )
    if range_queries:
        signals.append(
            _signal(
                "range_queries",
                "range queries (PK + sort key range with ORDER BY)",
                range_queries,
            )
        )
    if status_filters:
        signals.append(
            _signal(
                "status_filters",
                "status-based filter queries (WHERE status=?, EXISTS, IS NULL)",
                status_filters,
            )
        )
    if write_heavy:
        signals.append(
            _signal(
                "write_heavy",
                "high-frequency write queries (≥5 cps)",
                write_heavy,
            )
        )
    if low_freq_writes:
        signals.append(
            _signal(
                "low_frequency_writes",
                "low-frequency write queries (<5 cps): infrequent, and possibly business-critical",
                low_freq_writes,
            )
        )
    if low_freq_reads:
        signals.append(
            _signal(
                "low_frequency_reads",
                "low-frequency read queries (<0.1 cps): admin, reporting, or health checks",
                low_freq_reads,
            )
        )
    if complex_joins:
        signals.append(
            _signal(
                "complex_joins",
                "complex join queries (3+ tables)",
                complex_joins,
            )
        )
    if aggregations:
        signals.append(
            _signal(
                "aggregations",
                "aggregation queries (SUM/COUNT/AVG with GROUP BY or JOINs)",
                aggregations,
            )
        )
    if large_scans:
        signals.append(
            _signal(
                "large_scans",
                "large scan / bulk count queries",
                large_scans,
            )
        )
    if timeseries:
        time_series = _signal(
            "time_series",
            "time-series / event log queries (timestamp in WHERE/ORDER BY)",
            timeseries,
        )
        by_name = len(dict.fromkeys(timeseries_by_name))
        if by_name:
            time_series.evidence += (
                f"; {by_name} by column name (the column's table could not be told)"
            )
        signals.append(time_series)
    if session_store:
        signals.append(
            _signal(
                "session_store",
                "session store queries (session/token keywords)",
                session_store,
            )
        )
    if metadata_config:
        signals.append(
            _signal(
                "metadata_config",
                "metadata/config store queries (small result, high reads)",
                metadata_config,
            )
        )
    if text_searches:
        signals.append(
            _signal(
                "text_search",
                "text search queries (LIKE, MATCH, tsvector)",
                text_searches,
            )
        )
    if time_ranges:
        signals.append(
            _signal(
                "time_range_filters",
                "time-range filter queries",
                time_ranges,
            )
        )
    if leaderboards:
        signals.append(
            _signal(
                "leaderboard_pattern",
                f"hot leaderboard / top-N queries (ORDER BY + LIMIT, TOP or FETCH, "
                f"≥{HOT_READ_MIN_CALLS_PER_SECOND:g} calls/s)",
                leaderboards,
            )
        )
    if subqueries:
        signals.append(
            _signal(
                "subqueries",
                "queries with subqueries (correlated or EXISTS)",
                subqueries,
            )
        )
    if high_freq_reads:
        signals.append(
            _signal(
                "high_frequency_reads",
                "high-frequency read queries (≥10 cps)",
                high_freq_reads,
            )
        )

    return signals


def _build_signal(signal: str, description: str, query_ids: list[str]) -> TriageSignal:
    """The ``signal`` built from ``query_ids``, its targets read from the catalog
    (``src/catalog/triage_signals.toml``, #921)."""
    deduped = list(dict.fromkeys(query_ids))
    return TriageSignal(
        signal=signal,
        targets=catalog.signal_targets(signal),
        evidence=f"{len(deduped)} {description}",
        query_ids=deduped,
        query_count=len(deduped),
    )


# ---------------------------------------------------------------------------
# Hard capability detection
# ---------------------------------------------------------------------------


def detect_query_capabilities(co: dict, signals: list[TriageSignal]) -> dict[str, list[str]]:
    """Detect hard capability requirements per query.

    Combines regex detection on SQL text with signal-based derivation.
    Only returns entries for queries that actually require capabilities.
    """
    queries = co.get("queries", {}).get("query_patterns", [])
    if not queries:
        return {}
    source = SourceScope.from_collector(co)

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
        caps = detect_required_capabilities(query_text, sigs, q.get("tables_accessed"), source)
        if caps:
            result[qid] = caps

    return result


# ---------------------------------------------------------------------------
# Agent selection
# ---------------------------------------------------------------------------


# Reason recorded for a modelled engine no triage signal names (#726).
UNNAMED_ENGINE_REASON = "Every modelled engine is scored on every query; no triage signal names it"


def _select_agents(
    signals: list[TriageSignal],
    aurora_agents: list[str] | None = None,
    *,
    no_source_engine: bool = False,
) -> TriageResult:
    """Select the analysis agents to dispatch: every query-owning engine (#726).

    ADR-031 Decision 2 step 1: every modelled target engine is a candidate, minus
    any engine the source makes impossible. So every query-owning engine in
    ``ANALYSIS_AGENTS`` is always selected, and the Aurora agent(s) the source
    allows are added. Signals never gate an owning candidate: they are recorded
    as evidence, as the selected engine's reasons, and scoring decides.

    A cache overlay engine (``CACHE_OVERLAY_ENGINES``, ElastiCache) owns no
    query, so it is not a routing candidate: it is selected only when one of its
    signals names it, exactly as before #726, so the overlay is unchanged.

    Aurora signals are tracked separately in `baseline` for Synthesis.
    Deferred agents (future placeholders, ADR-031 Decision 9) are tracked in
    `deferred` for reporting and never selected.

    ``aurora_agents`` is 0, 1 or 2 Aurora analysis agents: 1 when the source
    engine has its own Aurora dialect, 2 when it is a known relational engine
    with none (#381, both compete and the resolver picks one) or when no source
    engine is recorded (``no_source_engine``, #726, the same competition), 0
    otherwise.
    """
    from src.agents.referee.aurora_choice import AURORA_ENGINES  # import cycle

    result = TriageResult(signals=signals)

    # Aurora baseline — always present, signals accumulated for Synthesis
    result.baseline[BASELINE] = ["Relational baseline: tables not claimed by purpose-built agents"]

    aurora_agents = aurora_agents or []
    owning = ANALYSIS_AGENTS - AURORA_ENGINES - CACHE_OVERLAY_ENGINES
    candidates = owning | set(aurora_agents)

    evidence: dict[str, list[str]] = defaultdict(list)
    for sig in signals:
        for target in sig.targets:
            if target == BASELINE:
                result.baseline[BASELINE].append(f"{sig.signal}: {sig.evidence}")
            elif target in DEFERRED:
                result.deferred.setdefault(target, []).append(f"{sig.signal}: {sig.evidence}")
            else:
                evidence[target].append(f"{sig.signal}: {sig.evidence}")
    # A cache overlay engine joins only on its own signals (it owns no query).
    candidates |= CACHE_OVERLAY_ENGINES & set(evidence)

    for agent in sorted(candidates):
        reasons = list(evidence.get(agent, []))
        if agent in AURORA_ENGINES:
            # The source engine is inherently relational. Two agents means it has
            # no Aurora dialect of its own (#381): both compete on the workload,
            # and the assignment resolver picks exactly one before anything is
            # assigned.
            if len(aurora_agents) > 1 and no_source_engine:
                reasons.insert(
                    0,
                    "No source engine recorded, so both Aurora engines compete on workload fit",
                )
            elif len(aurora_agents) > 1:
                reasons.insert(
                    0,
                    "Source engine has no Aurora dialect of its own, so both Aurora "
                    "engines compete on workload fit",
                )
            else:
                reasons.insert(
                    0, "Source engine is relational; Aurora analysis provides baseline scoring"
                )
        result.selected[agent] = reasons or [UNNAMED_ENGINE_REASON]

    # Only an Aurora flavour the source rules out, or a cache overlay engine no
    # signal names, is skipped (never both selected and skipped).
    for agent in sorted(AURORA_ENGINES - set(aurora_agents)):
        result.skipped[agent] = "Source engine does not match this Aurora variant"
    for agent in sorted(CACHE_OVERLAY_ENGINES - candidates):
        result.skipped[agent] = "No matching workload signals detected"

    return result
