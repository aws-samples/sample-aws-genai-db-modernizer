"""Post-gate feasibility reviewer (ADR-029 Layer C).

Runs on the customer-edited assignment after the review gate applies it (first
pass and re-entry) and produces structured findings for routings that will not
work. It pushes back out loud rather than letting an infeasible routing ship and
fail in production, but it does not auto-fix: the customer fixes the routing or
explicitly accepts the risk.

Two checks:

- **Read/write split.** A table whose writes land on one engine while some of its
  reads are routed to an engine that never receives those writes cannot work
  without a replication/CQRS pattern (the read engine serves stale or absent
  data). Blocking.
- **Capability-aware co-dependency split.** A co-dependency group (queries sharing
  a significant JOIN) split across engines where at least one destination lacks
  ``complex_joins`` cannot serve the JOIN. Blocking. A split where every side is
  join-capable is advisory (a cross-engine join is still a design smell).
"""

from __future__ import annotations

from src.agents.referee.reality_check import ENGINE_CAPABILITIES
from src.contracts.assignment_models import Assignment
from src.contracts.feasibility_models import FeasibilityFinding, FindingKind, FindingSeverity

# QueryPattern.query_type values that mutate data vs. read it. OTHER / None is
# unclassifiable and ignored (it cannot be attributed to a read or a write).
_WRITE_TYPES = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})
_READ_TYPES = frozenset({"SELECT"})

_REPLICATION_HINT = (
    "Move both the reads and the writes to one engine, or add a replication "
    "pattern (CQRS / materialized view, e.g. DynamoDB to OpenSearch via zero-ETL) "
    "so the read engine is kept in sync with the engine that owns the writes."
)


def _engine_lacks_complex_joins(engine: str) -> bool:
    return "complex_joins" not in ENGINE_CAPABILITIES.get(engine, set())


def _in_scope_engine_by_query(assignment: Assignment) -> dict[str, str]:
    """query_id -> assigned_engine for in-scope query assignments only."""
    return {
        qa.query_id: qa.assigned_engine
        for qa in assignment.query_assignments
        if qa.in_scope and qa.assigned_engine
    }


def _detect_read_write_splits(
    assignment: Assignment, collector_output: dict
) -> list[FeasibilityFinding]:
    engine_by_query = _in_scope_engine_by_query(assignment)
    query_patterns = collector_output.get("queries", {}).get("query_patterns", [])

    # table -> {"write_engines", "read_engines", "write_qids", "read_qids"}
    per_table: dict[str, dict[str, set]] = {}
    for q in query_patterns:
        qid = q.get("query_id")
        engine = engine_by_query.get(qid)
        if not engine:
            continue  # not routed / out of scope
        qtype = str(q.get("query_type") or "").upper()
        if qtype in _WRITE_TYPES:
            bucket = "write"
        elif qtype in _READ_TYPES:
            bucket = "read"
        else:
            continue  # unclassifiable (OTHER / None)
        for table in q.get("tables_accessed") or []:
            slot = per_table.setdefault(
                table,
                {
                    "write_engines": set(),
                    "read_engines": set(),
                    "write_qids": set(),
                    "read_qids": set(),
                },
            )
            slot[f"{bucket}_engines"].add(engine)
            slot[f"{bucket}_qids"].add(qid)

    findings: list[FeasibilityFinding] = []
    for table in sorted(per_table):
        slot = per_table[table]
        write_engines = slot["write_engines"]
        read_engines = slot["read_engines"]
        if not write_engines:
            continue  # read-only table: nothing to keep in sync
        reads_without_writes = read_engines - write_engines
        if not reads_without_writes:
            continue  # every read engine also owns the writes
        involved = sorted(write_engines | reads_without_writes)
        findings.append(
            FeasibilityFinding(
                kind=FindingKind.READ_WRITE_SPLIT,
                severity=FindingSeverity.BLOCKING,
                table=table,
                engines=involved,
                query_ids=sorted(slot["read_qids"] | slot["write_qids"]),
                message=(
                    f"Table '{table}' has writes on {sorted(write_engines)} but reads routed "
                    f"to {sorted(reads_without_writes)}, which never receive those writes. "
                    f"This will not work as routed: {_REPLICATION_HINT}"
                ),
            )
        )
    return findings


def _detect_co_dependency_splits(assignment: Assignment) -> list[FeasibilityFinding]:
    engine_by_query = _in_scope_engine_by_query(assignment)
    findings: list[FeasibilityFinding] = []
    for group in assignment.co_dependency_groups:
        engines = {engine_by_query[qid] for qid in group if qid in engine_by_query}
        if len(engines) <= 1:
            continue  # co-located, fine
        incapable = sorted(e for e in engines if _engine_lacks_complex_joins(e))
        if incapable:
            findings.append(
                FeasibilityFinding(
                    kind=FindingKind.CO_DEPENDENCY_SPLIT,
                    severity=FindingSeverity.BLOCKING,
                    table=None,
                    engines=sorted(engines),
                    query_ids=sorted(group),
                    message=(
                        f"Co-dependent queries {sorted(group)} share a significant JOIN but are "
                        f"split across engines {sorted(engines)}; {incapable} cannot serve the "
                        f"JOIN (no complex-join support). Keep the group on one join-capable "
                        f"engine, or move the joined data together."
                    ),
                )
            )
        else:
            findings.append(
                FeasibilityFinding(
                    kind=FindingKind.CO_DEPENDENCY_SPLIT,
                    severity=FindingSeverity.ADVISORY,
                    table=None,
                    engines=sorted(engines),
                    query_ids=sorted(group),
                    message=(
                        f"Co-dependent queries {sorted(group)} are split across engines "
                        f"{sorted(engines)}. Each side can serve joins, but a cross-engine join "
                        f"means federated or application-side joining; confirm this is intended."
                    ),
                )
            )
    return findings


def review_assignment_feasibility(
    assignment: Assignment, collector_output: dict
) -> list[FeasibilityFinding]:
    """Return feasibility findings for a routing, blocking findings first.

    Pure function, no side effects. Callers treat a non-empty blocking subset as a
    reason to loop the assignment-review gate.
    """
    findings = _detect_read_write_splits(assignment, collector_output)
    findings += _detect_co_dependency_splits(assignment)
    findings.sort(key=lambda f: 0 if f.severity is FindingSeverity.BLOCKING else 1)
    return findings
