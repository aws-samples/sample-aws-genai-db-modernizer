"""Referee-Reality-Check agent handler — CTO-level engine consolidation.

Reads assignment, triage, analysis, and collector artifacts, runs the reality
check logic, writes the revised assignment and reality check output via
ArtifactStore. Slots between assignment and schema design in the pipeline.
"""

import json
import logging
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime

from pydantic import ValidationError

from src.agents.prompt_framing import SYSTEM_PROMPT_DATA_DIRECTIVE, frame_untrusted
from src.agents.referee.assignment_overrides import refresh_consolidated_assignment
from src.agents.referee.aurora_choice import source_database_engine
from src.agents.referee.cache_overlay import normalize_cache_owners
from src.agents.referee.consolidation_validator import (
    CONSOLIDATION_REVIEW_SKILL,
    sanity_sweep,
    validate_consolidations,
)
from src.agents.referee.model_flags import apply_consolidation_flags, parse_model_flags
from src.agents.referee.move_ledger import (
    MoveLedger,
    apply_to_assignment,
    check_replay,
    ledger_for,
    state_of,
)
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.agents.referee.reality_check import (
    AURORA_ENGINES,
    _build_query_signals,
    _engine_infra_cost,
    refresh_patterns_and_recommendations,
    run_reality_check,
)
from src.agents.referee.reality_check_records import (
    consolidations_from_moves,
    removal_log_lines,
)
from src.agents.referee.reality_check_request import (
    build_reality_check_request,
    query_signal_map,
    render_reality_check_request,
    with_read_pages,
)
from src.agents.referee.reality_check_summary import finalize_executive_summary
from src.agents.referee.tie_break import settled_counts
from src.contracts.assignment_models import AssignmentSource
from src.contracts.model_flag import ResponseContractError, validation_error_note
from src.contracts.move_ledger import MoveActor, MovePass, ReasonCode
from src.contracts.reality_check_model_response import (
    RealityCheckModelResponse,
    parse_reality_check_response,
)
from src.contracts.reality_check_output import RealityCheckOutputContract
from src.shared.engine_elimination import eliminated_engines as settled_eliminated_engines
from src.shared.engine_names import PLACEHOLDER_ENGINES
from src.shared.models import Role, build_bedrock_model, resolve_model_id
from src.shared.skill_sections import skill_sections
from src.storage.artifact_store import ArtifactStore
from src.storage.assignment_versioning import (
    next_assignment_version,
    reality_check_run_for_lineage,
    resolve_reality_check_input_version,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seam 1: Deterministic — reads artifacts and runs consolidation logic, no LLM
# ---------------------------------------------------------------------------


def run_reality_check_deterministic(
    job_id: str,
    database_name: str,
    store: ArtifactStore,
    assignment_version: int | None = None,
) -> dict:
    """Run all deterministic reality-check logic without invoking any LLM.

    Reads assignment, triage, collector, and analysis artifacts from the store,
    runs the core consolidation logic, and builds recommendations.

    ``assignment_version`` defaults to
    :func:`~src.storage.assignment_versioning.resolve_reality_check_input_version`
    (the newest version Reality Check did not produce itself, issue #189).

    Returns a dict with keys:
        consolidations, recommendations, architectural_patterns,
        unique_value_assessment, before_distribution, after_distribution,
        executive_summary (always None), assignment, collector_output,
        analysis_outputs
    """
    if assignment_version is None:
        assignment_version = resolve_reality_check_input_version(store, database_name, job_id)

    # Read required artifacts
    assignment_key = f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
    assignment = store.read_json(assignment_key)

    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    triage = store.read_json(triage_key)

    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key)

    # Load analysis outputs for each engine in the triage
    selected_engines = [a["agent_type"] for a in triage.get("selected_agents", [])]
    analysis_outputs: dict[str, dict] = {}
    for engine in selected_engines:
        if engine in PLACEHOLDER_ENGINES:  # ADR-031 Decision 9
            continue
        analysis_key = f"{database_name}/{job_id}/analysis-{engine}/analysis.json"
        if store.exists(analysis_key):
            analysis_outputs[engine] = store.read_json(analysis_key)

    # #381 review: triage selects both Aurora engines for a heterogeneous source
    # (SQL Server, Oracle, DB2), so both are read back in above -- but the
    # assignment resolver already collapsed them to one winner
    # (``Assignment.aurora_engine_choice``) before anything was assigned. Drop the
    # loser here too, so Reality Check's consolidation logic
    # never treats it as a live candidate to move a query onto.
    aurora_choice = assignment.get("aurora_engine_choice")
    winner = aurora_choice.get("engine") if aurora_choice else None
    if winner in AURORA_ENGINES:
        loser = next(e for e in AURORA_ENGINES if e != winner)
        analysis_outputs.pop(loser, None)

    # The move ledger (#616) of the version this check writes, starting from the
    # stored input: every engine change from here on is one Move on it, and the
    # revision replays from ``input_state`` plus those Moves.
    input_state = state_of(assignment.get("query_assignments", []))
    ledger = ledger_for(assignment.get("query_assignments", []))

    # An assignment written before the cache overlay (#296) has ElastiCache owners:
    # move them to their system-of-record engine first, so the revision carries
    # no cache owner (recorded in cache_notes). Each is a cache_owner_normalize
    # Move, recorded before the check's own.
    normalize_cache_owners(
        assignment,
        collector_output.get("queries", {}).get("query_patterns", []),
        analysis_outputs,
        source_database_engine(collector_output),
        triage=triage,
        collector=collector_output,
        ledger=ledger,
    )

    # Extract query capabilities from triage
    query_capabilities = triage.get("query_capabilities", {})

    # Count queries per engine before reality check
    before_distribution: dict[str, int] = defaultdict(int)
    for qa in assignment.get("query_assignments", []):
        before_distribution[qa["assigned_engine"]] += 1

    # Run the core reality check (deterministic consolidation logic). Queries the
    # customer re-routed (customer_override) are not consolidation candidates:
    # Reality Check never moves them, so they are held out and re-attached as-is.
    # Their engines therefore stay in the plan, and the consolidation is told so
    # (#860): it never removes one and moves the other queries off it.
    all_qas = assignment.get("query_assignments", [])
    pinned = [qa for qa in all_qas if qa.get("customer_override")]
    candidates = [qa for qa in all_qas if not qa.get("customer_override")]
    result = run_reality_check(
        {**assignment, "query_assignments": candidates} if pinned else assignment,
        triage,
        analysis_outputs,
        collector_output,
        query_capabilities,
        source_engine=source_database_engine(collector_output),
        ledger=ledger,
        customer_kept_engines=sorted({qa["assigned_engine"] for qa in pinned}),
    )
    if pinned:
        revised_by_id = {qa["query_id"]: qa for qa in result["revised_assignments"]}
        # Copies: later steps edit revised assignments in place, and the input
        # assignment is the before-state the records are reconciled against.
        result["revised_assignments"] = [
            revised_by_id.get(qa["query_id"]) or deepcopy(qa) for qa in all_qas
        ]

    # The move ledger (#616): every engine change this check makes, from the
    # cache owner normalisation to the customer-override restore, is one Move on
    # this ledger. It holds the Moves ``run_reality_check`` returned (the
    # normalisation's first, on the ledger passed in), and its engines are where
    # every query (customer picks included) is now. The revised version holds
    # exactly these Moves (moves are per-version).
    check_ledger = result.get("move_ledger")
    ledger = MoveLedger(
        {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]},
        check_ledger.moves if isinstance(check_ledger, MoveLedger) else ledger.moves,
        tied={qa["query_id"]: qa.get("tied_engines") or [] for qa in result["revised_assignments"]},
    )

    # Count queries per engine after reality check
    after_distribution: dict[str, int] = defaultdict(int)
    for qa in result["revised_assignments"]:
        after_distribution[qa["assigned_engine"]] += 1

    return {
        "consolidations": result["consolidations"],
        "recommendations": result["recommendations"],
        "architectural_patterns": result["architectural_patterns"],
        "unique_value_assessment": result["unique_value_assessment"],
        "before_distribution": dict(before_distribution),
        "after_distribution": dict(after_distribution),
        "executive_summary": None,
        # Pass-through data needed by LLM and downstream writing steps
        "assignment": assignment,
        "triage": triage,
        "collector_output": collector_output,
        "analysis_outputs": analysis_outputs,
        # Keep revised_assignments and lightweight_recommendations for the write step
        "revised_assignments": result["revised_assignments"],
        "lightweight_recommendations": result.get("lightweight_recommendations", []),
        # This check's Moves so far (#616); the later steps add theirs.
        "move_ledger": ledger,
        # The stored input's (engine, tied_engines) per query: what the
        # revision replays from (before the cache owner normalisation).
        "input_state": input_state,
    }


# ---------------------------------------------------------------------------
# Seam 2: Prepare — formats the LLM request payload from deterministic result
# ---------------------------------------------------------------------------


def prepare_reality_check_llm_input(deterministic_result: dict) -> dict:
    """Format the external LLM request from a deterministic result dict (#285).

    Returns a compact, bounded request (``reality_check_request``):
        read_pages: Read tool pages covering the rendered request
        consolidation_validation: the consolidations, each with its moved queries
        executive_summary: context for the executive summary
    """
    det = deterministic_result
    request = build_reality_check_request(
        det,
        absorption_candidates(
            det["before_distribution"], det["after_distribution"], det.get("consolidations")
        ),
    )
    return with_read_pages(request)


def absorption_candidates(
    before_distribution: dict[str, int],
    after_distribution: dict[str, int],
    consolidations: list[dict] | None = None,
) -> list[str]:
    """Kept engines whose fate can still change after the model's flags (#236, #647).

    A flag re-routes its query only among the kept engines and never brings a
    removed engine back (maintainer decision, 2026-10-14). So a removed engine
    stays removed, and the only engines that can still lose queries -- and end
    empty, or small enough for the sanity sweep to remove -- are kept engines a
    consolidation moved queries onto (a flag only applies to such a query).
    The external LLM writes its summary before finalize, so it must not state
    these engines' final fate. ``before_distribution`` is kept for the
    signature's callers; it no longer changes the answer.
    """
    kept = {engine for engine, count in after_distribution.items() if count > 0}
    targets = {c.get("to_engine") for c in consolidations or [] if c.get("query_count", 1)}
    return sorted(kept & targets)


# ---------------------------------------------------------------------------
# Seam 3: Apply — merges LLM output back into the deterministic result
# ---------------------------------------------------------------------------


def apply_reality_check_llm_output(
    deterministic_result: dict, llm_output: dict | RealityCheckModelResponse
) -> dict:
    """Merge a model's answer into the deterministic result dict.

    ``llm_output`` is validated against
    :class:`~src.contracts.reality_check_model_response.RealityCheckModelResponse`
    first, the one response contract both LLM modes share (#628): the headless
    ``llm_responses/reality_check.json`` and the Bedrock path (the validator's
    flags, then its summary) go through this same function. A response off the
    contract raises ``pydantic.ValidationError``. Its two optional parts:

    - consolidation_corrections: model flags (``ModelFlag``) on queries a
      consolidation moved; the shared scorer decides where a flagged query goes
    - executive_summary: CTO-facing narrative string (``None``: not given)

    Returns an updated copy of deterministic_result (mutated in place).
    """
    result = deterministic_result
    response = parse_reality_check_response(llm_output)

    if response.consolidation_corrections:
        # Model flags (ADR-031 Decision 11 as amended 2026-10-14, #647): each
        # entry must match the ModelFlag contract, in both LLM modes. A flag is
        # evidence -- the flagged engine cannot serve those queries -- and the
        # shared scorer decides where they go (src.agents.referee.model_flags).
        flags, rejected = parse_model_flags(response.consolidation_corrections)
        if rejected:
            result["model_flags_rejected"] = result.get("model_flags_rejected", 0) + len(rejected)
        if flags:
            _apply_model_flags(result, flags)

    if response.executive_summary is not None:
        result["executive_summary"] = response.executive_summary
        # A new summary is checked afresh at write time (finalize_executive_summary).
        result.pop("executive_summary_source", None)
        result.pop("executive_summary_llm", None)
        result.pop("executive_summary_validation_warnings", None)

    return result


# ---------------------------------------------------------------------------
# Top-level handler — backward-compatible, delegates to seam functions
# ---------------------------------------------------------------------------


def run_reality_check_handler(
    job_id: str,
    database_name: str,
    store: ArtifactStore,
    assignment_version: int | None = None,
    llm_mode: str = "bedrock",
) -> dict:
    """Run the reality check agent.

    Reads assignment, triage, analysis, and collector artifacts.
    Writes:
      - reality-check/output.json (contract-validated)
      - assignment/v{next}/assignment.json (revised, if consolidation occurred),
        where ``next`` is ``next_assignment_version()`` so an existing version is
        never overwritten (issue #189)

    Args:
        assignment_version: the version to consolidate. ``None`` (the default)
                  skips the run when Reality Check already ran for the current
                  lineage (``reality_check_run_for_lineage``: its own output, or a
                  customer edit made after it), else resolves the input with
                  ``resolve_reality_check_input_version``. An explicit version
                  always runs and still writes to the next version. Either way,
                  queries with ``customer_override`` are never moved.
        llm_mode: "bedrock" (default) — validate consolidations + generate executive summary
                  "external" — write LLM input to store and mark as awaiting; the
                               revised assignment is written by the finalize step
                  "none" — skip all LLM calls, use deterministic result only

    Returns ``{"status", "input_version", "output_version"}``: status is
    ``"complete"``, ``"awaiting_llm"`` (external) or ``"skipped"`` (already
    for this lineage); ``output_version`` is the revised version written (or,
    when skipped, the earlier run's), else None. A run that consolidates nothing
    writes no version.
    """
    import time

    start_time = time.time()

    if assignment_version is None:
        previous_run = reality_check_run_for_lineage(store, database_name, job_id)
        if previous_run is not None:
            print(
                f"[reality-check] Already ran for this lineage (v{previous_run.input_version}"
                f" -> {previous_run.output_version or 'unchanged'}); skipping so later "
                "customer edits are not re-consolidated"
            )
            return _skipped(previous_run)
        assignment_version = resolve_reality_check_input_version(store, database_name, job_id)

    print(f"[reality-check] Starting for {database_name} (assignment v{assignment_version})")

    # Step 1: always run deterministic logic
    det = run_reality_check_deterministic(job_id, database_name, store, assignment_version)

    print(f"[reality-check] Before: {det['before_distribution']}")
    print(f"[reality-check] After:  {det['after_distribution']}")

    for line in removal_log_lines(det["consolidations"]):
        print(f"[reality-check] {line}")

    for p in det["architectural_patterns"]:
        print(f"[reality-check] Pattern: {p['name']}")

    for lw in det.get("lightweight_recommendations", []):
        print(
            f"[reality-check] LIGHTWEIGHT: {lw['service']} for "
            f"{len(lw['query_ids'])} queries (replaces {lw['replaces_engine']})"
        )

    # Step 2: LLM phase — determined by llm_mode. Bedrock validates the
    # consolidations here; its summary is written after the sweep (step 3b).
    if llm_mode == "bedrock":
        _run_bedrock_llm_phase(det, database_name)

    elif llm_mode == "external":
        llm_input = prepare_reality_check_llm_input(det)
        llm_input_key = f"{database_name}/{job_id}/reality-check/llm_input.json"
        # One record per line, so the Read pages in its header stay small (#285)
        store.write_bytes(llm_input_key, render_reality_check_request(llm_input).encode())
        awaiting_key = f"{database_name}/{job_id}/reality-check/awaiting_llm.json"
        store.write_json(awaiting_key, {"status": "awaiting_llm", "input_key": llm_input_key})
        print(f"[reality-check] LLM input written to {llm_input_key} — awaiting external LLM")

    # llm_mode == "none": skip LLM entirely, use det as-is

    # Step 3: Sanity sweep — the same shared step every llm_mode runs, in the
    # same order, after its own LLM phase has applied any corrections (#601):
    # bedrock above, external at its own finalize_reality_check call.
    _run_sanity_sweep(det)

    # Step 3b: the Bedrock summary describes the settled records, not the
    # pre-correction ones (#236); write_reality_check_result still checks it.
    if llm_mode == "bedrock":
        _settle_records(det)
        _run_bedrock_summary(det, database_name)

    # Step 4: validate and write output. In external mode the output is a
    # deterministic preview; the revised assignment is left to the finalize step
    # so a provisional consolidation never occupies a version number.
    awaiting = llm_mode == "external"
    new_version = write_reality_check_result(
        store, job_id, database_name, det, assignment_version, write_revision=not awaiting
    )

    elapsed = time.time() - start_time
    print(f"[reality-check] Complete in {elapsed:.1f}s")
    return {
        "status": "awaiting_llm" if awaiting else "complete",
        "input_version": assignment_version,
        "output_version": new_version,
    }


def write_reality_check_result(
    store: ArtifactStore,
    job_id: str,
    database_name: str,
    result: dict,
    assignment_version: int,
    *,
    write_revision: bool = True,
) -> int | None:
    """Write ``reality-check/output.json`` and, on consolidation, the revised assignment.

    ``result`` is a deterministic result (optionally merged with LLM output) for
    input ``assignment_version``. The revision goes to ``next_assignment_version()``
    — never ``assignment_version + 1`` — so an existing version (an earlier
    consolidation, a re-entry, a customer edit) is never overwritten. It is
    stamped ``source = reality_check`` and ``previous_version = assignment_version``,
    which is what ``is_reality_check_produced`` keys on.

    Returns the version written, or None when nothing was consolidated (or
    ``write_revision`` is False).
    """
    _settle_records(result)
    # #235's single rule: an engine is eliminated when no in-scope query remains on
    # it, computed once from this check's own before/after assignments. Written to
    # the output below, and threaded into the summary check further down, so the
    # handler, the UI, synthesis and the summary all read this one answer instead
    # of each re-deriving their own.
    eliminated = settled_eliminated_engines(
        result["assignment"].get("query_assignments", []), result["revised_assignments"]
    )
    # The summary was written before the steps that settle the records (in external
    # mode, by the LLM from the deterministic preview): it must describe the final
    # records, or be replaced by one that does (#236).
    finalize_executive_summary(result, eliminated)
    consolidated = bool(result["consolidations"]) and write_revision
    # max() guards a store whose listing lags its reads: the revision must land
    # above its own input even then.
    new_version = (
        max(next_assignment_version(store, database_name, job_id), assignment_version + 1)
        if consolidated
        else None
    )

    output = RealityCheckOutputContract.model_validate(
        {
            "source_assignment_version": assignment_version,
            "output_assignment_version": new_version,
            "unique_value_assessment": result["unique_value_assessment"],
            "consolidations": result["consolidations"],
            "architectural_patterns": result["architectural_patterns"],
            "executive_summary": result["executive_summary"],
            "executive_summary_source": result.get("executive_summary_source"),
            "executive_summary_llm": result.get("executive_summary_llm"),
            "executive_summary_validation_warnings": result.get(
                "executive_summary_validation_warnings", []
            ),
            "recommendations": result["recommendations"],
            "before_distribution": result["before_distribution"],
            "after_distribution": result["after_distribution"],
            "eliminated_engines": sorted(eliminated),
            "lightweight_recommendations": result.get("lightweight_recommendations", []),
            "validation_incomplete": result.get("validation_incomplete", []),
            # #503 review item 1: how many queries are still an unresolved,
            # per-query exact-score tie once the reality check has
            # re-resolved what it can.
            "query_tie_unresolved_count": sum(
                1 for qa in result.get("revised_assignments", []) if qa.get("tied_engines")
            ),
            # #589: the sanity sweep's own kept/removed verdict for every
            # tiny orphan engine it evaluated -- otherwise a KEPT engine left
            # no trace in this artifact at all.
            "sweep_decisions": result.get("sweep_decisions", []),
            # #647: what each model flag did (destination, before/after scores).
            "flag_decisions": result.get("flag_decisions", []),
            "model_flags_rejected": result.get("model_flags_rejected", 0),
        }
    )
    output_key = f"{database_name}/{job_id}/reality-check/output.json"
    store.write_json(output_key, output.model_dump(mode="json"))
    awaiting_key = f"{database_name}/{job_id}/reality-check/awaiting_llm.json"
    if write_revision and store.exists(awaiting_key):
        # A completed run supersedes any external-mode preview; from here on the
        # output counts as this lineage's run (reality_check_run_for_lineage).
        store.write_json(awaiting_key, {"status": "finalized"})

    if new_version is None:
        if not result["consolidations"]:
            print("[reality-check] No consolidations — assignment unchanged")
        return None

    moves = _ledger(result).moves
    _check_replay(result, moves)

    revised_assignment = {
        **result["assignment"],
        "version": new_version,
        "previous_version": assignment_version,
        "source": AssignmentSource.REALITY_CHECK.value,
        "timestamp": datetime.now(UTC).isoformat(),
        "query_assignments": result["revised_assignments"],
        "reality_check_applied": True,
        # #459 round 2: this version's own customer-facing safety-net
        # explanation, not carried forward from the dict this spreads --
        # a prior version's note (different numbers) would otherwise read as
        # current fact about this version's overlay. cache_notes (the
        # cumulative audit trail) is still carried forward by the spread
        # above, unchanged.
        "cache_safety_net_notes": [],
        # #616: moves are per-version -- this version holds exactly the Moves
        # that produced it (this check's), never the earlier version's.
        "moves": [m.model_dump(mode="json") for m in moves],
    }
    # ADR-029 Layers B+E: recompute derived views against the consolidated
    # routing, refresh validation warnings, and drop per-query warnings that
    # name an engine consolidation eliminated, instead of spreading the
    # previous version's stale derived views and dead-engine warnings forward.
    # ``eliminated`` (computed above, #235) is the shared in-scope rule, not the
    # any-query diff this used to compute inline -- an engine left with only
    # out-of-scope queries now has its warnings dropped too.
    revised_assignment = refresh_consolidated_assignment(
        revised_assignment,
        result["collector_output"],
        result["analysis_outputs"],
        dead_engines=eliminated,
    )
    revised_key = f"{database_name}/{job_id}/assignment/v{new_version}/assignment.json"
    store.write_json(revised_key, revised_assignment)
    print(
        f"[reality-check] Revised assignment v{assignment_version} written to v{new_version} "
        f"({len(result['consolidations'])} consolidations)"
    )
    return new_version


def finalize_reality_check(
    store: ArtifactStore,
    job_id: str,
    database_name: str,
    llm_response: dict,
    assignment_version: int | None = None,
) -> dict:
    """Merge an external LLM response and write the Reality Check result.

    The finalize half of ``llm_mode="external"``, shared by
    ``scripts/run_assessment.py --resume-reality-check`` and
    ``scripts/run_reality_check.py --finalize``. Resolves the input version the
    same way as :func:`run_reality_check_handler` (so it consolidates the version
    the external run prepared) and is a no-op when Reality Check already ran for
    the lineage, so finalizing twice never writes a duplicate version.

    Runs ``_run_sanity_sweep`` right after applying the LLM response, the
    same step in the same order as the handler's ``bedrock``/``none`` paths
    (#601), so all three modes write the same assignment for the same input
    and corrections.

    Returns the same summary shape as :func:`run_reality_check_handler`.
    """
    if assignment_version is None:
        previous_run = reality_check_run_for_lineage(store, database_name, job_id)
        if previous_run is not None:
            return _skipped(previous_run)
        assignment_version = resolve_reality_check_input_version(store, database_name, job_id)

    # The response contract both modes share (#628): an answer off it fails
    # here, before any work, with where it failed (never the model's text).
    try:
        response = parse_reality_check_response(llm_response)
    except ValidationError as exc:
        raise ResponseContractError(
            "the LLM response does not match RealityCheckModelResponse "
            f"({validation_error_note(exc)})"
        ) from exc
    det = run_reality_check_deterministic(job_id, database_name, store, assignment_version)
    result = apply_reality_check_llm_output(det, response)
    # Same shared step the handler runs for every llm_mode, in the same
    # order -- the model's response is applied first, then the sweep (#601).
    # Before this, finalize wrote a result bedrock/none never would for the
    # same input and corrections: a tiny orphan engine the sweep would have
    # removed stayed in the external version.
    _run_sanity_sweep(result)
    new_version = write_reality_check_result(
        store, job_id, database_name, result, assignment_version
    )
    return {
        "status": "complete",
        "input_version": assignment_version,
        "output_version": new_version,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _scoring_inputs(det: dict) -> tuple[dict, ScoringContext]:
    """The collector's query map and the shared scorer's context for ``det``.

    The context carries every model-flag gap accepted so far
    (``det["model_flag_gaps"]``), so a later step never routes a flagged query
    back onto the engine it was flagged on.
    """
    query_map = {
        q["query_id"]: q
        for q in det.get("collector_output", {}).get("queries", {}).get("query_patterns", [])
    }
    scoring_context = ScoringContext.from_pipeline(
        det.get("analysis_outputs", {}), det.get("triage"), det.get("collector_output")
    ).with_flag_gaps(det.get("model_flag_gaps") or {})
    return query_map, scoring_context


def _apply_model_flags(result: dict, flags: list) -> None:
    """Apply validated model flags to ``result`` in place (#647, #667).

    Maintainer decision (2026-10-14, #647): a flagged query is re-routed by the
    shared scorer only among the engines this reality check KEPT. A flag never
    brings back an engine the check removed and never re-settles any other
    query; when no kept engine can serve the flagged query, it stays where it
    is and its ``flag_decisions`` record says why.
    """
    kept_engines = {e for e, n in result["after_distribution"].items() if n}
    query_map, scoring_context = _scoring_inputs(result)
    source_engine = source_database_engine(result.get("collector_output", {}))
    (
        result["revised_assignments"],
        result["consolidations"],
        decisions,
        gaps,
    ) = apply_consolidation_flags(
        flags,
        result["revised_assignments"],
        result["consolidations"],
        result.get("assignment", {}).get("query_assignments"),
        scoring_context,
        query_map,
        kept_engines,
        query_signals=_build_query_signals(result.get("triage", {}).get("signals", [])),
        source_engine=source_engine,
        settled=settled_counts(result["revised_assignments"]),
        co_dependency_groups=result.get("assignment", {}).get("co_dependency_groups", []),
        ledger=_ledger(result),
    )
    result["flag_decisions"] = result.get("flag_decisions", []) + decisions
    merged_gaps = {k: set(v) for k, v in (result.get("model_flag_gaps") or {}).items()}
    for qid, engines in gaps.items():
        merged_gaps.setdefault(qid, set()).update(engines)
    result["model_flag_gaps"] = merged_gaps
    if not any(d["outcome"] == "moved" for d in decisions):
        return
    # Patterns and recommendations must follow the corrected assignment
    refresh_patterns_and_recommendations(result)
    after_distribution: dict[str, int] = defaultdict(int)
    for qa in result["revised_assignments"]:
        after_distribution[qa["assigned_engine"]] += 1
    result["after_distribution"] = dict(after_distribution)


def _check_replay(result: dict, moves: list) -> None:
    """Fail loudly when the revision does not replay from its input plus its Moves (#616).

    The input is the stored assignment version this check read
    (``result["input_state"]``, before the cache owner normalisation, whose
    changes are Moves too); a result built without one (an older caller)
    replays from ``result["assignment"]``. Any engine or tie set without a Move
    (a stray write that bypassed ``move_ledger.apply``) raises here instead of
    reaching a version that cannot be explained.
    """
    start = result.get("input_state") or state_of(result["assignment"].get("query_assignments", []))
    check_replay(moves, start, state_of(result["revised_assignments"]), "reality check")


def _ledger(result: dict) -> MoveLedger:
    """The move ledger of this reality check's run (#616).

    ``run_reality_check_deterministic`` puts it on the result; a result built
    without one (an older caller) gets one starting from its current engines,
    so its later steps still record their Moves. The consolidation records are
    derived from these Moves (``consolidations_from_moves``), so
    ``_settle_records`` refuses such a result when its engines changed before it
    got its ledger (``_check_replay``).
    """
    ledger = result.get("move_ledger")
    if not isinstance(ledger, MoveLedger):
        ledger = ledger_for(result.get("revised_assignments", []))
        result["move_ledger"] = ledger
    return ledger


def _skipped(previous_run) -> dict:
    """Summary for a run skipped because Reality Check already ran for the lineage."""
    return {
        "status": "skipped",
        "input_version": previous_run.input_version,
        "output_version": previous_run.output_version,
    }


def _run_sanity_sweep(det: dict) -> None:
    """Remove a tiny orphan engine the other committed engines can serve
    (ADR-031 Decision 7, #579), using the exact same shared scorer the rest
    of the pipeline uses -- never a raw query count.

    Every ``llm_mode`` calls this, in the same order, right after its own
    LLM phase has applied any corrections: :func:`run_reality_check_handler`
    for ``bedrock``/``none``/the external preview, and
    :func:`finalize_reality_check` for the external finalize (#601 -- before
    this was shared, finalize never called it, so it could write a different
    result than ``bedrock``/``none`` for the same input and corrections).

    Mutates ``det`` in place: ``revised_assignments``, ``consolidations``,
    ``sweep_decisions`` and ``after_distribution``.
    """
    query_map, scoring_context = _scoring_inputs(det)
    if "co_dependency_groups" not in det.get("assignment", {}):
        # Every version writes this key today (#579 round 3) -- only a
        # legacy artifact predating it would land here, so this is a guard
        # for the future, not a case any current run should ever hit.
        logger.warning(
            "reality-check: assignment has no co_dependency_groups; the sanity sweep "
            "will treat every query as ungrouped"
        )
    det["revised_assignments"], det["consolidations"], det["sweep_decisions"] = sanity_sweep(
        det["revised_assignments"],
        det["consolidations"],
        scoring_context,
        query_map,
        source_engine=source_database_engine(det.get("collector_output", {})),
        # Query structure, not current placement (#579 round 2): stays
        # valid regardless of how many consolidations this run already
        # made, so a co-dependency group still on the same orphan engine
        # moves as one unit instead of being split between destinations.
        co_dependency_groups=det.get("assignment", {}).get("co_dependency_groups", []),
        # The same eligibility rule Phase 2 applies needs to know which
        # specialty signals this query matched (#579 round 3,
        # ``may_absorb``'s specialist-absorb restriction).
        query_signals=_build_query_signals(det.get("triage", {}).get("signals", [])),
        ledger=_ledger(det),
    )
    # Recompute after_distribution post-sweep
    sweep_distribution: dict[str, int] = defaultdict(int)
    for qa in det["revised_assignments"]:
        sweep_distribution[qa["assigned_engine"]] += 1
    det["after_distribution"] = dict(sweep_distribution)


def _settle_records(result: dict) -> None:
    """Bring the records in line with the final revised assignment. Idempotent.

    Restores customer overrides, derives the consolidation records from the
    check's Moves so they reconcile ``before_`` with ``after_distribution`` (#218,
    #616: one record per net move the ledger replays to), and recomputes patterns
    and recommendations, which were detected before the sweep and the restore.
    """
    _restore_customer_overrides(result)
    # Derived from the ledger (#616). The records are only right when the input
    # replayed with this check's Moves is the revised assignment, so check that
    # first: a result whose engines changed without Moves (one built without a
    # ledger) fails here, rather than writing empty records beside distributions
    # that show the move.
    moves = _ledger(result).moves
    _check_replay(result, moves)
    result["consolidations"] = consolidations_from_moves(
        result["assignment"].get("query_assignments", []),
        moves,
        result["consolidations"],
        result.get("unique_value_assessment"),
        _engine_infra_cost(result.get("analysis_outputs")),
    )
    refresh_patterns_and_recommendations(result)


def _restore_customer_overrides(result: dict) -> None:
    """Put every ``customer_override`` query back on the engine the customer chose.

    Defense in depth behind the candidate exclusion in
    :func:`run_reality_check_deterministic`: the LLM corrections and the sanity
    sweep operate on the full revised list and could still move one. Mutates
    ``result`` and recomputes ``after_distribution`` when anything was restored.
    Each restore is a Move (#616; pass and rule ``customer_pick``, actor
    ``customer``) with the shared scorer's scores on both engines.
    """
    chosen = {
        qa["query_id"]: qa
        for qa in result["assignment"].get("query_assignments", [])
        if qa.get("customer_override")
    }
    if not chosen:
        return
    restored = 0
    revised: list[dict] = []
    scoring: tuple[dict, ScoringContext] | None = None
    for qa in result["revised_assignments"]:
        original = chosen.get(qa.get("query_id"))
        if original is not None and qa.get("assigned_engine") != original["assigned_engine"]:
            if scoring is None:
                scoring = _scoring_inputs(result)
            query_map, scoring_context = scoring
            query = query_map.get(qa["query_id"]) or {"query_id": qa["query_id"]}
            ledger = _ledger(result)
            # The engine the Move leaves is the ledger's (``apply`` records it).
            left = ledger.engine_of(qa["query_id"]) or qa["assigned_engine"]
            qa = dict(qa)
            apply_to_assignment(
                ledger,
                qa,
                original["assigned_engine"],
                pass_=MovePass.CUSTOMER_PICK,
                reason_code=ReasonCode.CUSTOMER_PICK,
                score_before=score_query_for_engine(query, left, scoring_context).score,
                score_after=score_query_for_engine(
                    query, original["assigned_engine"], scoring_context
                ).score,
                actor=MoveActor.CUSTOMER,
            )
            restored += 1
        revised.append(qa)
    if not restored:
        return
    print(f"[reality-check] Kept {restored} customer-overridden queries on their chosen engine")
    result["revised_assignments"] = revised
    after: dict[str, int] = defaultdict(int)
    for qa in revised:
        after[qa["assigned_engine"]] += 1
    result["after_distribution"] = dict(after)


def _run_bedrock_llm_phase(det: dict, database_name: str) -> None:
    """Run the Bedrock consolidation validation and apply its corrections.

    Mutates det in place via apply_reality_check_llm_output. The executive summary
    is generated later by :func:`_run_bedrock_summary`, once the corrections, the
    sanity sweep and the record reconciliation have settled the outcome (#236).
    """
    if not det["consolidations"]:
        return
    collector_output = det["collector_output"]
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    query_map = {q["query_id"]: q for q in queries}

    incomplete: list[dict] = []
    rejected: list[str] = []
    corrections = validate_consolidations(
        consolidations=det["consolidations"],
        revised_assignments=det["revised_assignments"],
        query_map=query_map,
        query_signals=query_signal_map(det.get("triage", {})),
        original_assignments=det["assignment"].get("query_assignments"),
        incomplete=incomplete,
        rejected=rejected,
    )
    if rejected:
        # The same count the headless response gets for an entry off the contract.
        det["model_flags_rejected"] = det.get("model_flags_rejected", 0) + len(rejected)
    if incomplete:
        # Batches with no verdict keep their moves; record them for audit
        det["validation_incomplete"] = incomplete
        print(
            f"[reality-check] WARNING: {len(incomplete)} validation batch(es) got no "
            "verdict; their moves stand unreviewed"
        )
    if corrections:
        # The same contract and the same path as the external response (#647).
        print(f"[reality-check] LLM flagged {len(corrections)} queries — re-routing by score")
        apply_reality_check_llm_output(
            det,
            RealityCheckModelResponse(
                consolidation_corrections=[f.model_dump(mode="json") for f in corrections]
            ),
        )


def _run_bedrock_summary(det: dict, database_name: str) -> None:
    """Generate the Bedrock executive summary from the settled records."""
    executive_summary = _generate_executive_summary(det)
    if executive_summary is not None:
        apply_reality_check_llm_output(
            det, RealityCheckModelResponse(executive_summary=executive_summary)
        )


# The reality check's own summary has one instruction for both LLM modes: the
# "Executive Summary" section of the consolidation-review skill, which
# ``/reality-check`` tells Claude Code to follow (#628).
REALITY_CHECK_SUMMARY_SECTION = "Executive Summary"


def reality_check_summary_payload(det: dict) -> dict:
    """The ``executive_summary`` block of the external request, for ``det``.

    Bedrock writes its summary after the flags, the sweep and the record
    settlement, so no engine's fate is still open: ``absorption_candidates``
    is empty. Headless gets the same block from the preview, with the engines
    its flags can still change listed there (#628).
    """
    summary_block: dict = build_reality_check_request(det, [])["executive_summary"]
    return summary_block


def _generate_executive_summary(det: dict) -> str | None:
    """Generate the CTO-facing reality-check summary on Bedrock.

    The prompt is the shared skill section plus the request's
    ``executive_summary`` block (:func:`reality_check_summary_payload`), the
    same instruction and data shape ``/reality-check`` gives Claude Code. The
    answer is asked for as :class:`RealityCheckModelResponse`, the contract the
    headless response uses; a plain-text answer is read as the summary.

    Returns None if the LLM is unavailable or gives no usable answer: the
    deterministic summary is built at write time instead.
    """
    try:
        from strands import Agent
    except ImportError:
        logger.warning("Strands not available — skipping executive summary")
        return None

    section = skill_sections(CONSOLIDATION_REVIEW_SKILL)[REALITY_CHECK_SUMMARY_SECTION]
    prompt = (
        "EXECUTIVE SUMMARY:\n"
        + section
        + "\n\n"
        # The block carries customer-derived strings (consolidation reasons,
        # pattern names): framed as untrusted data, not instructions (R1).
        + frame_untrusted(
            json.dumps(reality_check_summary_payload(det), separators=(",", ":"), sort_keys=True),
            label="assessment context",
        )
        + "\n\nWrite the summary now."
    )

    try:
        model = build_bedrock_model(Role.SUMMARY)
        agent = Agent(
            model=model,
            system_prompt=(
                "You are a senior database architect presenting the result of a completed "
                "modernization assessment to a CTO.\n\n" + SYSTEM_PROMPT_DATA_DIRECTIVE
            ),
            tools=[],
            structured_output_model=RealityCheckModelResponse,
            callback_handler=None,
        )

        print("[reality-check] Generating executive summary with LLM...")
        result = agent(prompt)
        structured = getattr(result, "structured_output", None)
        raw: object = (
            structured
            if isinstance(structured, RealityCheckModelResponse)
            else {"executive_summary": str(result).strip()}
        )
        narrative = (parse_reality_check_response(raw).executive_summary or "").strip()
        if len(narrative) > 20:
            print(f"[reality-check] Executive summary generated ({len(narrative)} chars)")
            return narrative

    except ValidationError as exc:
        logger.error("LLM executive summary is off the contract (%s)", validation_error_note(exc))
    except Exception as exc:
        model_id = resolve_model_id(Role.SUMMARY)
        # The underlying error from an LLM call can carry prompt/response
        # fragments (#143): full text at DEBUG only.
        logger.debug("LLM executive summary failed (model=%s): %s", model_id, exc)
        logger.error("LLM executive summary failed (model=%s): %s", model_id, type(exc).__name__)
        print(
            f"[reality-check] ERROR: LLM summary failed with model '{model_id}' ({type(exc).__name__})"
        )
        print("[reality-check] Set SUMMARY_MODEL_ID env var to override the model.")

    return None
