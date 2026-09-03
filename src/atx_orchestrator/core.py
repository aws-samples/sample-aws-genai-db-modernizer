"""Shared pipeline logic for the AWS Transform integration.

This module holds the storage-agnostic functions that BOTH the orchestrator
tools (tools.py) and the subagent wrappers call. Single source of truth — no
duplication between the in-process tool path and the A2A subagent path.

Everything here operates on the ArtifactStore abstraction. Storage type
(local dir vs S3) is controlled by env vars via create_artifact_store().
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from typing import NamedTuple

logger = logging.getLogger(__name__)


def make_store():
    """Create a text-capable ArtifactStore using the project's own factory.

    core-modernizer's ``create_artifact_store()`` decides S3-vs-local from env;
    ``upgrade_store`` re-homes that choice onto the Transform subclass that adds
    ``write_text``. See src/atx_orchestrator/store.py for why that capability is
    not on the shared ABC.
    """
    from src.atx_orchestrator.runtime.store import upgrade_store
    from src.storage import create_artifact_store

    return upgrade_store(create_artifact_store())


def make_orchestrator(store=None):
    """Create a LocalOrchestrator bound to the given store.

    llm_mode="none" keeps all phases fully deterministic. Set LLM_MODE=bedrock
    to enable the optional LLM schema-design / synthesis passes.
    """
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    s = store or make_store()
    llm_mode = os.environ.get("LLM_MODE", "none")
    return LocalOrchestrator(store=s, llm_mode=llm_mode)


def default_input_key(job_id: str, database_name: str) -> str:
    """Default ArtifactStore key for the raw offline collection JSON."""
    return f"{database_name}/{job_id}/uploads/collector-output.json"


def _discover_uploaded_input(store, job_id: str = "", database_name: str = "") -> str | None:
    """Locate a customer's WebApp-uploaded offline collection via the ATX
    Artifact API and stage it at the seed key for the collector.

    A customer's upload is a platform **artifact** (category ``CUSTOMER_INPUT``),
    not an object in this pipeline's ``S3_BUCKET``. The Artifact Store owns where
    it physically lives — an ATX-managed bucket or the customer's own bucket, in
    a different account than this fleet — and mints a presigned URL for it with
    its own role. Listing a prefix in our ``S3_BUCKET`` (the old approach) only
    ever found the upload when the WebApp happened to write into the same bucket
    the runtime reads, i.e. same-account dev; cross-account it silently found
    nothing. So discovery goes through ``ListArtifacts`` +
    ``CreateArtifactDownloadUrl`` instead, which is account/bucket-agnostic.

    Flow (Option 1 — stage at the seed key so the collector's read path is
    unchanged):

      1. Resolve the ATX agent context (workspace/job/agent-instance) and build
         the SDK ``ArtifactStore``. Outside the ATX runtime this raises, and we
         return ``None`` (local/dev falls back to a pre-staged seed key).
      2. ``list_artifacts(category=CUSTOMER_INPUT)`` for THIS agent instance (the
         orchestrator's), filter to JSON, exclude the auto-written
         ``job_objective``. Expect exactly one; more than one is ambiguous and
         raises.
      3. Download it and stage it into ``store`` at the seed key
         ``{db}/{job}/uploads/collector-output.json``; return that key. The
         collector then reads it exactly as it does a dev/reference seed.

    Returns the staged seed key, or ``None`` when not in the ATX runtime or no
    upload was found. Raises ``ValueError`` on an ambiguous upload.
    """
    try:
        from agent_builder_sdk.agentic_framework.artifact_store import ArtifactStore
        from agent_builder_sdk.agentic_framework.client_factory import get_agentic_api_client
        from agent_builder_sdk.env_var import get_agent_context_from_env

        ctx = get_agent_context_from_env()
        artifacts = ArtifactStore(
            workspace_id=ctx.workspace_id,
            job_id=ctx.job_id,
            agent_instance_id=ctx.agent_instance_id,
            client=get_agentic_api_client(),
        )
    except Exception as exc:  # noqa: BLE001 -- not in the ATX runtime; no upload to discover
        logger.info(
            "upload discovery: no ATX agent context (%s); returning None "
            "(collector will fall back to the seed key)",
            exc,
        )
        return None

    # List the job's artifacts with NO server-side filter, then match in Python.
    # Server-side category/agent filters both returned listed=0 in the field even
    # though the WebApp Artifacts panel shows the upload — the customer upload's
    # stored category is not necessarily CUSTOMER_INPUT, and it is surfaced by a
    # "User Uploads/" ``fileMetadata.path`` rather than a category we can predict.
    # The live-tested agentcore_list_artifacts.py reference defaults to no filter
    # for exactly this reason; we mirror that and inspect each artifact's real
    # categoryType / fileType / path. Paginate.
    all_artifacts: list[dict] = []
    next_token: str | None = None
    while True:
        kwargs: dict = {
            "requestContext": artifacts._create_request_context(),
            "maxResults": 100,
        }
        if next_token:
            kwargs["nextToken"] = next_token
        resp = artifacts.client.list_artifacts(**kwargs)
        all_artifacts.extend(resp.get("artifacts") or [])
        next_token = resp.get("nextToken")
        if not next_token:
            break

    def _path(a: dict) -> str:
        return (a.get("fileMetadata") or {}).get("path") or ""

    def _label(a: dict) -> str:
        return a.get("artifactLabel") or ""

    # Log the artifact paths the platform returned. This is the observability that
    # a None/empty result was previously missing (it looked identical whether the
    # upload was absent, in the wrong scope, or filtered out). Paths are enough to
    # diagnose; full artifact dicts were only needed while reverse-engineering the
    # shape. See fix/atx-input-file.
    logger.info(
        "upload discovery: job=%s agent_instance=%s listed=%d paths=%s",
        ctx.job_id,
        ctx.agent_instance_id,
        len(all_artifacts),
        [_path(a) or _label(a) for a in all_artifacts],
    )

    # A customer collection upload is a .json whose path basename ends in
    # ``.json``. The auto-written objective is ALSO CUSTOMER_INPUT/JSON, but with
    # label ``default`` (not ``job_objective``) and a bare ``job_objective`` path
    # -- so it must be excluded by its path basename, not by label or a
    # leading-slash suffix (both of which it evades, which produced a spurious
    # "found 2" ambiguity in the field). See the ground-truth discovery log above.
    def _basename(a: dict) -> str:
        return _path(a).rsplit("/", 1)[-1]

    candidates = [
        a
        for a in all_artifacts
        if _basename(a).endswith(".json") and _basename(a) != "job_objective"
    ]
    if len(candidates) > 1:
        paths = sorted(_path(a) or _label(a) for a in candidates)
        raise ValueError(
            f"Expected exactly one uploaded collection JSON artifact, found "
            f"{len(candidates)}: {paths}. The pipeline cannot choose among multiple "
            "uploads without a naming convention."
        )
    if not candidates:
        logger.warning(
            "upload discovery found no CUSTOMER_INPUT JSON artifact for agent_instance=%s "
            "(listed %d artifact(s)). input_key will be empty; the collector will fall "
            "back to the seed key and fail if none is staged. Confirm the customer "
            "uploaded a collection file to this job.",
            ctx.agent_instance_id,
            len(all_artifacts),
        )
        return None

    artifact_id = candidates[0]["artifactId"]
    seed_key = default_input_key(job_id, database_name)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        artifacts.download_artifact(artifact_id, tmp_path)
        with open(tmp_path) as fh:
            collection = json.load(fh)
        store.write_json(seed_key, collection)
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
    logger.info(
        "upload discovery: staged CUSTOMER_INPUT artifact %s (label=%r) at seed key %r",
        artifact_id,
        candidates[0].get("artifactLabel"),
        seed_key,
    )
    return seed_key


def _resolve_collector_input(store, job_id: str, database_name: str, input_key: str) -> str:
    """Resolve where the collector reads the raw offline collection.

    Discovery of a customer's WebApp upload happens in the orchestrator
    (``run_collect_via_a2a``), which reliably holds the customer's Transform job
    context and passes the resolved key here as ``input_key`` -- see
    ``_discover_uploaded_input``. The collector itself only chooses between an
    explicit key and the seed, so it never depends on its own subagent context
    matching the customer's job:

      1. an explicit ``input_key`` (from the orchestrator or the reference harness);
      2. the seed key ``{db}/{job}/uploads/collector-output.json`` (dev/reference
         workloads stage it there).

    Raises FileNotFoundError if neither is present. The raw input's location does
    not affect the pipeline's ``{db}/{job}/`` output tree: that tree is created by
    the collector writing ``{db}/{job}/collector/output.json`` and by every
    subsequent phase's own writes, not by where the input was read from.
    """
    if input_key:
        if not store.exists(input_key):
            raise FileNotFoundError(f"Offline collection input not found at '{input_key}'.")
        logger.info("collector input resolved from explicit input_key=%r", input_key)
        return input_key

    seed = default_input_key(job_id, database_name)
    if store.exists(seed):
        logger.info(
            "collector input resolved from SEED key %r (no input_key was passed). "
            "This is expected for dev/reference runs; a customer WebApp job should "
            "resolve via upload discovery instead.",
            seed,
        )
        return seed

    raise FileNotFoundError(
        f"No offline collection found for job '{job_id}': no input_key was passed and "
        f"no seed exists at '{seed}'. The orchestrator resolves customer uploads and "
        "passes the key; for dev/reference runs, stage the collection at the seed key."
    )


def ingest_offline_collection(store, job_id: str, database_name: str, raw_input: dict) -> dict:
    """Build a collector output contract from a raw offline collection dict.

    Mirrors scripts/run_assessment.py::phase_collect exactly, but reads the raw
    input from a dict (sourced via ArtifactStore) instead of a local file, and
    writes the result back through the ArtifactStore. Reuses the existing
    builder functions — no new parsing logic.

    Returns the collector output dict that was written.
    """
    import time

    from src.agents.collector.mysql_collector import (
        _build_metrics,
        _build_output,
        _build_procedures,
        _build_queries,
        _build_tables,
        _build_triggers,
        _build_views,
    )
    from src.contracts.collector_input import CollectorInput
    from src.tools.database.offline_parser import parse_offline_collection

    contract_version = raw_input.get("contract_version")
    is_contract = isinstance(contract_version, str) and contract_version.startswith("3")

    if is_contract:
        collector_data = raw_input
    else:
        parsed = parse_offline_collection(raw_input)
        start = time.monotonic()

        tables_built = _build_tables(parsed["tables"], database_name)
        queries_built = _build_queries(parsed.get("queries", []))
        views = _build_views(parsed.get("views", []))
        procedures = _build_procedures(parsed.get("procedures", []))
        triggers = _build_triggers(parsed.get("triggers", []))
        metrics = _build_metrics(queries_built, None)

        version_str = (parsed.get("metadata", {}).get("version") or "").lower()
        if "postgresql" in version_str or "postgres" in version_str:
            detected_engine = "postgresql"
            detected_port = 5432
        else:
            detected_engine = "mysql"
            detected_port = 3306

        inp = CollectorInput.model_validate(
            {
                "job_id": job_id,
                "engine": detected_engine,
                "cluster_endpoint": "offline",
                "port": detected_port,
                "database_name": database_name,
                "mode": "offline",
                "offline_config": {"s3_bucket": "local", "s3_key": "in-memory"},
            }
        )
        offline_meta = parsed.get("metadata", {})
        collector_data = json.loads(
            _build_output(
                inp,
                start,
                version=offline_meta.get("version", "unknown"),
                db_size=offline_meta.get("database_size_gb"),
                tables=tables_built,
                queries=queries_built,
                metrics=metrics,
                rds_meta=None,
                views=views,
                procedures=procedures,
                triggers=triggers,
            ).model_dump_json()
        )

    collector_key = f"{database_name}/{job_id}/collector/output.json"
    store.write_json(collector_key, collector_data)

    from src.agents.query_journey_materializer import materialize_source

    materialize_source(collector_data, database_name, job_id, store)
    return collector_data


def run_collect_core(job_id: str, database_name: str, input_key: str = "", store=None) -> dict:
    """Ingest offline collection only (no triage). Returns a summary dict.

    Wraps the collector step exactly as the existing collector agent does, reading
    the raw offline collection through the ArtifactStore and writing the collector
    output contract. This maps one-to-one to AGENT_TYPE='collector'.

    Raises FileNotFoundError if the offline input is missing.
    """
    store = store or make_store()
    key = _resolve_collector_input(store, job_id, database_name, input_key)
    raw_input = store.read_json(key)
    collector_data = ingest_offline_collection(store, job_id, database_name, raw_input)

    return {
        "job_id": job_id,
        "database_name": database_name,
        "tables": len(collector_data.get("database_schema", {}).get("tables", [])),
        "queries": len(collector_data.get("queries", {}).get("query_patterns", [])),
        "collector_artifact": f"{database_name}/{job_id}/collector/output.json",
    }


def run_triage_core(job_id: str, database_name: str, store=None) -> dict:
    """Run triage only (assumes collector output already exists). Returns a summary dict.

    Maps one-to-one to AGENT_TYPE='referee-triage'. Reads the collector output from
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


def run_collect_triage_core(
    job_id: str, database_name: str, input_key: str = "", store=None
) -> dict:
    """Ingest offline collection + run triage. Returns a summary dict.

    This is the shared core used by BOTH the orchestrator tool and the subagent.
    Raises FileNotFoundError if the offline input is missing.
    """
    store = store or make_store()
    key = _resolve_collector_input(store, job_id, database_name, input_key)
    raw_input = store.read_json(key)

    # Phase 1a: ingest collection (mirrors run_assessment.phase_collect)
    collector_data = ingest_offline_collection(store, job_id, database_name, raw_input)
    n_tables = len(collector_data.get("database_schema", {}).get("tables", []))
    n_queries = len(collector_data.get("queries", {}).get("query_patterns", []))

    # Phase 1b: triage (existing deterministic handler)
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
        "tables": n_tables,
        "queries": n_queries,
        "selected_engines": selected_engines,
        "signal_count": signal_count,
    }


class _AnalysisEngine(NamedTuple):
    """Per-engine knobs for :func:`run_analysis_core`.

    ``target_database`` is the ``TargetDatabase`` member name, the
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
    "aurora_pg": _AnalysisEngine(
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


# Triage emits engine tokens in ``target_database`` form (e.g. "aurora_postgresql"),
# but ``_ANALYSIS_ENGINES`` is keyed by short engine name ("aurora_pg"). This reverse
# map resolves either form to the canonical key so a PostgreSQL source's selected
# "aurora_postgresql" agent is not silently dropped as unknown. Built from
# ``_ANALYSIS_ENGINES`` so it cannot drift from the table.
_ENGINE_KEY_BY_TOKEN: dict[str, str] = {
    **{key: key for key in _ANALYSIS_ENGINES},
    **{spec.target_database: key for key, spec in _ANALYSIS_ENGINES.items()},
}


def _resolve_engine_key(token: str) -> str | None:
    """Resolve a triage engine token to a ``_ANALYSIS_ENGINES`` key, or None if unknown.

    Accepts either the short key ("aurora_pg") or the ``target_database`` form
    ("aurora_postgresql"). Non-analysis tokens (e.g. deferred "keyspaces",
    "neptune") resolve to None so callers can ignore them.
    """
    return _ENGINE_KEY_BY_TOKEN.get(token)


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

    Maps to AGENT_TYPE='analysis-<engine>'. Reads the collector output from the
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

    collector_output = store.read_json(collector_key)

    # Deferred imports — avoid loading heavy analysis modules at core.py import time.
    from src.contracts.analysis_input import AnalysisInput, TargetDatabase

    analyze_fn = spec.load_analyze()
    analysis_input = AnalysisInput(
        job_id=job_id,
        collector_output=collector_output,
        target_database=getattr(TargetDatabase, spec.target_database),
    )

    logger.info(
        "ATX analysis-%s starting: job_id=%s db=%s",
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
    store.write_json(trace_key, decision_trace)

    mermaid_key: str | None = None
    if spec.mermaid_always or mermaid_diagram:
        mermaid_key = f"{prefix}/er-diagram.mmd"
        store.write_text(mermaid_key, mermaid_diagram, content_type="text/x-mermaid")

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
    aurora_mysql, not aurora_pg), so the consolidated analysis agent trusts this
    list rather than re-deriving it.
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
    variant"). Tokens are resolved to ``_ANALYSIS_ENGINES`` keys; non-analysis
    tokens are ignored. Returns an empty list if triage is missing so callers can
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

    ``engines`` defaults to triage's selected engines. Triage emits engine tokens
    in ``target_database`` form (e.g. "aurora_postgresql"), which are resolved to
    the canonical ``_ANALYSIS_ENGINES`` key ("aurora_pg") before dispatch;
    unresolvable tokens are dropped with a warning. Resolved keys are de-duplicated
    while preserving order.

    The optional callbacks report per-engine progress (the WebApp job-plan
    sub-steps); each receives the engine key and the plan step label
    ``analysis_<target_database>``. When ``engines`` is left to default (derived
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

    # Resolve triage tokens (target_database form) to canonical engine keys,
    # de-duplicating while preserving order.
    known: list[str] = []
    unknown: list[str] = []
    for token in engines:
        key = _resolve_engine_key(token)
        if key is None:
            unknown.append(token)
        elif key not in known:
            known.append(key)
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

    Maps one-to-one to AGENT_TYPE='assignment'. Deterministic — no LLM invocation.
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
        "ATX assignment starting: job_id=%s db=%s",
        job_id,
        database_name,
    )
    run_assignment_resolver(job_id, database_name, store)

    # Summarize the produced assignment
    assignment_key = f"{database_name}/{job_id}/assignment/v1/assignment.json"
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
        "assignment_version": 1,
        "total_queries": total_queries,
        "queries_per_engine": engine_counts,
        "assignment_artifact": assignment_key,
    }


def _resolve_assignment_version(store, job_id: str, database_name: str) -> int:
    """Return the highest existing assignment version under ``assignment/``, or 0.

    Mirrors the REST API's ``_latest_assignment_version``: list the assignment
    prefix, parse the ``vN`` directory component, and return the max. This is how
    Schema Design and Synthesis pick up the consolidated assignment Reality Check
    produced (v2) without the version being threaded through the LLM. Returns 0
    when no versioned assignment exists.
    """
    prefix = f"{database_name}/{job_id}/assignment/"
    versions: list[int] = []
    for key in store.list_prefix(prefix):
        parts = key.replace(prefix, "").split("/")
        if parts and parts[0].startswith("v"):
            try:
                versions.append(int(parts[0][1:]))
            except ValueError:
                continue
    return max(versions) if versions else 0


def run_reality_check_core(
    job_id: str,
    database_name: str,
    assignment_version: int = 1,
    store=None,
) -> dict:
    """Run Reality Check (CTO-level engine consolidation) for a completed assignment.

    The final phase of the assessment core (ADR-018, ADR-026). Reads
    ``assignment/v{assignment_version}/assignment.json`` plus triage, collector,
    and per-engine analysis, applies the deterministic consolidation logic, then
    runs the Bedrock LLM pass (validates the consolidations and writes the CTO
    executive summary). Writes ``reality-check/output.json`` always and, only when
    it consolidates, ``assignment/v{N+1}/assignment.json``.

    ``llm_mode`` comes from ``REALITY_CHECK_LLM_MODE`` (default ``"bedrock"``,
    per ADR-026). Set it to ``"none"`` to run purely deterministically (e.g. in
    tests without Bedrock access); ``"external"`` is intentionally unsupported in
    ATX (it needs an out-of-band resume the A2A model cannot do).

    Prerequisites: collector/output.json, referee-triage/triage.json,
    assignment/v{assignment_version}/assignment.json, and at least one analysis
    output. Returns a summary with before/after distribution, the consolidation
    count, and ``effective_assignment_version`` (the latest version after the run,
    which is 2 when consolidation occurred, else the source version).
    """
    store = store or make_store()

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

    llm_mode = os.environ.get("REALITY_CHECK_LLM_MODE", "bedrock")
    logger.info(
        "ATX reality-check starting: job_id=%s db=%s assignment_version=%s llm_mode=%s",
        job_id,
        database_name,
        assignment_version,
        llm_mode,
    )
    run_reality_check_handler(
        job_id,
        database_name,
        store,
        assignment_version=assignment_version,
        llm_mode=llm_mode,
    )

    output_key = f"{prefix}/reality-check/output.json"
    before_distribution: dict = {}
    after_distribution: dict = {}
    consolidations = 0
    if store.exists(output_key):
        rc = store.read_json(output_key)
        before_distribution = rc.get("before_distribution", {})
        after_distribution = rc.get("after_distribution", {})
        consolidations = len(rc.get("consolidations", []))

    effective_version = (
        _resolve_assignment_version(store, job_id, database_name) or assignment_version
    )

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


def _triage_signals(store, job_id: str, database_name: str) -> list[str]:
    """Return the names of the workload signals triage detected, or [] if absent.

    Reads ``referee-triage/triage.json``. Best-effort: missing file or malformed
    entries yield an empty list rather than raising, because this feeds progress
    detail and chat narration, not control flow.
    """
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    if not store.exists(triage_key):
        return []
    triage = store.read_json(triage_key)
    names: list[str] = []
    for s in triage.get("signals", []):
        if isinstance(s, dict) and s.get("signal"):
            names.append(str(s["signal"]))
    return names


def _triage_signal_summary(store, job_id: str, database_name: str, max_signals: int = 6) -> str:
    """One-line summary of triage's detected signals and selected engines.

    Used as the Triage plan step's description so the WebApp progress panel shows
    what triage found, mirroring the web interface. Returns "" when triage output
    is unavailable.
    """
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    if not store.exists(triage_key):
        return ""
    triage = store.read_json(triage_key)
    signals = _triage_signals(store, job_id, database_name)
    selected = [
        a["agent_type"] if isinstance(a, dict) else a for a in triage.get("selected_agents", [])
    ]
    parts: list[str] = []
    if signals:
        shown = ", ".join(signals[:max_signals])
        extra = len(signals) - max_signals
        if extra > 0:
            shown += f" (+{extra} more)"
        parts.append(f"Signals: {shown}")
    if selected:
        parts.append(f"Selected: {', '.join(str(e) for e in selected)}")
    return ". ".join(parts)


def run_assessment_core(
    job_id: str,
    database_name: str,
    input_key: str = "",
    store=None,
    on_phase_start: Callable[[str], None] | None = None,
    on_phase_done: Callable[[str, dict, str], None] | None = None,
    on_phase_error: Callable[[str, str], None] | None = None,
    on_engine_start: Callable[[str, str], None] | None = None,
    on_engine_done: Callable[[str, str, dict], None] | None = None,
    on_engine_error: Callable[[str, str, str], None] | None = None,
    on_engine_skipped: Callable[[str, str, str], None] | None = None,
) -> dict:
    """Run the whole assessment front-half in one process (ADR-025, ADR-026).

    Composes the existing single-source cores in DAG order:
    ``run_collect_core`` -> ``run_triage_core`` -> ``run_all_analyses`` ->
    ``run_assignment_core`` -> ``run_reality_check_core``. Each phase writes the
    same artifacts it always has (``collector/output.json``,
    ``referee-triage/triage.json``, ``analysis-<engine>/analysis.json`` and
    friends, ``assignment/v1/assignment.json``, ``reality-check/output.json``, and
    when Reality Check consolidates, ``assignment/v2/assignment.json``), so Schema
    Design, Synthesis, and every downstream reader are unaffected. This is the
    single assessment-core agent that replaced the collector/triage/analysis/
    assignment runtimes (ADR-025) and now also runs Reality Check (ADR-026);
    consolidation is safe because every phase runs in-process, satisfying the
    ordering dependencies sequentially without a cross-agent wait.

    Collect, Triage, Analyze, and Assign are deterministic. Reality Check runs
    with an LLM pass (validates the consolidations and writes the CTO executive
    summary), which is why the agent is named for what it produces rather than
    for being purely deterministic.

    Progress callbacks drive the WebApp job-plan panel. ``on_phase_*`` fire for the
    five top-level plan steps (``collector``, ``triage``, ``analysis``,
    ``assignment``, ``reality_check``); ``on_phase_done`` carries a short
    human-readable ``detail`` string (e.g. triage's detected signals) for the step
    description. The ``on_engine_*`` callbacks are forwarded to
    :func:`run_all_analyses` and tick the nested per-engine
    ``analysis_<target_database>`` sub-steps, including skip reasons for engines
    triage did not select.

    Failures propagate: a phase that raises stops the chain (downstream phases
    depend on its artifacts), after reporting the error through ``on_phase_error``.
    Returns a merged summary with each phase's result, the
    ``effective_assignment_version`` (2 when Reality Check consolidated, else 1),
    plus a compact ``summary_for_chat`` block the orchestrator can narrate.
    """
    store = store or make_store()

    def _run_phase(label: str, fn: Callable[[], dict]) -> dict:
        if on_phase_start is not None:
            on_phase_start(label)
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - report then re-raise to stop the chain
            logger.exception("run_assessment_core: phase %s failed", label)
            if on_phase_error is not None:
                on_phase_error(label, str(exc))
            raise

    # 1. Collect
    collect_summary = _run_phase(
        "collector",
        lambda: run_collect_core(job_id, database_name, input_key=input_key, store=store),
    )
    if on_phase_done is not None:
        on_phase_done(
            "collector",
            collect_summary,
            f"{collect_summary.get('tables', 0)} tables, "
            f"{collect_summary.get('queries', 0)} queries",
        )

    # 2. Triage
    triage_summary = _run_phase(
        "triage", lambda: run_triage_core(job_id, database_name, store=store)
    )
    if on_phase_done is not None:
        on_phase_done(
            "triage", triage_summary, _triage_signal_summary(store, job_id, database_name)
        )

    # 3. Analyze — parent step plus nested per-engine sub-steps via run_all_analyses.
    if on_phase_start is not None:
        on_phase_start("analysis")
    try:
        analysis_summary = run_all_analyses(
            job_id,
            database_name,
            store=store,
            on_engine_start=on_engine_start,
            on_engine_done=on_engine_done,
            on_engine_error=on_engine_error,
            on_engine_skipped=on_engine_skipped,
        )
    except Exception as exc:  # noqa: BLE001 - report then re-raise to stop the chain
        logger.exception("run_assessment_core: phase analysis failed")
        if on_phase_error is not None:
            on_phase_error("analysis", str(exc))
        raise
    if on_phase_done is not None:
        analyzed = analysis_summary.get("engines_analyzed", [])
        on_phase_done("analysis", analysis_summary, f"{len(analyzed)} engines analyzed")

    # 4. Assign
    assign_summary = _run_phase(
        "assignment", lambda: run_assignment_core(job_id, database_name, store=store)
    )
    if on_phase_done is not None:
        qpe = assign_summary.get("queries_per_engine", {})
        detail = f"{assign_summary.get('total_queries', 0)} queries routed"
        if qpe:
            detail += ": " + ", ".join(f"{k} {v}" for k, v in qpe.items())
        on_phase_done("assignment", assign_summary, detail)

    # 5. Reality Check — CTO-level engine consolidation (ADR-018, ADR-026). Runs
    # on assignment v1 and, when it consolidates, writes assignment v2. Uses the
    # Bedrock LLM pass (consolidation validation + executive summary).
    reality_check_summary = _run_phase(
        "reality_check",
        lambda: run_reality_check_core(job_id, database_name, assignment_version=1, store=store),
    )
    effective_version = reality_check_summary.get("effective_assignment_version", 1)
    if on_phase_done is not None:
        consolidations = reality_check_summary.get("consolidations", 0)
        before = reality_check_summary.get("before_distribution", {})
        after = reality_check_summary.get("after_distribution", {})
        if consolidations:
            detail = f"Consolidated {len(before)} -> {len(after)} engines"
        else:
            detail = "No consolidation; assignment unchanged"
        on_phase_done("reality_check", reality_check_summary, detail)

    return {
        "job_id": job_id,
        "database_name": database_name,
        "collector": collect_summary,
        "triage": triage_summary,
        "analysis": analysis_summary,
        "assignment": assign_summary,
        "reality_check": reality_check_summary,
        # The version downstream schema-design and synthesis should read: 2 when
        # Reality Check consolidated, else 1. Downstream tools resolve this from
        # the store themselves (see _resolve_assignment_version); it is echoed here
        # for narration and tests.
        "effective_assignment_version": effective_version,
        # A compact, ready-to-narrate view for the orchestrator's post-core chat
        # summary (signals picked up, engines selected, query distribution,
        # consolidation).
        "summary_for_chat": {
            "tables": collect_summary.get("tables"),
            "queries": collect_summary.get("queries"),
            "signals": _triage_signals(store, job_id, database_name),
            "selected_engines": triage_summary.get("selected_engines", []),
            "engines_analyzed": analysis_summary.get("engines_analyzed", []),
            "engines_skipped": analysis_summary.get("engines_skipped", {}),
            "queries_per_engine": assign_summary.get("queries_per_engine", {}),
            "reality_check_consolidations": reality_check_summary.get("consolidations", 0),
            "after_distribution": reality_check_summary.get("after_distribution", {}),
        },
    }


def run_synthesis_core(
    job_id: str,
    database_name: str,
    assignment_version: int = 0,
    store=None,
) -> dict:
    """Run referee-synthesis for a completed analysis + assignment pipeline.

    Maps one-to-one to AGENT_TYPE='referee-synthesis'. Deterministic-first: all
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
    """
    store = store or make_store()

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
        "ATX referee-synthesis starting: job_id=%s db=%s assignment_version=%s",
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
    #      and query_groups are empty and build_architecture_recommendation skips
    #      every engine via `if not tables and not schema_design_available`. The
    #      assignment IS read and everything else populates: ranking, workload
    #      split, architecture_type, risk assessment, executive summary.
    #      WARN — the report is a real answer with two documented gaps.
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
        if not any_schema_design:
            warnings.append(
                f"No engine has schema-design output, so table_mappings, query_groups "
                f"and recommended_architecture.databases are empty. The assignment "
                f"(version {assignment_version}) was read correctly and every other "
                f"section is populated: engine ranking, workload distribution, "
                f"architecture type, risk assessment and executive summary. "
                f"table_mappings is derived from schema-design output, not from the "
                f"assignment, so running schema-design is what fills these three "
                f"fields. This is a known pipeline gap, not a failure."
            )

    # The customer-facing deliverables (Decision Report HTML, Engineering Report
    # MD) and their publication now live on the orchestrator, which owns the
    # synthesis plan step -- see tools._publish_synthesis_deliverables. The
    # subagent's contract ends at writing report.json to S3 above (the system of
    # record, already durable). published_artifacts stays here for payload-shape
    # stability; the orchestrator populates the panel, not this subagent.
    published: dict[str, str] = {}

    return {
        "job_id": job_id,
        "database_name": database_name,
        "assignment_version": assignment_version,
        "engines_ranked": len(ranking),
        "top_engine": ranking[0].get("target") if ranking else None,
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
        # Platform artifact ids, keyed by the label the customer sees. Empty when
        # running outside the ATX runtime or when publishing failed; the report is
        # still at report_artifact either way.
        "published_artifacts": published,
    }


# Target engines that share a family with a relational source, keyed by the
# source engine reported in the collector's metadata. Used to tell "no schema
# redesign is required" apart from "this report does not cover that conversion".
_SAME_FAMILY: dict[str, set[str]] = {
    "postgresql": {"aurora_postgresql"},
    "mysql": {"aurora_mysql"},
}


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
}

# Aurora targets have no designer upstream, so they have no design field either.
# Fall back to the DynamoDB name rather than guessing: a relational target that
# ever gains a designer will most plausibly emit table definitions, and the
# fallback only has to be empty-or-present, not exhaustive.
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
        collector = store.read_json(f"{database_name}/{job_id}/collector/output.json")
        engine = collector.get("metadata", {}).get("source_database", {}).get("engine")
        return str(engine or "").strip().lower()
    except Exception:  # noqa: BLE001
        logger.warning("Could not read source engine from collector metadata", exc_info=True)
        return ""


def run_schema_design_core(
    job_id: str,
    database_name: str,
    target_type: str,
    assignment_version: int = 1,
    store=None,
) -> dict:
    """Design the target schema for one engine. Returns a summary dict.

    Maps to AGENT_TYPE='schema-<engine>'. One call per engine, because
    core-modernizer's ``run_schema_design`` is parameterised by ``target_type``
    rather than iterating engines itself.

    Writes ``<db>/<job>/schema-<target_type>/v<N>/schema_output.json``, which is
    the exact key ``synthesis_data.py`` reads at the same version. Synthesis
    derives ``table_mappings``, ``query_groups`` and
    ``recommended_architecture.databases`` from that file, so this phase is what
    populates three fields that are otherwise empty in every report.

    ``assignment_version`` defaults to 1 and must match the version the
    assignment agent produced. At 0 the handler passes every query to every
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
        "ATX schema-design starting: job_id=%s db=%s target=%s assignment_version=%s",
        job_id,
        database_name,
        target_type,
        assignment_version,
    )

    from src.agents.schema_design.handler import run_schema_design

    run_schema_design(
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

    # Upstream dispatches on target_type alone and has designers for dynamodb,
    # documentdb, opensearch and elasticache; other targets take a default branch
    # that writes a placeholder. The source engine — which upstream does not
    # consult — is what distinguishes a target needing no redesign from one this
    # report simply does not cover. Reported as informational in the first case
    # and as a warning in the second, so that a warning always means the reader
    # needs to act.
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
        if target_type in _SAME_FAMILY.get(src, set()):
            notes.append(
                f"{target_type}: no schema design required. Source and target are both "
                f"{src}, so the existing schema carries over unchanged."
            )
        elif status == "skipped":
            notes.append(
                f"{target_type}: {output.get('reason') or 'no queries or tables assigned'}."
            )
        else:
            warnings.append(
                f"{target_type}: schema design not included in this report."
                + (
                    f" The source engine is {src}, so type and object mappings for this "
                    f"target would need a separate schema conversion assessment."
                    if src
                    else " Type and object mappings for this target are not covered here."
                )
            )

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
