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

from src.agents.referee.aurora_choice import source_database_engine, source_database_version
from src.agents.referee.migration_waves import build_migration_waves
from src.agents.referee.synthesis_data import load_synthesis_data
from src.agents.referee.synthesis_grounding import (
    build_effective_architecture,
    build_fallback_summary,
    check_summary_grounding,
    check_summary_internal_leaks,
    check_summary_wave_order,
    eliminated_engines,
    engine_table_scope,
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
    designed_and_not_designed_engines,
    generate_executive_summary,
    schema_table_defs,
)
from src.agents.referee.table_resolution import PSEUDO_TABLES, TableNameResolver
from src.contracts.synthesis_output import SynthesisOutputContract
from src.storage.artifact_store import ArtifactStore

# ---------------------------------------------------------------------------
# Seam 1: Deterministic — all builders, no LLM
# ---------------------------------------------------------------------------


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
    reality_check.py). This is the only way a saving a recommendation names
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
        summary (deterministic), executive_summary (deterministic fallback),
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
    reality_check_key = f"{database_name}/{job_id}/reality-check/output.json"
    reality_check_output: dict | None = None
    if store.exists(reality_check_key):
        reality_check_output = store.read_json(reality_check_key)
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
    eliminated = eliminated_engines(effective, reality_check_output)
    print("[synthesis] Building TCO analysis...")
    # #380: an eliminated engine's own analysed cost, read directly -- ``data.engines``
    # has already dropped it (ADR-029 Layer E, #202) by this point, so a dollar figure
    # a recommendation quotes for it is also traceable in the TCO facts, not just prose.
    eliminated_costs = _eliminated_engine_costs(store, database_name, job_id, eliminated)
    tco = build_tco_analysis(data, eliminated_costs=eliminated_costs)
    # Per-engine table scope of the effective assignment, for the summary LLM input and
    # the summary post-check (#205).
    known_tables: list[str] = sorted(
        {str(t["table_id"]) for t in data.source_tables if t.get("table_id")}
        | {m["source_table"] for m in table_mappings}
    )
    engine_tables = engine_table_scope(
        data.assignment, data.source_queries, table_mappings, known_tables
    )
    print("[synthesis] Building risk assessment...")
    risk_assessment = build_risk_assessment(data, eliminated)
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
    deterministic_summary = build_summary(
        data, ranking, table_mappings, tco, risk_assessment, query_groups, eliminated
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
        # #225: scope waves to tables/views the collector actually saw, so a parser
        # artifact (CTE alias, keyword, system catalog name) never reaches a wave.
        # The upstream noise in table_assignments itself is #316.
        known_tables=wave_known_tables,
        # #321: named in wave 1's homogeneity statement when the collector
        # reported it; never invented when it didn't.
        source_version=source_database_version(data.collector),
        mapped_tables=mapped_table_ids or None,
    )
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
        }

    # Merge reality check data into trade_offs for visibility
    reality_check_summary = None
    if reality_check_output:
        reality_check_summary = {
            "consolidations": reality_check_output.get("consolidations", []),
            "architectural_patterns": reality_check_output.get("architectural_patterns", []),
            "recommendations": reality_check_output.get("recommendations", []),
            "before_distribution": reality_check_output.get("before_distribution", {}),
            "after_distribution": reality_check_output.get("after_distribution", {}),
        }
        reality_check_summary = recompute_reality_check_patterns(
            reality_check_summary, (data.assignment or {}).get("query_assignments", [])
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

    return {
        "job_id": job_id,
        "database_name": database_name,
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
        "eliminated_engines": eliminated,
        "cache_overlay": cache_overlay,
        "migration_waves": migration_waves,
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


# ---------------------------------------------------------------------------
# Seam 2: Format LLM input payload
# ---------------------------------------------------------------------------


def prepare_synthesis_llm_input(deterministic_result: dict) -> dict:
    """Return the payload that should be sent to the executive-summary LLM.

    Keys returned:
        effective_architecture, deterministic_summary, ranking, query_groups,
        tco_analysis, risk_assessment, table_mappings, trade_offs, migration_waves,
        schema_design_status

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
    got one exactly the way the Bedrock prompt used to (#132).
    """
    designed, not_designed = designed_and_not_designed_engines(deterministic_result["ranking"])
    return {
        "effective_architecture": deterministic_result["effective_architecture"],
        "deterministic_summary": deterministic_result["summary"],
        "ranking": deterministic_result["ranking"],
        "query_groups": deterministic_result["query_groups"],
        "tco_analysis": deterministic_result["tco_analysis"],
        "risk_assessment": deterministic_result["risk_assessment"],
        "table_mappings": deterministic_result["table_mappings"],
        "trade_offs": deterministic_result["trade_offs"],
        "migration_waves": deterministic_result.get("migration_waves"),
        "schema_design_status": {"designed": designed, "not_designed": not_designed},
    }


# ---------------------------------------------------------------------------
# Seam 3: Apply LLM output back onto the deterministic result
# ---------------------------------------------------------------------------


def apply_synthesis_llm_output(deterministic_result: dict, llm_output: dict) -> dict:
    """Merge the LLM-generated executive summary into the deterministic result.

    If ``llm_output`` contains an ``executive_summary`` it is post-checked against the
    effective assignment (``check_summary_grounding``), against this codebase's own
    internal field names (``check_summary_internal_leaks``), and against the
    deterministic wave order (``check_summary_wave_order``, #393). It is used unless a
    check finds a high-confidence problem (a table named under an engine none of whose
    in-scope queries touch it, an internal field name, or a stated migration order that
    contradicts ``migration_waves``); lower-confidence findings are only recorded. On
    rejection: ``executive_summary`` stays
    deterministic, and the LLM text plus the warnings are kept for audit in
    ``summary_llm`` / ``summary_validation_warnings`` (#205). ``summary_source``
    records which one the customer sees: ``llm`` or ``deterministic_fallback``.

    Returns the updated result dict (mutates and returns the same dict).
    """
    if "executive_summary" not in llm_output:
        return deterministic_result
    llm_summary = llm_output["executive_summary"]
    engine_tables = deterministic_result.get("engine_tables")
    if engine_tables is None:
        engine_tables = engine_table_scope(
            None, [], deterministic_result.get("table_mappings") or [], set()
        )
    findings = check_summary_grounding(
        str(llm_summary or ""),
        engine_tables,
        deterministic_result.get("database_name", ""),
        deterministic_result.get("known_tables"),
    )
    # #380: a second, independent check -- an internal field name leaked into the
    # summary is always high-confidence (there is no low-stakes reading of it),
    # regardless of whether any table/engine attribution also happens to be wrong.
    findings = findings + check_summary_internal_leaks(str(llm_summary or ""))
    # #393: a third, independent check -- a summary that states a migration order
    # ("X first, then Y") contradicting the deterministic ``migration_waves`` sequence
    # is always high-confidence, the same way the wave order itself is never a model's
    # choice (``prepare_synthesis_llm_input``'s own docstring).
    findings = findings + check_summary_wave_order(
        str(llm_summary or ""), deterministic_result.get("migration_waves")
    )
    deterministic_result["summary_llm"] = llm_summary
    deterministic_result["summary_validation_warnings"] = [f["message"] for f in findings]
    for f in findings:
        print(f"[synthesis] WARNING: {f['message']}")
    if any(f["high_confidence"] for f in findings):
        # The customer sees a short narrative built from the effective architecture
        # (display names, no cost or confidence figures), not the raw deterministic
        # summary, which stays in summary_deterministic.
        deterministic_result["executive_summary"] = (
            build_fallback_summary(deterministic_result.get("effective_architecture"))
            or deterministic_result["summary"]
        )
        deterministic_result["summary_source"] = "deterministic_fallback"
    else:
        deterministic_result["executive_summary"] = llm_summary
        deterministic_result["summary_source"] = "llm"
    return deterministic_result


# ---------------------------------------------------------------------------
# Writer helper — shared by run_synthesis
# ---------------------------------------------------------------------------


def _write_synthesis_report(
    store: ArtifactStore,
    result: dict,
    assignment_version: int,
) -> None:
    """Validate against SynthesisOutputContract and write the report JSON."""
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
        "table_mappings": result["table_mappings"],
        "query_groups": result["query_groups"],
        "tco_analysis": result["tco_analysis"],
        "risk_assessment": result["risk_assessment"],
        "schema_designs": _build_schema_summaries(data),
        "trade_offs": result["trade_offs"],
        "assignment_summary": result["assignment_summary"],
        "cache_overlay": result.get("cache_overlay"),
        "migration_waves": result.get("migration_waves"),
        # #225: how many table_assignments names the waves dropped as parser noise.
        "unresolved_names": result.get("unresolved_names"),
    }
    if result.get("reality_check_summary"):
        output_data["reality_check"] = result["reality_check_summary"]

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
    the note in ``cache_notes``, the summary refreshed), in place: the owners and
    every other engine's scope are unchanged, and the cache's own scope shrinks to
    what its existing design serves, so no schema design goes stale.
    """
    if not data.cache_overlay_dropped or assignment_version <= 0:
        return
    from src.agents.referee.cache_overlay import overlay_summary

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
    raw["cache_overlay"] = overlay_summary(raw.get("query_assignments", []), data.source_queries)
    store.write_json(key, raw)
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
        trade_offs = result["trade_offs"]
        executive_summary = generate_executive_summary(
            result["summary"],
            result["ranking"],
            result["query_groups"],
            result["tco_analysis"],
            result["risk_assessment"],
            result["table_mappings"],
            trade_offs,
            effective_architecture=result["effective_architecture"],
        )
        # generate_executive_summary returns the deterministic text when the LLM fails;
        # only a real LLM narrative goes through the grounding post-check.
        if executive_summary != result["summary"]:
            result = apply_synthesis_llm_output(result, {"executive_summary": executive_summary})

    elif llm_mode == "external":
        llm_input = prepare_synthesis_llm_input(result)
        llm_input_key = (
            f"{database_name}/{job_id}/synthesis/llm_input.json"
            if assignment_version == 0
            else f"{database_name}/{job_id}/synthesis/v{assignment_version}/llm_input.json"
        )
        store.write_json(llm_input_key, llm_input)
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
    access_patterns = schema.get("access_patterns", [])
    hot_partition = schema.get("hot_partition_analysis", [])
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": [
            {
                "table_name": t["table_name"],
                "aggregate_pattern": t.get("aggregate_pattern"),
                "source_tables": t.get("source_tables", []),
                "gsi_count": len(t.get("gsis", [])),
                "item_count": t.get("item_count", 0),
                "item_size_bytes": t.get("item_size_bytes", 0),
            }
            for t in tables
        ],
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": sum(1 for hp in hot_partition if hp.get("at_risk")),
        "trade_offs": schema.get("trade_offs", []),
        "unsupported_patterns": schema.get("unsupported_patterns", []),
        "migration_notes": schema.get("migration_notes", []),
    }


def _build_documentdb_schema_summary(schema):
    """Build schema summary for DocumentDB (collections format)."""
    collections = schema.get("collections", [])
    access_patterns = schema.get("access_patterns", [])
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": [
            {
                "table_name": c.get("collection_name", ""),
                "aggregate_pattern": "document_collection",
                "source_tables": c.get("source_tables", []),
                "gsi_count": len(c.get("indexes", [])),
                "item_count": 0,
                "item_size_bytes": 0,
            }
            for c in collections
        ],
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": 0,
        "trade_offs": schema.get("trade_offs", []),
        "unsupported_patterns": schema.get("unsupported_patterns", []),
        "migration_notes": schema.get("migration_notes", []),
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
                "column_count": len(t.get("columns", [])),
                "primary_key": t.get("primary_key", []),
                "index_count": len(t.get("indexes", [])),
                "foreign_key_count": len(t.get("foreign_keys", [])),
            }
            for t in tables
        ],
        "index_count": sum(len(t.get("indexes", [])) for t in tables),
        "foreign_key_count": sum(len(t.get("foreign_keys", [])) for t in tables),
        "access_pattern_count": len(schema.get("access_patterns", [])),
        "hot_partitions_at_risk": 0,
        "optimizations": schema.get("optimizations", []),
        "app_layer_notes": schema.get("app_layer_notes", []),
        "ddl_available": bool(schema.get("generated_ddl")),
        "trade_offs": schema.get("trade_offs", []),
        "unsupported_patterns": schema.get("unsupported_patterns", []),
        "migration_notes": schema.get("migration_notes", []),
    }


def _build_elasticache_schema_summary(schema):
    """Build schema summary for ElastiCache/Redis (key_designs format)."""
    key_designs = schema.get("key_designs", [])
    access_patterns = schema.get("access_patterns", [])
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": [
            {
                "table_name": kd.get("key_pattern", ""),
                "aggregate_pattern": kd.get("data_type", "unknown"),
                "source_tables": kd.get("source_tables", []),
                "gsi_count": 0,
                "item_count": 0,
                "item_size_bytes": 0,
                "ttl_seconds": kd.get("ttl_seconds"),
            }
            for kd in key_designs
        ],
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": 0,
        "trade_offs": schema.get("trade_offs", []),
        "unsupported_patterns": schema.get("unsupported_patterns", []),
        "migration_notes": schema.get("migration_notes", []),
    }


def _build_opensearch_schema_summary(schema):
    index_designs = schema.get("index_designs", [])
    data_stream_designs = schema.get("data_stream_designs", [])
    if not index_designs and not data_stream_designs:
        return {"status": "not_available"}
    tables = []
    for idx in index_designs:
        settings = idx.get("settings", {})
        tables.append(
            {
                "table_name": idx.get("index_name", ""),
                "aggregate_pattern": "search_index",
                "source_tables": idx.get("source_tables", []),
                "gsi_count": 0,
                "item_count": 0,
                "item_size_bytes": 0,
                "shards": settings.get("number_of_shards", 0),
                "replicas": settings.get("number_of_replicas", 1),
                "field_count": len(idx.get("field_mappings", [])),
            }
        )
    for ds in data_stream_designs:
        ism = ds.get("ism_policy", {})
        ts = ds.get("index_template", {}).get("settings", {})
        tables.append(
            {
                "table_name": ds.get("data_stream_name", ""),
                "aggregate_pattern": "data_stream",
                "source_tables": ds.get("source_tables", []),
                "gsi_count": 0,
                "item_count": 0,
                "item_size_bytes": 0,
                "shards": ts.get("number_of_shards", 0),
                "replicas": ts.get("number_of_replicas", 1),
                "ism_hot_days": ism.get("hot_phase_days"),
                "ism_delete_days": ism.get("delete_after_days"),
            }
        )
    access_patterns = schema.get("access_patterns", [])
    return {
        "status": "completed",
        "validation_passed": schema.get("validation_passed", False),
        "tables": tables,
        "access_pattern_count": len(access_patterns),
        "hot_partitions_at_risk": 0,
        "trade_offs": schema.get("trade_offs", []),
        "unsupported_patterns": schema.get("unsupported_patterns", []),
        "migration_notes": [],
    }


def _collect_trade_offs(data):
    trade_offs: list[dict] = []
    seen: set[str] = set()
    for engine, artifacts in data.engines.items():
        schema = artifacts.schema_design or {}
        for t in schema.get("trade_offs", []):
            if isinstance(t, dict):
                # Structured TradeOff — ensure engine is set
                entry = {**t, "engine": t.get("engine") or engine}
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
    output = {
        "job_id": job_id,
        "database_name": database_name,
        "agent_type": "referee-synthesis",
        "status": "completed",
        "ranking": [],
        "needs_deeper_analysis": False,
        "summary": "No analysis outputs were available for synthesis.",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if assignment_version > 0:
        key = f"{database_name}/{job_id}/synthesis/v{assignment_version}/report.json"
    else:
        key = f"{database_name}/{job_id}/referee-synthesis/report.json"
    store.write_json(key, output)
