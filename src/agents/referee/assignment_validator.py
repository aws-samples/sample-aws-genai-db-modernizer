# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Assignment Validator — Validates assignments for co-dependency conflicts.

Pure function, no side effects. Detects:
- Co-dependent queries split across engines → WARNING per split
- A placement unit (``write_groups.placement_groups``: co-dependency groups, write
  groups and routine units merged, ADR-031 Decision 4, Amendments 9 and 21) whose
  in-scope members sit on more than one engine → hard ERROR (#2152), unless the
  customer's own picks inside it name two or more engines: a customer split of a
  JOIN group or routine unit stands, with an advisory feasibility finding
- A table's writes (its write group) on more than one engine → hard ERROR, whoever
  placed them: the customer gate never splits one (Amendment 9)
- The units themselves cannot be built → hard ERROR: the check fails closed
- Queries assigned to engine that did not analyze them → hard ERROR
- Queries owned by a future placeholder engine (ADR-031 Decision 9) → hard ERROR
- Queries owned by an engine that may not own them (a cache layer, or a write on
  an engine that is not a system of record, #296) → hard ERROR

A version saved before every unit was placed whole (contract version below 1.26, or
none) may hold a split its routing made then: a pass deriving a new version from it
keeps that split as a WARNING (``legacy_baseline``, :func:`legacy_engines`), so the
saved job still loads and edits. A split the pass itself makes is still an ERROR.

Requirements: 3.1, 3.2, 3.3, 3.4
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.agents.referee.assignment_resolver import build_co_dependency_groups
from src.agents.referee.aurora_choice import source_database_engine
from src.agents.referee.cache_overlay import CACHE_OVERLAY_ENGINES, can_own
from src.agents.referee.routine_units import routine_unit_members, routine_units_for
from src.agents.referee.write_groups import collector_write_groups, placement_groups
from src.contracts.assignment_models import Assignment, QueryAssignment, ValidationResult
from src.shared.engine_names import PLACEHOLDER_ENGINES, display_engine

# The contract version from which every routing pass places co-dependency groups, write
# groups and routine units whole (1.26, #1654: routine units, the last of the three).
UNITS_WHOLE_SINCE = (1, 26)


def legacy_engines(
    stored: Mapping[str, Any] | None, engines: Mapping[str, str] | None = None
) -> dict[str, str] | None:
    """``{query_id: engine}`` of a stored version saved before :data:`UNITS_WHOLE_SINCE`
    (no ``contract_version``, or a lower one), else ``None``. ``engines`` is the stored
    version's own engines when the caller already changed ``stored`` in memory."""
    if not stored:
        return None
    raw = str(stored.get("contract_version") or "")
    try:
        major, minor = (int(p) for p in raw.split(".", 1))
    except ValueError:
        major, minor = 0, 0  # no version (or an unreadable one): saved before versions
    if (major, minor) >= UNITS_WHOLE_SINCE:
        return None
    if engines is not None:
        return dict(engines)
    return {
        str(qa.get("query_id")): str(qa.get("assigned_engine") or "")
        for qa in stored.get("query_assignments") or []
        if isinstance(qa, Mapping)
    }


class InvalidAssignment(RuntimeError):
    """An assignment version failed validation with hard errors, so it was not written.

    Raised by every pass that writes a version (#2152): the first assignment, the
    reality check, the post-schema reroute, and (as ``AssignmentValidationFailed``)
    the customer gate and the source-of-truth reroute. ``writer`` names the pass. The
    message lists the errors, so the commands (their status line) and the UI (the
    phase's error message) show why.
    """

    def __init__(self, errors: list[str], warnings: list[str], writer: str = "") -> None:
        self.errors = errors
        self.warnings = warnings
        self.writer = writer
        shown = "; ".join(e.removeprefix("ERROR: ") for e in errors[:10])
        more = f"; and {len(errors) - 10} more" if len(errors) > 10 else ""
        where = f"{writer}: " if writer else ""
        super().__init__(
            f"{where}assignment validation failed with {len(errors)} hard error(s), "
            f"so nothing was written. {shown}{more}"
        )


class AssignmentValidator:
    """Validates assignments for co-dependency conflicts. Pure function, no side effects."""

    def validate(
        self,
        assignment: Assignment,
        collector_output: dict,
        analysis_outputs: dict[str, dict],
        *,
        legacy_baseline: Mapping[str, str] | None = None,
    ) -> ValidationResult:
        """Check assignment for conflicts.

        ``legacy_baseline`` (:func:`legacy_engines`) is the engines of the stored
        version this one derives from, when that version predates whole units: a unit
        it already split is a warning here, not an error.

        Returns ValidationResult with:
        - valid=True when no hard errors (warnings are acceptable)
        - warnings ordered by severity (HIGH → LOW)
        - errors for hard failures (e.g., query assigned to unanalyzed engine)

        This is a pure function with no side effects and no artifact writes.
        """
        warnings: list[str] = []
        errors: list[str] = []

        # --- Hard ERROR: a placeholder engine owns a query (ADR-031 Decision 9) ---
        for qa in assignment.query_assignments:
            if qa.assigned_engine in PLACEHOLDER_ENGINES:
                errors.append(
                    f"ERROR: Query {qa.query_id} is assigned to '{qa.assigned_engine}', a future "
                    "placeholder engine that cannot own a query."
                )

        # --- Hard ERROR: query assigned to engine that did not analyze it ---
        analyzed_engines = set(analysis_outputs.keys())
        for qa in assignment.query_assignments:
            if qa.assigned_engine not in analyzed_engines and (
                qa.assigned_engine not in PLACEHOLDER_ENGINES
            ):
                errors.append(
                    f"ERROR: Query {qa.query_id} assigned to engine "
                    f"'{qa.assigned_engine}' which did not analyze it. "
                    f"Analyzed engines: {sorted(analyzed_engines)}"
                )

        # --- Hard ERROR: an engine that may not own the query owns it (#296) ---
        queries = collector_output.get("queries", {}).get("query_patterns", [])
        query_by_id = {q.get("query_id"): q for q in queries}
        source_engine = source_database_engine(collector_output)
        for qa in assignment.query_assignments:
            if qa.assigned_engine in CACHE_OVERLAY_ENGINES:
                errors.append(
                    f"ERROR: Query {qa.query_id} is owned by '{qa.assigned_engine}', the cache "
                    "layer, which this plan does not use as a system of record. Assign it to "
                    "the engine that stores its data; the cache can still front it as a cache "
                    "overlay."
                )
            elif not can_own(qa.assigned_engine, query_by_id.get(qa.query_id), source_engine):
                errors.append(
                    f"ERROR: Query {qa.query_id} is a write owned by "
                    f"{display_engine(qa.assigned_engine)}, "
                    "which this plan does not use as a system of record for its tables."
                )

        # --- WARNING: co-dependent queries split across engines ---
        tables = collector_output.get("database_schema", {}).get("tables", [])
        co_dep_groups = build_co_dependency_groups(queries, tables)

        qid_to_engine = {qa.query_id: qa.assigned_engine for qa in assignment.query_assignments}

        for group in co_dep_groups:
            engines_in_group = {qid_to_engine[qid] for qid in group if qid in qid_to_engine}
            if len(engines_in_group) > 1:
                warnings.append(
                    f"WARNING [HIGH]: Co-dependent queries {sorted(group)} "
                    f"split across engines {sorted(engines_in_group)}. "
                    f"These queries share significant JOIN relationships "
                    f"and should ideally be assigned to the same engine."
                )

        # --- Hard ERROR: a unit routing places as one is on two or more engines (#2152) ---
        unit_errors, carried = _unit_split_errors(
            assignment, collector_output, queries, tables, legacy_baseline
        )
        errors += unit_errors
        warnings += carried

        # Order warnings by severity (HIGH → LOW)
        warnings.sort(key=_warning_severity_key)

        return ValidationResult(
            valid=len(errors) == 0,
            warnings=warnings,
            errors=errors,
        )


def _warning_severity_key(warning: str) -> int:
    """Sort key: lower number = higher severity (sorted first)."""
    if "[HIGH]" in warning:
        return 0
    if "[MEDIUM]" in warning:
        return 1
    if "[LOW]" in warning:
        return 2
    return 3


def _unit_split_errors(
    assignment: Assignment,
    collector_output: dict,
    queries: list,
    tables: list,
    legacy_baseline: Mapping[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for units split across engines (ADR-031 Decision 4,
    Amendments 9 and 21): each table's writes, and each placement unit, judged on its
    in-scope members. A split ``legacy_baseline`` already had is a warning. Fails
    closed: when the units cannot be built, that is the error."""
    try:
        write_groups = collector_write_groups(collector_output)
        units = placement_groups(
            build_co_dependency_groups(queries, tables),
            write_groups,
            routine_unit_members(routine_units_for(assignment, collector_output)),
        )
    except Exception as exc:  # noqa: BLE001 - reported as a hard error, never skipped
        return [
            "ERROR: The placement units (co-dependency groups, write groups and routine "
            f"units) could not be built ({type(exc).__name__}), so the assignment cannot "
            "be checked against them."
        ], []
    placed: dict[str, QueryAssignment] = {
        qa.query_id: qa for qa in assignment.query_assignments if qa.in_scope
    }
    errors: list[str] = []
    carried: list[str] = []

    def inherited(members: list[str]) -> bool:
        if legacy_baseline is None:
            return False
        before = {legacy_baseline[q] for q in members if q in placed and q in legacy_baseline}
        if len(before) < 2:
            return False
        carried.append(
            f"WARNING [HIGH]: Queries {_ids(members, placed)} form one unit routing now "
            "places on one engine, but the version this one derives from, saved before "
            f"that rule, already had them on {_engine_list(before)}."
        )
        return True

    for members in write_groups.values():
        on = _engines_of(members, placed)
        if len(on) > 1 and not inherited(members):
            errors.append(
                f"ERROR: The writes of one table ({_ids(members, placed)}) are on "
                f"{_engine_list(on)}; a table has one writer, so its writes and "
                "locking reads must share one engine."
            )
    for members in units:
        on = _engines_of(members, placed)
        if len(on) < 2:
            continue
        picked = {
            placed[q].assigned_engine
            for q in members
            if q in placed and placed[q].customer_override
        }
        if len(picked) > 1:
            continue  # the customer's own split, which stands (Amendment 21)
        if inherited(members):
            continue
        errors.append(
            f"ERROR: Queries {_ids(members, placed)} form one placement unit (a "
            "co-dependency group, write group or routine unit) but are on "
            f"{_engine_list(on)}; routing places a unit on one engine."
        )
    return errors, carried


def _engines_of(members: list[str], placed: dict[str, QueryAssignment]) -> set[str]:
    return {placed[q].assigned_engine for q in members if q in placed}


def _ids(members: list[str], placed: dict[str, QueryAssignment]) -> str:
    return ", ".join(sorted(q for q in members if q in placed))


def _engine_list(engines: set[str]) -> str:
    return ", ".join(display_engine(e) for e in sorted(engines))
