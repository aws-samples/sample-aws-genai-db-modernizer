"""Shared write path for customer assignment overrides (ADR-028).

One place builds the ``customer_modified`` next version from a set of per-query
overrides, so the web REST route and the ATX review gate produce byte-identical
artifacts (same version bump, status, source, provenance) instead of each
re-implementing the write. This is transport-agnostic: it raises domain errors
that callers translate to their own protocol (HTTP status, chat message, ...).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from src.agents.referee.assignment_validator import AssignmentValidator
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
    ValidationResult,
)
from src.storage.assignment_versioning import (
    assignment_artifact_path,
    resolve_effective_assignment_version,
)


class _Store(Protocol):
    """The store surface this module needs (satisfied by every ArtifactStore)."""

    def read_json(self, path: str) -> dict: ...
    def write_json(self, path: str, data: dict) -> None: ...
    def exists(self, path: str) -> bool: ...
    def list_prefix(self, prefix: str) -> Iterable[str]: ...


@dataclass
class QueryOverrideInput:
    """One customer edit to a query's routing. ``None`` fields are left unchanged."""

    query_id: str
    assigned_engine: str | None = None
    in_scope: bool | None = None


@dataclass
class AssignmentOverrideResult:
    """Outcome of applying overrides and writing the new assignment version."""

    assignment: Assignment
    validation: ValidationResult
    skipped_engines: list[str] = field(default_factory=list)
    written_path: str = ""


class AssignmentOverrideError(Exception):
    """Base class for override-application failures (caller maps to protocol)."""


class NoAssignmentFound(AssignmentOverrideError):
    """No assignment artifact exists to override."""


class UnknownQuery(AssignmentOverrideError):
    """An override referenced a query_id absent from the current assignment."""

    def __init__(self, query_id: str) -> None:
        self.query_id = query_id
        super().__init__(f"Query {query_id} not found in current assignment")


class AssignmentValidationFailed(AssignmentOverrideError):
    """The overridden assignment failed validation with hard errors."""

    def __init__(self, errors: list[str], warnings: list[str]) -> None:
        self.errors = errors
        self.warnings = warnings
        super().__init__("Assignment validation failed with hard errors")


def _read_collector_output(store: _Store, database_name: str, job_id: str) -> dict:
    return store.read_json(f"{database_name}/{job_id}/collector/output.json")


def _read_analysis_outputs(store: _Store, database_name: str, job_id: str) -> dict[str, dict]:
    """Read every ``analysis-<engine>/analysis.json`` for the job."""
    prefix = f"{database_name}/{job_id}/"
    engines: set[str] = set()
    for key in store.list_prefix(prefix):
        relative = key.replace(prefix, "")
        if relative.startswith("analysis-"):
            engines.add(relative.split("/")[0].replace("analysis-", ""))
    outputs: dict[str, dict] = {}
    for engine in engines:
        path = f"{database_name}/{job_id}/analysis-{engine}/analysis.json"
        if store.exists(path):
            outputs[engine] = store.read_json(path)
    return outputs


def apply_assignment_overrides(
    store: _Store,
    database_name: str,
    job_id: str,
    overrides: list[QueryOverrideInput],
    exclude_tables: list[str] | None = None,
    *,
    source: AssignmentSource = AssignmentSource.CUSTOMER_GATE,
) -> AssignmentOverrideResult:
    """Apply customer overrides to the effective assignment and write the next version.

    Reads the effective assignment, applies per-query engine/scope overrides and
    optional table-level scope narrowing, builds a new ``customer_modified``
    version (``version+1``, ``previous_version`` set, ``source`` stamped, fresh
    timestamp), validates it, and writes it. Returns the new assignment plus the
    validation result and the engines left with zero in-scope queries.

    Raises:
        NoAssignmentFound: no assignment artifact exists yet.
        UnknownQuery: an override names a query not in the assignment.
        AssignmentValidationFailed: the result has hard validation errors (the
            new version is NOT written in that case).
    """
    current_version = resolve_effective_assignment_version(store, database_name, job_id)
    if current_version == 0:
        raise NoAssignmentFound

    current_path = assignment_artifact_path(database_name, job_id, current_version)
    current = Assignment.model_validate(store.read_json(current_path))

    qa_map: dict[str, QueryAssignment] = {qa.query_id: qa for qa in current.query_assignments}

    # Per-query overrides. A changed engine marks the query customer-overridden;
    # an in_scope toggle is a scoping change, not an engine reassignment.
    for override in overrides:
        qa = qa_map.get(override.query_id)
        if qa is None:
            raise UnknownQuery(override.query_id)
        if override.assigned_engine is not None:
            qa.assigned_engine = override.assigned_engine
            qa.customer_override = True
        if override.in_scope is not None:
            qa.in_scope = override.in_scope

    # Table-level scope narrowing: a query whose tables are all excluded drops
    # out of scope; a partial overlap stays in scope with a low-severity warning.
    scope_warnings: list[str] = []
    if exclude_tables:
        excluded = set(exclude_tables)
        for qa in qa_map.values():
            tables = set(qa.source_tables)
            if tables and tables.issubset(excluded):
                qa.in_scope = False
            elif tables & excluded:
                scope_warnings.append(
                    f"WARNING [LOW]: Query {qa.query_id} accesses both in-scope and "
                    f"excluded tables ({sorted(tables & excluded)}). Keeping query in scope."
                )

    new_version = current_version + 1
    new_assignment = current.model_copy(
        update={
            "version": new_version,
            "status": AssignmentStatus.CUSTOMER_MODIFIED,
            "source": source,
            "timestamp": datetime.now(UTC),
            "query_assignments": list(qa_map.values()),
            "previous_version": current_version,
        }
    )

    collector_output = _read_collector_output(store, database_name, job_id)
    analysis_outputs = _read_analysis_outputs(store, database_name, job_id)
    validation = AssignmentValidator().validate(new_assignment, collector_output, analysis_outputs)
    if not validation.valid:
        raise AssignmentValidationFailed(validation.errors, validation.warnings)

    new_assignment.validation_warnings = scope_warnings + validation.warnings

    in_scope_counts: dict[str, int] = {}
    for qa in new_assignment.query_assignments:
        if qa.in_scope:
            in_scope_counts[qa.assigned_engine] = in_scope_counts.get(qa.assigned_engine, 0) + 1
    skipped_engines = sorted(
        engine
        for engine in {qa.assigned_engine for qa in new_assignment.query_assignments}
        if in_scope_counts.get(engine, 0) == 0
    )

    new_path = assignment_artifact_path(database_name, job_id, new_version)
    store.write_json(new_path, new_assignment.model_dump(mode="json"))

    return AssignmentOverrideResult(
        assignment=new_assignment,
        validation=validation,
        skipped_engines=skipped_engines,
        written_path=new_path,
    )


def mark_assignment_customer_approved(
    store: _Store, database_name: str, job_id: str
) -> Assignment | None:
    """Stamp the effective assignment's status as ``CUSTOMER_APPROVED`` in place.

    Used when the customer approves the routing **as-is** (no edits) at the review
    gate. The gate's approval signal remains the ``.meta`` ASSIGNMENT_REVIEW phase;
    this additionally records approval on the artifact itself so it is
    self-describing (an auditor reading ``assignment/v<N>/assignment.json`` sees
    ``customer_approved``). It does NOT write a new version — the content is
    unchanged, so append-only lineage is preserved and downstream staleness
    detection (which keys on the version number) is unaffected.

    Idempotent and narrow: a ``CUSTOMER_MODIFIED`` artifact is left untouched (a
    modification already implies approval-with-changes), and re-stamping an
    already-approved artifact is a no-op. Returns the (possibly unchanged)
    effective assignment, or ``None`` when no assignment exists.
    """
    version = resolve_effective_assignment_version(store, database_name, job_id)
    if version == 0:
        return None
    path = assignment_artifact_path(database_name, job_id, version)
    current = Assignment.model_validate(store.read_json(path))
    if current.status in (AssignmentStatus.CUSTOMER_MODIFIED, AssignmentStatus.CUSTOMER_APPROVED):
        return current
    updated = current.model_copy(update={"status": AssignmentStatus.CUSTOMER_APPROVED})
    store.write_json(path, updated.model_dump(mode="json"))
    return updated
