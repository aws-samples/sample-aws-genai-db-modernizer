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
    retained_engine_for,
)
from src.agents.referee.assignment_validator import AssignmentValidator
from src.agents.referee.aurora_choice import source_database_engine
from src.agents.referee.cache_overlay import (
    CACHE_OVERLAY_ENGINES,
    CUSTOMER_UNCACHE_REASON,
    available_cache_engine,
    fallback_owner,
    normalize_cache_owners,
    overlay_summary,
    pin_customer_cache,
    refresh_cache_overlay,
    write_heavy_tables,
)
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    CacheOverlaySummary,
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
    """One customer edit to a query's routing. ``None`` fields are left unchanged.

    ``cached`` pins (True) or removes (False) the cache overlay (#296). Naming a
    cache engine as ``assigned_engine`` is the same as ``cached=True``: the cache
    never owns a query, so the owner stays.
    """

    query_id: str
    assigned_engine: str | None = None
    in_scope: bool | None = None
    cached: bool | None = None


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


def load_assignment_for_edit(
    store: _Store, database_name: str, job_id: str, version: int
) -> tuple[dict, dict, dict[str, dict]]:
    """``(assignment, collector_output, analysis_outputs)`` with legacy cache owners moved.

    An assignment written before the cache overlay (#296) can have ElastiCache
    owners, which the validator rejects; they are moved to their system-of-record
    engine here, in memory, with a note in ``cache_notes``, so the assignment
    stays viewable and editable. The stored artifact is not rewritten.
    """
    raw = store.read_json(assignment_artifact_path(database_name, job_id, version))
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key) if store.exists(collector_key) else {}
    analysis_outputs = _read_analysis_outputs(store, database_name, job_id)
    if normalize_cache_owners(
        raw,
        collector_output.get("queries", {}).get("query_patterns", []),
        analysis_outputs,
        source_database_engine(collector_output),
    ):
        qas = [QueryAssignment.model_validate(qa) for qa in raw["query_assignments"]]
        raw["table_assignments"] = [
            ta.model_dump(mode="json")
            for ta in derive_table_assignments(
                qas, retained_engine=retained_engine_for(collector_output)
            )
        ]
    return raw, collector_output, analysis_outputs


def _apply_cache_override(
    qa: QueryAssignment,
    override: QueryOverrideInput,
    cache: str,
    query: dict | None,
    heavy: set[str],
    analysis_outputs: dict[str, dict],
    source_engine: str,
    owner_counts: dict[str, int],
) -> str | None:
    """Apply a cache request to ``qa`` in place; return the note to record, if any."""
    wants_cache = override.cached is True or override.assigned_engine in CACHE_OVERLAY_ENGINES
    if wants_cache:
        note = None
        if qa.assigned_engine in CACHE_OVERLAY_ENGINES:
            owner = fallback_owner(query, analysis_outputs, source_engine, owner_counts)
            note = (
                f"Query {qa.query_id}: {qa.assigned_engine} is a cache layer and cannot own "
                f"it; owned by {owner}, cached by {cache} as requested."
            )
            qa.assigned_engine = owner
        elif override.assigned_engine in CACHE_OVERLAY_ENGINES:
            note = (
                f"Query {qa.query_id}: requested engine {override.assigned_engine} is a "
                f"cache layer, not a system of record; it stays owned by {qa.assigned_engine} "
                f"and is cached by {cache}."
            )
        d = qa.model_dump()
        pin_customer_cache(d, cache, query, heavy)
        for k in ("cache_engine", "cache_pattern", "cache_reason", "warnings"):
            setattr(qa, k, d[k])
        qa.cache_customer_override = True
        qa.cache_dropped = False
        if note:
            qa.warnings = [*qa.warnings, f"NOTE: {note}"]
        return note
    if override.cached is False:
        # Recorded even when the query is not cached now, so a later re-evaluation
        # (Reality Check refresh) never caches it against the customer's choice
        qa.cache_engine = None
        qa.cache_pattern = None
        qa.cache_reason = CUSTOMER_UNCACHE_REASON
        qa.cache_customer_override = True
        qa.cache_dropped = False
        return None
    return None


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

    raw, collector_output, analysis_outputs = load_assignment_for_edit(
        store, database_name, job_id, current_version
    )
    current = Assignment.model_validate(raw)
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    query_by_id = {q.get("query_id"): q for q in queries}
    cache = available_cache_engine(analysis_outputs) or "elasticache"
    heavy = write_heavy_tables(queries)
    source_engine = source_database_engine(collector_output)
    owner_counts: dict[str, int] = {}
    for existing in current.query_assignments:
        owner_counts[existing.assigned_engine] = owner_counts.get(existing.assigned_engine, 0) + 1

    qa_map: dict[str, QueryAssignment] = {qa.query_id: qa for qa in current.query_assignments}
    cache_notes: list[str] = []

    # Per-query overrides. A changed engine marks the query customer-overridden;
    # an in_scope toggle is a scoping change, not an engine reassignment. A cache
    # engine is never an owner (#296): naming it, or ``cached``, pins the cache
    # overlay and keeps the owner.
    for override in overrides:
        qa = qa_map.get(override.query_id)
        if qa is None:
            raise UnknownQuery(override.query_id)
        note = _apply_cache_override(
            qa,
            override,
            cache,
            query_by_id.get(qa.query_id),
            heavy,
            analysis_outputs,
            source_engine,
            owner_counts,
        )
        if note:
            cache_notes.append(note)
        if (
            override.assigned_engine is not None
            and override.assigned_engine not in CACHE_OVERLAY_ENGINES
        ):
            qa.assigned_engine = override.assigned_engine
            qa.customer_override = True
        if override.in_scope is not None:
            qa.in_scope = override.in_scope

    # Co-dependency-aware propagation (ADR-029 Amendment 3): if the customer
    # re-routed a query that shares a significant JOIN group with others, move the
    # group's other in-scope members with it so the JOIN stays co-located instead
    # of silently splitting. A cache request is not a re-route.
    owner_overrides = [o for o in overrides if o.assigned_engine not in CACHE_OVERLAY_ENGINES]
    propagated_query_ids = _propagate_codependent_overrides(
        qa_map, owner_overrides, collector_output
    )

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
            "cache_notes": [*current.cache_notes, *cache_notes],
        }
    )

    # Recompute derived views against the NEW routing instead of carrying the
    # previous version's forward (ADR-029 Layer B). model_copy only replaced
    # query_assignments, so table_assignments and co_dependency_groups would
    # otherwise go stale relative to the customer's edits.
    _recompute_derived_views(new_assignment, collector_output)
    # The cache overlay summary counts in-scope queries only, so a scope edit
    # changes it; a customer re-route never touches a query's cache overlay.
    summary = overlay_summary(
        [qa.model_dump() for qa in new_assignment.query_assignments],
        collector_output.get("queries", {}).get("query_patterns", []),
    )
    new_assignment.cache_overlay = CacheOverlaySummary(**summary) if summary else None

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
    assignment.table_assignments = derive_table_assignments(
        assignment.query_assignments, retained_engine=retained_engine_for(collector_output)
    )
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

    table_assignments = derive_table_assignments(
        qas, retained_engine=retained_engine_for(collector_output)
    )
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
    # Re-evaluate the cache overlay against the consolidated routing (#296): an
    # overlay survives its owner's consolidation, and the summary follows scope.
    refresh_cache_overlay(
        out, collector_output.get("queries", {}).get("query_patterns", []), analysis_outputs
    )
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
