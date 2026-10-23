"""Referee-Synthesis agent handler — produces the modernization report.

Reads all pipeline artifacts (triage, collector, analysis, schema design)
via ArtifactStore, builds a comprehensive report with architecture
recommendations, table mappings, query groups, TCO analysis, and risk
assessment.

The synthesis report is consumed by:
- The UI (results page — architecture view, query groups, table mappings)
- Step Functions (needs_deeper_analysis flag for the analysis loop)

LLM seam functions (for Skill Sync / external LLM integration):
- run_synthesis_deterministic — full report without any LLM call
- prepare_synthesis_llm_input — formats the LLM request payload
- apply_synthesis_llm_output  — merges LLM output into deterministic result
"""

from datetime import UTC, datetime

from src.agents.prompt_framing import with_untrusted_notice
from src.agents.referee.aurora_choice import source_database_engine, source_database_version
from src.agents.referee.capability_registry import triage_query_capabilities
from src.agents.referee.engine_scope import scope_of, scope_unservable_rows
from src.agents.referee.flagged_unservable import flagged_unservable_records
from src.agents.referee.migration_risks import (
    add_migration_risks,
    build_migration_risks,
    last_risk_number,
)
from src.agents.referee.migration_waves import build_migration_waves, wave_table_owners
from src.agents.referee.query_pattern import (
    ACCESS_PATTERNS_FILE,
    fixable_findings,
    redesign_requirements,
)
from src.agents.referee.schema_fields import field_list
from src.agents.referee.summary_input_budget import apply_summary_input_budget, table_query_weights
from src.agents.referee.summary_input_slim import slim_summary_input
from src.agents.referee.synthesis_data import SynthesisData, load_synthesis_data
from src.agents.referee.synthesis_grounding import (
    build_effective_architecture,
    build_fallback_summary,
    check_summary_grounding,
    check_summary_internal_leaks,
    check_summary_risk_counts,
    check_summary_style,
    check_summary_wave_order,
    eliminated_engines,
    engine_table_scope,
    never_justified_engines,
    reality_check_settled_engines,
    recompute_reality_check_patterns,
)
from src.agents.referee.synthesis_report import (
    AURORA_ENGINES,
    build_architecture_recommendation,
    build_cache_overlay,
    build_query_groups,
    build_ranking,
    build_risk_assessment,
    build_summary,
    build_table_mappings,
    build_tco_analysis,
    build_wave_checks,
    designed_and_not_designed_engines,
    generate_executive_summary,
    schema_design_case,
    schema_table_defs,
    source_database_label,
    source_table_count,
    table_normaliser,
)
from src.agents.referee.table_assignments import retained_engine_for
from src.agents.referee.table_resolution import PSEUDO_TABLES, SourceScope, TableNameResolver
from src.agents.referee.write_groups import table_sources, unservable_write_groups
from src.contracts.executive_summary_response import (
    ExecutiveSummaryResponse,
    parse_executive_summary_response,
)
from src.contracts.skipped_placeholders import SkippedSynthesisReport
from src.contracts.synthesis_output import SynthesisOutputContract
from src.shared.bedrock_guardrail import require_valid_guardrail_config
from src.shared.catalog.prices import estimates_price_basis
from src.shared.contract_versioning import stamp_artifact_version
from src.shared.engine_names import display_engine
from src.shared.engines_in_use import engines_in_use
from src.shared.executive_brief import check_summary_brief, executive_brief_text
from src.shared.prose_style import collector_identifiers, replace_dashes
from src.shared.query_citations import QueryCitations, collector_query_ids, named_query_ids
from src.shared.report_facts import (
    assignment_table_usage,
    retired_in_use_engine_facts,
    table_owner_fields,
    wave_retained_engine,
)
from src.storage.artifact_store import ArtifactStore
from src.tools.analysis.relational_gaps import detect_relational_gaps
from src.tools.sql.query_facts import source_engine_of

# ---------------------------------------------------------------------------
# Seam 1: Deterministic — all builders, no LLM
# ---------------------------------------------------------------------------


def _moves_since_reality_check(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    reality_check_output: dict | None,
    assignment: dict | None,
) -> dict[str, dict[str, int]]:
    """Where ``assignment`` put each engine's in-scope queries since the reality check (#215).

    ``{from_engine: {to_engine: count}}``: the queries on each engine in the version the
    reality check left (``reality_check_record_version``: the version it wrote, else
    the one it checked unchanged), against their engine in ``assignment``. A later customer edit's moves,
    so an engine it emptied is described by where the edit put its queries. Empty when
    either version cannot be read.
    """
    from src.storage.assignment_versioning import read_assignment, reality_check_record_version

    rc = reality_check_output or {}
    # A pre-#192 consolidation recorded no output version (source + 1, #193).
    version = reality_check_record_version(rc)
    checked = (
        read_assignment(store, database_name, job_id, version) if isinstance(version, int) else None
    )
    if not checked or not assignment:
        return {}
    now = {
        qa.get("query_id"): qa.get("assigned_engine")
        for qa in assignment.get("query_assignments") or []
        if qa.get("in_scope", True)
    }
    moves: dict[str, dict[str, int]] = {}
    for qa in checked.get("query_assignments") or []:
        src, dst = qa.get("assigned_engine"), now.get(qa.get("query_id"))
        if qa.get("in_scope", True) and src and dst and src != dst:
            moves.setdefault(src, {})
            moves[src][dst] = moves[src].get(dst, 0) + 1
    return moves


def _eliminated_engine_costs(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    eliminated: dict[str, str | None],
) -> dict[str, float]:
    """Each eliminated engine's own analysed infrastructure cost (#380).

    By the time ``build_tco_analysis`` runs, ``data.engines`` has already
    dropped every engine the reality check eliminated (ADR-029 Layer E,
    #202), so their analysis artifacts are read directly here -- the same
    real, per-engine ``cost_estimate.monthly_cost_usd`` figure the reality
    check's own recommendation text quotes (``_engine_infra_cost`` in
    reality_check_patterns.py). This is the only way a saving a recommendation names
    for an eliminated engine (e.g. "$271.80/mo") is also traceable in the TCO
    facts the report publishes, not just in prose. An engine missing an
    analysis artifact, or missing a cost estimate within it, is simply absent
    from the returned dict -- callers must handle that, not assume every
    eliminated engine has one.
    """
    costs: dict[str, float] = {}
    for engine in eliminated:
        key = f"{database_name}/{job_id}/analysis-{engine}/analysis.json"
        if not store.exists(key):
            continue
        analysis = store.read_json(key) or {}
        monthly = (analysis.get("cost_estimate") or {}).get("monthly_cost_usd")
        if isinstance(monthly, (int, float)):
            costs[engine] = float(monthly)
    return costs


def run_synthesis_deterministic(
    job_id: str,
    database_name: str,
    store: ArtifactStore,
    assignment_version: int = 0,
) -> dict:
    """Run all deterministic synthesis logic without invoking any LLM.

    Loads all pipeline artifacts, calls all deterministic builders, and
    sets ``executive_summary`` to the deterministic summary (fallback value).

    Returns a dict with keys:
        job_id, database_name, timestamp, needs_deeper_analysis,
        ranking, table_mappings, query_groups, tco_analysis, risk_assessment,
        architecture, trade_offs, assignment_summary, reality_check_summary,
        cache_overlay, migration_waves (the incremental roadmap, #225),
        flagged_unservable (#888), summary (deterministic), executive_summary
        (deterministic fallback),
        data (internal SynthesisData — for use by apply_synthesis_llm_output)
    """
    print("[synthesis] Loading all pipeline artifacts...")
    data = load_synthesis_data(store, job_id, database_name, assignment_version)
    print(
        f"[synthesis] Loaded: {len(data.engines)} engines, "
        f"{len(data.source_tables)} source tables, "
        f"{len(data.source_queries)} queries"
    )

    # Load reality check output if available (optional — may not exist)
    # The run this assignment descends from (#193): a forced run that changed
    # nothing never replaces the consolidation the assignment carries.
    from src.storage.assignment_versioning import read_reality_check_output

    reality_check_output = read_reality_check_output(
        store, database_name, job_id, assignment_version
    )
    if reality_check_output is not None:
        print(
            f"[synthesis] Reality check loaded: "
            f"{len(reality_check_output.get('consolidations', []))} consolidations, "
            f"{len(reality_check_output.get('architectural_patterns', []))} patterns"
        )

    print("[synthesis] Building ranking...")
    ranking = build_ranking(data)
    print("[synthesis] Building table mappings...")
    table_mappings = build_table_mappings(data)
    print("[synthesis] Building query groups...")
    query_groups = build_query_groups(data)
    # Engines the reality check removed from the effective assignment, mapped to the
    # engine that absorbed them.
    # Every target recommendation below is grounded in the effective set (#202).
    effective = {
        engine
        for qa in (data.assignment or {}).get("query_assignments", [])
        if qa.get("in_scope", True)
        # the cache layer is part of the architecture while it fronts a query (#296)
        for engine in (qa.get("assigned_engine"), qa.get("cache_engine"))
        if engine
    } or set(data.engines)
    eliminated = eliminated_engines(
        effective,
        reality_check_output,
        _moves_since_reality_check(
            store, database_name, job_id, reality_check_output, data.assignment
        ),
    )
    print("[synthesis] Building TCO analysis...")
    # #380: a retired engine's own analysed cost, read directly -- ``data.engines``
    # has already dropped it (ADR-029 Layer E, #202) by this point, so a dollar figure
    # a recommendation quotes for it is also traceable in the TCO facts, not just prose.
    # #475 review, finding 3: an engine none of whose first-assignment queries ever
    # cleared the #326 floor was never justified by the workload, so its removal is
    # not a "saving" to carry into the TCO facts -- excluded here, the same way
    # ``_build_recommendations`` skips the dollar figure in prose. Only ``retired_
    # costs`` is filtered: ``eliminated`` itself still names the engine for every
    # other consumer (risk assessment, the deliverables' "eliminated_engines" field).
    never_justified = never_justified_engines(reality_check_output)
    # #949: only an engine the source runs today is a saving when the plan retires it.
    # Every other eliminated engine was a candidate the plan considered and dropped:
    # the customer never paid for it, so it is listed with no dollar figure.
    running = engines_in_use(data.collector)
    retired_costs = _eliminated_engine_costs(
        store,
        database_name,
        job_id,
        {e: a for e, a in eliminated.items() if e in running and e not in never_justified},
    )
    considered = [
        {"engine": e, "absorbed_by": a} for e, a in sorted(eliminated.items()) if e not in running
    ]
    tco = build_tco_analysis(data, retired_costs=retired_costs)
    # #1195: a kept engine's ranking entry states its TCO figure (sized for its assigned
    # queries), never the whole-collection analysis estimate, so every cost agrees.
    sized = {
        str(row["database"]): row["monthly_cost_usd"]
        for row in tco.get("cost_breakdown") or []
        if isinstance(row, dict) and isinstance(row.get("monthly_cost_usd"), (int, float))
    }
    for entry in ranking:
        if entry.get("target") in sized:
            entry["monthly_cost_usd"] = sized[str(entry["target"])]
    # Per-engine table scope of the effective assignment, for the summary LLM input and
    # the summary post-check (#205).
    known_tables: list[str] = sorted(
        {str(t["table_id"]) for t in data.source_tables if t.get("table_id")}
        | {m["source_table"] for m in table_mappings}
    )
    engine_tables = engine_table_scope(
        data.assignment, data.source_queries, table_mappings, known_tables
    )
    # #1231: the queries routing scored after redesign, each with the engine serving it,
    # read before the risks: the risk register raises the ones whose engine's plan relies
    # on the redesign (``design.redesign``), and the anti-pattern risks leave out every
    # finding a redesign removes.
    redesign_requirements = _redesign_requirements(
        store, database_name, job_id, assignment_version, data.assignment
    )
    print("[synthesis] Building risk assessment...")
    risk_assessment = build_risk_assessment(
        data, eliminated, lifted=_lifted_findings(store, database_name, job_id, assignment_version)
    )
    print("[synthesis] Building architecture recommendation...")
    architecture = build_architecture_recommendation(data, ranking, table_mappings)
    # Intentionally the analysis average (confidence_score), not routed_confidence
    # (#152): this flag asks for a deeper *analysis* of an engine, a property of the
    # analysis over its tables, and it is computed whether or not an assignment
    # exists. The routed fit judges the assigned work and drives the deliverables.
    needs_deeper = any(
        40 <= r["confidence_score"] < 70 and r.get("migration_complexity_avg") == "HIGH"
        for r in ranking
    )
    trade_offs = _collect_trade_offs(data)

    print("[synthesis] Building migration waves...")
    cache_overlay = build_cache_overlay(data)
    assignment = data.assignment or {}
    raw_table_assignments = assignment.get("table_assignments") or []
    # #225: a wave is scoped to tables *and views* the collector saw. ``known_tables``
    # itself stays table-only (unchanged) so the summary grounding check (#205) and
    # engine_table_scope above are unaffected -- views are a wave-only concern, added
    # to a copy used only here.
    view_ids = {
        str(v["view_id"])
        for v in (data.collector.get("database_schema", {}) or {}).get("views") or []
        if v.get("view_id")
    }
    wave_known_tables = set(known_tables) | view_ids
    # #380 review: wave 1 still carries every table/view in ``wave_known_tables``
    # (never narrowed to what some engine's schema design mapped -- that silently
    # dropped real, unmapped application tables). ``mapped_table_ids`` is used only
    # to add an explanatory sentence to wave 1's rationale, reconciling its full
    # count against the migration map's smaller "mapped" total; ``None`` (not an
    # empty set) when no schema design ran for any engine (e.g. ``--llm-mode
    # none``), so there is nothing to reconcile against yet and no sentence is added.
    mapped_table_ids = {m["source_table"] for m in table_mappings if m.get("source_table")}
    migration_waves = build_migration_waves(
        ranking=ranking,
        table_assignments=raw_table_assignments,
        query_assignments=assignment.get("query_assignments") or [],
        co_dependency_groups=assignment.get("co_dependency_groups") or [],
        cache_overlay=cache_overlay,
        source_engine=source_database_engine(data.collector),
        source=SourceScope.from_collector(data.collector),
        # #225: scope waves to tables/views the collector actually saw, so a parser
        # artifact (CTE alias, keyword, system catalog name) never reaches a wave.
        # The upstream noise in table_assignments itself is #316.
        known_tables=wave_known_tables,
        # #321: named in wave 1's homogeneity statement when the collector
        # reported it; never invented when it didn't.
        source_version=source_database_version(data.collector),
        mapped_tables=mapped_table_ids or None,
        # #381: present only for a heterogeneous source where the resolver picked one
        # of the two competing Aurora engines; used only for wave 1's cross-engine
        # rationale (totals, deciding features). ``None`` for every other assignment,
        # including one written before this field existed.
        aurora_engine_choice=assignment.get("aurora_engine_choice"),
        # #1329: a scope with no Aurora engine gives wave 1 no relational target.
        engine_scope=scope_of(data.triage),
    )
    # #339: the per-query decision traces from the move ledger (the close calls
    # and the queries no engine fits), read before the risks that cite them.
    query_traces = (
        _query_traces(
            store,
            database_name,
            job_id,
            data.assignment,
            assignment_version,
            data.collector,
            data.triage,
        )
        if data.assignment and assignment_version > 0
        else None
    )
    # #992: each written table's source of truth and replicas, from the
    # assignment's write groups (None for an assignment written before them).
    # #1329: a table whose every write has no engine in the scope keeps the source
    # database as its writer, with a replica on each engine that reads it.
    sources = (
        table_sources(data.assignment, _unservable_writes(data))
        if "write_groups" in (data.assignment or {})
        else None
    )
    # #888: the flagged queries no kept engine can take (a reality-check flag
    # it kept, a post-schema router terminal query), which the ledger never sees;
    # read before the risks, which raise them (#1148).
    router_key = f"{database_name}/{job_id}/post-schema-router/router_output.json"
    flagged_unservable = flagged_unservable_records(
        data.assignment,
        reality_check_output,
        store.read_json(router_key) if store.exists(router_key) else None,
        triage_query_capabilities(data.triage),
    )

    # #1329: the queries no engine in the customer's engine scope can serve (no
    # engine, no wave, no design), read before the risks, which raise them.
    scope_unservable = scope_unservable_rows(
        data.assignment, triage_query_capabilities(data.triage)
    )
    # #471: the relational move's gaps, read before the risks, which raise them (#1149).
    relational_gaps = _relational_gaps(data)
    # #950: the risks the migration's shape carries (tables kept in sync, close
    # calls, pending ties, design trade-offs, ...), from the facts built above,
    # before the summary states the risk count.
    add_migration_risks(
        risk_assessment,
        build_migration_risks(
            migration_waves=migration_waves,
            query_assignments=assignment.get("query_assignments") or [],
            source_queries=data.source_queries,
            cache_overlay=cache_overlay,
            query_traces=query_traces,
            tied_queries=_tied_queries(data.assignment),
            schema_designs={e: a.schema_design or {} for e, a in data.engines.items()},
            trade_offs=trade_offs,
            cache_only_engines=_cache_only_engines(assignment),
            trigger_tables=_trigger_tables(data.collector),
            normalise=table_normaliser(data),
            table_sources=sources,
            flagged_unservable=flagged_unservable,
            relational_gaps=relational_gaps,
            scope_unservable=scope_unservable,
            redesign_requirements=redesign_requirements,
            carried_writes=_carried_writes(store, database_name, job_id, data),
            start_id=last_risk_number(risk_assessment.get("risks") or []),
            source_engine=source_engine_of(data.collector),
        ),
    )
    # #1076: the removed engines the customer runs today, from the one function the facts
    # block uses, so the summary states them as the deliverables do.
    retired_in_use = retired_in_use_engine_facts(
        {
            "engines_in_use": sorted(running),
            "tco_analysis": tco,
            "ranking": ranking,
            "reality_check": reality_check_output or {},
        },
        [{"engine": e, "moved_to": [a] if a else []} for e, a in sorted(eliminated.items())],
    )
    deterministic_summary = build_summary(
        data,
        ranking,
        table_mappings,
        tco,
        risk_assessment,
        query_groups,
        eliminated,
        reality_check_settled=reality_check_settled_engines(reality_check_output),
        retired_in_use=retired_in_use,
    )
    # #645: the post-migration checks come from the waves, built just above.
    risk_assessment["mitigation_strategies"] = [
        *risk_assessment.get("mitigation_strategies", []),
        *build_wave_checks(
            migration_waves,
            query_groups,
            assignment.get("query_assignments") or [],
            cache_overlay,
        ),
    ]
    # #316: an assignment produced by this fix already resolved source_tables
    # noise against the collector's canonical schema (CTE aliases, system
    # catalogs, sequences, keywords, columns the SQL parser mistook for
    # tables -- and spelling mismatches between the parser and the collector,
    # via the same canonical resolver the migration-waves builder above
    # uses) and recorded the drop in unresolved_table_names. Copy it
    # unchanged rather than recompute a second, looser version here. An
    # assignment written before this fix has no such field at all -- it is
    # absent, not an empty default -- so it is recomputed the same way, with
    # the same canonical resolver, scoped to wave_known_tables (the same set
    # build_migration_waves above was given), rather than silently reporting
    # zero for every job that predates this fix.
    if "unresolved_table_names" in assignment:
        unresolved_names = assignment.get("unresolved_table_names") or {"count": 0, "names": []}
    else:
        legacy_resolver = TableNameResolver.from_known_ids(wave_known_tables)
        _unresolved = sorted(
            {
                str(t["table_id"])
                for t in raw_table_assignments
                if t.get("table_id")
                and str(t["table_id"]) not in PSEUDO_TABLES
                and (legacy_resolver is None or legacy_resolver.resolve(str(t["table_id"])) is None)
            }
        )
        unresolved_names = {"count": len(_unresolved), "names": _unresolved}

    assignment_summary = None
    if data.assignment:
        assignment_summary = {
            "version": data.assignment.get("version"),
            "status": data.assignment.get("status"),
            "source": data.assignment.get("source"),
            "query_count": len(data.assignment.get("query_assignments", [])),
            "in_scope_count": sum(
                1 for qa in data.assignment.get("query_assignments", []) if qa.get("in_scope", True)
            ),
            "co_dependency_groups": len(data.assignment.get("co_dependency_groups", [])),
            # #503 review item 1: a per-query exact-score tie no ADR-031 rule
            # decided, counted here so the approval gate and the deliverables
            # can show it without re-walking query_assignments themselves.
            "tie_unresolved_count": sum(
                1 for qa in data.assignment.get("query_assignments", []) if qa.get("tied_engines")
            ),
            # #381 review: the only way this choice reached report.json before was
            # buried inside wave 1's rationale text -- carry the structured decision
            # itself so the decision/engineering reports and the UI can all point at
            # one place for it.
            "aurora_engine_choice": data.assignment.get("aurora_engine_choice"),
        }

    # Merge reality check data into trade_offs for visibility
    reality_check_summary = None
    if reality_check_output:
        reality_check_summary = {
            # #861: each with the queries it moved and those an edit put back.
            "consolidations": consolidation_query_moves(
                store, database_name, job_id, reality_check_output, data.assignment
            ),
            "architectural_patterns": reality_check_output.get("architectural_patterns", []),
            "recommendations": reality_check_output.get("recommendations", []),
            "before_distribution": reality_check_output.get("before_distribution", {}),
            "after_distribution": reality_check_output.get("after_distribution", {}),
            # #589: the sanity sweep's own kept/removed verdict for every tiny
            # orphan engine it evaluated -- carried through verbatim so the UI
            # and any other report.json reader can show the full record, not
            # just the trade-off text derived from it below.
            "sweep_decisions": reality_check_output.get("sweep_decisions", []),
            # #746: the consolidation-review batches that got no model verdict
            # (their moves stand unreviewed), for facts.unreviewed_move_count.
            "validation_incomplete": reality_check_output.get("validation_incomplete", []),
        }
        reality_check_summary = recompute_reality_check_patterns(
            reality_check_summary,
            (data.assignment or {}).get("query_assignments", []),
            (data.assignment or {}).get("write_groups"),
        )
        for rec in reality_check_summary["recommendations"]:
            rec_desc = rec if isinstance(rec, str) else str(rec)
            if not any(t.get("description") == rec_desc for t in trade_offs):
                trade_offs.append(
                    {
                        "description": rec_desc,
                        "impact": rec_desc,
                        "source_tables": [],
                        "target_tables": [],
                        "query_ids": [],
                        "engine": "reality-check",
                    }
                )
        # #589: a KEPT engine never produces a consolidation or recommendation
        # (nothing moved), so without this it left no trace in report.json at
        # all -- the "Migration trade-offs" sections of the engineering report
        # and the PDF report only ever learn about a sanity sweep decision
        # through this list.
        for sd in reality_check_summary["sweep_decisions"]:
            if sd.get("decision") != "kept":
                continue
            desc = f"{display_engine(sd.get('engine', ''))} kept: {sd.get('reason', '')}"
            if any(t.get("description") == desc for t in trade_offs):
                continue
            trade_offs.append(
                {
                    "description": desc,
                    "impact": desc,
                    "source_tables": [],
                    "target_tables": [],
                    "query_ids": list(sd.get("blocking_query_ids") or []),
                    "engine": "reality-check",
                }
            )

    result = {
        "job_id": job_id,
        "database_name": database_name,
        # The job's own source engine, as the executive summary names it (#628):
        # data, so the shared skill never carries a hardcoded engine list.
        "source_database": source_database_label(source_database_engine(data.collector)),
        "timestamp": datetime.now(UTC).isoformat(),
        "needs_deeper_analysis": needs_deeper,
        "ranking": ranking,
        "table_mappings": table_mappings,
        "query_groups": query_groups,
        "tco_analysis": tco,
        "risk_assessment": risk_assessment,
        "architecture": architecture,
        "trade_offs": trade_offs,
        "assignment_summary": assignment_summary,
        "reality_check_summary": reality_check_summary,
        "flagged_unservable": flagged_unservable,
        "scope_unservable": scope_unservable,
        "relational_gaps": relational_gaps,
        "eliminated_engines": eliminated,
        "considered_not_recommended": considered,
        "engines_in_use": sorted(running),
        "cache_overlay": cache_overlay,
        "migration_waves": migration_waves,
        # #212: each table's owner as the wave plan states it, for the map's owner lines.
        "wave_table_owners": wave_table_owners(
            ranking=ranking,
            table_assignments=raw_table_assignments,
            query_assignments=assignment.get("query_assignments") or [],
            source_engine=source_database_engine(data.collector),
            known_tables=wave_known_tables,
            aurora_engine_choice=assignment.get("aurora_engine_choice"),
            source=SourceScope.from_collector(data.collector),
            engine_scope=scope_of(data.triage),
        ),
        "query_traces": query_traces,
        "table_sources": sources,
        "unresolved_names": unresolved_names,
        "known_tables": known_tables,
        "engine_tables": engine_tables,
        "effective_architecture": build_effective_architecture(
            engine_tables, table_mappings, ranking, query_groups, database_name, eliminated
        ),
        "summary": deterministic_summary,
        "executive_summary": deterministic_summary,  # fallback; overwritten by LLM
        # Internal — holds the SynthesisData object for schema summaries in the writer
        "data": data,
    }
    # #1403: the facts the report will carry, computed now so the executive summary
    # (deterministic, the model's input and its checks) words them, never recomputes them.
    # The writer computes them again from the same fields.
    result["facts"] = _report_output_data(store, result, assignment_version)["facts"]
    result["executive_summary"] = (
        executive_brief_text(result["facts"], database_name) or deterministic_summary
    )
    return result


# ---------------------------------------------------------------------------
# Seam 2: Format LLM input payload
# ---------------------------------------------------------------------------


def _compact_query_groups_for_llm(query_groups: list[dict]) -> list[dict]:
    """Compact ``query_groups`` for the executive-summary LLM input (#478).

    ``effective_architecture`` is already the model's primary per-engine view
    (compact tables, top query groups by name, capabilities); the full
    ``query_groups`` embeds every access pattern's description and every
    source query's text/tables verbatim, which the model does not reason
    over for the summary and which grows with every query a group carries --
    on a large workload (and now that a relational engine's queries get
    their own groups too, #478) that is most of the input. Each group here
    is name, engines, tables and how many distinct queries it carries --
    enough to describe a group by name, nothing per-query.

    No single combined "calls per second": a group two
    engines share (say a cache layer and the relational engine reading the
    same source table) mixes each engine's own metric under one number --
    DynamoDB's ``design_rps`` already includes peak and growth factors from
    its own analysis, while a relational engine's is the collector's raw
    measured rate, so summing them invited the model to rank throughput
    across engines that are not on the same scale. ``calls_per_second_by_
    engine`` keeps each engine's own figure separate instead.
    """
    compact = []
    for g in query_groups:
        aps = g.get("access_patterns") or []
        engines = sorted({e for e in (g.get("engines") or []) if e})
        # Aurora's access_pattern entries carry no table_name (redundant
        # with this group's own name, since tables carry
        # over 1:1) -- fall back to the group name's own (sorted,
        # comma-joined) source tables when no entry has one.
        tables = sorted({ap.get("table_name") for ap in aps if ap.get("table_name")})
        if not tables and g.get("group_name"):
            tables = [t for t in g["group_name"].split(", ") if t]
        query_ids: set[str] = set()
        calls_by_engine: dict[str, float] = {}
        for ap in aps:
            query_ids.update(ap.get("query_ids") or [])
            eng = ap.get("engine")
            if eng:
                calls_by_engine[eng] = calls_by_engine.get(eng, 0) + (ap.get("design_rps") or 0)
        compact.append(
            {
                "group_name": g.get("group_name"),
                "engines": engines,
                "tables": tables,
                "query_count": len(query_ids),
                "calls_per_second_by_engine": {
                    e: round(v, 1) for e, v in sorted(calls_by_engine.items())
                },
            }
        )
    return compact


# The engine figures the executive summary states (#1403): its share, its cache reads,
# its cost. The rest of each engine's facts is engineering detail.
_BRIEF_ENGINE_KEEP = (
    "engine",
    "role",
    "workload_percent",
    "cached_queries",
    "cached_call_share_percent",
    "monthly_cost_usd",
)


def _executive_brief_input(facts: dict) -> dict:
    """The facts the executive summary's three parts state (#1403), copied as is:
    the projected cost with its price basis, each engine's share, the waves, the risk
    level and its drivers, the wave to approve, whether any saving may be claimed."""
    return {
        "projected_monthly_cost_usd": facts.get("projected_monthly_cost_usd"),
        "price_list": facts.get("price_list"),
        "engine_count": facts.get("engine_count"),
        "engines": [
            {k: e.get(k) for k in _BRIEF_ENGINE_KEEP}
            for e in facts.get("engines") or []
            if isinstance(e, dict)
        ],
        "retired_in_use_engines": facts.get("retired_in_use_engines") or [],
        "open_risk_count": facts.get("open_risk_count"),
        "open_risks_by_severity": facts.get("open_risks_by_severity"),
        **(facts.get("executive_brief") or {}),
    }


def prepare_synthesis_llm_input(deterministic_result: dict) -> dict:
    """Return the payload that should be sent to the executive-summary LLM.

    Keys returned:
        executive_brief, effective_architecture, deterministic_summary, ranking,
        query_groups, tco_analysis, risk_assessment, table_mappings, trade_offs,
        migration_waves, source_database, schema_design_status

    ``executive_brief`` (#1403) is the facts the summary's three parts state, copied
    from the result's ``facts``: the projected cost and its price basis, each engine's
    share, what each wave does, the overall risk level and the risks driving it, the
    wave to approve, and whether any saving may be claimed. ``deterministic_summary``
    is the deterministic summary in that structure (``executive_brief_text``).

    ``effective_architecture`` comes first: the compact per-engine table list, top
    query groups and capabilities, plus the eliminated engines, together with the
    rule that every engine/table claim must match it (#205).

    ``migration_waves`` (#225) is passed in as read-only facts:
    the sequence is one deterministic rule, never a model's choice, but a
    narrative that silently assumes a different sequence than the one synthesis
    already computed would be ungrounded. The LLM may explain a wave; it does
    not get to invent one.

    ``schema_design_status`` (#132 review, 370-2) names which engines have a
    schema design and which don't (``designed_and_not_designed_engines``, the
    same split ``generate_executive_summary`` uses for the Bedrock path): the
    external (Claude Code) path builds its own prompt from this payload, so it
    needs the same signal, or it narrates a design for an engine that never
    got one exactly the way the Bedrock prompt used to (#132). Its ``case``
    names the ``Schema Design Status`` case of ``src/skills/executive-summary.md``
    that applies, the same one the Bedrock prompt uses (#628).

    The payload stays within a fixed token budget for any workload size
    (#762, ``summary_input_budget``): over it, the growing lists are cut
    deterministically, every cut list keeps its full length as
    ``<field>_total``, and the payload gains ``input_budget`` saying what was
    cut (absent when nothing was). Applied here, to the one payload both modes
    read, so payload parity holds; scalars, ranking, costs and every risk entry
    are never cut.
    """
    designed, not_designed = designed_and_not_designed_engines(deterministic_result["ranking"])
    facts = deterministic_result.get("facts")
    payload = {
        # #1403: the facts the summary's recommendation, risk and decision state.
        **({"executive_brief": _executive_brief_input(facts)} if facts else {}),
        "effective_architecture": deterministic_result["effective_architecture"],
        "deterministic_summary": (
            executive_brief_text(facts, deterministic_result.get("database_name", ""))
            or deterministic_result["summary"]
        ),
        "ranking": deterministic_result["ranking"],
        "query_groups": _compact_query_groups_for_llm(deterministic_result["query_groups"]),
        "tco_analysis": deterministic_result["tco_analysis"],
        "risk_assessment": deterministic_result["risk_assessment"],
        "table_mappings": deterministic_result["table_mappings"],
        "trade_offs": deterministic_result["trade_offs"],
        "migration_waves": deterministic_result.get("migration_waves"),
        "source_database": deterministic_result.get("source_database", ""),
        "schema_design_status": {
            "case": schema_design_case(designed, not_designed),
            "designed": designed,
            "not_designed": not_designed,
        },
    }
    # Tables are ranked by the in-scope queries touching them across every
    # engine's assignment, not only the request's (relational) query groups.
    data = deterministic_result.get("data")
    assignment = getattr(data, "assignment", None)
    weights = table_query_weights(assignment.get("query_assignments") or []) if assignment else None
    return apply_summary_input_budget(slim_summary_input(payload), table_weights=weights)


# ---------------------------------------------------------------------------
# Seam 3: Apply LLM output back onto the deterministic result
# ---------------------------------------------------------------------------


def apply_synthesis_llm_output(
    deterministic_result: dict, llm_output: dict | ExecutiveSummaryResponse
) -> dict:
    """Merge the LLM-generated executive summary into the deterministic result.

    ``llm_output`` is the model's answer in either LLM mode: the headless
    ``llm_response.json`` or the Bedrock structured output, validated against
    :class:`~src.contracts.executive_summary_response.ExecutiveSummaryResponse`
    here, so both modes go through the same contract and the same checks (#628).
    A dict without ``executive_summary`` changes nothing.

    If ``llm_output`` contains an ``executive_summary`` it is post-checked against the
    effective assignment (``check_summary_grounding``), against this codebase's own
    internal field names (``check_summary_internal_leaks``), against the deterministic
    wave order (``check_summary_wave_order``, #393), and against ``risk_assessment``'s
    own risk counts and severities (``check_summary_risk_counts``, #536). It is used
    unless a check finds a high-confidence problem (a table named under an engine none
    of whose in-scope queries touch it, an internal field name, a stated migration
    order that contradicts ``migration_waves``, or a stated risk count/severity that
    contradicts ``risk_assessment``); lower-confidence findings are only recorded. On
    rejection: ``executive_summary`` stays
    deterministic, and the LLM text plus the warnings are kept for audit in
    ``summary_llm`` / ``summary_validation_warnings`` (#205). ``summary_source``
    records which one the customer sees: ``llm`` or ``deterministic_fallback``.

    Returns the updated result dict (mutates and returns the same dict).
    """
    if isinstance(llm_output, dict) and "executive_summary" not in llm_output:
        return deterministic_result
    # The one response contract both modes share (#628); raises
    # pydantic.ValidationError when the answer is off it.
    response = parse_executive_summary_response(llm_output)
    # #1034: dashes are replaced mechanically; every check below reads the
    # cleaned text, and the customer sees it when nothing rejects it.
    # ``summary_llm`` keeps the text as received, for audit.
    raw_summary = response.executive_summary
    # #1321: every str field of the response cites a query by its report label,
    # never by a bare id.
    llm_summary = _cite_queries(deterministic_result, response).executive_summary
    identifiers = collector_identifiers(
        getattr(deterministic_result.get("data"), "collector", None)
    ) | set(deterministic_result.get("known_tables") or [])
    summary_text = replace_dashes(str(llm_summary or ""), identifiers)
    engine_tables = deterministic_result.get("engine_tables")
    if engine_tables is None:
        engine_tables = engine_table_scope(
            None, [], deterministic_result.get("table_mappings") or [], set()
        )
    findings = check_summary_grounding(
        summary_text,
        engine_tables,
        deterministic_result.get("database_name", ""),
        deterministic_result.get("known_tables"),
    )
    # #380: a second, independent check -- an internal field name leaked into the
    # summary is always high-confidence (there is no low-stakes reading of it),
    # regardless of whether any table/engine attribution also happens to be wrong.
    findings = findings + check_summary_internal_leaks(summary_text)
    # #393: a third, independent check -- a summary that states a migration order
    # ("X first, then Y") contradicting the deterministic ``migration_waves`` sequence
    # is always high-confidence, the same way the wave order itself is never a model's
    # choice (``prepare_synthesis_llm_input``'s own docstring).
    findings = findings + check_summary_wave_order(
        summary_text, deterministic_result.get("migration_waves")
    )
    # #536: a fourth, independent check -- a stated risk count or severity
    # ("two HIGH-severity risks") that contradicts risk_assessment.risks is always
    # high-confidence, the same way a wrong migration order is: it is a fact a
    # model never gets to restate incorrectly.
    findings = findings + check_summary_risk_counts(
        summary_text, deterministic_result.get("risk_assessment")
    )
    # #1034: a fifth, independent check -- any item of the plain-prose style list
    # left after the dash replacement (filler, hedges, "not just ... but",
    # all-caps severity labels) sends the summary back to the deterministic text.
    findings = findings + check_summary_style(summary_text, identifiers)
    # #1403: a sixth -- the executive structure: the projected cost as the facts state it,
    # the overall risk level, a decision sentence, no saving claimed without a source
    # cost, and the word budget.
    facts = deterministic_result.get("facts")
    findings = findings + check_summary_brief(summary_text, facts)
    deterministic_result["summary_llm"] = raw_summary
    deterministic_result["summary_validation_warnings"] = [f["message"] for f in findings]
    for f in findings:
        print(f"[synthesis] WARNING: {f['message']}")
    if any(f["high_confidence"] for f in findings):
        # The customer sees the deterministic executive summary (#1403), worded from the
        # facts; a result with no facts (an older caller) gets the short narrative built
        # from the effective architecture. The raw deterministic note stays in
        # summary_deterministic.
        deterministic_result["executive_summary"] = (
            executive_brief_text(facts, deterministic_result.get("database_name", ""))
            or build_fallback_summary(deterministic_result.get("effective_architecture"))
            or deterministic_result["summary"]
        )
        deterministic_result["summary_source"] = "deterministic_fallback"
    else:
        deterministic_result["executive_summary"] = summary_text
        deterministic_result["summary_source"] = "llm"
    return deterministic_result


def _cite_queries(
    deterministic_result: dict, response: ExecutiveSummaryResponse
) -> ExecutiveSummaryResponse:
    """``response`` (validated) with every bare id of a job's query, in any of its
    str fields, replaced by the query's report label (``src.shared.query_citations``,
    #1321). The job's queries are the collection's and the ones the result names."""
    collector = getattr(deterministic_result.get("data"), "collector", None)
    named = named_query_ids({k: v for k, v in deterministic_result.items() if k != "data"})
    citations = QueryCitations(collector_query_ids(collector) | named)
    if not citations:
        return response
    return citations.fields(response)


# ---------------------------------------------------------------------------
# Writer helper — shared by run_synthesis
# ---------------------------------------------------------------------------


def consolidation_query_moves(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    reality_check_output: dict,
    effective: dict | None,
) -> list[dict]:
    """The reality check's consolidations, each with the queries it moved (#861).

    ``moved_query_ids``: the queries on ``from_engine`` in the version the
    check read and on ``to_engine`` in the version it wrote.
    ``moved_back_query_ids``: those of them the effective assignment has put
    back on ``from_engine`` since (a later edit). Copies; a consolidation is
    returned as it is when the check wrote no revision or a version is
    missing.
    """
    from src.storage.assignment_versioning import assignment_artifact_path

    records = [dict(c) for c in reality_check_output.get("consolidations") or []]
    source = reality_check_output.get("source_assignment_version")
    output = reality_check_output.get("output_assignment_version")
    if not (isinstance(source, int) and isinstance(output, int)):
        return records
    try:
        before = _engine_of(
            store.read_json(assignment_artifact_path(database_name, job_id, source))
        )
        after = _engine_of(store.read_json(assignment_artifact_path(database_name, job_id, output)))
    except Exception:  # noqa: BLE001 -- a missing version leaves the records as they are
        return records
    now = _engine_of(effective)
    for record in records:
        src, dst = record.get("from_engine"), record.get("to_engine")
        moved = sorted(q for q, e in after.items() if e == dst and before.get(q) == src)
        record["moved_query_ids"] = moved
        record["moved_back_query_ids"] = [q for q in moved if now.get(q) == src]
    return records


def _engine_of(assignment: dict | None) -> dict[str, str]:
    return {
        qa["query_id"]: qa["assigned_engine"]
        for qa in (assignment or {}).get("query_assignments", [])
    }


def _query_traces(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    assignment: dict,
    version: int,
    collector: dict | None = None,
    triage: dict | None = None,
) -> dict:
    """``report.json``'s ``query_traces`` (#339): the effective lineage's decision traces.

    The collected SQL of each query gives its listing an excerpt to recognise it by;
    triage's per-query capabilities say what a query on a fallback engine needs (#729).
    """
    from src.shared.query_trace import query_trace_digest
    from src.storage.assignment_versioning import assignment_artifact_path

    def _read(v: int) -> dict | None:
        if v == version:
            return assignment
        try:
            return store.read_json(assignment_artifact_path(database_name, job_id, v))
        except Exception:  # noqa: BLE001 -- a missing version ends the lineage
            return None

    patterns = ((collector or {}).get("queries") or {}).get("query_patterns") or []
    texts = {
        str(q["query_id"]): str(q.get("query_text") or "")
        for q in patterns
        if isinstance(q, dict) and q.get("query_id")
    }
    capabilities = triage_query_capabilities(triage)
    digest = query_trace_digest(
        assignment,
        _read,
        version,
        texts,
        capabilities,
        _key_candidates(store, database_name, job_id, version),
    )
    return digest.model_dump(mode="json", by_alias=True)


def _key_candidates(
    store: ArtifactStore, database_name: str, job_id: str, version: int
) -> dict[str, list[dict]]:
    """``query_id -> candidate_keys`` from assignment ``v<version>``'s access-pattern
    record (#1361); none without a version or a record (one written before #1361)."""
    if version <= 0:
        return {}
    key = f"{database_name}/{job_id}/assignment/v{version}/{ACCESS_PATTERNS_FILE}"
    if not store.exists(key):
        return {}
    from src.shared.query_trace import candidate_keys_by_query

    return candidate_keys_by_query(store.read_json(key))


def _query_pattern_groups(data: SynthesisData) -> list[dict] | None:
    """``report.json``'s ``query_pattern_groups`` (#1146): the effective assignment's
    in-scope queries grouped by query pattern (``group_by_query_pattern``, read in the
    collection's dialect), each with its id, collected frequency and assigned engine.
    A query the collection has no text for is its own pattern. None with no assignment.
    Routing never reads it: the query stays the unit (ADR-031)."""
    from src.tools.sql.query_facts import remember_collected_facts, source_engine_of
    from src.tools.sql.query_pattern_groups import group_by_query_pattern

    if not data.assignment:
        return None
    collector = data.collector or {}
    remember_collected_facts(collector)
    collected = {
        str(q["query_id"]): q
        for q in (collector.get("queries") or {}).get("query_patterns") or []
        if isinstance(q, dict) and q.get("query_id")
    }
    engine_of: dict[str, str | None] = {}
    queries: list[dict] = []
    for qa in data.assignment.get("query_assignments") or []:
        qid = str(qa.get("query_id") or "")
        if not qid or not qa.get("in_scope", True) or qid in engine_of:
            continue
        engine_of[qid] = qa.get("assigned_engine")
        queries.append(collected.get(qid) or {"query_id": qid})
    groups = []
    for g in group_by_query_pattern(queries, source_engine_of(collector)):
        frequency = {q: collected.get(q, {}).get("frequency_per_hour") for q in g.query_ids}
        groups.append(
            {
                "pattern_id": g.pattern_id,
                "operation": g.operation,
                "tables": list(g.tables),
                "returned_columns": list(g.returned_columns),
                "frequency_per_hour": g.frequency_per_hour,
                "members": [
                    {
                        "query_id": q,
                        "frequency_per_hour": (
                            float(f) if isinstance(f, (int, float)) and f >= 0 else 0.0
                        ),
                        "engine": engine_of.get(q),
                    }
                    for q, f in frequency.items()
                ],
            }
        )
    return groups


def _engine_choices(data: SynthesisData) -> dict:
    """``pending_engine_choices`` and ``auto_decided_choices`` for report.json (#1003),
    built the way the customer gate builds them (``assignment_overrides``)."""
    from src.agents.referee.assignment_overrides import pending_choices_from
    from src.agents.referee.pending_choices import auto_decisions_still_holding
    from src.contracts.assignment_models import AutoDecidedChoice

    assignment = data.assignment or {}
    qas = assignment.get("query_assignments") or []
    outputs = {e: art.analysis for e, art in data.engines.items() if art.analysis}
    pending = pending_choices_from(assignment, data.collector, outputs, data.triage)
    decided = auto_decisions_still_holding(
        [AutoDecidedChoice.model_validate(d) for d in assignment.get("auto_decided_choices") or []],
        qas,
    )
    return {
        "pending_engine_choices": [c.model_dump(mode="json") for c in pending],
        "auto_decided_choices": [d.model_dump(mode="json") for d in decided],
    }


def _lifted_findings(
    store: ArtifactStore, database_name: str, job_id: str, assignment_version: int
) -> set[tuple[str, str, str]]:
    """``(query_id, engine, pattern)`` of every anti-pattern finding a redesign removes in
    assignment ``v<assignment_version>``'s record (#1231), whichever engine serves it."""
    if assignment_version <= 0:
        return set()
    key = f"{database_name}/{job_id}/assignment/v{assignment_version}/{ACCESS_PATTERNS_FILE}"
    if not store.exists(key):
        return set()
    return {
        (f["query_id"], str(f.get("engine")), str(f.get("pattern")))
        for f in fixable_findings(store.read_json(key))
    }


def _redesign_requirements(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    assignment_version: int,
    assignment: dict | None,
) -> list[dict]:
    """Each in-scope query of assignment ``v<assignment_version>`` that routing scored
    after redesign, as its engine's designer is given it, with that ``engine`` (#1231,
    ``query_pattern.redesign_requirements``); none without a version or a record."""
    if assignment_version <= 0:
        return []
    key = f"{database_name}/{job_id}/assignment/v{assignment_version}/{ACCESS_PATTERNS_FILE}"
    if not store.exists(key):
        return []
    record = store.read_json(key)
    engine_of = {
        str(qa["query_id"]): str(qa["assigned_engine"])
        for qa in (assignment or {}).get("query_assignments") or []
        if qa.get("in_scope", True) and qa.get("query_id") and qa.get("assigned_engine")
    }
    out = []
    for engine in sorted(set(engine_of.values())):
        for requirement in redesign_requirements(record, engine):
            if engine_of.get(requirement["query_id"]) == engine:
                out.append({**requirement, "engine": engine})
    return out


def _carried_writes(
    store: ArtifactStore, database_name: str, job_id: str, data: SynthesisData
) -> list[dict]:
    """The write-group members of the effective assignment that their engine serves
    poorly, each with its intent and the engine's approach (#1362): the same function
    the schema-design step gives each designer (``carried_write_records``)."""
    from src.agents.referee.carried_writes import carried_write_records
    from src.agents.referee.query_pattern import read_analysis_outputs

    if not data.assignment or not data.collector:
        return []
    return carried_write_records(
        data.assignment,
        data.collector,
        read_analysis_outputs(store, database_name, job_id),
        data.triage,
    )


def _relational_gaps(data: SynthesisData) -> dict | None:
    """The relational move's gap check against wave 1's Aurora engine (#471), from
    the collection; None without one."""
    if not data.collector:
        return None
    return detect_relational_gaps(
        data.collector,
        retained_engine_for(
            data.collector,
            (data.assignment or {}).get("query_assignments") or [],
            (data.assignment or {}).get("aurora_engine_choice"),
        ),
    )


def _tied_queries(assignment: dict | None) -> list[dict]:
    """The queries an exact tie no rule decided keeps on a placeholder engine (#503)."""
    return [
        {
            "query_id": qa["query_id"],
            "assigned_engine": qa["assigned_engine"],
            "tied_engines": qa["tied_engines"],
        }
        for qa in (assignment or {}).get("query_assignments", [])
        if qa.get("tied_engines")
    ]


def _cache_only_engines(assignment: dict) -> set[str]:
    """Engines that front in-scope reads and own none (#950): the per-engine
    risk loop never reads their designs."""
    rows = [qa for qa in assignment.get("query_assignments") or [] if qa.get("in_scope", True)]
    owners = {qa.get("assigned_engine") for qa in rows}
    return {
        str(qa["cache_engine"])
        for qa in rows
        if qa.get("cache_engine") and qa["cache_engine"] not in owners
    }


def _trigger_tables(collector: dict | None) -> dict[str, str]:
    """Each collected trigger's name (bare and schema-qualified) to its table id."""
    out: dict[str, str] = {}
    schema = (collector or {}).get("database_schema") or {}
    for t in schema.get("triggers") or []:
        if not isinstance(t, dict) or not t.get("trigger_name") or not t.get("table_id"):
            continue
        out.setdefault(str(t["trigger_name"]), str(t["table_id"]))
        if t.get("schema_name"):
            out.setdefault(f"{t['schema_name']}.{t['trigger_name']}", str(t["table_id"]))
    return out


def _write_prose_style_rejections(
    store: ArtifactStore, result: dict, rejections: list[str] | None, assignment_version: int
) -> None:
    """Keep the style-list rejections (#1034) in the job's ``_logs``, never in report.json.

    Each line quotes the model's text and names an engine id, so it is
    diagnostics, not customer text (#1211). The file is versioned like the
    report (``prose_style_rejections-v<N>.json``), so a later synthesis never
    reads or overwrites an earlier version's lines. Nothing is written when
    none was rejected.
    """
    if not rejections:
        return
    store.write_json(
        f"{result['database_name']}/{result['job_id']}/_logs/"
        f"prose_style_rejections-v{assignment_version}.json",
        {
            "job_id": result["job_id"],
            "assignment_version": assignment_version,
            "prose_style_rejections": list(rejections),
        },
    )


def _report_output_data(store: ArtifactStore, result: dict, assignment_version: int) -> dict:
    """The ``report.json`` fields synthesis writes for ``result``, ``facts`` included
    (ADR-032); the summary fields are the ones ``result`` holds now."""
    data = result["data"]
    output_data: dict = {
        "job_id": result["job_id"],
        "database_name": result["database_name"],
        "timestamp": result["timestamp"],
        "needs_deeper_analysis": result["needs_deeper_analysis"],
        "ranking": result["ranking"],
        "summary": result["executive_summary"],
        "summary_deterministic": result["summary"],
        "summary_source": result.get("summary_source", "deterministic"),
        "summary_llm": result.get("summary_llm"),
        "summary_validation_warnings": result.get("summary_validation_warnings", []),
        "recommended_architecture": result["architecture"],
        # #212: each row's owner line (the writer, the engines that also read it, the
        # cache), from the final assignment, for the facts layer and the deliverables.
        "table_mappings": table_owner_fields(
            result["table_mappings"],
            assignment_table_usage(data.assignment),
            owners=result.get("wave_table_owners"),
            retained_engine=wave_retained_engine(result.get("migration_waves")),
        ),
        "query_groups": result["query_groups"],
        "tco_analysis": result["tco_analysis"],
        "risk_assessment": result["risk_assessment"],
        "schema_designs": _build_schema_summaries(data),
        "trade_offs": result["trade_offs"],
        "assignment_summary": result["assignment_summary"],
        "cache_overlay": result.get("cache_overlay"),
        # #949: candidate engines the plan analysed and dropped, never a saving.
        "considered_not_recommended": result.get("considered_not_recommended"),
        "engines_in_use": result.get("engines_in_use"),
        "migration_waves": result.get("migration_waves"),
        # #225: how many table_assignments names the waves dropped as parser noise.
        "unresolved_names": result.get("unresolved_names"),
        # #530 review: the collected schema's own tables-only total (no
        # views), structured so a renderer no longer has to parse it out of
        # summary_deterministic's prose.
        "source_table_count": source_table_count(data),
        # #503: the actual queries behind AssignmentSummary.tie_unresolved_count,
        # so the engineering report can list them too, not just the gate.
        "tied_queries": _tied_queries(data.assignment),
        # #1003: the open ties grouped per table, one customer choice each, with
        # the deciding facts; and --auto's decisions that still hold.
        **_engine_choices(data),
        # #629: queries the post-schema router moved after approval, so every
        # deliverable states them (and the override path) from one list.
        # Only moves that still hold on this version (a customer edit that moved
        # a query back undoes its reroute, #713 review round 3).
        "post_schema_reroutes": [
            r
            for r in (data.assignment or {}).get("post_schema_reroutes") or []
            if _engine_of(data.assignment).get(r.get("query_id")) == r.get("to_engine")
        ],
        # #762: the summary request's budget status, only when it cut a list.
        "summary_input_budget": result.get("summary_input_budget"),
        # #992: each written table's source of truth and replicas, from the
        # assignment's write groups (None for an assignment written before them).
        "table_sources": (
            result["table_sources"]
            if "table_sources" in result
            else (
                table_sources(data.assignment, _unservable_writes(data))
                if "write_groups" in (data.assignment or {})
                else None
            )
        ),
        # #888: flagged queries no kept engine can take, for facts.unservable.
        "flagged_unservable": result.get("flagged_unservable") or [],
        # #1329: queries no engine in the customer's engine scope can serve.
        "scope_unservable": result.get("scope_unservable") or [],
        # #1146: the in-scope queries grouped by query pattern, for facts.query_pattern_counts.
        "query_pattern_groups": _query_pattern_groups(data),
    }
    if result.get("reality_check_summary"):
        output_data["reality_check"] = result["reality_check_summary"]
    # #339: the per-query decision traces from the move ledger, for the
    # engineering report's appendix (before the facts, which count them).
    if result.get("query_traces") is not None:
        output_data["query_traces"] = result["query_traces"]
    elif data.assignment and assignment_version > 0:
        output_data["query_traces"] = _query_traces(
            store,
            result["database_name"],
            result["job_id"],
            data.assignment,
            assignment_version,
            data.collector,
            data.triage,
        )
    # #615 (ADR-032): every customer-facing figure, computed once from the
    # fields above; the deliverables only format it. The collected views let
    # wave 1 state its tables and views apart (#578).
    from src.shared.report_facts import compute_report_facts

    schema = (data.collector or {}).get("database_schema") or {}
    view_ids = {
        str(v["view_id"])
        for v in schema.get("views") or []
        if isinstance(v, dict) and v.get("view_id")
    }
    # #584: the signals that overrode queries onto each engine, as data.
    signal_overrides: dict[str, set[str]] = {}
    for qa in (data.assignment or {}).get("query_assignments", []):
        if qa.get("signal_override") and qa.get("assigned_engine"):
            signal_overrides.setdefault(qa["assigned_engine"], set()).add(qa["signal_override"])
    # #471: the relational move's version and feature gaps against wave 1's Aurora
    # engine (the one retained_engine_for names), from the collection; routing never
    # reads them.
    # Synthesis computes it once, before the risks (#1149); an older caller's
    # result without it gets it here.
    relational_gaps = (
        result["relational_gaps"] if "relational_gaps" in result else _relational_gaps(data)
    )
    output_data["facts"] = compute_report_facts(
        output_data,
        view_ids=view_ids,
        signal_overrides=signal_overrides,
        relational_gaps=relational_gaps,
        # #1328: the customer's engine scope triage recorded; every deliverable states it.
        engine_scope=scope_of(data.triage),
        # #1196: the price table each TCO figure was priced from; since #1195 the TCO
        # prices each kept engine itself, so each row records its own (ADR-032 Decision 6).
        price_list=estimates_price_basis(
            {
                str(row.get("database")): row.get("price_list")
                for row in (output_data.get("tco_analysis") or {}).get("cost_breakdown") or []
                if isinstance(row, dict)
            }
        ),
    )

    return output_data


def _write_synthesis_report(
    store: ArtifactStore,
    result: dict,
    assignment_version: int,
) -> None:
    """Validate against SynthesisOutputContract and write the report JSON."""
    data = result["data"]
    _write_prose_style_rejections(
        store, result, getattr(data, "prose_style_rejections", None), assignment_version
    )
    output_data = _report_output_data(store, result, assignment_version)
    output = SynthesisOutputContract.model_validate(output_data)
    job_id = result["job_id"]
    database_name = result["database_name"]
    _persist_cache_safety_net(store, data, assignment_version)
    if assignment_version > 0:
        key = f"{database_name}/{job_id}/synthesis/v{assignment_version}/report.json"
    else:
        key = f"{database_name}/{job_id}/referee-synthesis/report.json"
    store.write_json(key, output.model_dump(mode="json"))
    print(f"[synthesis] Report written to {key}")

    ranking = result["ranking"]
    # Routed confidence (#152), the analysis average alongside for audit
    ranking_str = ", ".join(
        f"{r['target']}={r.get('routed_confidence')}% routed "
        f"(analysis {r['confidence_score']}%)"
        for r in ranking
    )
    print(f"[synthesis] Ranking: [{ranking_str}]")
    if data.assignment:
        workload_parts = []
        for r in ranking:
            aq = r.get("assigned_queries", 0)
            wp = r.get("workload_percent", 0)
            workload_parts.append(f"{r['target']}={aq} queries ({wp}%)")
        workload_str = ", ".join(workload_parts)
        print(f"[synthesis] Workload: {workload_str}")
    architecture = result["architecture"]
    print(f"[synthesis] Architecture: {architecture['architecture_type']}")
    print(f"[synthesis] Table mappings: {len(result['table_mappings'])}")
    print(f"[synthesis] Query groups: {len(result['query_groups'])}")
    risk_count = len(result["risk_assessment"]["risks"])
    risk_level = result["risk_assessment"]["overall_risk_level"]
    print(f"[synthesis] Risks: {risk_count} ({risk_level})")


def _persist_cache_safety_net(store: ArtifactStore, data, assignment_version: int) -> None:
    """Write the safety net's drops into the assignment synthesis read (#296).

    The query journeys and the assignment gate read the assignment artifact, so a
    dropped overlay must be there, not only in the synthesis report. Only the
    drop is written (``cache_engine`` cleared, ``cache_dropped``, ``cache_reason``,
    the note in ``cache_notes`` and the customer-facing ``cache_safety_net_notes``
    (#459), the summary refreshed), in place: the owners and every other engine's
    scope are unchanged, and the cache's own scope shrinks to what its existing
    design serves, so no schema design goes stale.
    """
    if not data.cache_overlay_dropped or assignment_version <= 0:
        return
    from src.agents.referee.cache_overlay import candidate_summary, overlay_summary

    key = f"{data.database_name}/{data.job_id}/assignment/v{assignment_version}/assignment.json"
    if not store.exists(key):
        return
    raw = store.read_json(key)
    by_id = {qa["query_id"]: qa for qa in data.assignment.get("query_assignments", [])}
    dropped = set(data.cache_overlay_dropped)
    for qa in raw.get("query_assignments", []):
        if qa.get("query_id") in dropped:
            src = by_id.get(qa["query_id"], {})
            for k in ("cache_engine", "cache_pattern", "cache_reason", "cache_dropped"):
                qa[k] = src.get(k)
    notes = raw.setdefault("cache_notes", [])
    for note in data.cache_overlay_notes:
        if note not in notes:
            notes.append(note)
    safety_net_notes = raw.setdefault("cache_safety_net_notes", [])
    for note in data.cache_overlay_notes:
        if note not in safety_net_notes:
            safety_net_notes.append(note)
    # #1252: the gate's figure survives the rewrite. The candidates are stored once, from the
    # overlay as the gate showed it (or the flagged queries), and never overwritten.
    prior = raw.get("cache_overlay") or {}
    summary = overlay_summary(raw.get("query_assignments", []), data.source_queries)
    if summary is not None:
        if "candidate_query_count" in prior:
            summary["candidate_query_count"] = prior["candidate_query_count"]
            summary["candidate_call_share_percent"] = prior.get("candidate_call_share_percent")
        else:
            (
                summary["candidate_query_count"],
                summary["candidate_call_share_percent"],
            ) = candidate_summary(raw.get("query_assignments", []), data.source_queries)
    raw["cache_overlay"] = summary
    # A rewrite in place, at today's version, not the one read (#1018).
    store.write_json(key, stamp_artifact_version(key, raw))
    print(f"[synthesis] Cache overlay dropped for {len(dropped)} queries, written to {key}")


# ---------------------------------------------------------------------------
# Top-level handler
# ---------------------------------------------------------------------------


def run_synthesis(
    job_id: str,
    database_name: str,
    store: ArtifactStore,
    assignment_version: int = 0,
    llm_mode: str = "bedrock",
) -> None:
    """Run the synthesis agent. Reads all artifacts, writes report.

    Args:
        job_id: Pipeline job identifier.
        database_name: Source database name.
        store: ArtifactStore instance.
        assignment_version: Assignment version to load (0 = unversioned).
        llm_mode: Controls how the executive summary is generated.
            "bedrock"  — call generate_executive_summary() via Bedrock (default).
            "external" — write LLM input to store and skip the LLM call.
            "none"     — use the deterministic summary; no LLM call.
    """
    result = run_synthesis_deterministic(job_id, database_name, store, assignment_version)

    if not result["data"].engines:
        print("[synthesis] ERROR: No engine artifacts found")
        _write_empty_report(store, database_name, job_id, assignment_version)
        return

    if llm_mode == "bedrock":
        require_valid_guardrail_config()
        # Payload parity (#628): Bedrock reasons over exactly the request the
        # external (Claude Code) path writes to llm_input.json, and its answer
        # goes through the same contract and the same post-checks.
        llm_input = prepare_synthesis_llm_input(result)
        # #762: recorded whatever the model answers, so a capped request is in
        # the report even when the summary falls back to the deterministic one.
        result["summary_input_budget"] = llm_input.get("input_budget")
        response = generate_executive_summary(llm_input)
        # None: no usable answer, so the deterministic summary stays.
        if response is not None:
            result = apply_synthesis_llm_output(result, response)

    elif llm_mode == "external":
        llm_input = prepare_synthesis_llm_input(result)
        result["summary_input_budget"] = llm_input.get("input_budget")
        llm_input_key = (
            f"{database_name}/{job_id}/synthesis/llm_input.json"
            if assignment_version == 0
            else f"{database_name}/{job_id}/synthesis/v{assignment_version}/llm_input.json"
        )
        # The file says its content is data, not instructions (#1317); the Bedrock
        # prompt frames the same payload with frame_untrusted instead.
        store.write_json(llm_input_key, with_untrusted_notice(llm_input))
        print(f"[synthesis] LLM input written to {llm_input_key}")

    # llm_mode == "none" — keep the deterministic summary already set

    _write_synthesis_report(store, result, assignment_version)


def _build_schema_summaries(data):
    summaries = {}
    for engine, artifacts in data.engines.items():
        schema = artifacts.schema_design or {}
        if engine == "opensearch":
            summaries[engine] = _build_opensearch_schema_summary(schema)
        elif engine == "elasticache" and schema.get("key_designs"):
            summaries[engine] = _build_elasticache_schema_summary(schema)
        elif engine == "documentdb" and schema.get("collections"):
            summaries[engine] = _build_documentdb_schema_summary(schema)
        elif engine in AURORA_ENGINES and schema.get("table_definitions"):
            summaries[engine] = _build_aurora_schema_summary(engine, schema)
        elif schema.get("table_definitions"):
            summaries[engine] = _build_standard_schema_summary(schema)
        else:
            summaries[engine] = {"status": "not_available"}
    return summaries


def _build_standard_schema_summary(schema):
    tables = schema["table_definitions"]
    access_patterns = schema.get("access_patterns") or []
    hot_partition = schema.get("hot_partition_analysis") or []
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": [
            {
                "table_name": t["table_name"],
                "aggregate_pattern": t.get("aggregate_pattern"),
                "source_tables": t.get("source_tables") or [],
                "gsi_count": len(t.get("gsis") or []),
                "item_count": t.get("item_count", 0),
                "item_size_bytes": t.get("item_size_bytes", 0),
            }
            for t in tables
        ],
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": sum(1 for hp in hot_partition if hp.get("at_risk")),
        "trade_offs": field_list(schema, "trade_offs"),
        "unsupported_patterns": field_list(schema, "unsupported_patterns"),
        "migration_notes": field_list(schema, "migration_notes"),
    }


def _build_documentdb_schema_summary(schema):
    """Build schema summary for DocumentDB (collections format)."""
    collections = schema.get("collections") or []
    access_patterns = schema.get("access_patterns") or []
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": [
            {
                "table_name": c.get("collection_name", ""),
                "aggregate_pattern": "document_collection",
                "source_tables": field_list(c, "source_tables"),
                "gsi_count": len(c.get("indexes") or []),
                "item_count": 0,
                "item_size_bytes": 0,
            }
            for c in collections
        ],
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": 0,
        "trade_offs": field_list(schema, "trade_offs"),
        "unsupported_patterns": field_list(schema, "unsupported_patterns"),
        "migration_notes": field_list(schema, "migration_notes"),
    }


def _build_aurora_schema_summary(engine, schema):
    """Build schema summary for Aurora MySQL/PostgreSQL (relational table format).

    Aurora carries tables over 1:1, so the summary reports relational structure
    (columns, indexes, foreign keys) plus the Aurora-specific optimizations and
    app-layer notes. The full DDL stays in the schema artifact.
    """
    tables = schema_table_defs(engine, schema)
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "migration_strategy": schema.get("migration_strategy"),
        "tables": [
            {
                "table_name": t["table_name"],
                "aggregate_pattern": t["aggregate_pattern"],
                "source_tables": t["source_tables"],
                "gsi_count": 0,
                "item_count": 0,
                "item_size_bytes": 0,
                "column_count": len(t.get("columns") or []),
                "primary_key": t.get("primary_key") or [],
                "index_count": len(t.get("indexes") or []),
                "foreign_key_count": len(t.get("foreign_keys") or []),
            }
            for t in tables
        ],
        "index_count": sum(len(t.get("indexes") or []) for t in tables),
        "foreign_key_count": sum(len(t.get("foreign_keys") or []) for t in tables),
        "access_pattern_count": len(schema.get("access_patterns") or []),
        "hot_partitions_at_risk": 0,
        "optimizations": field_list(schema, "optimizations"),
        "app_layer_notes": field_list(schema, "app_layer_notes"),
        "ddl_available": bool(schema.get("generated_ddl")),
        "trade_offs": field_list(schema, "trade_offs"),
        "unsupported_patterns": field_list(schema, "unsupported_patterns"),
        "migration_notes": field_list(schema, "migration_notes"),
    }


def _build_elasticache_schema_summary(schema):
    """Build schema summary for ElastiCache/Redis (key_designs format)."""
    key_designs = schema.get("key_designs") or []
    access_patterns = schema.get("access_patterns") or []
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": [
            {
                "table_name": kd.get("key_pattern", ""),
                "aggregate_pattern": kd.get("data_type", "unknown"),
                "source_tables": field_list(kd, "source_tables"),
                "gsi_count": 0,
                "item_count": 0,
                "item_size_bytes": 0,
                "ttl_seconds": kd.get("ttl_seconds"),
            }
            for kd in key_designs
        ],
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": 0,
        "trade_offs": field_list(schema, "trade_offs"),
        "unsupported_patterns": field_list(schema, "unsupported_patterns"),
        "migration_notes": field_list(schema, "migration_notes"),
    }


def _build_opensearch_schema_summary(schema):
    index_designs = schema.get("index_designs") or []
    data_stream_designs = schema.get("data_stream_designs") or []
    if not index_designs and not data_stream_designs:
        return {"status": "not_available"}
    tables = []
    for idx in index_designs:
        settings = idx.get("settings") or {}
        tables.append(
            {
                "table_name": idx.get("index_name", ""),
                "aggregate_pattern": "search_index",
                "source_tables": field_list(idx, "source_tables"),
                "gsi_count": 0,
                "item_count": 0,
                "item_size_bytes": 0,
                "shards": settings.get("number_of_shards", 0),
                "replicas": settings.get("number_of_replicas", 1),
                "field_count": len(idx.get("field_mappings") or []),
            }
        )
    for ds in data_stream_designs:
        ism = ds.get("ism_policy") or {}
        ts = (ds.get("index_template") or {}).get("settings") or {}
        tables.append(
            {
                "table_name": ds.get("data_stream_name", ""),
                "aggregate_pattern": "data_stream",
                "source_tables": field_list(ds, "source_tables"),
                "gsi_count": 0,
                "item_count": 0,
                "item_size_bytes": 0,
                "shards": ts.get("number_of_shards", 0),
                "replicas": ts.get("number_of_replicas", 1),
                "ism_hot_days": ism.get("hot_phase_days"),
                "ism_delete_days": ism.get("delete_after_days"),
            }
        )
    access_patterns = schema.get("access_patterns") or []
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": tables,
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": 0,
        "trade_offs": field_list(schema, "trade_offs"),
        "unsupported_patterns": field_list(schema, "unsupported_patterns"),
        "migration_notes": [],
    }


_TRADE_OFF_LISTS = ("source_tables", "target_tables", "query_ids")


def _collect_trade_offs(data):
    trade_offs: list[dict] = []
    seen: set[str] = set()
    for engine, artifacts in data.engines.items():
        schema = artifacts.schema_design or {}
        for t in field_list(schema, "trade_offs"):
            if isinstance(t, dict):
                # Structured TradeOff — ensure engine is set
                # A null list field reads as empty (#757), the contract's own default.
                nulls = {k: [] for k in _TRADE_OFF_LISTS if k in t and t[k] is None}
                entry = {**t, **nulls, "engine": t.get("engine") or engine}
                dedup_key = entry.get("description", str(entry))
            else:
                # Legacy string format — wrap into structured form
                entry = {
                    "description": str(t),
                    "impact": str(t),
                    "source_tables": [],
                    "target_tables": [],
                    "query_ids": [],
                    "engine": engine,
                }
                dedup_key = str(t)
            if dedup_key not in seen:
                seen.add(dedup_key)
                trade_offs.append(entry)
    return trade_offs


def _write_empty_report(store, database_name, job_id, assignment_version=0):
    # A skipped placeholder of its own contract, not a SynthesisOutputContract (#1023).
    output = SkippedSynthesisReport(job_id=job_id, database_name=database_name)
    if assignment_version > 0:
        key = f"{database_name}/{job_id}/synthesis/v{assignment_version}/report.json"
    else:
        key = f"{database_name}/{job_id}/referee-synthesis/report.json"
    store.write_json(key, output.model_dump(mode="json"))


def _unservable_writes(data: SynthesisData) -> dict[str, list[str]]:
    """The write groups with no engine in the customer's engine scope (#1329)."""
    unservable = (data.assignment or {}).get("unservable_queries") or []
    return unservable_write_groups(data.collector, unservable) if unservable else {}
