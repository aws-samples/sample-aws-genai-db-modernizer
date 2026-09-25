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

from src.agents.referee.assignment_resolver import (
    build_co_dependency_groups,
    derive_table_assignments,
)
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
    # Query IDs moved automatically to stay co-located with a co-dependent query
    # the customer re-routed (ADR-029 Amendment 3). Empty when nothing propagated.
    propagated_query_ids: list[str] = field(default_factory=list)


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

    # Co-dependency-aware propagation (ADR-029 Amendment 3): if the customer
    # re-routed a query that shares a significant JOIN group with others, move the
    # group's other in-scope members with it so the JOIN stays co-located instead
    # of silently splitting. Read the collector once here and reuse it for the
    # derived-view recompute below.
    collector_output = _read_collector_output(store, database_name, job_id)
    propagated_query_ids = _propagate_codependent_overrides(qa_map, overrides, collector_output)

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

    analysis_outputs = _read_analysis_outputs(store, database_name, job_id)

    # Recompute derived views against the NEW routing instead of carrying the
    # previous version's forward (ADR-029 Layer B). model_copy only replaced
    # query_assignments, so table_assignments and co_dependency_groups would
    # otherwise go stale relative to the customer's edits.
    _recompute_derived_views(new_assignment, collector_output)

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
        propagated_query_ids=propagated_query_ids,
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


def _propagate_codependent_overrides(
    qa_map: dict[str, QueryAssignment],
    overrides: list[QueryOverrideInput],
    collector_output: dict,
) -> list[str]:
    """Move a customer-re-routed query's co-dependent group-mates with it.

    When the customer changes a query's engine and that query shares a significant
    JOIN group with others (``co_dependency_groups``), the group's other IN-SCOPE
    members that the customer did NOT explicitly set are moved to the same engine,
    so the JOIN group stays co-located instead of silently splitting (ADR-029
    Amendment 3). Mutates ``qa_map`` in place; returns the propagated query IDs.

    Respects explicit customer picks: a group the customer explicitly split (two
    members set to different engines in the same edit) is left as chosen — the
    resulting split is surfaced by the feasibility reviewer, not overridden here.
    A propagated query is tagged ``co_dependency_propagated`` (not
    ``customer_override``) so it is distinguishable from the customer's own picks.

    Best-effort: any error building the groups leaves the explicit overrides intact
    and propagates nothing — propagation is an enhancement, never a blocker.
    """
    explicit: dict[str, str] = {
        ov.query_id: ov.assigned_engine for ov in overrides if ov.assigned_engine is not None
    }
    if not explicit:
        return []
    try:
        groups = build_co_dependency_groups(
            collector_output.get("queries", {}).get("query_patterns", []),
            collector_output.get("database_schema", {}).get("tables", []),
        )
    except Exception:  # noqa: BLE001 - never block a customer edit on a grouping error
        return []

    propagated: list[str] = []
    for group in groups:
        touched = set(group) & set(explicit)
        if not touched:
            continue  # customer did not touch this group
        chosen = {explicit[q] for q in touched}
        if len(chosen) != 1:
            continue  # explicitly split across engines -> respect; feasibility flags it
        target = next(iter(chosen))
        anchor = sorted(touched)[0]
        for qid in group:
            if qid in explicit:
                continue  # the customer's own pick is authoritative
            qa = qa_map.get(qid)
            if qa is None or not qa.in_scope:
                continue
            if qa.assigned_engine != target:
                qa.assigned_engine = target
                qa.co_dependency_propagated = True
                qa.assignment_reason = (
                    f"Co-located with co-dependent query group of {anchor} (shared JOIN) "
                    f"after a customer routing change."
                )
                propagated.append(qid)
    return sorted(propagated)


def _recompute_derived_views(assignment: Assignment, collector_output: dict) -> None:
    """Recompute ``table_assignments`` and ``co_dependency_groups`` against the
    current query assignments and collector output, in place.

    Both are derived views that must reflect the routing rather than be carried
    forward from a previous version (ADR-029 Layer B).
    """
    assignment.table_assignments = derive_table_assignments(assignment.query_assignments)
    assignment.co_dependency_groups = build_co_dependency_groups(
        collector_output.get("queries", {}).get("query_patterns", []),
        collector_output.get("database_schema", {}).get("tables", []),
    )


def refresh_consolidated_assignment(
    raw: dict,
    collector_output: dict,
    analysis_outputs: dict[str, dict],
    *,
    dead_engines: set[str] | None = None,
) -> dict:
    """Refresh a Reality-Check consolidated assignment dict (ADR-029 Layers B+E).

    Recomputes the derived views against the consolidated routing, refreshes
    ``validation_warnings`` via the validator (so a split warning naming an engine
    that was consolidated away is not re-emitted), and drops per-query warnings
    that name an engine ``dead_engines`` says consolidation eliminated, so
    dead-engine noise does not persist into the revised version.

    Operates on ``raw`` as a dict and patches only the recomputed fields, so all
    other keys (including the ``reality_check_applied`` marker) are preserved and
    partial/legacy assignment dicts are tolerated — a probe model is built only to
    drive the pure computations.
    """
    qa_dicts = raw.get("query_assignments", [])
    qas = [
        QueryAssignment(
            query_id=qa.get("query_id", ""),
            assigned_engine=qa.get("assigned_engine", ""),
            confidence=int(qa.get("confidence", 0) or 0),
            source_tables=qa.get("source_tables", []) or [],
            assignment_reason=qa.get("assignment_reason", ""),
            in_scope=qa.get("in_scope", True),
            customer_override=qa.get("customer_override", False),
            warnings=qa.get("warnings", []) or [],
        )
        for qa in qa_dicts
    ]

    table_assignments = derive_table_assignments(qas)
    co_dependency_groups = build_co_dependency_groups(
        collector_output.get("queries", {}).get("query_patterns", []),
        collector_output.get("database_schema", {}).get("tables", []),
    )
    probe = Assignment(
        job_id=str(raw.get("job_id", "")),
        version=int(raw.get("version", 1)),
        status=AssignmentStatus.CUSTOMER_MODIFIED,
        timestamp=datetime.now(UTC),
        query_assignments=qas,
        table_assignments=table_assignments,
        co_dependency_groups=co_dependency_groups,
        validation_warnings=[],
    )
    validation = AssignmentValidator().validate(probe, collector_output, analysis_outputs)

    out = dict(raw)
    out["table_assignments"] = [ta.model_dump(mode="json") for ta in table_assignments]
    out["co_dependency_groups"] = co_dependency_groups
    out["validation_warnings"] = validation.warnings
    if dead_engines:
        pruned: list[dict] = []
        for qa in qa_dicts:
            qa = dict(qa)
            qa["warnings"] = [
                w for w in qa.get("warnings", []) if not any(e in w for e in dead_engines)
            ]
            pruned.append(qa)
        out["query_assignments"] = pruned
    return out
