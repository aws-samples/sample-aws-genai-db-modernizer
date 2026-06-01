"""
Assignment Validator — Validates assignments for co-dependency conflicts.

Pure function, no side effects. Detects:
- Co-dependent queries split across engines → WARNING per split
- Queries assigned to engine that did not analyze them → hard ERROR

Requirements: 3.1, 3.2, 3.3, 3.4
"""

from __future__ import annotations

from src.agents.referee.assignment_resolver import build_co_dependency_groups
from src.contracts.assignment_models import Assignment, ValidationResult


class AssignmentValidator:
    """Validates assignments for co-dependency conflicts. Pure function, no side effects."""

    def validate(
        self,
        assignment: Assignment,
        collector_output: dict,
        analysis_outputs: dict[str, dict],
    ) -> ValidationResult:
        """Check assignment for conflicts.

        Returns ValidationResult with:
        - valid=True when no hard errors (warnings are acceptable)
        - warnings ordered by severity (HIGH → LOW)
        - errors for hard failures (e.g., query assigned to unanalyzed engine)

        This is a pure function with no side effects and no artifact writes.
        """
        warnings: list[str] = []
        errors: list[str] = []

        # --- Hard ERROR: query assigned to engine that did not analyze it ---
        analyzed_engines = set(analysis_outputs.keys())
        for qa in assignment.query_assignments:
            if qa.assigned_engine not in analyzed_engines:
                errors.append(
                    f"ERROR: Query {qa.query_id} assigned to engine "
                    f"'{qa.assigned_engine}' which did not analyze it. "
                    f"Analyzed engines: {sorted(analyzed_engines)}"
                )

        # --- WARNING: co-dependent queries split across engines ---
        queries = collector_output.get("queries", {}).get("query_patterns", [])
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
