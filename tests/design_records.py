# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis after the schema design phase, for tests that build a job by hand (#2154).

Synthesis needs a design or a skip record for every engine it designs. The schema design
phase writes one or the other for each engine; a test that writes only the designs it
cares about (or none) stands in for that phase here: each engine of the version with
neither gets a skip record, then synthesis runs. A record the test wrote is kept.
"""

from __future__ import annotations

from typing import Any

from src.agents.referee import synthesis_data, synthesis_handler
from src.contracts.phase_models import SCHEMA_DESIGN_SKIP_REASON_NO_LLM
from src.storage.artifact_store import ArtifactStore

STAND_IN_SKIP_REASON = "Not designed by this test's stand-in designer"
"""The skip reason a stand-in designer records for an engine it does not design."""


def skip_undesigned(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    version: int,
    reason: str = SCHEMA_DESIGN_SKIP_REASON_NO_LLM,
) -> list[str]:
    """Write a skip record for each engine of ``version`` with neither a design nor a
    skip record, as the schema design phase does without a model; return them."""
    from src.agents.schema_design.handler import record_design_skip
    from src.storage.assignment_versioning import schema_design_engines

    if version <= 0:
        return []
    written = []
    for engine in schema_design_engines(store, database_name, job_id, version):
        key = f"{database_name}/{job_id}/schema-{engine}/v{version}/schema_output.json"
        if not store.exists(key):
            record_design_skip(store, database_name, job_id, engine, version, reason)
            written.append(engine)
    return written


def skip_undesigned_effective(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    reason: str = STAND_IN_SKIP_REASON,
) -> list[str]:
    """:func:`skip_undesigned` on the job's effective assignment version, the one a
    ``scripts/run_synthesis.py`` run reads; by default a stand-in designer's record."""
    from src.storage.assignment_versioning import resolve_effective_assignment_version

    version = resolve_effective_assignment_version(store, database_name, job_id)
    return skip_undesigned(store, database_name, job_id, version, reason)


def run_synthesis_deterministic(
    job_id: str, database_name: str, store: ArtifactStore, assignment_version: int = 0
) -> dict:
    skip_undesigned(store, database_name, job_id, assignment_version)
    return synthesis_handler.run_synthesis_deterministic(
        job_id, database_name, store, assignment_version
    )


def run_synthesis(
    job_id: str,
    database_name: str,
    store: ArtifactStore,
    assignment_version: int = 0,
    **kwargs: Any,
) -> None:
    skip_undesigned(store, database_name, job_id, assignment_version)
    synthesis_handler.run_synthesis(job_id, database_name, store, assignment_version, **kwargs)


def load_synthesis_data(
    store: ArtifactStore,
    job_id: str,
    database_name: str,
    assignment_version: int = 0,
    **kwargs: Any,
) -> synthesis_data.SynthesisData:
    skip_undesigned(store, database_name, job_id, assignment_version)
    return synthesis_data.load_synthesis_data(
        store, job_id, database_name, assignment_version, **kwargs
    )
