# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Assignment Validator — Validates assignments for co-dependency conflicts.

Pure function, no side effects. Detects:
- Co-dependent queries split across engines → WARNING per split
- A placement unit (``write_groups.placement_groups``: co-dependency groups, write
  groups and routine units merged, ADR-031 Decision 4, Amendments 9 and 21) whose
  in-scope members sit on more than one engine → hard ERROR (#2152), unless the
  customer's own picks inside it name two or more engines and every engine it is on
  is one of those picks: a customer split of a JOIN group or routine unit stands,
  with an advisory feasibility finding
- A table's writes (its write group) on more than one engine → hard ERROR, whoever
  placed them: the customer gate never splits one (Amendment 9)
- The units themselves cannot be built → hard ERROR: the check fails closed
- Queries assigned to engine that did not analyze them → hard ERROR
- Queries owned by a future placeholder engine (ADR-031 Decision 9) → hard ERROR
- Queries owned by an engine that may not own them (a cache layer, or a write on
  an engine that is not a system of record, #296) → hard ERROR

A version saved before every unit was placed whole (contract version below 1.26, or
none) may hold a split its routing made then: a pass deriving a new version from it
keeps that split as a WARNING while the unit sits only on engines it had in the version
the pass started from (:class:`LegacyBaseline`), so the saved job still loads and edits,
version after version. An engine the pass adds to the unit is an ERROR.

Requirements: 3.1, 3.2, 3.3, 3.4
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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

# The longest an InvalidAssignment message gets: run_assessment.py's status line keeps
# 1000 characters, with the exception's type name in front.
MESSAGE_BUDGET = 900

# At most this many query ids are listed in one error line; the rest are counted.
IDS_SHOWN = 3


def saved_before_units(version: Mapping[str, Any]) -> bool:
    """Whether a stored version was saved before :data:`UNITS_WHOLE_SINCE` (no
    ``contract_version``, or a lower one)."""
    raw = str(version.get("contract_version") or "")
    try:
        major, minor = (int(p) for p in raw.split(".", 1))
    except ValueError:
        return True  # no version (or an unreadable one): saved before versions
    return (major, minor) < UNITS_WHOLE_SINCE


def _version_engines(version: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(qa.get("query_id")): str(qa.get("assigned_engine") or "")
        for qa in version.get("query_assignments") or []
        if isinstance(qa, Mapping)
    }


@dataclass(frozen=True)
class LegacyBaseline:
    """What a pass deriving a version from a job saved before whole units may carry.

    ``legacy`` holds the engines of each version of the lineage saved before
    :data:`UNITS_WHOLE_SINCE`; ``start`` the engines of the version the pass starts from
    (as the pass read it). A unit that some legacy version already split is carried
    (a warning) as long as it sits only on engines it had in ``start``: a split
    that old routing made stays visible, and any engine the pass adds is an error.
    Every derived version passes the same test, so the carried split can only
    shrink, version after version.
    """

    legacy: tuple[dict[str, str], ...]
    start: dict[str, str]


def legacy_baseline(
    lineage: Sequence[Mapping[str, Any]], start_engines: Mapping[str, str] | None = None
) -> LegacyBaseline | None:
    """The :class:`LegacyBaseline` of ``lineage`` (the start version's history, oldest
    first, ending with the start version), or ``None`` when none of it was saved
    before whole units. ``start_engines`` overrides the last version's engines when
    the caller changed it in memory (a legacy cache owner moved on load)."""
    legacy = tuple(_version_engines(v) for v in lineage if saved_before_units(v))
    if not legacy or not lineage:
        return None
    start = dict(start_engines) if start_engines is not None else _version_engines(lineage[-1])
    return LegacyBaseline(legacy, start)


def lineage_legacy_baseline(
    read: Callable[[int], dict | None],
    start: Mapping[str, Any],
    start_engines: Mapping[str, str] | None = None,
) -> LegacyBaseline | None:
    """:func:`legacy_baseline` of ``start`` and the versions it descends from
    (``previous_version``), read with ``read(version)``."""
    from src.shared.query_trace import assignment_lineage

    previous = start.get("previous_version")
    earlier = assignment_lineage(read, previous) if isinstance(previous, int) else []
    return legacy_baseline([*earlier, start], start_engines)


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
        where = f"{writer}: " if writer else ""
        head = (
            f"{where}assignment validation failed with {len(errors)} hard error(s), "
            "so nothing was written."
        )
        # The commands print it on one status line, cut at 1000 characters: whole
        # errors only, then how many more.
        shown: list[str] = []
        for e in errors:
            text = e.removeprefix("ERROR: ")
            if len(head) + sum(len(t) + 2 for t in shown) + len(text) + 30 > MESSAGE_BUDGET:
                break
            shown.append(text)
        more = len(errors) - len(shown)
        tail = f" And {more} more." if more else ""
        super().__init__(f"{head} {' '.join(shown)}{tail}".rstrip())


class AssignmentValidator:
    """Validates assignments for co-dependency conflicts. Pure function, no side effects."""

    def validate(
        self,
        assignment: Assignment,
        collector_output: dict,
        analysis_outputs: dict[str, dict],
        *,
        legacy: LegacyBaseline | None = None,
    ) -> ValidationResult:
        """Check assignment for conflicts.

        ``legacy`` (:func:`lineage_legacy_baseline`) is set when the job's history
        holds a version saved before whole units: a unit one of those versions already
        split, still only on engines it had in the start version, is a warning here.

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
        # One error per engine (#2152 review): its name, how many statements, what to do.
        analyzed_engines = set(analysis_outputs.keys())
        unanalysed: dict[str, int] = {}
        for qa in assignment.query_assignments:
            if qa.assigned_engine not in analyzed_engines and (
                qa.assigned_engine not in PLACEHOLDER_ENGINES
            ):
                unanalysed[qa.assigned_engine] = unanalysed.get(qa.assigned_engine, 0) + 1
        for engine, count in sorted(unanalysed.items()):
            noun = "1 statement is" if count == 1 else f"{count} statements are"
            errors.append(
                f"ERROR: {display_engine(engine)} has no analysis (it failed or did not "
                f"run), yet {noun} placed on it. Run its analysis again, then the "
                "assignment."
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

        # --- Hard ERROR: a unit routing places as one is on two or more engines (#2152) ---
        tables = collector_output.get("database_schema", {}).get("tables", [])
        unit_errors, carried, refused = _unit_split_errors(
            assignment, collector_output, queries, tables, legacy
        )
        errors += unit_errors
        warnings += carried

        # --- WARNING: co-dependent queries split across engines ---
        # A group inside a unit already reported as an error is not reported twice.
        co_dep_groups = build_co_dependency_groups(queries, tables)

        qid_to_engine = {qa.query_id: qa.assigned_engine for qa in assignment.query_assignments}

        for group in co_dep_groups:
            engines_in_group = {qid_to_engine[qid] for qid in group if qid in qid_to_engine}
            if len(engines_in_group) > 1 and not set(group) <= refused:
                warnings.append(
                    f"WARNING [HIGH]: Co-dependent queries {sorted(group)} "
                    f"split across engines {sorted(engines_in_group)}. "
                    f"These queries share significant JOIN relationships "
                    f"and should ideally be assigned to the same engine."
                )

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
    legacy: LegacyBaseline | None = None,
) -> tuple[list[str], list[str], set[str]]:
    """``(errors, warnings, refused)`` for units split across engines (ADR-031 Decision 4,
    Amendments 9 and 21): each table's writes, and each placement unit, judged on its
    in-scope members; ``refused`` holds the members of every unit reported as an
    error. A unit the customer split stands when every engine it is on is one the
    customer picked for one of its members; a split ``legacy`` carries is a warning.
    Fails closed: when the units cannot be built, that is the error."""
    try:
        write_groups = collector_write_groups(collector_output)
        units = placement_groups(
            build_co_dependency_groups(queries, tables),
            write_groups,
            routine_unit_members(routine_units_for(assignment, collector_output)),
        )
    except Exception as exc:  # noqa: BLE001 - reported as a hard error, never skipped
        return (
            [
                "ERROR: The placement units (co-dependency groups, write groups and routine "
                f"units) could not be built ({type(exc).__name__}), so the assignment "
                "cannot be checked against them."
            ],
            [],
            set(),
        )
    placed: dict[str, QueryAssignment] = {
        qa.query_id: qa for qa in assignment.query_assignments if qa.in_scope
    }
    errors: list[str] = []
    carried: list[str] = []
    refused: set[str] = set()

    def inherited(members: list[str], on: set[str]) -> bool:
        if legacy is None:
            return False
        split_before = any(
            len({old[q] for q in members if q in placed and q in old}) > 1 for old in legacy.legacy
        )
        had = {legacy.start[q] for q in members if q in placed and q in legacy.start}
        if not split_before or not on <= had:
            return False  # never split by old routing, or the pass added an engine
        carried.append(
            f"WARNING [HIGH]: Queries {_ids(members, placed)} form one unit, which routing "
            "now places on one engine, but this job was saved before that rule and "
            f"already had them on {_engine_list(had)}; they are on {_engine_list(on)}."
        )
        return True

    for members in write_groups.values():
        on = _engines_of(members, placed)
        if len(on) > 1 and not inherited(members, on):
            refused.update(members)
            errors.append(
                f"ERROR: The writes of one table ({_ids(members, placed)}) are on "
                f"{_engine_list(on)}; a table has one writer, so its writes and "
                "locking reads must share one engine."
            )
    for members in units:
        on = _engines_of(members, placed)
        if len(on) < 2 or set(members) <= refused:
            continue  # whole, or already reported through its table's writes
        picked = {
            placed[q].assigned_engine
            for q in members
            if q in placed and placed[q].customer_override
        }
        if len(picked) > 1 and on <= picked:
            continue  # the customer's own split, which stands (Amendment 21)
        if inherited(members, on):
            continue
        refused.update(members)
        errors.append(
            f"ERROR: Queries {_ids(members, placed)} form one placement unit (a "
            "co-dependency group, write group or routine unit) but are on "
            f"{_engine_list(on)}; routing places a unit on one engine."
        )
    return errors, carried, refused


def _engines_of(members: list[str], placed: dict[str, QueryAssignment]) -> set[str]:
    return {placed[q].assigned_engine for q in members if q in placed}


def _ids(members: list[str], placed: dict[str, QueryAssignment]) -> str:
    """The first :data:`IDS_SHOWN` placed members, then how many more."""
    ids = sorted(q for q in members if q in placed)
    shown = ", ".join(ids[:IDS_SHOWN])
    return shown + (f" and {len(ids) - IDS_SHOWN} more" if len(ids) > IDS_SHOWN else "")


def _engine_list(engines: set[str]) -> str:
    return ", ".join(display_engine(e) for e in sorted(engines))
