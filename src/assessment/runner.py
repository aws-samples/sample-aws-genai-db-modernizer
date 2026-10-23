"""Run the pipeline's phases for one :class:`AssessmentRequest`.

The phases are the core entry points every other path uses
(``src.atx_orchestrator.core``): ingest the collection, triage, analyse, then place
statements and run the reality check. No routing happens here. Each phase is one
entry of :data:`PHASES`, run in order until ``options.run_through``, so a later phase
is one more entry.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.assessment.models import AssessmentRequest, AssessmentResult
from src.assessment.result_builder import build_result, load_run_state


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


#: Each ``run_through`` value and the work that phase adds, in order. ``schema_design``
#: and ``synthesis`` are refused by the request model until they are added here.
PHASES: tuple[tuple[str, Callable[[_Job], None]], ...] = (
    ("analysis", _ingest_and_analyse),
    ("reality_check", _place_and_reality_check),
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
    which needs model access.
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
