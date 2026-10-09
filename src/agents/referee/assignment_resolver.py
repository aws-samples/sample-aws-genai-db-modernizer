"""
Assignment Resolver — Auto-generates query→engine mappings from analysis results.

Implements the assignment resolution algorithm:
  1. Build signal-based overrides from triage (query-level pattern→engine mapping)
  2. Build anti-pattern penalties from analysis (query-level demotions)
  3. Build co-dependency groups from significant JOIN relationships
  4. Score each query against each engine (0–100), adjusted by signals + anti-patterns
  5. Assign co-dependent groups atomically (all queries → best engine for group)
  6. Assign remaining queries individually (highest adjusted score wins)
  7. Fallback to aurora for queries with no scores
  8. Mark the cache overlay (hot reads ElastiCache can front, #296)
  9. Derive table assignments, dropping source_tables noise the SQL parser
     introduced that is not a table or view the collector saw (#316)

Only system-of-record engines own queries (#296): ElastiCache is a cache layer
(``cache_overlay.CACHE_OVERLAY_ENGINES``) and never owns one, and an engine that
is not a system of record never owns a write. Exact score ties go to the
source-compatible engine, then to the engine already serving more of the query's
tables' traffic (calls/s), then to the engine with more queries, then by name.

The key insight: assignment happens QUERY BY QUERY, not table by table.
Triage signals like text_search→opensearch override table-level averages
because the signal is about the query's workload pattern, not the table's
general suitability.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from src.agents.referee.aurora_choice import (
    AURORA_ENGINES,
    choose_heterogeneous_engine,
    detect_heterogeneous_features,
    pick_aurora_engine,
    source_database_engine,
)
from src.agents.referee.cache_overlay import apply_cache_overlay, can_own, overlay_summary
from src.agents.referee.capability_registry import (
    can_engine_serve_capability,
    detect_required_capabilities,
)
from src.agents.referee.engine_exclusions import check_all_exclusions, check_exclusions
from src.agents.referee.table_resolution import PSEUDO_TABLES, TableNameResolver
from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.agents.referee.utility_statements import is_utility_statement
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    AuroraEngineChoice,
    CacheOverlaySummary,
    QueryAssignment,
    TableAssignment,
    UnresolvedNames,
)
from src.shared.engine_capabilities import (
    ACID_TRANSACTION_ENGINES,
    FUZZY_SEARCH_ENGINES,
    TEXT_SEARCH_ENGINES,
)
from src.shared.engine_names import display_engine
from src.shared.migration_wave_engines import NON_OWNER_ENGINES

logger = logging.getLogger(__name__)

# Triage signals that strongly indicate an engine is the RIGHT fit for a query.
# When a signal maps query→engine and that engine was selected by triage,
# the query should go to that engine regardless of table-level confidence.
# Cache signals (leaderboard_pattern, session_store) are not here: ElastiCache
# never owns a query, so they are only cache-overlay hints for hot reads (#296,
# ``cache_overlay.CACHE_HINT_SIGNALS``).
SIGNAL_ENGINE_OVERRIDES: dict[str, str] = {
    "text_search": "opensearch",
    "graph_traversal": "neptune",
}

# Analysis anti-pattern types that indicate an engine is the WRONG fit for a query.
# When a query appears in one of these anti-patterns for an engine, that engine's
# score is penalized for this specific query.
ANTI_PATTERN_PENALTIES: dict[str, int] = {
    # NoSQL engine anti-patterns (queries wrong for DynamoDB/DocumentDB)
    "text_search": 40,  # DynamoDB can't do full-text search at all
    "wildcard-search": 40,  # LIKE '%term%' is not DynamoDB's job
    "full-text-search": 40,  # tsvector/MATCH AGAINST — same reasoning
    "regex-search": 40,  # REGEXP/RLIKE/~ — same reasoning
    "fuzzy-search": 40,  # pg_trgm similarity() — same reasoning
    "complex-aggregation": 50,  # Heavy penalty — better engines exist, but DynamoDB can pre-compute
    "complex_aggregation": 50,  # Alias (some analysis agents use underscore)
    "multi-index-joins": 25,
    "acid-transactions": 20,
    "frequent-writes": 15,
    # Aurora anti-patterns (queries wrong for Aurora — better on purpose-built engines)
    "high-frequency-pk-lookup": 30,  # DynamoDB does this better at scale
    "simple-cache-read": 25,  # ElastiCache does this at microsecond latency
    "no-relational-need": 20,  # DynamoDB can serve simple KV without relational overhead
    "single-access-pattern-table": 15,  # DynamoDB is cheaper for simple patterns
    "high-volume-text-search": 35,  # OpenSearch purpose-built for this
}

# Which ANTI_PATTERN_PENALTIES keys are capability statements rather than pure
# workload-fit anti-patterns: "this query needs X" versus "this engine is a
# worse fit for this query than a purpose-built one". The "patterns detected
# by other engines penalise OTHER engines" step in ``_build_anti_pattern_map``
# must not apply a capability key's penalty to an engine that has the
# capability natively -- a capability signal says nothing about whether THIS
# engine is a good fit, only that engines without the capability need one.
# Mapped to ``src.shared.engine_capabilities`` so this stays the single fact
# every caller agrees on, instead of a second, possibly-drifted copy.
#
# ``acid-transactions``: Aurora PostgreSQL, Aurora MySQL and DocumentDB run
# multi-row/-document transactions natively (#477).
#
# ``wildcard-search``, ``full-text-search`` and ``regex-search``: OpenSearch's
# os-01..os-03 patterns all ask the same question -- can this engine run a
# text/pattern-search-shaped query at all, even as an unindexed scan -- so
# all three share ``TEXT_SEARCH_ENGINES`` (#480). ``fuzzy-search`` (os-04)
# asks a narrower one -- does this engine have a similarity/fuzzy-match
# operator at all -- which only Aurora PostgreSQL's ``pg_trgm`` answers yes
# to, so it gets its own ``FUZZY_SEARCH_ENGINES``. OpenSearch itself is
# still worth the move when traffic, data size or search depth justify it
# (#326); that is a separate, untouched mechanism (``high-volume-text-search``
# above is Aurora's own self-detected anti-pattern for exactly that case).
CAPABILITY_SIGNAL_ENGINES: dict[str, frozenset[str]] = {
    "acid-transactions": ACID_TRANSACTION_ENGINES,
    "wildcard-search": TEXT_SEARCH_ENGINES,
    "full-text-search": TEXT_SEARCH_ENGINES,
    "regex-search": TEXT_SEARCH_ENGINES,
    "fuzzy-search": FUZZY_SEARCH_ENGINES,
}

# Reduced penalty for an engine that lacks a capability outright but has a
# documented, bounded way to redesign around it, so the cost stays visible
# instead of collapsing to the same full "wrong engine" penalty as an engine
# with no path at all (#477). DynamoDB's own ``TransactWriteItems`` (no
# interactive BEGIN/COMMIT, each item usable at most once per transaction,
# <=100 items, <=4 MB, one account/Region, 2x the write capacity of the same
# writes done without it) or an idempotent multi-step flow both work, at a
# real engineering cost; 10 sits below ``acid-transactions``'s own full 20.
#
# The search patterns have no entry here (#480): every engine without the
# relevant capability set (``TEXT_SEARCH_ENGINES`` or ``FUZZY_SEARCH_ENGINES``)
# has no bounded redesign to point to the way ``TransactWriteItems`` is one
# -- they keep the full 40.
CAPABILITY_REDESIGN_PENALTIES: dict[str, dict[str, int]] = {
    "acid-transactions": {"dynamodb": 10},
}

# The reason text for a reduced redesign penalty: plain language, no raw
# engine id, worded so it reads as "this engine needs extra work", never as
# "this engine already relies on transactions". ``{engine}`` is replaced
# with the engine's display name (e.g. "DynamoDB") -- see ``resolve()`` for
# where this is attached (only to the engine the note is about, and only if
# that engine is the one the query is actually assigned to).
CAPABILITY_REDESIGN_NOTES: dict[str, str] = {
    "acid-transactions": (
        "Uses transactions: moving it to {engine} needs a redesign "
        "({engine} transactions, up to 100 items, or retry-safe steps)"
    ),
}

# The reason text for a capability EXEMPTION (decision-trace note, #480):
# unlike a redesign note, this fires when the engine the query ended up on
# is the one with the capability, so it reads as "nothing to see here, this
# engine already runs it" rather than "this needs extra work". ``{engine}``
# is replaced with the engine's display name the same way
# ``CAPABILITY_REDESIGN_NOTES`` is. Shared by all four search patterns --
# the wording fits each of them (LIKE, regex, full-text or fuzzy, all
# "search patterns"). ``acid-transactions`` has no entry: that exemption
# stays silent, unchanged from before #480.
_TEXT_SEARCH_EXEMPT_NOTE = (
    "{engine} runs this search pattern natively: not penalized. OpenSearch "
    "is worth the move only when traffic, data size or search depth justify "
    "a dedicated search engine"
)
CAPABILITY_EXEMPT_NOTES: dict[str, str] = {
    "wildcard-search": _TEXT_SEARCH_EXEMPT_NOTE,
    "full-text-search": _TEXT_SEARCH_EXEMPT_NOTE,
    "regex-search": _TEXT_SEARCH_EXEMPT_NOTE,
    "fuzzy-search": _TEXT_SEARCH_EXEMPT_NOTE,
}


def _capability_scoped_penalty(
    pattern_type: str, engine: str, penalty: int
) -> tuple[int, str | None]:
    """Scope a capability-shaped anti-pattern penalty to engines that need it.

    Returns ``(scoped_penalty, note)``. A pure workload-fit anti-pattern (not
    in ``CAPABILITY_SIGNAL_ENGINES``) returns ``penalty`` unchanged. For a
    capability signal: an engine with the capability is exempt (penalty 0),
    plus a note from ``CAPABILITY_EXEMPT_NOTES`` when one is configured for
    that pattern type; an engine without the capability but with a
    documented redesign path (``CAPABILITY_REDESIGN_PENALTIES``) gets that
    smaller penalty plus a note; every other engine keeps the full
    configured penalty.
    """
    capable_engines = CAPABILITY_SIGNAL_ENGINES.get(pattern_type)
    if capable_engines is None:
        return penalty, None
    if engine in capable_engines:
        exempt_template = CAPABILITY_EXEMPT_NOTES.get(pattern_type)
        exempt_note = (
            exempt_template.format(engine=display_engine(engine)) if exempt_template else None
        )
        return 0, exempt_note
    redesign_penalty = CAPABILITY_REDESIGN_PENALTIES.get(pattern_type, {}).get(engine)
    if redesign_penalty is not None:
        note_template = CAPABILITY_REDESIGN_NOTES.get(pattern_type)
        note = note_template.format(engine=display_engine(engine)) if note_template else None
        return redesign_penalty, note
    return penalty, None


class AssignmentResolver:
    """Generates default query→engine assignments from analysis outputs."""

    def resolve(
        self,
        triage: dict,
        analysis_outputs: dict[str, dict],
        collector_output: dict,
    ) -> Assignment:
        """Produce initial assignment based on query-level scoring.

        For each query:
        1. Check if triage signals mandate a specific engine (override)
        2. Compute per-engine confidence adjusted by anti-pattern penalties
        3. Pick the engine with highest adjusted score

        Respects co-dependent query groups (queries sharing significant
        JOINs on the same tables stay together).
        """
        # Local copy: this method mutates ``analysis_outputs`` (#381, Step 4b below)
        # when it collapses two competing Aurora engines to one, and the caller's own
        # dict is also handed to ``AssignmentValidator`` afterwards -- it must see every
        # engine it originally analyzed, not this method's internal decision.
        analysis_outputs = dict(analysis_outputs)

        queries = collector_output.get("queries", {}).get("query_patterns", [])
        tables = collector_output.get("database_schema", {}).get("tables", [])
        # selected_agents can be a list of strings or dicts with agent_type
        raw_selected = triage.get("selected_agents", [])
        selected_engines = {a["agent_type"] if isinstance(a, dict) else a for a in raw_selected}

        # Step 1: Build signal-based overrides from triage
        signal_overrides = self._build_signal_overrides(triage, selected_engines)

        # Step 2: Build per-query anti-pattern penalties from analysis
        anti_pattern_map, anti_pattern_notes = self._build_anti_pattern_map(analysis_outputs)

        # Step 3: Build co-dependency groups
        co_dep_groups = build_co_dependency_groups(queries, tables)

        # Step 3b: Hard capability requirements per query (#338 follow-up,
        # review of #375 finding 8): the triage signals a query's required
        # capabilities (aggregation, complex_joins, sql_admin, ...) the same
        # way the reality check's serviceability gate does. Pre-#375 this
        # gate only ran inside the reality check, so a bare COUNT(*)/
        # FOUND_ROWS()/SQL_CALC_FOUND_ROWS query (or a SHOW/SET statement
        # before the resolver's own utility pin runs) could still win the
        # v1 confidence-scoring path onto an engine that cannot run it --
        # the reality check corrected it in v2, but v1 (and any consumer
        # that reads it directly) showed the wrong engine in the meantime.
        query_capabilities: dict[str, list[str]] = triage.get("query_capabilities") or {}
        if not query_capabilities:
            query_capabilities = {
                q["query_id"]: caps
                for q in queries
                if (
                    caps := detect_required_capabilities(
                        q.get("query_text", ""), [], q.get("tables_accessed")
                    )
                )
            }

        # Step 4: Score each query against each engine (adjusted)
        scores: dict[str, dict[str, int]] = {}
        exclusion_notes: dict[str, list[str]] = {}  # query_id → list of exclusion messages
        for engine, analysis in analysis_outputs.items():
            for query in queries:
                qid = query["query_id"]
                if qid not in scores:
                    scores[qid] = {}

                # Hard exclusion check — if the query cannot run on this engine, score=0
                query_text = query.get("query_text", "")
                exclusion = check_exclusions(qid, query_text, engine)
                if exclusion:
                    scores[qid][engine] = 0
                    exclusion_notes.setdefault(qid, []).append(
                        f"[{exclusion.rule_id}] Excluded from {engine}: {exclusion.description}"
                    )
                    continue

                # Hard capability check — an engine with no aggregation/join/
                # sql_admin capability scores 0, the same way an exclusion does.
                required_caps = query_capabilities.get(qid, [])
                if required_caps and not can_engine_serve_capability(engine, required_caps):
                    scores[qid][engine] = 0
                    exclusion_notes.setdefault(qid, []).append(
                        f"[capability] {engine} lacks required capability: "
                        f"{', '.join(required_caps)}"
                    )
                    continue

                base_score = self._compute_query_confidence(qid, query, engine, analysis)

                # Apply anti-pattern penalty for this query+engine
                penalty = anti_pattern_map.get((qid, engine), 0)
                adjusted = max(0, base_score - penalty)

                scores[qid][engine] = adjusted

        # Step 4b: Collapse a heterogeneous source's two competing Aurora engines to
        # one (#381). A source with no Aurora dialect of its own (SQL Server, Oracle,
        # DB2) has triage select both aurora_mysql and aurora_postgresql, and both were
        # just scored above like any other candidate. Compare the total adjusted score
        # each would earn across every query -- the design's "winner takes all
        # relational queries" -- before any query is actually assigned, and drop the
        # losing engine from this method's own ``analysis_outputs``/``scores`` so every
        # step after this one (co-dependency groups, ties, the Aurora fallback, the
        # utility-statement pin, table derivation) only ever sees the winner, the same
        # as a homogeneous source only ever had one Aurora candidate to begin with.
        source_engine = source_database_engine(collector_output)
        aurora_engine_choice: AuroraEngineChoice | None = None
        both_aurora = AURORA_ENGINES & set(analysis_outputs)
        if source_engine not in SOURCE_ENGINE_TO_AURORA and len(both_aurora) == 2:
            totals = {
                engine: float(sum(scores.get(q["query_id"], {}).get(engine, 0) for q in queries))
                for engine in both_aurora
            }
            features = detect_heterogeneous_features(collector_output, source_engine)
            winner, trace = choose_heterogeneous_engine(sorted(both_aurora), totals, features)
            loser = next(e for e in both_aurora if e != winner)
            analysis_outputs = {e: a for e, a in analysis_outputs.items() if e != loser}
            for qid_scores in scores.values():
                qid_scores.pop(loser, None)
            aurora_engine_choice = AuroraEngineChoice(
                source_engine=source_engine,
                engine=winner,
                totals=trace["totals"],
                margin=trace["margin"],
                deciding_features=trace["deciding_features"],
                reason=trace["reason"],
            )

        # The "retained Aurora engine" passed to ``derive_table_assignments`` below,
        # the same role ``retained_engine_for`` plays for every other caller (#317):
        # the source's own dialect when it has one, else the #381 winner just computed,
        # else ``None`` (no source engine at all, or no Aurora candidate selected --
        # unaffected by this change). Computed here, rather than by calling
        # ``retained_engine_for(collector_output)`` again, because that helper only
        # knows ``SOURCE_ENGINE_TO_AURORA`` -- it has no way to see the #381 choice
        # this method just made.
        effective_relational_engine = SOURCE_ENGINE_TO_AURORA.get(source_engine) or (
            aurora_engine_choice.engine if aurora_engine_choice else None
        )

        # Capability guarantee for a homogeneous source (review of #474, round 3):
        # this exact query already ran on the source database, so the
        # source-compatible Aurora engine (MySQL/MariaDB -> aurora_mysql,
        # PostgreSQL -> aurora_postgresql) is always ownable and serviceable for
        # it -- never hard-excluded (``check_exclusions``) or capability-zeroed
        # (``can_engine_serve_capability``) the way a purpose-built engine can
        # be, even if a future rule would otherwise drop it. This is a presence
        # guarantee, not a score override: it still competes on its own
        # confidence score below, and a clearly higher-scoring engine still
        # wins. An exact tie already goes to it, via the resolver's existing
        # source-compatible tie-break (``break_owner_tie``'s first rule) -- not
        # duplicated here. A heterogeneous source (#381, no entry in
        # ``SOURCE_ENGINE_TO_AURORA``) has no such guarantee: ``aurora_engine_
        # choice``'s winner, if any, is only a normal scored candidate here.
        homogeneous_aurora = SOURCE_ENGINE_TO_AURORA.get(source_engine)
        if homogeneous_aurora and homogeneous_aurora in analysis_outputs:
            analysis = analysis_outputs[homogeneous_aurora]
            for query in queries:
                qid = query["query_id"]
                base_score = self._compute_query_confidence(
                    qid, query, homogeneous_aurora, analysis
                )
                penalty = anti_pattern_map.get((qid, homogeneous_aurora), 0)
                scores.setdefault(qid, {})[homogeneous_aurora] = max(0, base_score - penalty)
                if qid in exclusion_notes:
                    exclusion_notes[qid] = [
                        n
                        for n in exclusion_notes[qid]
                        if f"Excluded from {homogeneous_aurora}" not in n
                        and f"capability] {homogeneous_aurora} " not in n
                    ]
                    if not exclusion_notes[qid]:
                        del exclusion_notes[qid]

        # Step 5: Assign co-dependent groups atomically. Only engines that may own
        # every query of the group compete (cache engines never own; engines that
        # are not a system of record own no write, #296).
        query_by_id = {q["query_id"]: q for q in queries}
        assigned: dict[str, str] = {}
        assigned_confidence: dict[str, int] = {}
        assigned_reason: dict[str, str] = {}
        assigned_signal: dict[str, str] = {}

        # Queries left for the Aurora fallback. The engine is chosen once the
        # rest are assigned, since it can depend on how many queries each
        # Aurora engine already serves (#288).
        fallback_qids: list[str] = []
        # Exact score ties, resolved once every untied query is placed: the
        # tie-break looks at the traffic each engine already serves (#296).
        tied: list[tuple[list[str], list[str], int, str]] = []

        for group in co_dep_groups:
            candidates = [
                e
                for e in analysis_outputs
                if all(can_own(e, query_by_id.get(qid)) for qid in group)
            ]
            if not candidates:
                for qid in group:
                    fallback_qids.append(qid)
                    assigned[qid] = ""
                    assigned_confidence[qid] = 0
                    assigned_reason[qid] = "no analysis available"
                continue
            group_score = {
                e: sum(scores.get(qid, {}).get(e, 0) for qid in group) / len(group)
                for e in candidates
            }
            top = max(group_score.values())
            best = [e for e in candidates if group_score[e] == top]
            if len(best) > 1:
                tied.append((list(group), best, 0, "co-dependency group"))
                for qid in group:
                    assigned[qid] = ""
                continue
            for qid in group:
                assigned[qid] = best[0]
                assigned_confidence[qid] = scores.get(qid, {}).get(best[0], 0)
                assigned_reason[qid] = f"co-dependency group → {best[0]}"

        # Step 6: Assign remaining queries individually
        for query in queries:
            qid = query["query_id"]
            if qid in assigned:
                continue

            # Check signal override first
            if qid in signal_overrides and can_own(signal_overrides[qid]["engine"], query):
                engine = signal_overrides[qid]["engine"]
                signal = signal_overrides[qid]["signal"]
                assigned[qid] = engine
                assigned_confidence[qid] = scores.get(qid, {}).get(engine, 0)
                assigned_reason[qid] = f"signal override: {signal} → {engine}"
                assigned_signal[qid] = signal
                continue

            # Otherwise pick highest adjusted score among the engines that may own it
            query_scores = {e: s for e, s in scores.get(qid, {}).items() if can_own(e, query)}
            if query_scores:
                top_score = max(query_scores.values())
                best = [e for e in query_scores if query_scores[e] == top_score]
                if len(best) > 1:
                    tied.append(([qid], best, top_score, "individual"))
                    assigned[qid] = ""
                    continue
                assigned[qid] = best[0]
                assigned_confidence[qid] = top_score
                assigned_reason[qid] = f"highest confidence for {best[0]}"
            else:
                # Step 7: Fallback to aurora engine from triage
                fallback_qids.append(qid)
                assigned[qid] = ""
                assigned_confidence[qid] = 0
                assigned_reason[qid] = "no engine scored this query"

        # Step 6b: Resolve exact ties against the traffic already placed (#296).
        if tied:
            source_aurora = SOURCE_ENGINE_TO_AURORA.get(source_engine)
            table_traffic, engine_counts = _placed_traffic(assigned, query_by_id)
            for qids, best, top_score, kind in tied:
                winner, why = break_owner_tie(
                    best, qids, query_by_id, source_aurora, table_traffic, engine_counts
                )
                for qid in qids:
                    assigned[qid] = winner
                    if kind == "individual":
                        assigned_confidence[qid] = top_score
                        assigned_reason[qid] = (
                            f"highest confidence for {winner} (tie with "
                            f"{', '.join(e for e in best if e != winner)}: {why})"
                        )
                    else:
                        assigned_confidence[qid] = scores.get(qid, {}).get(winner, 0)
                        assigned_reason[qid] = f"co-dependency group → {winner} (tie: {why})"

        if fallback_qids:
            # #381 review: still ``selected_engines`` (the fallback must use whichever
            # Aurora engine triage selected, whether or not it was ever analyzed -- the
            # pre-#381 contract this fallback has always had), but with the Step 4b
            # loser explicitly excluded, so a heterogeneous source's fallback only ever
            # sees the engine the resolver actually chose, the same restriction every
            # other step after Step 4b already respects.
            fallback_pool = selected_engines - (
                {e for e in AURORA_ENGINES if e != aurora_engine_choice.engine}
                if aurora_engine_choice
                else set()
            )
            aurora_fallback = _resolve_aurora_fallback(
                fallback_pool,
                source_engine,
                Counter(e for e in assigned.values() if e),
                prior_choice=aurora_engine_choice.engine if aurora_engine_choice else None,
            )
            for qid in fallback_qids:
                assigned[qid] = aurora_fallback

        # Step 6c: Surface a reduced capability-redesign penalty's reason
        # (#477) in the assignment reason -- but only on the engine the note
        # is actually about, and only when the query ended up assigned to
        # that engine. Attaching it regardless of the winner (as an earlier
        # version of this fix did) would show up on, say, an Aurora win's
        # reason and misleadingly read as if Aurora were the one that needed
        # the redesign; checking ``assigned[qid]`` here means the note only
        # ever describes the engine the query actually landed on.
        for qid, engine in assigned.items():
            note = anti_pattern_notes.get((qid, engine))
            if note:
                assigned_reason[qid] = f"{assigned_reason[qid]} | {note}"

        # Step 7b: Utility and metadata statements never enter engine routing
        # (#327). SHOW/SET/DESCRIBE/EXPLAIN/CREATE/ALTER/DROP and catalog
        # lookups (information_schema, pg_catalog) are database-administration
        # traffic, not an application access pattern a target engine needs to
        # serve -- a purpose-built engine like DynamoDB has no concept of a
        # column list to SHOW or a session variable to SET. They are pinned to
        # the source-compatible relational engine, overriding whatever the
        # confidence-scoring path above picked, so every other artifact still
        # accounts for every collected query. If no Aurora engine is in play,
        # the scored assignment is left as-is rather than left unset.
        utility_engine = pick_aurora_engine(
            analysis_outputs.keys(),
            source_engine,
            Counter(e for e in assigned.values() if e),
            prior_choice=aurora_engine_choice.engine if aurora_engine_choice else None,
        )
        if utility_engine:
            for query in queries:
                qid = query["query_id"]
                if is_utility_statement(query.get("query_text"), query.get("tables_accessed")):
                    assigned[qid] = utility_engine
                    assigned_confidence[qid] = 100
                    assigned_reason[qid] = (
                        "utility/metadata statement — kept on source-compatible "
                        f"relational engine ({utility_engine})"
                    )
                    assigned_signal.pop(qid, None)

        # Step 8: Cache overlay (#296): hot bounded reads get cache_engine; the
        # owner stays the engine assigned above.
        overlay_dicts = apply_cache_overlay(
            [
                {"query_id": q["query_id"], "assigned_engine": assigned[q["query_id"]]}
                for q in queries
            ],
            queries,
            analysis_outputs.keys(),
        )
        overlay_by_id = {o["query_id"]: o for o in overlay_dicts}

        # Build query→tables lookup
        query_tables: dict[str, list[str]] = {}
        for query in queries:
            qid = query["query_id"]
            query_tables[qid] = query.get("tables_accessed", [])

        # Build query assignments
        query_assignments: list[QueryAssignment] = []
        for query in queries:
            qid = query["query_id"]
            engine = assigned[qid]
            confidence = assigned_confidence.get(qid, 0)
            reason = assigned_reason.get(qid, f"highest confidence for {engine}")

            # Append exclusion notes to the reason if this query was excluded from other engines
            if qid in exclusion_notes:
                reason = reason + " | " + "; ".join(exclusion_notes[qid])

            query_assignments.append(
                QueryAssignment(
                    query_id=qid,
                    assigned_engine=engine,
                    confidence=confidence,
                    source_tables=query_tables.get(qid, []),
                    assignment_reason=reason,
                    signal_override=assigned_signal.get(qid),
                    cache_engine=overlay_by_id[qid]["cache_engine"],
                    cache_pattern=overlay_by_id[qid]["cache_pattern"],
                    cache_reason=overlay_by_id[qid]["cache_reason"],
                )
            )

        # Step 9: Derive table assignments. Noise the SQL parser can put in
        # source_tables (CTE aliases, catalogs, sequences, keywords, columns)
        # is dropped first (#316), scoped to the tables and views the
        # collector actually saw. A table's primary_engine must always be a
        # durable owner (#317), so pass the retained source-compatible engine
        # as the fallback for tables with no owner engine among their
        # assigned engines.
        table_assignments, unresolved_table_names = derive_table_assignments(
            query_assignments,
            known_tables=TableNameResolver.from_collector(collector_output),
            retained_engine=effective_relational_engine,
        )

        # Flatten co-dep groups to list of lists of query_ids
        co_dep_lists = [list(g) for g in co_dep_groups]

        return Assignment(
            job_id=collector_output.get("job_id", "unknown"),
            version=1,
            status=AssignmentStatus.AUTO_GENERATED,
            source=AssignmentSource.ASSIGNMENT_RESOLUTION,
            timestamp=datetime.now(tz=UTC),
            query_assignments=query_assignments,
            table_assignments=table_assignments,
            co_dependency_groups=co_dep_lists,
            validation_warnings=[],
            cache_overlay=_overlay_model(overlay_summary(overlay_dicts, queries)),
            unresolved_table_names=unresolved_table_names,
            aurora_engine_choice=aurora_engine_choice,
        )

    def _build_signal_overrides(
        self,
        triage: dict,
        selected_engines: set[str],
    ) -> dict[str, dict]:
        """Build query_id → {engine, signal} overrides from triage signals.

        Only creates overrides when:
        - The signal type has a known engine mapping (SIGNAL_ENGINE_OVERRIDES)
        - The target engine was selected by triage (available for assignment)
        - The signal has specific query_ids attached
        """
        overrides: dict[str, dict] = {}
        for signal in triage.get("signals", []):
            signal_name = signal.get("signal", "")
            override_engine = SIGNAL_ENGINE_OVERRIDES.get(signal_name)
            if not override_engine:
                continue
            if override_engine not in selected_engines:
                continue
            for qid in signal.get("query_ids", []):
                overrides[qid] = {
                    "engine": override_engine,
                    "signal": signal_name,
                }
        return overrides

    def _build_anti_pattern_map(
        self,
        analysis_outputs: dict[str, dict],
    ) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], str]]:
        """Build (query_id, engine) → penalty map from analysis anti-patterns.

        When a query appears in an anti-pattern for an engine, its confidence
        score for that engine gets reduced by the penalty amount. This ensures
        queries that are fundamentally wrong for an engine (e.g., text search
        on DynamoDB) don't get assigned there just because the TABLE average
        is high.

        A capability signal (``CAPABILITY_SIGNAL_ENGINES``) is scoped through
        ``_capability_scoped_penalty`` before being recorded, whether it was
        self-detected or picked up from the "patterns detected by other
        engines" step below, so an engine that has the capability is never
        penalised for it. Returns the penalty map plus a
        ``(query_id, engine) → note`` map carrying the human-readable reason
        for a reduced "needs a redesign" penalty, or for a capability
        exemption that has a note configured (``CAPABILITY_EXEMPT_NOTES``),
        so the resolver can surface it in the query's assignment reason
        (``QueryAssignment.assignment_reason``, Step 6c below). An exempt
        note is only kept when no OTHER anti-pattern has already put a real
        (non-zero) penalty on the same ``(query_id, engine)`` pair -- once
        one does, that penalty's own note (or lack of one) replaces it, the
        same "biggest penalty wins" rule the non-zero branch below already
        applies, so the note never claims an engine was unpenalised when it
        actually lost points to a different anti-pattern.
        """
        penalties: dict[tuple[str, str], int] = {}
        notes: dict[tuple[str, str], str] = {}

        def _record(key: tuple[str, str], pattern_type: str, raw_penalty: int) -> None:
            _qid, target_engine = key
            scoped_penalty, note = _capability_scoped_penalty(
                pattern_type, target_engine, raw_penalty
            )
            if scoped_penalty == 0:
                if note and key not in penalties:
                    notes.setdefault(key, note)
                return
            if scoped_penalty >= penalties.get(key, 0):
                penalties[key] = scoped_penalty
                if note:
                    notes[key] = note
                else:
                    notes.pop(key, None)

        for engine, analysis in analysis_outputs.items():
            wa = analysis.get("workload_analysis", {})
            for ap in wa.get("anti_patterns_detected") or []:
                ap_type = ap.get("anti_pattern_type", "")
                penalty = ANTI_PATTERN_PENALTIES.get(ap_type, 0)
                if penalty == 0:
                    continue
                for qid in ap.get("query_ids") or []:
                    _record((qid, engine), ap_type, penalty)

            # Also check patterns_detected from OTHER engines as positive signals
            # (e.g., if OpenSearch detects wildcard-search pattern for a query,
            # that's a signal the query belongs there)
            for pattern in wa.get("patterns_detected") or []:
                pattern_type = pattern.get("pattern_type", "")
                # If a pattern type matches an anti-pattern penalty name,
                # penalize OTHER engines for those queries
                penalty = ANTI_PATTERN_PENALTIES.get(pattern_type, 0)
                if penalty == 0:
                    continue
                for qid in pattern.get("query_ids", []):
                    for other_engine in analysis_outputs:
                        if other_engine != engine:
                            _record((qid, other_engine), pattern_type, penalty)

        return penalties, notes

    def _compute_query_confidence(
        self,
        query_id: str,
        query: dict,
        engine: str,
        analysis: dict,
    ) -> int:
        """Compute base confidence (0–100) for a query→engine pair.

        Uses query-level pattern matching first, then falls back to
        table-level averaging only if the query isn't found in any pattern.
        """
        table_recs = {r["table_id"]: r for r in analysis.get("table_recommendations", [])}
        wa = analysis.get("workload_analysis", {})

        # First: check if this query appears in any detected PATTERN for this engine.
        # If so, use the confidence of the tables involved in that pattern.
        pattern_tables: list[str] = []
        for pattern in wa.get("patterns_detected", []):
            if query_id in (pattern.get("query_ids") or []):
                pattern_tables.extend(pattern.get("table_ids") or [])

        pattern_tables = list(dict.fromkeys(pattern_tables))

        if pattern_tables:
            matched_scores = [
                table_recs[t].get("confidence_score", 0) for t in pattern_tables if t in table_recs
            ]
            if matched_scores:
                return int(sum(matched_scores) / len(matched_scores))

        # Second: use the query's tables_accessed to look up table recommendations
        query_tables = query.get("tables_accessed", [])
        if query_tables:
            matched_scores = [
                table_recs[t].get("confidence_score", 0) for t in query_tables if t in table_recs
            ]
            if matched_scores:
                return int(sum(matched_scores) / len(matched_scores))

        # Last resort: average across all table recommendations
        if table_recs:
            scores = [r.get("confidence_score", 0) for r in table_recs.values()]
            return int(sum(scores) / len(scores))
        return 0


# ---------------------------------------------------------------------------
# Owner tie-break (#296)
# ---------------------------------------------------------------------------


def _overlay_model(summary: dict | None) -> CacheOverlaySummary | None:
    return CacheOverlaySummary(**summary) if summary else None


def _placed_traffic(
    assigned: Mapping[str, str], query_by_id: Mapping[str, dict]
) -> tuple[dict[tuple[str, str], float], Counter]:
    """``(table, engine) -> calls/s`` and ``engine -> queries`` of the placed queries."""
    traffic: dict[tuple[str, str], float] = defaultdict(float)
    counts: Counter = Counter()
    for qid, engine in assigned.items():
        if not engine:
            continue
        counts[engine] += 1
        q = query_by_id.get(qid) or {}
        cps = float(q.get("calls_per_second") or 0)
        for table in q.get("tables_accessed") or []:
            traffic[(table, engine)] += cps
    return traffic, counts


def break_owner_tie(
    engines: list[str],
    qids: list[str],
    query_by_id: Mapping[str, dict],
    source_aurora: str | None,
    table_traffic: Mapping[tuple[str, str], float],
    engine_counts: Mapping[str, int],
) -> tuple[str, str]:
    """Pick the owner among engines with the same score, and say why (#296).

    1. the source-compatible engine (the Aurora engine of the source's dialect);
    2. the engine already serving more of the queries' tables' traffic (calls/s);
    3. the engine already serving more queries;
    4. the engine name, so the pick never depends on dict or set order.

    Cache engines are never in ``engines`` (they own nothing), so a tie can never
    tip a query, hot or not, onto the cache.
    """
    if source_aurora in engines:
        return source_aurora, "source-compatible engine"
    tables = sorted(
        {t for qid in qids for t in (query_by_id.get(qid) or {}).get("tables_accessed") or []}
    )

    def traffic(e: str) -> float:
        return round(sum(table_traffic.get((t, e), 0.0) for t in tables), 6)

    ranked = sorted(engines, key=lambda e: (-traffic(e), -engine_counts.get(e, 0), e))
    winner, runner_up = ranked[0], ranked[1]
    if traffic(winner) > traffic(runner_up):
        return winner, f"serves more of these tables' traffic ({traffic(winner):.2f} calls/s)"
    if engine_counts.get(winner, 0) > engine_counts.get(runner_up, 0):
        return winner, "serves more queries"
    return winner, "name order"


# ---------------------------------------------------------------------------
# Aurora fallback resolution
# ---------------------------------------------------------------------------


def _resolve_aurora_fallback(
    selected_engines: set[str],
    source_engine: str = "",
    query_counts: Mapping[str, int] | None = None,
    *,
    prior_choice: str | None = None,
) -> str:
    """Determine which Aurora engine to use as fallback.

    Uses the Aurora engine selected by triage. When both are selected, the one
    matching the source database's dialect wins, then the one with more
    queries, then Aurora PostgreSQL (``pick_aurora_engine``, #288). If no
    Aurora engine was selected, uses a generic 'aurora' placeholder (legacy
    behavior).

    ``prior_choice`` (#381 review) is ``Assignment.aurora_engine_choice.engine``
    when the resolver already made that choice this run -- passed through to
    ``pick_aurora_engine`` so a 0/0-count tie here never overrides it.
    """
    return (
        pick_aurora_engine(selected_engines, source_engine, query_counts, prior_choice=prior_choice)
        or "aurora"
    )


# ---------------------------------------------------------------------------
# Co-dependency detection (Task 4.2)
# ---------------------------------------------------------------------------


def is_significant_join(query: dict, table: str) -> bool:
    """Determine if a query's JOIN on a table is significant (load-bearing).

    A JOIN is significant when:
    - join_count >= 2 (multi-table join)
    - has_aggregations is True (GROUP BY across joined tables; the collector's field)
    - table appears in filter_tables (WHERE clause references the joined table)

    Light JOINs that only fetch a display field do not create co-dependencies.

    Requirements: 2.3
    """
    return (
        query.get("join_count", 0) >= 2
        or query.get("has_aggregations", False)
        or table in query.get("filter_tables", [])
    )


def build_co_dependency_groups(
    queries: list[dict],
    tables: list[dict],
) -> list[list[str]]:
    """Build co-dependency groups using union-find with significance filter.

    Groups queries that share significant JOINs on the same tables.
    Only returns groups with 2+ queries.

    Requirements: 2.3
    """
    # Map: table → query_ids (input order, deduplicated) with significant JOINs
    # on that table. A dict, not a set: set order depends on PYTHONHASHSEED and
    # would leak into the group order and membership order (#288).
    table_to_queries: dict[str, dict[str, None]] = {}
    for q in queries:
        if q.get("has_joins") or q.get("join_count", 0) > 0:
            for table in q.get("tables_accessed", []):
                if is_significant_join(q, table):
                    table_to_queries.setdefault(table, {})[q["query_id"]] = None

    # Union-find with path compression
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])  # path compression
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Union queries that share significant JOINs on the same table
    for _table, query_ids in table_to_queries.items():
        query_list = list(query_ids)
        for i in range(1, len(query_list)):
            union(query_list[0], query_list[i])

    # Collect groups
    groups: dict[str, list[str]] = {}
    for qid in parent:
        root = find(qid)
        groups.setdefault(root, []).append(qid)

    # Also include query_ids that appear in table_to_queries but not in parent
    # (single-entry sets that were never unioned)
    all_qids_in_tables: dict[str, None] = {}
    for qids in table_to_queries.values():
        all_qids_in_tables.update(qids)
    for qid in all_qids_in_tables:
        if qid not in parent:
            root = find(qid)
            groups.setdefault(root, []).append(qid)

    # Only return groups with 2+ queries
    return [g for g in groups.values() if len(g) >= 2]


# ---------------------------------------------------------------------------
# Table assignment derivation (Task 4.3)
# ---------------------------------------------------------------------------


def retained_engine_for(
    collector_output: Mapping,
    query_assignments: Iterable[Mapping | QueryAssignment] | None = None,
    aurora_engine_choice: str | Mapping | AuroraEngineChoice | None = None,
) -> str | None:
    """The retained source-compatible Aurora engine for ``collector_output`` (#317).

    The Aurora engine matching the source database's dialect (``aurora_mysql``
    for a MySQL source, ``aurora_postgresql`` for Postgres) — never a member of
    ``NON_OWNER_ENGINES``. ``None`` when the source engine is missing or
    unrecognized and neither ``aurora_engine_choice`` nor ``query_assignments``
    is given. Every caller that recomputes ``table_assignments`` from a
    ``collector_output`` should pass this through so a table with no durable
    owner among its own engines never keeps a cache or search engine as
    ``primary_engine``.

    ``aurora_engine_choice`` (#381 review) is the authoritative
    ``Assignment.aurora_engine_choice`` -- as the engine string itself, the
    ``AuroraEngineChoice`` model, or its ``model_dump()``/raw-JSON dict -- when
    the caller has it, for a source with no Aurora dialect of its own (SQL
    Server, Oracle, DB2 — no entry in ``SOURCE_ENGINE_TO_AURORA``). Preferred
    over reconstructing the choice from ``query_assignments``: a customer
    override that moves one query onto the *losing* Aurora engine would make
    the reconstruction see both engines in use and resolve the ambiguity by
    alphabetical sort, not by the resolver's actual decision.

    ``query_assignments`` (#381), when given and ``aurora_engine_choice`` is
    not, recovers the same Aurora engine from the current assignment's own
    routing instead: by the time an assignment exists, the resolver's own
    choice (``choose_heterogeneous_engine``) already dropped the losing engine
    from scoring, so at most one of ``aurora_mysql``/``aurora_postgresql``
    ever appears as an ``assigned_engine`` -- unless a later customer override
    changed that, which is exactly why ``aurora_engine_choice`` is preferred
    when available.
    """
    native = SOURCE_ENGINE_TO_AURORA.get(source_database_engine(collector_output))
    if native:
        return native
    if aurora_engine_choice is not None:
        engine: str | None
        if isinstance(aurora_engine_choice, str):
            engine = aurora_engine_choice
        elif isinstance(aurora_engine_choice, Mapping):
            engine = aurora_engine_choice.get("engine")
        else:
            engine = getattr(aurora_engine_choice, "engine", None)
        if engine in AURORA_ENGINES:
            return engine
    if not query_assignments:
        return None
    used = {
        (qa.get("assigned_engine") if isinstance(qa, Mapping) else qa.assigned_engine)
        for qa in query_assignments
    }
    chosen = sorted(AURORA_ENGINES.intersection(used))
    return chosen[0] if chosen else None


def derive_table_assignments(
    query_assignments: list[QueryAssignment],
    *,
    known_tables: TableNameResolver | None = None,
    retained_engine: str | None = None,
) -> tuple[list[TableAssignment], UnresolvedNames]:
    """Derive table-level assignments from query assignments.

    For each table:
    - Find all engines with assigned queries referencing it
    - Set primary_engine to the owner engine with most assigned queries for
      that table — never a cache or search/read-model engine (#317): among a
      table's engines, ElastiCache and OpenSearch (``NON_OWNER_ENGINES``) are
      skipped, and the durable owner engine (Aurora, DynamoDB or DocumentDB)
      with the most queries wins. An exact tie goes to ``retained_engine``
      (the least migration), then to the engine name, so the pick never
      depends on dict insertion order. If the table has no owner engine at
      all (every assigned query went to a cache or search engine),
      ``primary_engine`` falls back to ``retained_engine``, then to an Aurora
      engine used anywhere in ``query_assignments``, then to the busiest
      owner engine used anywhere in ``query_assignments`` — never a member of
      ``NON_OWNER_ENGINES``, even with no ``retained_engine`` and no owner
      engine on the table itself.
    - Set multi_engine_reason when engines list has 2+ entries

    ``known_tables`` (:class:`.table_resolution.TableNameResolver`, built via
    ``TableNameResolver.from_collector``), when given, resolves each
    ``source_tables`` entry to the collector's canonical ``table_id``/
    ``view_id``. An entry that does not resolve is not a table at all -- a
    CTE alias, a system catalog, a sequence, a keyword or a column the SQL
    parser mistook for a table (#316) -- and is dropped before building
    ``table_assignments`` rather than counted as one; the second return value
    records what was dropped (minus ``PSEUDO_TABLES``, a parser or dialect
    artifact, never a real table, so never worth reporting as unresolved).
    ``known_tables=None`` (no collector schema available) keeps every name,
    the behavior before #316.

    A name that resolves canonically but is spelled differently across
    queries (``wp_posts`` in one, ``wordpress.wp_posts`` in another) is
    merged into a single row under the canonical ``table_id`` — never split
    into two tables.

    Requirements: 2.6, 11.1, 11.2
    """
    # table_id → engine → count of queries
    table_engine_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # table_id → total query count
    table_query_counts: dict[str, int] = defaultdict(int)
    unresolved: set[str] = set()

    for qa in query_assignments:
        for table_id in qa.source_tables:
            if table_id in PSEUDO_TABLES:
                continue
            resolved = table_id if known_tables is None else known_tables.resolve(table_id)
            if resolved is None:
                unresolved.add(table_id)
                continue
            table_engine_counts[resolved][qa.assigned_engine] += 1
            table_query_counts[resolved] += 1

    # Global, assignment-wide fallback (#317 finding 3): used only when a table
    # has no owner engine among its own engines *and* no retained_engine was
    # given. Prefers an Aurora engine used anywhere in the assignment (the
    # retained relational core), then the busiest owner engine used anywhere,
    # so the result is never a cache or search engine even in that corner case.
    global_counts = Counter(qa.assigned_engine for qa in query_assignments if qa.assigned_engine)
    global_owner_counts = {e: c for e, c in global_counts.items() if e not in NON_OWNER_ENGINES}
    global_fallback = pick_aurora_engine(global_owner_counts, query_counts=global_owner_counts) or (
        min(global_owner_counts, key=lambda e: (-global_owner_counts[e], e))
        if global_owner_counts
        else None
    )
    if global_fallback is None:
        logger.warning(
            "derive_table_assignments: no retained engine, and no owner engine anywhere in "
            "the assignment; falling back to the generic 'aurora' placeholder as primary_engine "
            "for a table with no owner engine of its own."
        )
        global_fallback = "aurora"

    result: list[TableAssignment] = []
    for table_id in sorted(table_engine_counts.keys()):
        engine_counts = table_engine_counts[table_id]
        engines = sorted(engine_counts.keys())
        owner_counts = {e: c for e, c in engine_counts.items() if e not in NON_OWNER_ENGINES}
        if owner_counts:
            primary_engine = min(
                owner_counts, key=lambda e: (-owner_counts[e], e != retained_engine, e)
            )
        elif retained_engine:
            primary_engine = retained_engine
        else:
            primary_engine = global_fallback

        multi_engine_reason = None
        if len(engines) >= 2:
            multi_engine_reason = (
                f"Table {table_id} has queries assigned to multiple engines: {', '.join(engines)}"
            )

        result.append(
            TableAssignment(
                table_id=table_id,
                primary_engine=primary_engine,
                engines=engines,
                query_count=table_query_counts[table_id],
                multi_engine_reason=multi_engine_reason,
            )
        )

    sorted_unresolved = sorted(unresolved)
    return result, UnresolvedNames(count=len(sorted_unresolved), names=sorted_unresolved)


# ---------------------------------------------------------------------------
# Customer override enforcement (post-review validation)
# ---------------------------------------------------------------------------


def _query_type(query_id: str, queries: list[dict]) -> str:
    return next((q.get("query_type", "") for q in queries if q.get("query_id") == query_id), "")


def enforce_exclusions_on_overrides(
    assignment: Assignment,
    queries: list[dict],
    analysis_outputs: dict[str, dict],
) -> Assignment:
    """Validate customer overrides against hard exclusions and auto-reassign violations.

    After a customer reviews and modifies assignments, this function checks
    if any customer-chosen engine is excluded for the query. If so, it:
    1. Reassigns the query to the next-best valid engine
    2. Adds a warning explaining why the override was rejected

    Returns a new Assignment with violations corrected.
    """
    query_text_map = {q["query_id"]: q.get("query_text", "") for q in queries}
    available_engines = set(analysis_outputs.keys())

    updated_assignments = []
    reassignment_count = 0

    for qa in assignment.query_assignments:
        query_text = query_text_map.get(qa.query_id, "")
        exclusion = check_exclusions(qa.query_id, query_text, qa.assigned_engine)

        if not exclusion:
            updated_assignments.append(qa)
            continue

        # This assignment violates a hard exclusion — find the next-best engine
        all_exclusions = check_all_exclusions(qa.query_id, query_text)
        excluded_engines = {e.excluded_engine for e in all_exclusions}
        valid_engines = {
            e
            for e in available_engines - excluded_engines
            if can_own(e, {"query_type": _query_type(qa.query_id, queries)})
        }

        if valid_engines:
            # Pick the first valid engine (in practice, the resolver would score these)
            new_engine = sorted(valid_engines)[0]
        else:
            new_engine = "aurora"

        warnings = list(qa.warnings)
        warnings.append(exclusion.customer_message)

        updated_assignments.append(
            qa.model_copy(
                update={
                    "assigned_engine": new_engine,
                    "assignment_reason": (
                        f"Auto-reassigned from {qa.assigned_engine}: {exclusion.description}"
                    ),
                    "customer_override": False,
                    "warnings": warnings,
                }
            )
        )
        reassignment_count += 1

    if reassignment_count == 0:
        return assignment

    # Recompute table assignments. No collector schema is available here, so
    # no filtering happens (#316): unresolved_table_names is carried over from
    # ``assignment`` unchanged, since source_tables itself does not change in
    # this reassignment pass.
    table_assignments, _ = derive_table_assignments(updated_assignments)

    return assignment.model_copy(
        update={
            "query_assignments": updated_assignments,
            "table_assignments": table_assignments,
        }
    )
