"""Run the pipeline's phases for one :class:`AssessmentRequest`.

The phases are the core entry points every other path uses
(``src.atx_orchestrator.core``): ingest the collection, triage, analyse, then place
statements and run the reality check, then design the schema of every engine the
effective assignment keeps, then synthesise the report. No routing happens here. Each
phase is one entry of :data:`PHASES`, run in order until ``options.run_through``, so a
later phase is one more entry.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.assessment.models import AssessmentRequest, AssessmentResult
from src.assessment.result_builder import build_result, load_run_state

logger = logging.getLogger(__name__)


class AssessmentRunError(RuntimeError):
    """A phase did not complete, so no result is built from partial artifacts."""


@dataclass(frozen=True)
class _Job:
    store: Any
    database_name: str
    job_id: str
    request: AssessmentRequest


def _collector_input(collection: dict[str, Any]) -> dict[str, Any]:
    """The collection to ingest: a raw collection as given; a collector output with
    every derived per-statement fact read again by the parser (never the caller's)."""
    from src.contracts.collector_output import CollectorOutputContract
    from src.shared.contract_versioning import carries_contract
    from src.tools.database.offline_parser import rederive_query_facts
    from src.tools.sql.query_facts import source_engine_of

    if not carries_contract(collection, CollectorOutputContract, artifact="assess() collection"):
        return collection
    return rederive_query_facts(collection, source_engine_of(collection))


def _ingest_and_analyse(job: _Job) -> None:
    from src.agents.referee.engine_scope import record_engine_scope
    from src.atx_orchestrator.core import (
        ingest_offline_collection,
        run_all_analyses,
        run_triage_core,
    )

    ingest_offline_collection(
        job.store,
        job.job_id,
        job.database_name,
        _collector_input(job.request.collection),
    )
    engines = job.request.options.target_engines
    if engines is not None:
        # Triage reads the job's recorded scope (ADR-031 Decision 2 Amendment 14).
        record_engine_scope(job.store, job.database_name, job.job_id, engines, via="library")
    run_triage_core(job.job_id, job.database_name, store=job.store)
    analyses = run_all_analyses(job.job_id, job.database_name, store=job.store)
    if analyses.get("engines_failed"):
        # A result missing an engine's scores would place statements differently.
        raise AssessmentRunError(
            f"analysis failed for {', '.join(analyses['engines_failed'])}; no result built"
        )


def _place_and_reality_check(job: _Job) -> None:
    from src.atx_orchestrator.core import run_assignment_core, run_reality_check_core

    run_assignment_core(job.job_id, job.database_name, store=job.store)
    run_reality_check_core(
        job.job_id,
        job.database_name,
        store=job.store,
        llm_mode="none" if job.request.options.deterministic else "bedrock",
    )


def _effective_version(job: _Job) -> int:
    from src.storage.assignment_versioning import resolve_effective_assignment_version

    version = resolve_effective_assignment_version(job.store, job.database_name, job.job_id)
    if not version:
        raise AssessmentRunError("the reality check left no assignment; no result built")
    return version


def _design_schemas(job: _Job) -> None:
    """Design every engine the effective assignment keeps (its owners and the cache
    layer, in triage's order, inside the engine scope), concurrently, as the pipeline
    does. Every designer uses a model (the request model refuses this phase with
    ``deterministic=True``). A failed design fails the run: no result is built from a
    partial set of designs."""
    from concurrent.futures import ThreadPoolExecutor

    from src.atx_orchestrator.core import run_schema_design_core
    from src.storage.assignment_versioning import schema_design_engines

    version = _effective_version(job)
    # Triage's selection already leaves out every engine outside the engine scope.
    engines = schema_design_engines(job.store, job.database_name, job.job_id, version)

    def design(engine: str) -> None:
        run_schema_design_core(
            job.job_id, job.database_name, engine, assignment_version=version, store=job.store
        )

    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, len(engines))) as pool:
        futures = {engine: pool.submit(design, engine) for engine in engines}
        for engine, future in futures.items():
            try:
                future.result()
            except Exception:  # noqa: BLE001 -- every failure is named below
                logger.exception("assess(): schema design failed for %s", engine)
                failed.append(engine)
    if failed:
        raise AssessmentRunError(f"schema design failed for {', '.join(failed)}; no result built")


def _synthesise(job: _Job) -> None:
    """Write the report for the effective assignment and its designs (the executive
    summary is written with a model)."""
    from src.atx_orchestrator.core import run_synthesis_core

    run_synthesis_core(
        job.job_id, job.database_name, assignment_version=_effective_version(job), store=job.store
    )


#: Each ``run_through`` value and the work that phase adds, in order.
PHASES: tuple[tuple[str, Callable[[_Job], None]], ...] = (
    ("analysis", _ingest_and_analyse),
    ("reality_check", _place_and_reality_check),
    ("schema_design", _design_schemas),
    ("synthesis", _synthesise),
)


def assess(
    request: AssessmentRequest | Mapping[str, Any],
    *,
    store: Any = None,
    job_id: str | None = None,
) -> AssessmentResult:
    """Assess one database's collection and return the typed result.

    ``request`` is an :class:`AssessmentRequest` or a mapping validated as one (a
    refused option raises ``pydantic.ValidationError``). With no ``store`` the run
    writes to a temporary directory that is removed before returning; a given
    ``store`` (an ``ArtifactStore``) keeps every artifact under
    ``<database_name>/<job_id>/``. ``job_id`` defaults to a new random id.

    With ``options.deterministic`` (the default) no model is called. With
    ``deterministic=False`` the reality check has a model review its consolidations,
    which needs model access; ``run_through="schema_design"`` and ``"synthesis"`` need
    it (they are refused with ``deterministic=True``) and call a model for every
    designed engine and for the report's executive summary.
    """
    from src.atx_orchestrator.runtime.store import upgrade_store
    from src.shared.job_id import validate_job_id
    from src.storage.local_store import LocalArtifactStore

    req = (
        request
        if isinstance(request, AssessmentRequest)
        else AssessmentRequest.model_validate(request)
    )
    job_id = validate_job_id(job_id) if job_id is not None else uuid.uuid4().hex[:8]
    run_through = req.options.run_through

    def run(target_store: Any) -> AssessmentResult:
        job = _Job(target_store, req.database_name, job_id, req)
        for phase, work in PHASES:
            work(job)
            if phase == run_through:
                break
        state = load_run_state(target_store, req.database_name, job_id, run_through)
        return build_result(state, req)

    # The analyses also write text artifacts (diagrams), which need ``write_text``.
    if store is not None:
        return run(store if hasattr(store, "write_text") else upgrade_store(store))
    with tempfile.TemporaryDirectory(prefix="assess-") as root:
        return run(upgrade_store(LocalArtifactStore(base_dir=root)))
