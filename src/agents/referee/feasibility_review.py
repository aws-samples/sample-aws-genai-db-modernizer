"""Post-gate feasibility reviewer (ADR-029 Layer C).

Runs on the customer-edited assignment after the review gate applies it (first
pass and re-entry) and produces structured findings for routings that will not
work. It pushes back out loud rather than letting an infeasible routing ship and
fail in production, but it does not auto-fix: the customer fixes the routing or
explicitly accepts the risk.

Two checks:

- **Read/write split.** A table whose writes land on one engine while some of its
  reads are routed to an engine that does not receive those writes directly needs
  a replication/CQRS pattern to stay in sync. This is the polyglot pattern the
  tool recommends (reads on a cache/search engine, writes on the primary), so it
  is **advisory**: the finding names the recommended replication pattern (CDC,
  zero-ETL, cache-aside, CQRS, Saga) keyed on the read engine's role, and the gate
  proceeds rather than blocking.
- **Capability-aware co-dependency check.** For a co-dependency group (queries
  sharing a significant JOIN):
  * split across engines where at least one destination lacks ``complex_joins``
    cannot serve the JOIN — **blocking**;
  * split across engines that are all join-capable — **advisory** (a cross-engine
    join is still a design smell / needs federation);
  * co-located on a single engine that lacks ``complex_joins`` (e.g. the whole
    group pinned to DynamoDB) — **advisory**: the JOIN cannot run server-side and
    needs a denormalized design or application-side joining, with the recommended
    pattern attached.
"""

from __future__ import annotations

from src.agents.referee.reality_check import ENGINE_CAPABILITIES
from src.contracts.assignment_models import Assignment
from src.contracts.feasibility_models import FeasibilityFinding, FindingKind, FindingSeverity

# QueryPattern.query_type values that mutate data vs. read it. OTHER / None is
# unclassifiable and ignored (it cannot be attributed to a read or a write).
_WRITE_TYPES = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})
_READ_TYPES = frozenset({"SELECT"})

# Engine -> architectural role, used to recommend the replication pattern that
# makes an intended read/write split work. A polyglot target routing reads to a
# cache or search engine while writes stay on the primary is the norm this tool
# recommends, not an infeasibility — it just needs a replication pattern.
_ENGINE_ROLE: dict[str, str] = {
    "elasticache": "cache",
    "opensearch": "search",
    "aurora_postgresql": "relational",
    "aurora_pg": "relational",
    "aurora_mysql": "relational",
    "dynamodb": "keyvalue",
    "documentdb": "document",
}


def _engine_role(engine: str) -> str:
    return _ENGINE_ROLE.get(engine, "other")


def _replication_recommendation(write_engines: set[str], read_dests: set[str]) -> str:
    """Recommend the replication/consistency pattern for a read/write split.

    Keyed on the ROLE of the read destination(s): a cache needs cache-aside/
    write-through kept fresh from the primary; a search index needs CDC/zero-ETL;
    a second system-of-record needs a CQRS read model fed by a change stream, with
    the Saga pattern for writes that must stay consistent across stores.
    """
    writers = sorted(write_engines)
    dest_roles = {_engine_role(e) for e in read_dests}
    parts: list[str] = []
    if "cache" in dest_roles:
        parts.append(
            "cache-aside or write-through caching, keeping ElastiCache fresh from the "
            "write store (DynamoDB Streams, or an Aurora CDC/Lambda updater) with a TTL"
        )
    if "search" in dest_roles:
        parts.append(
            "CDC / zero-ETL replication into the search index (DynamoDB zero-ETL to "
            "OpenSearch, or Aurora zero-ETL / AWS DMS CDC to OpenSearch)"
        )
    if dest_roles & {"relational", "keyvalue", "document"}:
        parts.append(
            "a CQRS read model fed by a change stream (DynamoDB Streams or Aurora "
            "logical replication via Kinesis / EventBridge); use the Saga pattern for "
            "writes that must stay consistent across stores"
        )
    if not parts:
        parts.append("a CDC or CQRS replication pattern from the write store to the read store")
    return f"Writes own the data on {writers}. Recommended: " + "; or ".join(parts) + "."


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
        recommendation = _replication_recommendation(write_engines, reads_without_writes)
        findings.append(
            FeasibilityFinding(
                kind=FindingKind.READ_WRITE_SPLIT,
                # ADVISORY, not blocking: routing a table's reads to a cache or
                # search engine while writes stay on the primary is the polyglot
                # pattern this tool recommends. It is feasible with replication, so
                # it is a risk to flag (with the pattern to use), not a wall.
                severity=FindingSeverity.ADVISORY,
                table=table,
                engines=involved,
                query_ids=sorted(slot["read_qids"] | slot["write_qids"]),
                message=(
                    f"Table '{table}' has writes on {sorted(write_engines)} but reads routed "
                    f"to {sorted(reads_without_writes)}, which do not receive those writes "
                    f"directly. This needs a replication pattern to keep the read engine in "
                    f"sync. {recommendation}"
                ),
                recommended_pattern=recommendation,
            )
        )
    return findings


def _detect_co_dependency_splits(assignment: Assignment) -> list[FeasibilityFinding]:
    engine_by_query = _in_scope_engine_by_query(assignment)
    findings: list[FeasibilityFinding] = []
    for group in assignment.co_dependency_groups:
        engines = {engine_by_query[qid] for qid in group if qid in engine_by_query}
        if not engines:
            continue  # none of the group's queries are routed / in scope
        if len(engines) == 1:
            # Co-located group. Fine on a join-capable engine. On a non-join
            # engine the whole group's JOINs cannot run server-side — they need a
            # denormalized (single-table) design or application-side joining. This
            # is a legitimate modernization target (the schema designer
            # denormalizes), so it is advisory with the pattern to use, not a
            # block. Catches a group the customer (or override propagation) pinned
            # onto an engine like DynamoDB.
            (engine,) = tuple(engines)
            if _engine_lacks_complex_joins(engine):
                recommendation = (
                    f"Denormalize the joined tables into a single-table / embedded design on "
                    f"{engine}, or perform the join in the application; a server-side JOIN is "
                    f"not available on {engine}."
                )
                findings.append(
                    FeasibilityFinding(
                        kind=FindingKind.CO_DEPENDENCY_ON_NON_JOIN_ENGINE,
                        severity=FindingSeverity.ADVISORY,
                        table=None,
                        engines=[engine],
                        query_ids=sorted(group),
                        message=(
                            f"Co-dependent queries {sorted(group)} share a significant JOIN and "
                            f"are all routed to {engine}, which has no server-side complex-join "
                            f"support. {recommendation}"
                        ),
                        recommended_pattern=recommendation,
                    )
                )
            continue  # co-located; capable engine needs no finding
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
