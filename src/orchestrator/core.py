# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The pipeline's phase entry points over one ArtifactStore.

``assess()`` (``src/assessment/runner.py``), the local orchestrator and
``scripts/run_post_schema_routing.py`` call these functions, so each phase has one
implementation.

Everything here operates on the ArtifactStore abstraction. Storage type
(local dir vs S3) is controlled by env vars via create_artifact_store().
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import NamedTuple

from src.shared.ranking import main_engine
from src.storage.assignment_versioning import (
    resolve_downstream_assignment_version,
    resolve_reality_check_input_version,
)

logger = logging.getLogger(__name__)


def make_store():
    """Create a text-capable ArtifactStore.

    ``create_artifact_store()`` decides S3-vs-local from env and ``upgrade_store``
    re-homes it onto the subclass that adds ``write_text``.
    """
    from src.storage import create_artifact_store
    from src.storage.text_store import upgrade_store

    return upgrade_store(create_artifact_store())


def ingest_offline_collection(store, job_id: str, database_name: str, raw_input: dict) -> dict:
    """Build a collector output contract from a raw offline collection dict.

    Keeps every statement (no cap, #384) and records the capture in
    ``metadata.query_capture``, as the scripts do by default.

    Mirrors scripts/run_assessment.py::phase_collect exactly, but reads the raw
    input from a dict (sourced via ArtifactStore) instead of a local file, and
    writes the result back through the ArtifactStore. Builds the output with the
    same ``build_offline_collector_output`` the scripts use (#1177), so the source
    engine is the one ``offline_parser.detect_source_engine`` reads.

    Returns the collector output dict that was written.
    """
    from src.agents.collector.offline_ingest import build_offline_collector_output
    from src.contracts.collector_output import CollectorOutputContract
    from src.shared.contract_versioning import (
        carries_contract,
        read_contract_payload,
        stamp_artifact_version,
    )
    from src.shared.sample_rows import collector_output_without_sample_rows
    from src.tools.database.offline_parser import parse_offline_collection
    from src.tools.validation.ingestion_limits import check_collection_counts

    # A stored collector output carries a contract_version (ADR-008 Decision 4: a
    # different MAJOR is refused); a raw collection carries none.
    is_contract = carries_contract(
        raw_input, CollectorOutputContract, artifact=f"{database_name}/{job_id} upload"
    )

    if is_contract:
        # Non-contract input is counted inside parse_offline_collection (#142);
        # contract-format input bypasses that parser, so check it here.
        check_collection_counts(
            table_count=len((raw_input.get("database_schema") or {}).get("tables") or []),
            query_count=len((raw_input.get("queries") or {}).get("query_patterns") or []),
            source=f"{database_name}/{job_id}",
        )
        # #387: an older collector output may carry table rows; none are kept.
        collector_data, sample_rows_dropped = collector_output_without_sample_rows(raw_input)
        # Read as a stored one is, before it is stamped with today's version: through the
        # adapter of an older MAJOR and every read's transforms, so no literal value is
        # kept in its text (#1625) and an older 0 for an unknown row count is unknown
        # (#1636).
        collector_data = dict(
            read_contract_payload(
                collector_data, CollectorOutputContract, artifact=f"{database_name}/{job_id} upload"
            )
        )
        if sample_rows_dropped:
            logger.warning(
                "ingest: dropped %d sample row(s) the collector output carried (#387)",
                sample_rows_dropped,
            )
    else:
        # The run's name never keys the tables: the collection's own name does (#1572).
        parsed = parse_offline_collection(
            raw_input, database_name=database_name, source=f"{database_name}/{job_id} upload"
        )
        # The one source-engine detector (#1177): this path used to guess on its own
        # ("postgres" in the version string, else MySQL).
        collector_data = build_offline_collector_output(
            parsed, job_id=job_id, database_name=database_name, s3_key="in-memory"
        )

    # #384: no cap, so nothing is cut; the capture is recorded.
    from src.shared.query_capture import cap_collection

    cap_collection(collector_data)

    # #2145: the same merge figures the scripts' collect status line reports; the
    # collector output's ``metadata.query_capture`` (captured, query_shapes) holds them too.
    from src.shared.query_capture import query_merge_summary

    if not is_contract:
        merge = query_merge_summary(
            int(parsed.get("database_query_rows") or 0),
            len(collector_data.get("queries", {}).get("query_patterns", [])),
        )
        if merge:
            logger.info(
                "collect: %d script rows -> %d query shapes, %d literal variants merged "
                "(routing counts query shapes; executions and time are unchanged)",
                merge["script_rows"],
                merge["query_shapes"],
                merge["literal_variants_merged"],
            )

    collector_key = f"{database_name}/{job_id}/collector/output.json"
    # An uploaded collector output may be at an older MINOR: written at today's (#1018).
    store.write_json(collector_key, stamp_artifact_version(collector_key, collector_data))

    return collector_data


def run_triage_core(job_id: str, database_name: str, store=None) -> dict:
    """Run triage only (assumes collector output already exists). Returns a summary dict.

    Reads the collector output from
    the ArtifactStore and writes the triage decision.

    Raises FileNotFoundError if the collector output is missing.
    """
    store = store or make_store()
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    if not store.exists(collector_key):
        raise FileNotFoundError(
            f"Collector output not found at '{collector_key}'. Run the collector agent first."
        )

    from src.agents.referee.triage_handler import run_triage

    run_triage(job_id, database_name, store)

    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    selected_engines: list[str] = []
    signal_count = 0
    if store.exists(triage_key):
        triage = store.read_json(triage_key)
        selected_engines = [
            a["agent_type"] if isinstance(a, dict) else a for a in triage.get("selected_agents", [])
        ]
        signal_count = len(triage.get("signals", []))

    return {
        "job_id": job_id,
        "database_name": database_name,
        "selected_engines": selected_engines,
        "signal_count": signal_count,
        "triage_artifact": triage_key,
    }


class _AnalysisEngine(NamedTuple):
    """Per-engine knobs for :func:`run_analysis_core`.

    ``target_database`` is the ``TargetDatabase`` value, the
    ``analysis-<...>`` artifact prefix suffix, and the value echoed in the
    summary. ``load_analyze`` returns the engine's analyze function via a
    deferred literal import, so only the selected engine's (heavy) analysis
    module loads and never at core.py import time. ``pass_llm_mode`` calls the
    analyze function with ``llm_mode="none"`` (LLM-capable engines) vs no
    llm_mode arg (purely deterministic ones). ``mermaid_always`` writes the ER
    diagram unconditionally (DynamoDB/DocumentDB) vs only when the analyzer
    produced one. ``llm_advisor_from_trace`` reads ``llm_advisor.status`` from
    the decision trace vs reporting ``"not_applicable"``.
    """

    target_database: str
    load_analyze: Callable[[], Callable]
    pass_llm_mode: bool
    mermaid_always: bool
    llm_advisor_from_trace: bool


def _load_dynamodb_analyze() -> Callable:
    from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb

    return analyze_for_dynamodb


def _load_documentdb_analyze() -> Callable:
    from src.agents.analysis.documentdb_analysis_agent import analyze_for_documentdb

    return analyze_for_documentdb


def _load_elasticache_analyze() -> Callable:
    from src.agents.analysis.elasticache_analysis_agent import analyze_for_elasticache

    return analyze_for_elasticache


def _load_opensearch_analyze() -> Callable:
    from src.agents.analysis.opensearch_analysis_agent import analyze_for_opensearch

    return analyze_for_opensearch


def _load_aurora_pg_analyze() -> Callable:
    from src.agents.analysis.aurora_pg_analysis_agent import analyze_for_aurora_pg

    return analyze_for_aurora_pg


def _load_aurora_mysql_analyze() -> Callable:
    from src.agents.analysis.aurora_mysql_analysis_agent import analyze_for_aurora_mysql

    return analyze_for_aurora_mysql


_ANALYSIS_ENGINES: dict[str, _AnalysisEngine] = {
    "dynamodb": _AnalysisEngine(
        target_database="dynamodb",
        load_analyze=_load_dynamodb_analyze,
        pass_llm_mode=True,
        mermaid_always=True,
        llm_advisor_from_trace=True,
    ),
    "documentdb": _AnalysisEngine(
        target_database="documentdb",
        load_analyze=_load_documentdb_analyze,
        pass_llm_mode=True,
        mermaid_always=True,
        llm_advisor_from_trace=True,
    ),
    "elasticache": _AnalysisEngine(
        target_database="elasticache",
        load_analyze=_load_elasticache_analyze,
        pass_llm_mode=False,
        mermaid_always=False,
        llm_advisor_from_trace=False,
    ),
    "opensearch": _AnalysisEngine(
        target_database="opensearch",
        load_analyze=_load_opensearch_analyze,
        pass_llm_mode=False,
        mermaid_always=False,
        llm_advisor_from_trace=False,
    ),
    "aurora_postgresql": _AnalysisEngine(
        target_database="aurora_postgresql",
        load_analyze=_load_aurora_pg_analyze,
        pass_llm_mode=True,
        mermaid_always=False,
        llm_advisor_from_trace=False,
    ),
    "aurora_mysql": _AnalysisEngine(
        target_database="aurora_mysql",
        load_analyze=_load_aurora_mysql_analyze,
        pass_llm_mode=True,
        mermaid_always=False,
        llm_advisor_from_trace=False,
    ),
}


# Keys are engine catalog ids (``src/catalog/engines/<id>.toml``), each equal to its
# ``target_database``; ``test_engine_catalog.py`` guards it (#1179).


def _resolve_engine_key(token: str) -> str | None:
    """The token when it is an analysis engine id, else None.

    Non-analysis tokens (e.g. deferred "keyspaces", "neptune") resolve to None so
    callers can ignore them.
    """
    return token if token in _ANALYSIS_ENGINES else None


def _confidence_band_counts(table_recommendations) -> dict[str, int]:
    """Aggregate table recommendations by confidence band.

    Thresholds per the shared scoring layer (src/tools/analysis/scoring.py):
    >=80 HIGHLY_SUITABLE, >=60 SUITABLE, >=40 MARGINAL, <40 NOT_SUITABLE.
    """
    counts = {"HIGHLY_SUITABLE": 0, "SUITABLE": 0, "MARGINAL": 0, "NOT_SUITABLE": 0}
    for tr in table_recommendations:
        score = tr.confidence_score
        if score >= 80:
            counts["HIGHLY_SUITABLE"] += 1
        elif score >= 60:
            counts["SUITABLE"] += 1
        elif score >= 40:
            counts["MARGINAL"] += 1
        else:
            counts["NOT_SUITABLE"] += 1
    return counts


def run_analysis_core(engine: str, job_id: str, database_name: str, store=None) -> dict:
    """Run one engine's analysis for a completed collector run. Returns a summary dict.

    Reads the collector output from the
    ArtifactStore, constructs AnalysisInput, invokes the engine's analyze
    function, and writes analysis.json + decision-trace.json (+ er-diagram.mmd
    for engines that produce one) under ``<db>/<job>/analysis-<target_database>/``.

    Per-engine behaviour is table-driven — see ``_ANALYSIS_ENGINES``. DynamoDB and
    DocumentDB run the LLM advisor (``llm_mode="none"`` keeps it off, matching
    core-modernizer's ``run_analysis()``, which overrides the analyze function's
    own "bedrock" default; hardcoded so container config cannot re-enable the
    30-60 min advisor pass) and always emit an ER diagram. The other four are
    purely deterministic and emit an ER diagram only when the analyzer produced one.

    Raises FileNotFoundError if the collector output is missing, or KeyError if
    ``engine`` is not a known analysis engine.
    """
    spec = _ANALYSIS_ENGINES[engine]
    store = store or make_store()
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    if not store.exists(collector_key):
        raise FileNotFoundError(
            f"Collector output not found at '{collector_key}'. " "Run the collector agent first."
        )

    from src.storage.collector_for_models import read_collector_for_models

    collector_output = read_collector_for_models(store, database_name, job_id)

    # Deferred imports — avoid loading heavy analysis modules at core.py import time.
    from src.contracts.analysis_input import AnalysisInput, TargetDatabase

    analyze_fn = spec.load_analyze()
    analysis_input = AnalysisInput(
        job_id=job_id,
        collector_output=collector_output,
        target_database=TargetDatabase(spec.target_database),
    )

    logger.info(
        "analysis-%s starting: job_id=%s db=%s",
        spec.target_database,
        job_id,
        database_name,
    )
    if spec.pass_llm_mode:
        contract, decision_trace, mermaid_diagram = analyze_fn(analysis_input, llm_mode="none")
    else:
        contract, decision_trace, mermaid_diagram = analyze_fn(analysis_input)

    prefix = f"{database_name}/{job_id}/analysis-{spec.target_database}"
    analysis_key = f"{prefix}/analysis.json"
    trace_key = f"{prefix}/decision-trace.json"

    store.write_json(analysis_key, contract.model_dump(mode="json"))
    from src.agents.analysis.decision_trace import write_decision_trace

    write_decision_trace(store, trace_key, decision_trace)

    mermaid_key: str | None = None
    if spec.mermaid_always or mermaid_diagram:
        mermaid_key = f"{prefix}/er-diagram.mmd"
        try:
            store.write_text(mermaid_key, mermaid_diagram, content_type="text/x-mermaid")
        except NotImplementedError:
            # assess(store=...) runs on a caller's store as given. One that cannot hold
            # the .mmd text does not fail the engine's analysis: the ER diagram is rebuilt
            # on demand from the analysis contract, so drop the reference.
            logger.debug(
                "analysis-%s: store cannot write text; skipping er-diagram.mmd",
                spec.target_database,
            )
            mermaid_key = None

    level_counts = _confidence_band_counts(contract.table_recommendations)

    if spec.llm_advisor_from_trace:
        llm_status = "unknown"
        if isinstance(decision_trace, dict):
            llm_status = decision_trace.get("llm_advisor", {}).get("status", "unknown")
    else:
        llm_status = "not_applicable"

    return {
        "job_id": job_id,
        "database_name": database_name,
        "target_database": spec.target_database,
        "tables_analyzed": len(contract.table_recommendations),
        "highly_suitable_count": level_counts["HIGHLY_SUITABLE"],
        "suitable_count": level_counts["SUITABLE"],
        "marginal_count": level_counts["MARGINAL"],
        "not_suitable_count": level_counts["NOT_SUITABLE"],
        "estimated_monthly_cost_usd": contract.cost_estimate.monthly_cost_usd,
        "llm_advisor_status": llm_status,
        "analysis_artifact": analysis_key,
        "decision_trace_artifact": trace_key,
        "er_diagram_artifact": mermaid_key,
    }


def _selected_engines_from_triage(job_id: str, database_name: str, store) -> list[str]:
    """Read triage's selected engines. Raises FileNotFoundError if triage is missing.

    Triage already encodes source-engine constraints (a MySQL source yields
    aurora_mysql, not aurora_postgresql), so the consolidated analysis agent trusts
    this list rather than re-deriving it.
    """
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    if not store.exists(triage_key):
        raise FileNotFoundError(f"Triage output not found at '{triage_key}'. Run triage first.")
    triage = store.read_json(triage_key)
    engines: list[str] = []
    for a in triage.get("selected_agents", []):
        engine = a["agent_type"] if isinstance(a, dict) else a
        if engine:
            engines.append(str(engine))
    return engines


def _skipped_engines_from_triage(job_id: str, database_name: str, store) -> list[tuple[str, str]]:
    """Read triage's skipped engines as ``(engine_key, reason)`` pairs.

    Triage records why each candidate engine was not analyzed (e.g. a PostgreSQL
    source skips ``aurora_mysql`` with "Source engine does not match this Aurora
    variant"). Tokens that are not an analysis engine id are ignored. Returns an empty list if triage is missing so callers can
    treat skip reporting as best-effort.
    """
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    if not store.exists(triage_key):
        return []
    triage = store.read_json(triage_key)
    skipped: list[tuple[str, str]] = []
    for a in triage.get("skipped_agents", []):
        if not isinstance(a, dict):
            continue
        key = _resolve_engine_key(str(a.get("agent_type", "")))
        if key is None:
            continue
        skipped.append((key, str(a.get("reason", ""))))
    return skipped


def run_all_analyses(
    job_id: str,
    database_name: str,
    engines: list[str] | None = None,
    store=None,
    on_engine_start: Callable[[str, str], None] | None = None,
    on_engine_done: Callable[[str, str, dict], None] | None = None,
    on_engine_error: Callable[[str, str, str], None] | None = None,
    on_engine_skipped: Callable[[str, str, str], None] | None = None,
) -> dict:
    """Run every triage-selected engine's analysis in one process.

    The consolidated form of the six per-engine analysis subagents (see ADR-024).
    Analysis is deterministic and millisecond-scale, so engines run sequentially:
    a plain loop over the unchanged :func:`run_analysis_core`, which writes the
    same ``analysis-<engine>/analysis.json`` artifacts the Assign phase reads.
    Because the durable contract is those artifacts, nothing downstream changes.

    ``engines`` defaults to triage's selected engines. Engines are named by their
    catalog id (e.g. "aurora_postgresql"), the same id triage emits. A
    caller-supplied id that is not an analysis engine raises ``ValueError``; a
    triage token that is not one is dropped with a warning. Engines are
    de-duplicated while preserving order.

    The optional callbacks report per-engine progress; each receives the engine
    key and the step label ``analysis_<target_database>``. When ``engines`` is left to default (derived
    from triage), ``on_engine_skipped`` is also invoked for each candidate engine
    triage chose not to analyze, carrying triage's reason so the UI can explain
    why (e.g. a PostgreSQL source skips Aurora MySQL). Skip reporting is suppressed
    when the caller passes an explicit ``engines`` override.

    A single engine's failure is recorded and reported but does not abort the rest
    (Assign tolerates partial analysis); only an all-engines failure raises.
    """
    store = store or make_store()
    from_triage = engines is None
    if engines is None:
        engines = _selected_engines_from_triage(job_id, database_name, store)

    # Keep the analysis engine ids, de-duplicating while preserving order.
    known: list[str] = []
    unknown: list[str] = []
    for token in engines:
        key = _resolve_engine_key(token)
        if key is None:
            unknown.append(token)
        elif key not in known:
            known.append(key)
    if unknown and not from_triage:
        # A caller's list names engines by catalog id; an unknown one is a mistake,
        # never a silent skip.
        raise ValueError(
            f"Unknown analysis engine id(s) {unknown}; expected catalog ids "
            f"{sorted(_ANALYSIS_ENGINES)}"
        )
    if unknown:
        logger.warning("run_all_analyses: skipping unknown engines %s", unknown)

    results: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for engine in known:
        phase = f"analysis_{_ANALYSIS_ENGINES[engine].target_database}"
        if on_engine_start is not None:
            on_engine_start(engine, phase)
        try:
            summary = run_analysis_core(engine, job_id, database_name, store=store)
            results[engine] = summary
            if on_engine_done is not None:
                on_engine_done(engine, phase, summary)
        except Exception as exc:  # noqa: BLE001 - record per-engine, keep going
            logger.exception("run_all_analyses: engine %s failed", engine)
            errors[engine] = str(exc)
            if on_engine_error is not None:
                on_engine_error(engine, phase, str(exc))

    if known and not results:
        raise RuntimeError(f"All {len(known)} analysis engines failed: {errors}")

    # Report triage-derived skips so the UI can explain why an engine was not
    # analyzed. Only meaningful when engines came from triage, not an override.
    skipped: dict[str, str] = {}
    if from_triage:
        for engine, reason in _skipped_engines_from_triage(job_id, database_name, store):
            if engine in known:
                continue
            skipped[engine] = reason
            if on_engine_skipped is not None:
                phase = f"analysis_{_ANALYSIS_ENGINES[engine].target_database}"
                on_engine_skipped(engine, phase, reason)

    return {
        "job_id": job_id,
        "database_name": database_name,
        "engines_requested": list(engines),
        "engines_analyzed": list(results.keys()),
        "engines_failed": list(errors.keys()),
        "engines_skipped": skipped,
        "per_engine": results,
    }


def run_assignment_core(
    job_id: str,
    database_name: str,
    store=None,
) -> dict:
    """Run assignment for a completed collector + triage + analysis pipeline.

    Deterministic — no LLM invocation.
    Scores every query against selected candidate engines (from triage output)
    using each engine's analysis output, producing a per-query assignment
    mapping. Writes to S3:

      - <db>/<job>/assignment/v1/assignment.json (query -> engine mapping)

    Prerequisites: collector/output.json + referee-triage/triage.json + at least
    one analysis-<engine>/analysis.json must exist. If those are missing,
    assignment_resolver will raise.
    """
    store = store or make_store()

    # Verify collector + triage prerequisites (analysis outputs are checked by
    # the resolver itself).
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    for key in (collector_key, triage_key):
        if not store.exists(key):
            raise FileNotFoundError(
                f"Prerequisite artifact missing: '{key}'. Run collector + triage first."
            )

    from src.agents.referee.assignment_handler import run_assignment_resolver

    logger.info(
        "assignment starting: job_id=%s db=%s",
        job_id,
        database_name,
    )
    run_assignment_resolver(job_id, database_name, store)

    # Summarize the produced assignment. The resolver writes next_assignment_version
    # (v1 on a fresh job, v3+ on a re-run), so read the version it just wrote.
    assignment_version = _resolve_assignment_version(store, job_id, database_name) or 1
    assignment_key = f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
    engine_counts: dict[str, int] = {}
    total_queries = 0
    if store.exists(assignment_key):
        assignment = store.read_json(assignment_key)
        for qa in assignment.get("query_assignments", []):
            e = qa.get("assigned_engine", "unknown")
            engine_counts[e] = engine_counts.get(e, 0) + 1
            total_queries += 1

    return {
        "job_id": job_id,
        "database_name": database_name,
        "assignment_version": assignment_version,
        "total_queries": total_queries,
        "queries_per_engine": engine_counts,
        "assignment_artifact": assignment_key,
    }


def _resolve_assignment_version(store, job_id: str, database_name: str) -> int:
    """Return the highest existing assignment version under ``assignment/``, or 0.

    Thin wrapper over the shared resolver (ADR-028) preserving this module's
    ``(store, job_id, database_name)`` argument order for existing callers. This
    is how Schema Design and Synthesis pick up the consolidated assignment
    Reality Check produced (v2) without the version being threaded through the
    LLM. Returns 0 when no versioned assignment exists.
    """
    from src.storage.assignment_versioning import resolve_effective_assignment_version

    return resolve_effective_assignment_version(store, database_name, job_id)


def run_reality_check_core(
    job_id: str,
    database_name: str,
    assignment_version: int | None = None,
    store=None,
    llm_mode: str | None = None,
) -> dict:
    """Run Reality Check (CTO-level engine consolidation) for a completed assignment.

    The final phase of the assessment core (ADR-018, ADR-026). Reads
    ``assignment/v{assignment_version}/assignment.json`` plus triage, collector,
    and per-engine analysis, applies the deterministic consolidation logic, then
    runs the Bedrock LLM pass (validates the consolidations and writes the CTO
    executive summary). Writes ``reality-check/output.json`` always and, only when
    it consolidates, ``assignment/v{N+1}/assignment.json``.

    ``llm_mode``, when given, is the mode; otherwise it comes from
    ``REALITY_CHECK_LLM_MODE`` (default ``"bedrock"``, per ADR-026). ``"none"`` runs
    purely deterministically (e.g. in tests without Bedrock access, or the public
    ``assess()`` with ``deterministic=True``); ``"external"`` is rejected with a ``ValueError``
    before any work (it needs an out-of-band resume this core cannot do), and so
    is any other value than ``"bedrock"`` or ``"none"``.

    Prerequisites: collector/output.json, referee-triage/triage.json,
    assignment/v{assignment_version}/assignment.json, and at least one analysis
    output. Returns a summary with before/after distribution, the consolidation
    count, and ``effective_assignment_version`` (the latest version after the run:
    the consolidation Reality Check wrote, else the source version).

    ``assignment_version`` defaults to ``resolve_reality_check_input_version``
    (the newest version Reality Check did not produce itself), and the revision
    goes to ``next_assignment_version()`` so no version is overwritten (issue
    #189). With the default, a run whose input is already consolidated is a no-op.
    """
    if llm_mode is None:
        llm_mode = os.environ.get("REALITY_CHECK_LLM_MODE", "bedrock")
    if llm_mode == "external":
        # ADR-026 Decision 4: this core has no mid-run input, so nothing could
        # supply the response and the run would report success while still waiting.
        raise ValueError(
            "REALITY_CHECK_LLM_MODE=external is not supported in the assessment core "
            "(ADR-026 Decision 4); use 'bedrock' or 'none'."
        )
    if llm_mode not in ("bedrock", "none"):
        raise ValueError(
            f"REALITY_CHECK_LLM_MODE={llm_mode!r} is not a valid mode; " "use 'bedrock' or 'none'."
        )

    store = store or make_store()

    requested_version = assignment_version
    if assignment_version is None:
        assignment_version = resolve_reality_check_input_version(store, database_name, job_id)

    prefix = f"{database_name}/{job_id}"
    collector_key = f"{prefix}/collector/output.json"
    triage_key = f"{prefix}/referee-triage/triage.json"
    assignment_key = f"{prefix}/assignment/v{assignment_version}/assignment.json"
    for key in (collector_key, triage_key, assignment_key):
        if not store.exists(key):
            raise FileNotFoundError(
                f"Prerequisite artifact missing: '{key}'. "
                "Run collector + triage + assignment first."
            )

    from src.agents.referee.reality_check_handler import run_reality_check_handler

    logger.info(
        "reality-check starting: job_id=%s db=%s assignment_version=%s llm_mode=%s",
        job_id,
        database_name,
        assignment_version,
        llm_mode,
    )
    run_reality_check_handler(
        job_id,
        database_name,
        store,
        assignment_version=requested_version,
        llm_mode=llm_mode,
    )

    from src.storage.assignment_versioning import (
        reality_check_output_candidates,
        reality_check_output_path,
    )

    effective_version = (
        _resolve_assignment_version(store, job_id, database_name) or assignment_version
    )
    # The record of the run the effective version descends from (#193, #215), as
    # synthesis, the UI and the graph read it.
    output_key = next(
        (
            k
            for k in reality_check_output_candidates(
                store, database_name, job_id, effective_version
            )
            if store.exists(k)
        ),
        reality_check_output_path(database_name, job_id),
    )
    rc = store.read_json(output_key) if store.exists(output_key) else {}
    before_distribution: dict = rc.get("before_distribution", {})
    after_distribution: dict = rc.get("after_distribution", {})
    consolidations = len(rc.get("consolidations", []))

    return {
        "job_id": job_id,
        "database_name": database_name,
        "source_assignment_version": assignment_version,
        "effective_assignment_version": effective_version,
        "consolidations": consolidations,
        "before_distribution": before_distribution,
        "after_distribution": after_distribution,
        "reality_check_artifact": output_key,
    }


def run_synthesis_core(
    job_id: str,
    database_name: str,
    assignment_version: int | None = None,
    store=None,
) -> dict:
    """Run referee-synthesis for a completed analysis + assignment pipeline.

    Deterministic-first: all
    builders run without an LLM, then a single Bedrock call generates the
    executive summary. This matches core-modernizer, whose ``entrypoint.py``
    calls ``run_synthesis(...)`` without an ``llm_mode`` argument and therefore
    takes its ``"bedrock"`` default. Unlike the analysis phases — where
    core-modernizer passes ``llm_mode="none"`` via ``run_analysis`` — synthesis
    is genuinely LLM-assisted upstream, so parity means keeping it.

    Writes to S3, at a key that depends on ``assignment_version``:
      - assignment_version > 0:  <db>/<job>/synthesis/v<N>/report.json
      - assignment_version == 0: <db>/<job>/referee-synthesis/report.json

    ``assignment_version`` controls three separate behaviours, and passing it
    correctly is the single most important input to this phase:
      1. whether the assignment is read at all (at 0 it is skipped entirely);
      2. which schema-design path is loaded (versioned vs unversioned);
      3. where this report is written (above).

    Prerequisites: collector/output.json, referee-triage/triage.json, and at
    least one analysis-<engine>/analysis.json. When assignment_version > 0 the
    matching assignment/v<N>/assignment.json must also exist.

    ``assignment_version=None`` (the default) resolves the effective version with
    ``resolve_downstream_assignment_version`` (the newest version, coerced to 1),
    so a caller that omits it reads the latest consolidation, not a pinned v1.
    """
    store = store or make_store()
    if assignment_version is None:
        assignment_version = resolve_downstream_assignment_version(store, database_name, job_id)

    collector_key = f"{database_name}/{job_id}/collector/output.json"
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    for key in (collector_key, triage_key):
        if not store.exists(key):
            raise FileNotFoundError(
                f"Prerequisite artifact missing: '{key}'. Run collector + triage first."
            )

    if assignment_version > 0:
        assignment_key = (
            f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
        )
        if not store.exists(assignment_key):
            raise FileNotFoundError(
                f"Prerequisite artifact missing: '{assignment_key}'. "
                f"Either run the assignment agent to produce version "
                f"{assignment_version}, or pass the version that exists — "
                f"synthesis silently produces an empty report when the "
                f"assignment cannot be found."
            )

    from src.agents.referee.synthesis_handler import run_synthesis

    logger.info(
        "referee-synthesis starting: job_id=%s db=%s assignment_version=%s",
        job_id,
        database_name,
        assignment_version,
    )
    run_synthesis(job_id, database_name, store, assignment_version=assignment_version)

    if assignment_version > 0:
        report_key = f"{database_name}/{job_id}/synthesis/v{assignment_version}/report.json"
    else:
        report_key = f"{database_name}/{job_id}/referee-synthesis/report.json"

    if not store.exists(report_key):
        raise FileNotFoundError(f"Synthesis completed but no report was written at '{report_key}'.")

    report = store.read_json(report_key)
    ranking = report.get("ranking") or []
    architecture = report.get("recommended_architecture") or {}
    databases = architecture.get("databases") or []
    risk = (report.get("risk_assessment") or {}).get("overall_risk_level")

    # An empty recommended_architecture has TWO distinct causes, and they warrant
    # different treatment. Measured against webapp-test-24 and v2-e2e-01 on
    # 2026-08-21.
    #
    #   a) wrong assignment_version -> the assignment is never read at all, so
    #      assignment_summary is absent too. The report is genuinely worthless:
    #      no ranking input, no workload distribution, and a rationale reading
    #      "Insufficient data to recommend a specific architecture." This is the
    #      defect that went unnoticed for a month in core-modernizer, where the
    #      deployed Step Functions does not pass ASSIGNMENT_VERSION to
    #      referee-synthesis. RAISE — publishing it would misinform.
    #
    #   b) schema-design never ran -> build_table_mappings derives mappings from
    #      schema_design output rather than from the assignment, so table_mappings
    #      and query_groups are empty. build_architecture_recommendation still
    #      lists every engine with assigned queries (with no tables; #281), so
    #      databases is NOT empty in this case. The assignment IS read and
    #      everything else populates: ranking, workload split, architecture_type,
    #      risk assessment, executive summary.
    #      WARN — the report is a real answer with documented gaps.
    #
    # (b) was originally also a raise. That was wrong, and the way it was wrong is
    # worth remembering: the exception fired AFTER _write_synthesis_report had
    # already persisted a valid 48 KB report, so it converted a partial success
    # into a total phase failure and left the orchestrator telling the customer
    # "no report was produced" while the report sat in S3 with a 1,483-character
    # executive summary. A guard that runs after the artifact is durable must not
    # raise on a state the pipeline is expected to reach.
    warnings: list[str] = []
    # Hoisted above the guard below: when no schema design exists, the generated
    # executive summary cannot be trusted to describe it, so the guard downgrades
    # to a warning rather than failing. The orchestrator recomputes this from the
    # report when it renders the customer deliverables.
    any_schema_design = any(r.get("schema_design_available") for r in ranking)
    if ranking and not databases:
        assignment_was_read = bool(report.get("assignment_summary"))
        if not assignment_was_read:
            raise ValueError(
                f"Synthesis ranked {len(ranking)} engine(s) but never read the "
                f"assignment, so recommended_architecture is empty. This is the "
                f"signature of a wrong assignment_version — it was "
                f"{assignment_version}. At version 0 the assignment is skipped "
                f"entirely and the schema design is looked up at an unversioned path "
                f"that does not exist. Pass the version the assignment agent actually "
                f"produced."
            )
    if ranking and not any_schema_design:
        warnings.append(
            f"No engine has schema-design output, so table_mappings and query_groups "
            f"are empty and recommended_architecture.databases allocates no tables. "
            f"The assignment (version {assignment_version}) was read correctly and "
            f"every other section is populated: engine ranking, workload "
            f"distribution, architecture type, risk assessment and executive summary. "
            f"table_mappings is derived from schema-design output, not from the "
            f"assignment, so running schema-design is what fills these fields. This "
            f"is a known pipeline gap, not a failure."
        )

    return {
        "job_id": job_id,
        "database_name": database_name,
        "assignment_version": assignment_version,
        "engines_ranked": len(ranking),
        # The engine owning the largest workload share, never the cache layer (#152)
        "top_engine": (main_engine(ranking) or {}).get("target"),
        "architecture_type": architecture.get("architecture_type"),
        "recommended_databases": [d.get("service") for d in databases],
        "table_mappings": len(report.get("table_mappings") or []),
        "query_groups": len(report.get("query_groups") or []),
        "overall_risk_level": risk,
        # The report key is "summary" (LLM) with "summary_deterministic" alongside
        # it; there is no "executive_summary" key. Measured on webapp-test-24.
        "has_executive_summary": bool(report.get("summary")),
        "warnings": warnings,
        "report_artifact": report_key,
    }


# Target engines that share a family with a relational source, keyed by the
# source engine reported in the collector's metadata. Used to tell "no schema
# redesign is required" apart from "this report does not cover that conversion".
_SAME_FAMILY: dict[str, set[str]] = {
    "postgresql": {"aurora_postgresql"},
    "mysql": {"aurora_mysql"},
}


def schema_no_design_notes(
    target_type: str,
    source_engine: str,
    status: str = "skipped",
    reason: str = "",
) -> tuple[list[str], list[str]]:
    """Return ``(notes, warnings)`` for an engine that produced no schema design.

    A same-family target gets an informational note that the existing schema
    carries over; a skipped engine gets its skip reason; anything else gets a
    warning that the report does not cover the conversion. Called by
    ``run_schema_design_core`` after a design runs and produces nothing.
    """
    notes: list[str] = []
    warnings: list[str] = []
    if target_type in _SAME_FAMILY.get(source_engine, set()):
        notes.append(
            f"{target_type}: no schema design required. Source and target are both "
            f"{source_engine}, so the existing schema carries over unchanged."
        )
    elif status == "skipped":
        notes.append(f"{target_type}: {reason or 'no queries or tables assigned'}.")
    else:
        warnings.append(
            f"{target_type}: schema design not included in this report."
            + (
                f" The source engine is {source_engine}, so type and object mappings for "
                f"this target would need a separate schema conversion assessment."
                if source_engine
                else " Type and object mappings for this target are not covered here."
            )
        )
    return notes, warnings


# Each schema designer names its output after the target's own vocabulary, so
# there is no single field that means "a design exists". Values are
# (artifact field, human-readable unit).
#
# Upstream's ``schema_design_available`` normalises ``collections`` and
# ``index_designs`` into a common count but not ``key_designs``, so an
# ElastiCache design reads as absent in the synthesis report. That is
# core-modernizer's to fix and is on the list for their team; we report
# accurately on our side regardless.
_DESIGN_SHAPE: dict[str, tuple[str, str]] = {
    "dynamodb": ("table_definitions", "target tables"),
    "documentdb": ("collections", "collections"),
    "elasticache": ("key_designs", "key designs"),
    "opensearch": ("index_designs", "index designs"),
    "aurora_postgresql": ("table_definitions", "target tables"),
    "aurora_mysql": ("table_definitions", "target tables"),
}

# Fallback for a target with no entry above (e.g. a future engine not yet
# mapped). Defaults to the DynamoDB/Aurora shape rather than guessing wildly: a
# relational target is the most plausible next addition, and the fallback only
# has to be empty-or-present, not exhaustive.
_DESIGN_SHAPE_DEFAULT: tuple[str, str] = ("table_definitions", "target tables")


def _design_shape(target_type: str) -> tuple[str, str]:
    """Return (artifact field, unit label) holding the design for one target."""
    return _DESIGN_SHAPE.get(target_type, _DESIGN_SHAPE_DEFAULT)


def _source_engine(store, job_id: str, database_name: str) -> str:
    """Read the source engine from the collector output's metadata.

    Returns "" when absent rather than raising: the classification this feeds is
    advisory, and a missing engine should not fail a phase whose real work has
    already completed.
    """
    try:
        from src.storage.collector_for_models import read_collector_for_models

        collector = read_collector_for_models(store, database_name, job_id)
        engine = collector.get("metadata", {}).get("source_database", {}).get("engine")
        return str(engine or "").strip().lower()
    except Exception:  # noqa: BLE001
        logger.warning("Could not read source engine from collector metadata", exc_info=True)
        return ""


def run_schema_design_core(
    job_id: str,
    database_name: str,
    target_type: str,
    assignment_version: int | None = None,
    store=None,
) -> dict:
    """Design the target schema for one engine. Returns a summary dict.

    One call per engine, because
    core-modernizer's ``run_schema_design`` is parameterised by ``target_type``
    rather than iterating engines itself.

    Writes ``<db>/<job>/schema-<target_type>/v<N>/schema_output.json``, which is
    the exact key ``synthesis_data.py`` reads at the same version. Synthesis
    derives ``table_mappings``, ``query_groups`` and
    ``recommended_architecture.databases`` from that file, so this phase is what
    populates three fields that are otherwise empty in every report.

    ``assignment_version`` defaults to ``resolve_downstream_assignment_version``
    (the newest version, coerced to 1 per ADR-026) and must match the version the
    assignment/Reality Check phases produced. At 0 the handler passes every query to every
    engine instead of the ones assigned to it, and writes to a different key than
    synthesis reads.

    This phase is LLM-driven and there is no deterministic mode: upstream's
    ``llm_mode`` only branches on ``"external"``, which writes the prepared model
    input and designs nothing. That is the opposite of the analysis phases, where
    ``llm_mode="none"`` is a full deterministic path. Model selection is left to
    ``SCHEMA_AGENT_MODEL_ID`` in the runtime environment so it is a deployment
    decision rather than an inherited library default.

    Prerequisites: collector/output.json and assignment/v<N>/assignment.json.
    """
    store = store or make_store()
    if assignment_version is None:
        assignment_version = resolve_downstream_assignment_version(store, database_name, job_id)
    prefix = f"{database_name}/{job_id}"

    collector_key = f"{prefix}/collector/output.json"
    if not store.exists(collector_key):
        raise FileNotFoundError(f"Collector output not found at {collector_key}")

    assignment_key = f"{prefix}/assignment/v{assignment_version}/assignment.json"
    if assignment_version > 0 and not store.exists(assignment_key):
        raise FileNotFoundError(
            f"Assignment not found at {assignment_key}. Schema design filters queries "
            f"by assignment; run the assignment phase first."
        )

    logger.info(
        "schema-design starting: job_id=%s db=%s target=%s assignment_version=%s",
        job_id,
        database_name,
        target_type,
        assignment_version,
    )

    # Group related queries and design them together (ADR-027 amendment). auto
    # splits an engine's queries into affinity groups (FK + aggregate + co-access
    # + co-dependency JOINs), designs each group, and merges; it falls back to a
    # single pass at or below MAX_STATEMENTS_PER_GROUP and forces single-pass for Aurora.
    from src.agents.schema_design.handler import run_schema_design_auto

    run_schema_design_auto(
        job_id=job_id,
        database_name=database_name,
        target_type=target_type,
        store=store,
        assignment_version=assignment_version,
    )

    output_key = f"{prefix}/schema-{target_type}/v{assignment_version}/schema_output.json"
    if not store.exists(output_key):
        raise FileNotFoundError(
            f"Schema design reported success but no output exists at {output_key}"
        )
    output = store.read_json(output_key)

    status = str(output.get("status") or "completed")
    design_field, design_units = _design_shape(target_type)
    designs = output.get(design_field) or []
    access_patterns = output.get("access_patterns") or []

    # Upstream dispatches on target_type alone and now has designers for all six
    # target engines: dynamodb, documentdb, opensearch, elasticache,
    # aurora_postgresql and aurora_mysql. The source engine — which upstream
    # does not consult — is what distinguishes a target needing no redesign
    # from one this report simply does not cover. Reported as informational in
    # the first case and as a warning in the second, so that a warning always
    # means the reader needs to act.
    #
    # "Did a design happen" is asked of the engine's own field, not of
    # ``table_definitions``. Only DynamoDB uses that name; DocumentDB produces
    # ``collections``, ElastiCache ``key_designs``, OpenSearch ``index_designs``.
    # Testing ``table_definitions`` universally made three engines that had
    # designed 20 collections, 10 key structures and 5 index mappings look like
    # they had produced nothing, and emitted a warning telling the reader a
    # separate schema conversion assessment was needed for designs that were
    # already in the artifact. See §13 Step 19.
    notes: list[str] = []
    warnings: list[str] = []
    if not designs:
        src = _source_engine(store, job_id, database_name)
        _nd_notes, _nd_warnings = schema_no_design_notes(
            target_type, src, status, output.get("reason") or ""
        )
        notes.extend(_nd_notes)
        warnings.extend(_nd_warnings)

    summary = {
        "job_id": job_id,
        "database_name": database_name,
        "target_type": target_type,
        "assignment_version": assignment_version,
        "status": status,
        # Reported under a neutral name with the engine's own unit alongside,
        # because "table_definitions: 0" for DocumentDB read as a failed design
        # when 20 collections had in fact been produced. The orchestrator relays
        # this into the chat, so the label is customer-visible.
        "designs": len(designs),
        "design_unit": design_units,
        "access_patterns": len(access_patterns),
        "unsupported_patterns": len(output.get("unsupported_patterns") or []),
        "schema_artifact": output_key,
    }
    if notes:
        summary["notes"] = notes
    if warnings:
        summary["warnings"] = warnings

    logger.info(
        "[schema-design/%s] status=%s %s=%d access_patterns=%d",
        target_type,
        status,
        design_field,
        len(designs),
        len(access_patterns),
    )
    return summary


def run_post_schema_routing_core(
    job_id: str,
    database_name: str,
    store=None,
    llm_mode: str = "bedrock",
    redesign: Callable[[str, str, str, int], None] | None = None,
) -> dict:
    """Run the post-schema routing after schema design (#1492). Returns a summary dict.

    The one implementation the local pipeline and ``assess()`` share
    (``post_schema_routing_handler.PostSchemaRouting``): each statement a design
    reports unsupported, or a PE reviewer flags, moves with its placement unit to its
    best remaining engine among the ones the effective assignment keeps (the shared
    scorer, the move ledger, never an engine outside the customer's engine scope);
    the moves are a new assignment version, and every engine they touch is designed
    again on it (``redesign``), the others' designs carried forward. A statement with
    no remaining engine stays where it is and is listed as terminal. A failed
    redesign writes a rollback version restating the one the reroute started from
    and raises. Bounded at ``POST_SCHEMA_MAX_DEPTH`` cascade passes.

    ``redesign(job_id, database_name, engine, assignment_version)`` designs one
    engine on the reroute's version; by default ``run_schema_design_auto`` in
    ``llm_mode``, as the local pipeline designs. Prerequisites: the designs of the
    effective assignment version (with none, nothing is routed).
    """
    from src.agents.referee.post_schema_routing_handler import PostSchemaRouting, pipeline_redesign

    store = store or make_store()
    routing = PostSchemaRouting(store, redesign or pipeline_redesign(store, llm_mode))
    run = routing.run(job_id, database_name)
    return {
        "job_id": job_id,
        "database_name": database_name,
        "source_assignment_version": run["start_version"],
        "effective_assignment_version": run["assignment_version"],
        "rerouted": len([r for r in run["routings"] if r.get("to_engine")]),
        "terminal_queries": len(run["terminal_queries"]),
        "held_queries": len(run["held_queries"]),
    }
