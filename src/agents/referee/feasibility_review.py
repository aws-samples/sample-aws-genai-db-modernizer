"""Post-gate feasibility reviewer (ADR-029 Layer C).

Runs on the customer-edited assignment after the review gate applies it (first
pass and re-entry) and produces structured findings for routings that will not
work. It pushes back out loud rather than letting an infeasible routing ship and
fail in production, but it does not auto-fix: the customer fixes the routing or
explicitly accepts the risk.

Three checks:

- **One source of truth.** A table's writes and locking reads land on one engine
  (ADR-031 Amendment 9, #992). A table written on more than one engine is
  **blocking**: it has no single source of truth to replicate from.
- **Read replica.** A table whose reads are routed to an engine other than its
  source of truth reads an eventually consistent replica, fed by CDC from the
  source of truth. This is the polyglot pattern the tool recommends (CQRS: reads
  on a cache, search or another store, writes on the source of truth), so it is
  **advisory**: the finding names the source of truth, the replica engines, the
  CDC direction, the reads that may lag a write, and the recommended replication
  pattern (CDC, zero-ETL, cache-aside, CQRS) keyed on the replica's role, and the
  gate proceeds rather than blocking. A table's writes are its write targets and
  locking reads (``write_groups.written_tables``), so a table a write only reads
  (``INSERT ... SELECT FROM t``) is read there, not written.
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

from src.agents.referee.capability_registry import can_engine_serve_capability
from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.utility_statements import is_utility_statement
from src.agents.referee.write_groups import written_tables
from src.contracts.assignment_models import Assignment
from src.contracts.feasibility_models import FeasibilityFinding, FindingKind, FindingSeverity
from src.shared import catalog
from src.shared.engine_names import display_engine
from src.shared.report_facts import REPLICA_READS_NAMED
from src.tools.sql.query_facts import source_engine_of

# Engine -> architectural role, used to recommend the replication pattern that
# makes an intended read/write split work. A polyglot target routing reads to a
# cache or search engine while writes stay on the primary is the norm this tool
# recommends, not an infeasibility — it just needs a replication pattern. The role
# is the engine file's (``src/catalog/engines/<id>.toml``, ADR-031 Decision 8, #921);
# only an engine routing can target has one here, any other id reads as "other".


def _engine_role(engine: str) -> str:
    for entry in catalog.engines():
        if entry.id == engine and entry.status in catalog.ACTIVE:
            return entry.role
    return "other"


def _is_system_of_record(engine: str) -> bool:
    return engine in catalog.engine_ids(status=catalog.ACTIVE, system_of_record=True)


def _replication_recommendation(write_engines: set[str], read_dests: set[str]) -> str:
    """Recommend the replication/consistency pattern for a read/write split.

    Keyed on the ROLE of the read destination(s): a cache needs cache-aside/
    write-through kept fresh from the primary; a search index needs CDC/zero-ETL;
    a second system-of-record needs a CQRS read model fed by a change stream, with
    the Saga pattern for writes that must stay consistent across stores.
    """
    writers = _engine_names(write_engines)
    dest_roles = {_engine_role(e) for e in read_dests}
    second_owner = any(_is_system_of_record(e) for e in read_dests)
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
    if second_owner:
        parts.append(
            "a CQRS read model fed by a change stream (DynamoDB Streams or Aurora "
            "logical replication via Kinesis / EventBridge); use the Saga pattern for "
            "writes that must stay consistent across stores"
        )
    if not parts:
        parts.append("a CDC or CQRS replication pattern from the write store to the read store")
    return f"Writes own the data on {writers}. Recommended: " + "; or ".join(parts) + "."


def _engine_names(engines: set[str] | list[str]) -> str:
    """``Aurora PostgreSQL and DynamoDB``: engines by display name, sorted by id."""
    names = [display_engine(e) for e in sorted(engines)]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _query_list(qids: set[str] | list[str]) -> str:
    """``q1, q2 and 3 more``: at most ``REPLICA_READS_NAMED`` query ids, the rest counted."""
    ids = sorted(qids)
    named = ", ".join(ids[:REPLICA_READS_NAMED])
    more = len(ids) - REPLICA_READS_NAMED
    return f"{named} and {more} more" if more > 0 else named


def _engine_lacks_complex_joins(engine: str) -> bool:
    # #490: reads the shared capability registry (the single capability
    # source) instead of reality_check's now-deleted, separately drifting
    # ``ENGINE_CAPABILITIES`` fit-scoring table.
    return not can_engine_serve_capability(engine, ["complex_joins"])


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
    source_engine = source_engine_of(collector_output)
    scope = SourceScope.from_collector(collector_output)

    # table -> {"write_engines", "read_engines", "write_qids", "read_qids"}
    per_table: dict[str, dict[str, set]] = {}
    for q in query_patterns:
        qid = q.get("query_id")
        engine = engine_by_query.get(qid)
        if not engine:
            continue  # not routed / out of scope
        tables = q.get("tables_accessed") or []
        if is_utility_statement(q.get("query_text"), tables, scope):
            continue  # administration traffic: pinned, never routed
        written = set(written_tables(q, source_engine, scope))
        for table in tables:
            bucket = "write" if table in written else "read"
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
        if len(write_engines) > 1:
            findings.append(
                FeasibilityFinding(
                    kind=FindingKind.WRITE_SPLIT,
                    severity=FindingSeverity.BLOCKING,
                    table=table,
                    engines=sorted(write_engines),
                    query_ids=sorted(slot["write_qids"]),
                    message=(
                        f"Table {table} has writes on {_engine_names(write_engines)}: a "
                        "table's writes must land on one engine, its source of truth, so the "
                        "other engines can replicate from it. Route its writes "
                        f"({_query_list(slot['write_qids'])}) to one engine."
                    ),
                )
            )
            continue
        (source,) = tuple(write_engines)
        replicas = sorted(read_engines - write_engines)
        if not replicas:
            continue  # every read runs on the source of truth
        replica_reads = sorted(
            qid for qid in slot["read_qids"] if engine_by_query.get(qid) in replicas
        )
        recommendation = _replication_recommendation(write_engines, set(replicas))
        findings.append(
            FeasibilityFinding(
                kind=FindingKind.READ_WRITE_SPLIT,
                # ADVISORY, not blocking: routing a table's reads to a cache or
                # search engine while writes stay on the primary is the polyglot
                # pattern this tool recommends. It is feasible with replication, so
                # it is a risk to flag (with the pattern to use), not a wall.
                severity=FindingSeverity.ADVISORY,
                table=table,
                engines=sorted({source, *replicas}),
                query_ids=sorted(slot["write_qids"] | set(replica_reads)),
                message=(
                    f"Table {table} has its source of truth on {display_engine(source)} and "
                    f"{'a replica' if len(replicas) == 1 else 'replicas'} on "
                    f"{_engine_names(replicas)}, kept in sync by CDC from "
                    f"{display_engine(source)} to {_engine_names(replicas)}. "
                    f"{len(replica_reads)} {'read is' if len(replica_reads) == 1 else 'reads are'} "
                    f"served by a replica ({_query_list(replica_reads)}) and eventually "
                    f"consistent: a read can miss a write until it replicates. {recommendation}"
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
                    f"{display_engine(engine)}, or perform the join in the application; a "
                    f"server-side JOIN is not available on {display_engine(engine)}."
                )
                findings.append(
                    FeasibilityFinding(
                        kind=FindingKind.CO_DEPENDENCY_ON_NON_JOIN_ENGINE,
                        severity=FindingSeverity.ADVISORY,
                        table=None,
                        engines=[engine],
                        query_ids=sorted(group),
                        message=(
                            f"Co-dependent queries ({_query_list(group)}) share a significant "
                            f"JOIN and are all routed to {display_engine(engine)}, which has no "
                            f"server-side complex-join support. {recommendation}"
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
                        f"Co-dependent queries ({_query_list(group)}) share a significant JOIN "
                        f"but are split across {_engine_names(engines)}; "
                        f"{_engine_names(incapable)} cannot serve the JOIN (no complex-join "
                        "support). Keep the group on one join-capable engine, or move the "
                        "joined data together."
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
                        f"Co-dependent queries ({_query_list(group)}) are split across "
                        f"{_engine_names(engines)}. Each side can serve joins, but a "
                        "cross-engine join means federated or application-side joining; "
                        "confirm this is intended."
                    ),
                )
            )
    return findings


def _detect_stranded_counts(
    assignment: Assignment, collector_output: dict
) -> list[FeasibilityFinding]:
    """A key-scoped count left on an engine its waiver no longer covers (#1151).

    A reroute never moves a customer's pin (ADR-028): when it moved a pinned count's
    table's writes anyway (the customer's pins disagree, or the pinned engine cannot
    take the writes), the count is left pinned and gated there, and only the customer
    can settle it: re-pick the count, or keep the table's writes on its engine."""
    from src.agents.referee.query_scoring import ScoringContext
    from src.agents.referee.waived_counts import stranded_counts

    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    if not queries or not assignment.write_groups:
        return []
    ctx = ScoringContext.from_pipeline({}, None, collector_output)
    findings = []
    for qid, table, engine, source in stranded_counts(
        assignment.query_assignments,
        {q.get("query_id"): q for q in queries},
        ctx,
        assignment.write_groups,
    ):
        where = f"on {display_engine(source)}" if source else "split across engines"
        findings.append(
            FeasibilityFinding(
                kind=FindingKind.STRANDED_COUNT,
                severity=FindingSeverity.BLOCKING,
                table=table,
                engines=sorted({engine, *([source] if source else [])}),
                query_ids=[qid],
                message=(
                    f"Query {qid} counts rows of {table} by its key on "
                    f"{display_engine(engine)}, which can serve that count only while "
                    f"{table}'s writes are there, and they are {where}. Pick another "
                    f"engine for the count, or move {table}'s writes back to "
                    f"{display_engine(engine)}."
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
    findings += _detect_stranded_counts(assignment, collector_output)
    findings.sort(key=lambda f: 0 if f.severity is FindingSeverity.BLOCKING else 1)
    return findings
