"""Reality check Pass 3 and 4: architectural patterns and recommendations."""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Iterable
from copy import deepcopy

from src.agents.referee.reality_check_records import _queries
from src.shared.engine_names import display_engine
from src.shared.engines_in_use import engines_in_use as running_engines

# ---------------------------------------------------------------------------
# Architectural patterns
# ---------------------------------------------------------------------------

ARCHITECTURAL_PATTERNS: dict[str, dict] = {
    "CQRS": {
        "name": "Command Query Responsibility Segregation (CQRS)",
        "description": (
            "Separate write operations (commands) from read operations (queries) "
            "across different databases. The write database is optimized for "
            "transactional consistency, while the read database is optimized for "
            "query performance and flexibility."
        ),
        "when": "One engine handles most writes and another handles reads/search/analytics",
        "example": (
            "DynamoDB handles all CRUD operations (writes + simple reads). "
            "OpenSearch serves as a read-optimized view for full-text search, "
            "faceted filtering, and analytics queries. Data flows from DynamoDB "
            "to OpenSearch via zero-ETL integration."
        ),
    },
    "MATERIALIZED_VIEW": {
        "name": "Materialized View Pattern",
        "description": (
            "Maintain a pre-computed, denormalized copy of data in a secondary "
            "database optimized for specific query patterns. The secondary store "
            "is a read-only projection: all writes go through the primary."
        ),
        "when": "A secondary engine serves read-only queries on data owned by the primary",
        "example": (
            "DynamoDB is the source of truth for forum data. OpenSearch maintains "
            "a materialized view of discussions + users for full-text search. "
            "The view is automatically kept in sync via DynamoDB zero-ETL."
        ),
    },
    "EVENT_SOURCING": {
        "name": "Event-Driven Sync",
        "description": (
            "Use database change streams or event logs to propagate changes "
            "between databases asynchronously. Each database maintains its own "
            "optimized representation of the data."
        ),
        "when": "Multiple engines need the same data in different shapes",
        "example": (
            "DynamoDB Streams captures all item changes. A Lambda function "
            "transforms and routes changes to OpenSearch (for search) and "
            "ElastiCache (for leaderboards). Each consumer shapes the data "
            "for its specific access patterns."
        ),
    },
    "POLYGLOT_PERSISTENCE": {
        "name": "Polyglot Persistence",
        "description": (
            "Use different databases for different bounded contexts within "
            "the application. Each service/module owns its data in the database "
            "best suited for its access patterns."
        ),
        "when": "Different parts of the application have fundamentally different data access needs",
        "example": (
            "User profiles and forum CRUD in DynamoDB (key-value + hierarchical). "
            "Search and discovery in OpenSearch (full-text + relevance). "
            "Real-time leaderboards in ElastiCache (sorted sets + pub/sub)."
        ),
    },
}


def _detect_architectural_patterns(
    assignments: list[dict],
    committed_engines: set[str],
    engine_queries: dict[str, list[dict]],
    write_engines: set[str] | None = None,
) -> list[dict]:
    """Detect which architectural patterns apply to this multi-engine setup.

    ``write_engines`` (#951) is every engine a written table's writes land on,
    from the assignment's write groups (``write_engines_of``). CQRS says the
    primary takes all write operations, so it applies only when the primary is
    the one engine every write lands on. ``None`` (an assignment written before
    write groups existed) keeps the earlier detection.
    """
    patterns: list[dict] = []

    # Get engines that actually have queries after consolidation
    active_engines: dict[str, int] = defaultdict(int)
    for qa in assignments:
        active_engines[qa["assigned_engine"]] += 1

    if len(active_engines) < 2:
        return patterns

    # Find the primary engine (most queries)
    primary = max(active_engines, key=lambda e: active_engines[e])
    secondaries = [e for e in active_engines if e != primary and active_engines[e] > 0]

    # Check for CQRS pattern
    # Primary handles writes, secondary handles reads/search
    if write_engines is None:
        primary_has_writes = any(qa["assigned_engine"] == primary for qa in assignments)
    else:
        primary_has_writes = write_engines == {primary}

    read_secondaries = [e for e in secondaries if e in ("opensearch", "elasticache", "dynamodb")]
    if primary_has_writes and read_secondaries:
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["CQRS"])
        pattern["applies_to"] = {
            "write_engine": primary,
            "read_engines": read_secondaries,
        }
        patterns.append(pattern)

    # Check for Materialized View pattern
    if "opensearch" in secondaries:
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["MATERIALIZED_VIEW"])
        pattern["applies_to"] = {
            "source_engine": primary,
            "view_engine": "opensearch",
        }
        patterns.append(pattern)

    # If 3+ engines, suggest Polyglot Persistence
    if len(active_engines) >= 3:
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["POLYGLOT_PERSISTENCE"])
        pattern["applies_to"] = {
            "engines": list(active_engines.keys()),
        }
        patterns.append(pattern)
    elif len(active_engines) == 2 and primary != "dynamodb":
        # Event sourcing when using non-serverless primary
        pattern = deepcopy(ARCHITECTURAL_PATTERNS["EVENT_SOURCING"])
        pattern["applies_to"] = {
            "source_engine": primary,
            "target_engines": secondaries,
        }
        patterns.append(pattern)

    return patterns


def write_engines_of(
    query_assignments: list[dict], write_groups: dict[str, list[str]] | None
) -> set[str] | None:
    """The engines the in-scope writes land on, from the write groups (table ->
    its write query ids), or ``None`` when the assignment records none (#951).

    ``None`` (the key is missing) means an assignment written before write groups
    existed, and keeps the earlier CQRS detection. ``{}`` means no in-scope query
    writes a table, so no engine takes "all write operations". The contract's
    ``write_groups`` defaults to ``{}`` for an artifact written before 1.17: a path
    that loads the assignment through the model must pass ``None`` for such an
    artifact, never the default. Synthesis and the reality check read the raw dict.
    """
    if write_groups is None:
        return None
    engine_of = {
        str(qa.get("query_id")): str(qa["assigned_engine"])
        for qa in query_assignments
        if qa.get("in_scope", True) and qa.get("assigned_engine")
    }
    return {
        engine_of[str(q)] for qids in write_groups.values() for q in qids if str(q) in engine_of
    }


def patterns_for_assignment(
    query_assignments: list[dict], write_groups: dict[str, list[str]] | None = None
) -> list[dict]:
    """Architectural patterns for the engines that carry in-scope queries.

    ``write_groups`` (the assignment's, table -> write query ids) decides which
    engines take writes (#951): CQRS is named only for a primary that takes them
    all, so the line never contradicts the sources of truth.
    """
    in_scope = [
        qa for qa in query_assignments if qa.get("in_scope", True) and qa.get("assigned_engine")
    ]
    return _detect_architectural_patterns(
        in_scope, set(), {}, write_engines_of(in_scope, write_groups)
    )


def refresh_patterns_and_recommendations(result: dict) -> None:
    """Recompute ``architectural_patterns`` and ``recommendations`` from the final assignment.

    Patterns are first detected before the LLM corrections, the post-correction re-settle,
    the sanity sweep and the customer-override restore, any of which can still move
    queries; the output must describe the assignment it ships with, so no pattern names
    an engine that ends up with no in-scope query (#202). Only in-scope queries count.
    Mutates ``result``.
    """
    patterns = patterns_for_assignment(
        result["revised_assignments"], (result.get("assignment") or {}).get("write_groups")
    )
    result["architectural_patterns"] = patterns
    result["recommendations"] = _build_recommendations(
        result["revised_assignments"],
        result["consolidations"],
        patterns,
        {},
        _engine_infra_cost(result.get("analysis_outputs")),
        result.get("unique_value_assessment"),
        engines_in_use=running_engines(result.get("collector_output")),
    )


def format_pattern_recommendation(p: dict) -> str:
    """Render one architectural pattern as a ``Recommended pattern: ...`` line.

    Shared with synthesis, which regenerates these lines from the effective assignment.
    """
    applies = p.get("applies_to", {})
    if "write_engine" in applies:
        read_names = ", ".join(display_engine(e) for e in applies.get("read_engines", []))
        return (
            f"Recommended pattern: {p['name']}. "
            f"Use {display_engine(applies['write_engine'])} for all write operations and "
            f"{read_names} for specialized reads. "
            f"{p['description']}"
        )
    if "source_engine" in applies and "view_engine" in applies:
        return (
            f"Recommended pattern: {p['name']}. "
            f"{display_engine(applies['source_engine'])} is the source of truth; "
            f"{display_engine(applies['view_engine'])} maintains a search-optimized projection. "
            f"{p['description']}"
        )
    return f"Recommended pattern: {p['name']}. {p['description']}"


def _engine_infra_cost(analysis_outputs: dict[str, dict] | None) -> dict[str, float]:
    """Each engine's own analysed ``cost_estimate.monthly_cost_usd`` (#375 review).

    This is the same real, per-engine infrastructure figure ``tco_analysis``
    builds its ``cost_breakdown`` from (``build_tco_analysis`` in
    synthesis_report.py reads the identical field) -- not
    ``ENGINE_BASE_COST``/``EXTRA_ENGINE_BURDEN_MONTHLY``, which is an internal
    operational-overhead heuristic used only to decide *whether* an engine is
    worth questioning (the mandatory-engine cost-share floor), never a real
    dollar figure to show a customer. An engine missing a cost estimate (or
    missing from ``analysis_outputs`` entirely) is simply absent from the
    returned dict; callers must handle that, not assume every engine has one.
    """
    costs: dict[str, float] = {}
    for engine, artifacts in (analysis_outputs or {}).items():
        monthly = ((artifacts or {}).get("cost_estimate") or {}).get("monthly_cost_usd")
        if isinstance(monthly, (int, float)):
            costs[engine] = float(monthly)
    return costs


def _build_recommendations(
    assignments: list[dict],
    consolidations: list[dict],
    patterns: list[dict],
    original_engine_queries: dict[str, list[dict]],
    engine_infra_cost: dict[str, float] | None = None,
    unique_value_assessment: dict[str, dict] | None = None,
    *,
    engines_in_use: Iterable[str] = (),
) -> list[str]:
    """Build human-readable recommendations.

    ``engine_infra_cost`` (``_engine_infra_cost``'s output) grounds the
    infrastructure-cost figure for a fully-eliminated engine in that engine's
    own analysed cost estimate -- the same figure ``tco_analysis`` is built
    from -- rather than a heuristic never meant to be shown to a customer. A
    review of #375 found a flat ``ENGINE_BASE_COST + EXTRA_ENGINE_BURDEN_MONTHLY``
    placeholder shown as "Saves ~$450/mo", with no entry anywhere in the
    deliverables' own facts to trace it to (OpenSearch's real infrastructure
    cost on that same sample is $240.96/mo, not $450); clearly labeling the
    placeholder as "not the infrastructure cost" in an earlier revision still
    read as ungrounded, since the number itself still appeared nowhere in
    facts. The real figure, when available, replaces it; when an engine has
    no analysed cost estimate, the sentence stays purely qualitative with no
    dollar figure at all, rather than fall back to the placeholder.

    A consolidation whose ``workload_justified`` is ``False`` (#475 review,
    finding 3; set by ``_workload_ever_justified``) never claims a saving at
    all, even when a real cost estimate exists: none of that engine's
    FIRST-ASSIGNMENT queries ever cleared the #326 floor, so its removal is
    not an infrastructure saving -- the engine was never justified by the
    workload to begin with.

    ``unique_value_assessment`` (optional, #502 review round 6, finding 4)
    surfaces any Phase 1 ``tie_unresolved`` pair here too, in the
    human-readable text a customer actually reads -- not just the internal
    ``unique_value_assessment`` dict. A tie is NOT a consolidation (neither
    engine moves, nothing changed), so it gets no ``Consolidation`` record:
    every other record upholds the invariant that the queries it names
    actually moved (``len(moved_queries(c, ...)) == c["query_count"]``,
    enforced in ``tests/unit/agents/referee/test_reality_check_llm_seam.py``),
    and a tie, by definition, moves none.

    ``engines_in_use`` (#949, ``src.shared.engines_in_use``) is what the source runs
    today. Removing an engine outside it -- a candidate the plan analysed and
    dropped -- is never stated as a saving or a retirement: the customer never paid
    for it.
    """
    recs = []
    engine_infra_cost = engine_infra_cost or {}
    running = set(engines_in_use)

    # Consolidation recommendations
    for c in consolidations:
        saved = c.get("saved_cost_estimate", 0)
        source = c["from_engine"]
        # #572: engine_infra_cost (below) stays keyed on the raw id; every
        # customer-facing sentence below names the engine with display_engine.
        source_name = display_engine(source)
        if c.get("action") == "partial":
            # The source engine stays, so no cluster is avoided (#218).
            outcome = (
                f"No operational savings are claimed: this move does not remove {source_name} "
                "from the architecture."
            )
        elif c.get("workload_justified") is False:
            # #475 review, finding 3: none of this engine's FIRST-ASSIGNMENT
            # queries ever cleared the #326 floor (real search depth plus
            # traffic or corpus size) -- it was never justified by the
            # workload, so removing it now is not an infrastructure saving to
            # claim, even though a real cost estimate exists for it.
            outcome = (
                f"No saving is claimed: {source_name} was never justified by the workload. "
                "None of its first-assignment queries clear the real-search-depth "
                "plus traffic-or-result-size floor a standing deployment needs, so no "
                "infrastructure cost was ever justified for it."
            )
        elif source not in running:
            # #949: a candidate the plan considered and dropped; the customer does not
            # run it today, so dropping it saves nothing. The first move says so, later
            # moves of the same engine say it briefly.
            first = next(r for r in consolidations if r["from_engine"] == source)
            outcome = (
                f"No saving is claimed: {source_name} does not run today; it was a "
                "candidate this plan considered and does not recommend."
                if c is first
                else f"{source_name} does not run today, so no saving is claimed."
            )
        elif c.get("removal_saving_claimed", bool(saved)):
            real_cost = engine_infra_cost.get(source)
            siblings = [
                r
                for r in consolidations
                if r["from_engine"] == source and r.get("action") == "full"
            ]
            # #793: the saving belongs to retiring the engine (all its moves). The
            # first move states it, with the figure first (the deck clips a card's
            # detail at 170 characters); later moves refer back and never repeat it.
            if len(siblings) > 1 and c is not siblings[0]:
                outcome = f"Part of retiring {source_name}" + (
                    " (saving counted on its first move)." if real_cost else "."
                )
            elif len(siblings) > 1:
                moves = ", ".join(
                    [
                        f"{_queries(siblings[0]['query_count'])} to "
                        f"{display_engine(siblings[0]['to_engine'])}",
                        *(
                            f"{r['query_count']} to {display_engine(r['to_engine'])}"
                            for r in siblings[1:]
                        ),
                    ]
                )
                if real_cost:
                    outcome = (
                        f"Removes ${real_cost:,.2f}/mo by retiring {source_name} ({moves}), "
                        f"{source_name}'s own analysed infrastructure estimate before removal, "
                        "plus a qualitative reduction in operational overhead from avoiding a "
                        f"dedicated {source_name} cluster: one fewer engine's worth of team "
                        "expertise, monitoring, backups and failover to maintain."
                    )
                else:
                    outcome = (
                        f"Retiring {source_name} ({moves}) avoids running a dedicated "
                        f"{source_name} cluster: one fewer engine's worth of team expertise, "
                        "monitoring, backups and failover to maintain. No specific "
                        f"infrastructure-cost figure is available for {source_name} to show here."
                    )
            elif real_cost:
                # #380 review: name the figure plainly instead of pointing at an
                # internal field name (``tco_analysis``'s ``cost_breakdown``) --
                # the number itself (``engine_infra_cost``, the same one
                # ``build_tco_analysis`` carries into ``eliminated_engine_costs``
                # for this engine) is the only thing a customer needs here.
                outcome = (
                    f"Removes ${real_cost:,.2f}/mo of infrastructure cost ({source_name}'s own "
                    "analysed estimate before removal), plus a qualitative reduction in "
                    f"operational overhead from avoiding a dedicated {source_name} cluster: one "
                    "fewer engine's worth of team expertise, monitoring, backups and failover "
                    "to maintain."
                )
            else:
                outcome = (
                    f"Avoids running a dedicated {source_name} cluster: one fewer engine's "
                    "worth of team expertise, monitoring, backups and failover to maintain. "
                    f"No specific infrastructure-cost figure is available for {source_name} "
                    "to show here."
                )
        else:
            outcome = f"{source_name} leaves the architecture."
        recs.append(
            f"Consolidated {_queries(c['query_count'])} from {source_name} → "
            f"{display_engine(c['to_engine'])}: {c['reason']}. {outcome}"
        )

    # Pattern recommendations
    recs.extend(format_pattern_recommendation(p) for p in patterns)

    # Phase 1 ties (#502 review round 6, finding 4): stated here, not just
    # in unique_value_assessment, since this is the text a customer reads.
    tied_engines = sorted(
        e for e, info in (unique_value_assessment or {}).items() if info.get("tie_unresolved")
    )
    for engine_a, engine_b in itertools.combinations(tied_engines, 2):
        recs.append(
            f"{display_engine(engine_a)} and {display_engine(engine_b)} are tied on "
            "operational cost per unique query, and no rule yet exists to decide between "
            "two such engines, so both are kept rather than one being removed arbitrarily."
        )

    if not recs:
        recs.append("No consolidation opportunities found. The current assignment stays as it is.")

    return recs
