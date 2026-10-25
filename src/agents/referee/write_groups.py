"""A table's write group: all of its write paths share one engine (ADR-031 Amendment 9, #992).

Maintainer decision (2026-10-17): a table has one source of truth. Every
statement that writes it (an ``INSERT``, ``UPDATE``, ``DELETE`` or ``MERGE``)
and every locking read on it (a read that takes the row locks a write needs)
lands on one engine. Its reads stay per query: another engine can serve a read
as an eventually consistent replica, fed by change data capture (CDC) from the
source of truth.

This is narrower than the table-owner rule Decision 1 forbids. It groups a
table's writes only, never its reads, and the group's engine comes from the
writes' own per-query scores through the shared group rule
(``co_dependency.group_engine_scores``, Decision 4 Amendment 4): the mean over
the members, every member servable, ties through ``tie_break``.

A write group and a co-dependency group that share a statement must move
together, or one of them would split, so every routing pass places
:func:`placement_groups`: the two kinds merged where they overlap.
Administration statements (``TRUNCATE``, DDL, ...) never enter routing (the
utility pin keeps them on the source-compatible engine), so they join no write
group.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.agents.referee.cache_overlay import is_locking_read, is_write_query
from src.agents.referee.table_resolution import SourceScope, is_engine_system_object
from src.agents.referee.utility_statements import is_utility_statement
from src.shared.engine_names import SOURCE_DATABASE
from src.tools.sql.query_facts import query_facts, source_engine_of

__all__ = [
    "WRITE_GROUP_LABEL",
    "assignment_placement_groups",
    "assignment_sources_of_truth",
    "assignment_unit_weights",
    "build_write_groups",
    "collector_write_groups",
    "inserted_tables",
    "placement_groups",
    "sources_of_truth",
    "table_sources",
    "unit_call_weights",
    "unit_write_tables",
    "write_group_tables",
    "written_tables",
]


# A write group's members are weighted by call rate (#992, maintainer decision
# 2026-10-17), relative to the unit's busiest member (weight 1.0). Any member
# below this share of the busiest member's rate, one with no recorded calls
# included, weighs this share, so it counts but cannot outweigh real traffic, and
# never vanishes. On the bundled samples it binds one write (trailgear's
# UserSession DELETE), and every result holds from 1e-9 to 0.05.
CALL_WEIGHT_FLOOR_SHARE = 0.01

# The assignment reason's label for a unit placed by this rule (the move ledger's
# rule is ``group.write``).
WRITE_GROUP_LABEL = "write group: one source of truth"


_DML_TYPES = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE"})


def _bare(table: str) -> str:
    return table.rsplit(".", 1)[-1].strip('[]"`').lower()


def written_tables(
    query: Mapping, source_engine: str = "", source: SourceScope | None = None
) -> list[str]:
    """The collected tables ``query`` writes or locks, as its ``tables_accessed`` names them.

    A write's tables are its targets (``QueryFacts.write_targets``), never a table
    it only reads; a locking read's are every table it reads, since it locks
    their rows. A write whose target is none of its collected tables (no text, or
    text the facts cannot read) counts as writing all of them: keeping too many
    writes together never splits a table's writes. An administration statement
    writes no table here.
    """
    text = str(query.get("query_text") or "")
    tables = [str(t) for t in query.get("tables_accessed") or [] if t and t != "unknown"]
    if is_utility_statement(text, tables, source):
        # An administration statement writes no table, but a DML statement that only
        # reads a catalog (``INSERT INTO stats SELECT ... FROM information_schema.tables``)
        # still writes its own target (#1287).
        if str(query.get("query_type") or "").upper() not in _DML_TYPES:
            return []
        targets = set(query_facts(text, source_engine).write_targets)
        return [t for t in tables if _bare(t) in targets and not is_engine_system_object(t, source)]
    out: list[str] = []
    if is_write_query(query, source_engine):
        targets = set(query_facts(text, source_engine).write_targets)
        out.extend([t for t in tables if _bare(t) in targets] or tables)
    if is_locking_read(query, source_engine):
        out.extend(tables)
    return list(dict.fromkeys(out))


def inserted_tables(
    query: Mapping, source_engine: str = "", source: SourceScope | None = None
) -> list[str]:
    """The :func:`written_tables` ``query`` only appends rows to (#1392).

    An append is an ``INSERT`` target the statement names (``QueryFacts.append_targets``,
    read in ``source_engine``'s dialect, #1459): a data-modifying CTE's insert counts
    whatever the collected ``query_type`` (the collector types ``WITH w AS (INSERT ...)
    SELECT ...`` as ``OTHER``), and so does ``ON CONFLICT DO NOTHING``, which never
    overwrites a row; an insert that can overwrite the row it collides with (``ON
    CONFLICT ... DO UPDATE``, ``ON DUPLICATE KEY UPDATE``, MySQL's ``REPLACE``) does not.
    It is counted on its write targets only, never on a table the statement only reads
    or locks. An ``UPDATE``, ``DELETE``, ``MERGE`` and a locking read append nothing.
    """
    written = written_tables(query, source_engine, source)
    if not written:
        return []
    facts = query_facts(str(query.get("query_text") or ""), source_engine)
    targets = set(facts.append_targets)
    matched = [t for t in written if _bare(t) in targets]
    if matched:
        return matched
    if str(query.get("query_type") or "").upper() != "INSERT":
        return []
    if not targets and (facts.write_targets or facts.upsert):
        return []  # the text names its target, and it overwrites (an upsert, a REPLACE)
    # The target is none of the collected tables, or the text names none
    # (``written_tables``' fallback): every written table, unless the statement also
    # locks what it reads.
    return [] if is_locking_read(query, source_engine) else written


def build_write_groups(
    queries: Iterable[Mapping], source_engine: str = "", source: SourceScope | None = None
) -> dict[str, list[str]]:
    """``{table: [query_id, ...]}``: each written table's write statements, in query order.

    Tables are sorted; a table with one write statement is listed too (it is
    still that table's source of truth), though only a group of two or more
    constrains routing.
    """
    groups: dict[str, list[str]] = {}
    for query in queries:
        qid = str(query.get("query_id") or "")
        if not qid:
            continue
        for table in written_tables(query, source_engine, source):
            members = groups.setdefault(table, [])
            if qid not in members:
                members.append(qid)
    return {table: groups[table] for table in sorted(groups)}


def placement_groups(
    co_dependency_groups: Iterable[Iterable[str]],
    write_groups: Mapping[str, Iterable[str]] | None,
) -> list[list[str]]:
    """The units every routing pass places as one: co-dependency groups and write
    groups (two or more members), merged where they share a query.

    Deterministic: units come in the order of their first member (co-dependency
    groups first, in their order, then write groups by table), members in the
    order first seen. A co-dependency group no write group touches comes back
    exactly as given.
    """
    sources = [list(g) for g in co_dependency_groups]
    sources += [list(m) for _, m in sorted((write_groups or {}).items()) if len(list(m)) >= 2]
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        root = x
        while parent.setdefault(root, root) != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    order: list[str] = []
    for group in sources:
        for qid in group:
            if qid not in parent:
                order.append(qid)
            find(qid)
        for qid in group[1:]:
            a, b = find(group[0]), find(qid)
            if a != b:
                parent[b] = a
    units: dict[str, list[str]] = {}
    for qid in order:
        units.setdefault(find(qid), []).append(qid)
    return [members for members in units.values() if len(members) >= 2]


def write_group_tables(write_groups: Mapping[str, Iterable[str]] | None) -> dict[str, list[str]]:
    """``{query_id: [table, ...]}`` for the members of every write group of two or more."""
    out: dict[str, list[str]] = {}
    for table, members in sorted((write_groups or {}).items()):
        members = list(members)
        if len(members) < 2:
            continue
        for qid in members:
            out.setdefault(qid, []).append(table)
    return out


def unit_write_tables(
    unit: Iterable[str], write_groups: Mapping[str, Iterable[str]] | None
) -> list[str]:
    """The tables whose write group (two or more statements) is part of ``unit``."""
    members = set(unit)
    return sorted(
        table
        for table, group in (write_groups or {}).items()
        if len(list(group)) >= 2 and set(group) <= members
    )


def assignment_placement_groups(assignment: Mapping | None) -> list[list[str]]:
    """:func:`placement_groups` of an assignment artifact (a dict): its
    ``co_dependency_groups`` and ``write_groups``. An artifact written before
    write groups existed gives its co-dependency groups alone."""
    assignment = assignment or {}
    return placement_groups(
        assignment.get("co_dependency_groups") or [], assignment.get("write_groups") or {}
    )


def collector_write_groups(collector_output: Mapping | None) -> dict[str, list[str]]:
    """:func:`build_write_groups` over a collector output's query patterns, read in
    its source engine's dialect."""
    collector_output = collector_output or {}
    return build_write_groups(
        collector_output.get("queries", {}).get("query_patterns", []),
        source_engine_of(collector_output),
        SourceScope.from_collector(collector_output),
    )


def sources_of_truth(
    write_groups: Mapping[str, Iterable[str]] | None, engine_of: Mapping[str, str]
) -> dict[str, str]:
    """``{table: engine}`` for each written table whose writes placed so far
    (``engine_of``, query id -> engine) all sit on one engine, its source of truth. A
    table with no placed write, or writes on two engines, has none."""
    out: dict[str, str] = {}
    for table, members in sorted((write_groups or {}).items()):
        writers = {engine_of[q] for q in members if engine_of.get(q)}
        if len(writers) == 1:
            (out[table],) = writers
    return out


def assignment_sources_of_truth(assignment: Mapping | None) -> dict[str, str]:
    """:func:`sources_of_truth` of an assignment artifact (a dict): its ``write_groups``
    and in-scope query engines. Empty for one written before write groups existed."""
    assignment = assignment or {}
    engine_of = {
        str(qa.get("query_id")): str(qa.get("assigned_engine") or "")
        for qa in assignment.get("query_assignments") or []
        if qa.get("in_scope", True)
    }
    return sources_of_truth(assignment.get("write_groups") or {}, engine_of)


def unservable_write_groups(
    collector_output: Mapping | None, unservable: Iterable[Any]
) -> dict[str, list[str]]:
    """The write groups whose every write has no engine in the customer's engine scope
    (#1329): ``{table: [query_id, ...]}`` from the collection, for the tables an
    assignment's ``unservable_queries`` leave with no assigned write. A write group is
    unservable as one unit, so a table's writes are all unservable or none is."""
    ids = {
        str(u.get("query_id") if isinstance(u, Mapping) else getattr(u, "query_id", u))
        for u in unservable
    }
    if not ids:
        return {}
    return {
        table: sorted(members)
        for table, members in collector_write_groups(collector_output).items()
        if members and set(members) <= ids
    }


def table_sources(
    assignment: Mapping | None,
    unservable_writes: Mapping[str, Iterable[str]] | None = None,
) -> list[dict[str, object]]:
    """Each written table's source of truth and replicas, for ``report.json``
    (``SynthesisOutputContract.table_sources``, #992), sorted by table.

    ``unservable_writes`` (:func:`unservable_write_groups`, #1329) are the tables whose
    every write has no engine in the customer's engine scope: the source database stays
    their writer (``source_of_truth`` is ``SOURCE_DATABASE``), and each in-scope engine
    that reads the table is a replica kept in sync from it (ADR-031 Amendments 9, 15).

    Read from the assignment's ``write_groups`` and in-scope query assignments: a
    table's writes give its source of truth (the one engine they sit on;
    ``write_engines`` lists them all, so a table whose writes still sit on two
    engines says so), and every other engine with an in-scope query on the table
    is a replica, with the reads it serves. Empty for an assignment written
    before write groups existed.
    """
    assignment = assignment or {}
    qas = [qa for qa in assignment.get("query_assignments") or [] if qa.get("in_scope", True)]
    engine_of = {str(qa.get("query_id")): str(qa.get("assigned_engine") or "") for qa in qas}
    readers: dict[str, dict[str, list[str]]] = {}
    groups = dict(assignment.get("write_groups") or {})
    kept_by_source = {
        str(t): sorted(str(q) for q in m)
        for t, m in (unservable_writes or {}).items()
        if t not in groups
    }
    groups.update(kept_by_source)
    for qa in qas:
        qid = str(qa.get("query_id"))
        for table in qa.get("source_tables") or []:
            if table in groups and qid not in groups[table]:
                readers.setdefault(table, {}).setdefault(engine_of[qid], []).append(qid)
    out: list[dict[str, object]] = []
    for table in sorted(groups):
        if table in kept_by_source:
            # #1329: no engine in the scope takes these writes; the source keeps them.
            out.append(
                {
                    "table": table,
                    "source_of_truth": SOURCE_DATABASE,
                    "write_engines": [],
                    "write_query_ids": kept_by_source[table],
                    "replicas": [
                        {"engine": engine, "read_query_ids": sorted(qids)}
                        for engine, qids in sorted(readers.get(table, {}).items())
                    ],
                }
            )
            continue
        writes = [q for q in groups[table] if q in engine_of]
        if not writes:
            continue
        writers = {engine_of[q] for q in writes}
        # The one writer, never a pick among several (a split has no source of truth).
        source: str | None = None
        if len(writers) == 1:
            (source,) = writers
        write_engines = sorted(writers)
        replicas = [
            {"engine": engine, "read_query_ids": sorted(qids)}
            for engine, qids in sorted(readers.get(table, {}).items())
            if source is not None and engine != source
        ]
        out.append(
            {
                "table": table,
                "source_of_truth": source,
                "write_engines": write_engines,
                "write_query_ids": sorted(writes),
                "replicas": replicas,
            }
        )
    return out


def unit_call_weights(
    units: Iterable[Iterable[str]],
    write_groups: Mapping[str, Iterable[str]] | None,
    query_by_id: Mapping[str, Mapping],
) -> dict[str, float]:
    """``{query_id: weight}`` for every member of a unit that holds a write group
    (two or more writes of one table): its call rate (``calls_per_second``) over
    the unit's busiest member's, so the busiest weighs 1.0 and equal rates weigh
    exactly 1.0 each (the plain mean), and at least ``CALL_WEIGHT_FLOOR_SHARE``. A
    unit with no call statistics at all gets no weights, so its plain mean decides
    (``co_dependency.group_engine_scores``); so does every unit with no write group.
    """
    out: dict[str, float] = {}
    for unit in units:
        members = list(unit)
        if not unit_write_tables(members, write_groups):
            continue
        rates = {q: _call_rate(query_by_id.get(q)) for q in members}
        busiest = max(rates.values(), default=0.0)
        if busiest <= 0:
            continue
        out.update({q: max(rate / busiest, CALL_WEIGHT_FLOOR_SHARE) for q, rate in rates.items()})
    return out


def _call_rate(query: Mapping | None) -> float:
    try:
        rate = float((query or {}).get("calls_per_second") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return rate if rate > 0 else 0.0


def assignment_unit_weights(
    assignment: Mapping | None, query_by_id: Mapping[str, Mapping]
) -> dict[str, float]:
    """:func:`unit_call_weights` of an assignment artifact's placement units."""
    assignment = assignment or {}
    return unit_call_weights(
        assignment_placement_groups(assignment), assignment.get("write_groups") or {}, query_by_id
    )
