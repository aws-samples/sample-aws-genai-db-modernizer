# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Graph populators — read pipeline artifacts and write nodes/edges to the graph."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from types import SimpleNamespace

from pydantic import ValidationError

from src.contracts.assignment_models import AssignmentStatus
from src.contracts.move_ledger import Move, ReasonCode
from src.graph.inputs_digest import graph_inputs_digest
from src.graph.schema import initialize_schema, stamp_graph_meta
from src.graph.store import GraphStore
from src.shared import catalog
from src.shared.engine_names import display_engine
from src.shared.query_trace import assignment_lineage, candidate_keys_by_query, version_moves
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)


def _stamp_agent(
    store: GraphStore,
    agent_id: str,
    agent_name: str,
    phase: str,
    from_label: str,
    node_ids: list[str],
) -> None:
    """MERGE the producing Agent and link each node to it via PRODUCED_BY.

    from_label is the source node label (Decision, Signal, AntiPattern,
    AccessPattern, LoadTestRun, Risk) — a module constant, never user input.
    node_ids are the ids of the just-created nodes of that label. PRODUCED_BY
    is a single multi-pair rel table.
    """
    if not node_ids:
        return
    store.execute(
        "MERGE (a:Agent {id: $id}) SET a.name = $name, a.phase = $phase",
        {"id": agent_id, "name": agent_name, "phase": phase},
    )
    store.execute(  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query -- Cypher text from internal catalog names; values are bound parameters
        f"UNWIND $rows AS r MATCH (n:{from_label} {{id: r.nid}}), (a:Agent {{id: $aid}}) "
        "MERGE (n)-[:PRODUCED_BY]->(a)",
        {"rows": [{"nid": nid} for nid in node_ids], "aid": agent_id},
    )


# Why a SourceTable is a placeholder (#306): one of the collector's names for "no
# table" (``report_facts.PLACEHOLDER_TABLE_NAMES``, in any case, or an empty name).
PLACEHOLDER_REASON = (
    "a collector placeholder, not a table of the source database: no table resolved "
    "for the statement, or a dummy table such as DUAL"
)


def _placeholder_reason(table: str) -> str | None:
    from src.shared.report_facts import PLACEHOLDER_TABLE_NAMES

    folded = {name.casefold() for name in PLACEHOLDER_TABLE_NAMES}
    if table.strip() and table.strip().casefold() not in folded:
        return None
    return PLACEHOLDER_REASON


def populate_from_collector(collector_output: dict, store: GraphStore) -> None:
    """Create Query and SourceTable nodes, READS_FROM and WRITES edges from collector output.

    READS_FROM links a query to every table it accesses (``tables_accessed``): an
    UPDATE or DELETE reads the rows it changes and a locking read reads what it
    locks, so a query that reads a table only is one with READS_FROM and no WRITES.
    WRITES links it to every table it writes or locks, exactly as routing's
    one-writer rule reads it (``write_groups.written_tables``, ADR-031 Amendment 9,
    in the collection's source dialect), with ``locking`` on a table the statement
    locks by a locking read and does not write (an ``INSERT ... SELECT ... FOR
    UPDATE`` target is a write, the tables its read locks are locking). A
    SourceTable a collector placeholder names (``unknown``, a dummy table) is kept
    and flagged ``placeholder``, with its reason (#306); it gets WRITES whenever
    routing counts it as written.
    """
    from src.agents.referee.cache_overlay import is_locking_read, is_write_query
    from src.agents.referee.table_resolution import SourceScope
    from src.agents.referee.write_groups import written_tables
    from src.agents.schema_design.scope import normalize_table_name
    from src.tools.sql.query_facts import query_facts, source_engine_of

    patterns = collector_output["queries"]["query_patterns"]

    table_rows = [
        {"id": t, "reason": _placeholder_reason(t)}
        for t in sorted({t for p in patterns for t in p["tables_accessed"]})
    ]
    if table_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (st:SourceTable {id: r.id}) "
            "SET st.database = '', st.row_estimate = 0, "
            "st.placeholder = r.reason IS NOT NULL, st.placeholder_reason = r.reason",
            {"rows": table_rows},
        )

    query_rows = [
        {
            "id": p["query_id"],
            "sql": p["query_text"],
            "cps": p["calls_per_second"],
            "op": p["query_type"],
            # source.frequency_per_hour + the nested performance / characteristics
            # blocks, carried so the report's per-query drill-down (General /
            # Performance / Characteristics tabs) renders from the graph exactly as
            # it did from the journey JSON. performance/characteristics are stored
            # as JSON strings (display-only; nothing traverses into them).
            # Default to 0.0 (a float), never None: LadybugDB infers the UNWIND
            # struct column type from the values, and a None would type the column
            # STRING and clash with the DOUBLE schema column.
            "freq": float(p.get("frequency_per_hour") or 0.0),
            "perf": json.dumps(p.get("performance") or {}),
            "chars": json.dumps(p.get("characteristics") or {}),
        }
        for p in patterns
    ]
    if query_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (q:Query {id: r.id}) "
            "SET q.sql_text = r.sql, q.calls_per_second = r.cps, "
            "q.operation_type = r.op, q.in_scope = true, "
            "q.frequency_per_hour = r.freq, q.performance_json = r.perf, "
            "q.characteristics_json = r.chars",
            {"rows": query_rows},
        )

    edge_rows = [{"qid": p["query_id"], "tid": t} for p in patterns for t in p["tables_accessed"]]
    if edge_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (st:SourceTable {id: r.tid}) "
            "MERGE (q)-[:READS_FROM]->(st)",
            {"rows": edge_rows},
        )

    engine = source_engine_of(collector_output)
    scope = SourceScope.from_collector(collector_output)
    write_rows: list[dict] = []
    for p in patterns:
        written = written_tables(p, engine, scope)
        if not written:
            continue
        locks = is_locking_read(p, engine)
        targets = (
            set(query_facts(str(p.get("query_text") or ""), engine).write_targets)
            if locks and is_write_query(p, engine)
            else set()
        )
        write_rows.extend(
            {
                "qid": p["query_id"],
                "tid": t,
                "lock": locks and normalize_table_name(t) not in targets,
            }
            for t in written
        )
    if write_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (st:SourceTable {id: r.tid}) "
            "MERGE (q)-[w:WRITES]->(st) SET w.locking = r.lock",
            {"rows": write_rows},
        )


def populate_from_access_patterns(record: dict, store: GraphStore) -> int:
    """Set each Query's ``key_candidates_json`` from the effective version's
    access-pattern record (#1361): the columns the redesign considered as a key for
    it, accepted or refused and why. A record written before #1361 holds none.
    Returns the number of queries set."""
    rows = [
        {"id": qid, "keys": json.dumps(found)}
        for qid, found in sorted(candidate_keys_by_query(record).items())
    ]
    if rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.id}) SET q.key_candidates_json = r.keys",
            {"rows": rows},
        )
    return len(rows)


def populate_from_triage(triage_output: dict, store: GraphStore) -> None:
    """Create Signal nodes and EMITS_SIGNAL edges from triage output."""
    signals = triage_output.get("signals", [])
    total_queries = sum(s.get("query_count", 0) for s in signals) or 1

    schema_signals = {
        "json_columns",
        "junction_tables",
        "high_fk_density",
        "self_referential_fk",
        "eav_pattern",
    }

    signal_rows = [
        {
            "id": s["signal"],
            "cat": "schema_signal" if s["signal"] in schema_signals else "query_signal",
            "description": s["evidence"],
        }
        for s in signals
    ]
    if signal_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (s:Signal {id: r.id}) "
            "SET s.category = r.cat, s.description = r.description",
            {"rows": signal_rows},
        )
        _stamp_agent(
            store,
            "triage",
            "Triage",
            "TRIAGE",
            "Signal",
            [r["id"] for r in signal_rows],
        )

    edge_rows = [
        {"qid": qid, "sid": s["signal"], "strength": s.get("query_count", 1) / total_queries}
        for s in signals
        for qid in s.get("query_ids", [])
    ]
    if edge_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (s:Signal {id: r.sid}) "
            "MERGE (q)-[:EMITS_SIGNAL {strength: r.strength}]->(s)",
            {"rows": edge_rows},
        )


def populate_from_assignment(assignment: dict, store: GraphStore) -> None:
    """Create Destination, Engine, CoDependencyGroup nodes and edges from assignment.

    A query's Destination is its engine in the effective assignment (``artifact_type``
    ``engine``), one MIGRATES_TO per query: the design's real objects are
    TargetObject nodes, linked per version by TARGETS (#306,
    :func:`populate_targets`). The Destination used to be guessed from the query's
    first source table.
    """
    qas = assignment["query_assignments"]

    def _dest_id(qa: dict) -> str:
        return str(qa["assigned_engine"])

    engine_rows = [
        {"id": e, "name": display_engine(e)} for e in sorted({qa["assigned_engine"] for qa in qas})
    ]
    if engine_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (e:Engine {id: r.id}) SET e.display_name = r.name",
            {"rows": engine_rows},
        )

    dest_rows = [
        {
            "id": _dest_id(qa),
            "engine": qa["assigned_engine"],
            "name": display_engine(qa["assigned_engine"]),
        }
        for qa in qas
    ]
    if dest_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (d:Destination {id: r.id}) "
            "SET d.engine = r.engine, d.artifact_type = 'engine', d.artifact_name = r.name",
            {"rows": [dict(t) for t in {tuple(sorted(r.items())) for r in dest_rows}]},
        )
        store.execute(
            "UNWIND $rows AS r MATCH (d:Destination {id: r.id}), (e:Engine {id: r.engine}) "
            "MERGE (d)-[:HOSTED_ON]->(e)",
            {"rows": dest_rows},
        )

    migrate_rows = [
        {
            "qid": qa["query_id"],
            "did": _dest_id(qa),
            "conf": qa["confidence"],
            "pinned": qa.get("pinned") or "",
            "reason": qa["assignment_reason"],
        }
        for qa in qas
    ]
    if migrate_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (d:Destination {id: r.did}) "
            "MERGE (q)-[:MIGRATES_TO {confidence: r.conf, pinned: r.pinned, "
            "assignment_reason: r.reason}]->(d)",
            {"rows": migrate_rows},
        )

    group_rows: list[dict] = []
    member_rows: list[dict] = []
    for i, group in enumerate(assignment.get("co_dependency_groups", [])):
        # A group is either a dict ({group_id, query_ids, reason}) or a bare
        # list of query ids. Normalize both to an id, reason, and query ids.
        if isinstance(group, dict):
            gid = group.get("group_id", f"group-{i}")
            reason = group.get("reason", "")
            qids = group.get("query_ids", [])
        else:
            gid, reason, qids = f"group-{i}", "", group
        group_rows.append({"id": gid, "reason": reason})
        member_rows.extend({"qid": qid, "gid": gid} for qid in qids)

    if group_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (g:CoDependencyGroup {id: r.id}) SET g.reason = r.reason",
            {"rows": group_rows},
        )
    if member_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (g:CoDependencyGroup {id: r.gid}) "
            "MERGE (q)-[:MEMBER_OF]->(g)",
            {"rows": member_rows},
        )


def populate_from_moves(assignment: dict, store: GraphStore) -> int:
    """Write one MOVED edge (query -> engine) per Move of one assignment version (#339).

    ``assignment.moves`` holds only the Moves that produced that version
    (#616), so calling this for every version of the lineage keeps the whole
    history: an edge is keyed on (version, seq) and never overwritten. A Move
    of a query the collector did not record (no Query node) is skipped.
    Returns the number of Moves read (0 for a version written before the
    ledger).
    """
    version = int(assignment.get("version") or 0)
    moves: list[Move] = []
    for raw in assignment.get("moves") or []:
        try:
            moves.append(Move.model_validate(raw))
        except ValidationError:
            # One unreadable record must not cost the whole graph its history.
            logger.warning("graph: skipped an unreadable Move in assignment v%s", version)
    if not moves or version < 1:
        return 0
    engine_rows = [
        {"id": e, "name": display_engine(e)} for e in sorted({m.to_engine for m in moves})
    ]
    store.execute(
        "UNWIND $rows AS r MERGE (e:Engine {id: r.id}) ON CREATE SET e.display_name = r.name",
        {"rows": engine_rows},
    )
    rows = [
        {
            "qid": m.query_id,
            "eng": m.to_engine,
            "ver": version,
            "seq": m.seq,
            "pss": m.pass_.value,
            "code": m.reason_code.value,
            "actor": m.actor.value,
            "frm": m.from_engine or "",
            "before": m.score_before,
            "after": m.score_after,
            "ru_engine": m.runner_up.engine if m.runner_up else "",
            "ru_score": m.runner_up.score if m.runner_up else None,
            "grp": m.group_key or "",
            "tied": json.dumps(m.tied_engines),
            "evidence": m.evidence.model_dump_json(),
        }
        for m in moves
    ]
    # CAST: an all-null batch would otherwise type the score columns STRING.
    store.execute(
        "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (e:Engine {id: r.eng}) "
        "MERGE (q)-[m:MOVED {version: r.ver, seq: r.seq}]->(e) "
        "SET m.pass = r.pss, m.reason_code = r.code, m.actor = r.actor, "
        "m.from_engine = r.frm, m.score_before = CAST(r.before AS DOUBLE), "
        "m.score_after = CAST(r.after AS DOUBLE), m.runner_up_engine = r.ru_engine, "
        "m.runner_up_score = CAST(r.ru_score AS DOUBLE), m.group_key = r.grp, "
        "m.tied_engines_json = r.tied, m.evidence_json = r.evidence",
        {"rows": rows},
    )
    return len(moves)


def populate_from_analysis(analysis_output: dict, engine: str, store: GraphStore) -> None:
    """Create AntiPattern nodes and OBSERVED_IN_QUERY/TABLE edges from analysis output."""
    workload = analysis_output.get("workload_analysis", {})
    anti_patterns = workload.get("anti_patterns_detected") or []

    node_rows = [
        {
            "id": ap["anti_pattern_id"],
            "type": ap["anti_pattern_type"],
            "sev": ap.get("severity_weight", 0.5),
            "description": ap.get("description", ""),
            "rec": ap.get("recommendation", ""),
        }
        for ap in anti_patterns
    ]
    if node_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (ap:AntiPattern {id: r.id}) "
            "SET ap.anti_pattern_type = r.type, ap.severity_weight = r.sev, "
            "ap.description = r.description, ap.recommendation = r.rec",
            {"rows": node_rows},
        )
        _stamp_agent(
            store,
            f"analysis-{engine}",
            f"Analysis {engine}",
            "ANALYSIS",
            "AntiPattern",
            [r["id"] for r in node_rows],
        )

    q_edges = [
        {"apid": ap["anti_pattern_id"], "qid": qid}
        for ap in anti_patterns
        for qid in (ap.get("query_ids") or [])
    ]
    if q_edges:
        store.execute(
            "UNWIND $rows AS r MATCH (ap:AntiPattern {id: r.apid}), (q:Query {id: r.qid}) "
            "MERGE (ap)-[:OBSERVED_IN_QUERY]->(q)",
            {"rows": q_edges},
        )

    t_edges = [
        {"apid": ap["anti_pattern_id"], "tid": tid}
        for ap in anti_patterns
        for tid in (ap.get("table_ids") or [])
    ]
    if t_edges:
        store.execute(
            "UNWIND $rows AS r MATCH (ap:AntiPattern {id: r.apid}), (st:SourceTable {id: r.tid}) "
            "MERGE (ap)-[:OBSERVED_IN_TABLE]->(st)",
            {"rows": t_edges},
        )


# A version's kind from its recorded source (ADR-028 provenance).
_KIND_BY_SOURCE = {
    "assignment_resolution": "first_assignment",
    "reality_check": "reality_check",
    "customer_gate": "customer_pick",
    "post_schema_routing": "post_schema_reroute",
    "source_of_truth_reroute": "source_of_truth_reroute",
    "auto_decided": "auto_decided",
}


def _engines_and_scope(assignment: dict | None) -> tuple[dict, dict]:
    qas = [qa for qa in (assignment or {}).get("query_assignments") or [] if isinstance(qa, dict)]
    return (
        {qa.get("query_id"): qa.get("assigned_engine") for qa in qas},
        {qa.get("query_id"): qa.get("in_scope", True) for qa in qas},
    )


def _customer_edit_kind(assignment: dict, previous: dict | None) -> str:
    """``customer_scope`` for an edit that changed only which queries are in scope
    (no query changed engine), else ``customer_pick``."""
    if previous is None:
        return "customer_pick"
    engines, scope = _engines_and_scope(assignment)
    engines_before, scope_before = _engines_and_scope(previous)
    if engines == engines_before and scope != scope_before:
        return "customer_scope"
    return "customer_pick"


def _version_kind(assignment: dict, previous: dict | None) -> str:
    """The pass that made a version, from its status and recorded ``source``.

    A version written before ``source`` existed (contract 1.1) has no Moves either
    (1.10), so its kind comes from the legacy shapes ADR-028's own readers use:
    the old Reality Check writer left ``previous_version`` null and set
    ``reality_check_applied``; every override edit set an int ``previous_version``.
    """
    from src.storage.assignment_versioning import _is_override_edit, is_reality_check_produced

    if assignment.get("status") == AssignmentStatus.REROUTE_ROLLED_BACK.value:
        return "rollback"
    source = assignment.get("source")
    if source is not None:
        kind = _KIND_BY_SOURCE.get(source, "unknown")
    elif is_reality_check_produced(assignment):
        kind = "reality_check"
    elif _is_override_edit(assignment):
        kind = "customer_pick"
    else:
        kind = "first_assignment"
    if kind == "customer_pick":
        return _customer_edit_kind(assignment, previous)
    return kind


def _net_moves(moves: list[Move]) -> dict[str, tuple[str | None, str]]:
    """Each query's net engine change over one version's Moves, in ``seq`` order:
    the engine it started on and the engine it ended on."""
    net: dict[str, tuple[str | None, str]] = {}
    for m in sorted(moves, key=lambda m: m.seq):
        start = net[m.query_id][0] if m.query_id in net else m.from_engine
        net[m.query_id] = (start, m.to_engine)
    return net


def _merge_engines(store: GraphStore, engine_ids: set[str]) -> None:
    if engine_ids:
        store.execute(
            "UNWIND $rows AS r MERGE (e:Engine {id: r.id}) ON CREATE SET e.display_name = r.name",
            {"rows": [{"id": e, "name": display_engine(e)} for e in sorted(engine_ids)]},
        )


def _affects_engines(store: GraphStore, label: str, rows: list[dict]) -> None:
    """AFFECTS edges from ``label`` nodes (``r.src`` is the id) to engines, by role."""
    rows = [dict(t) for t in {tuple(sorted(r.items())) for r in rows}]
    if not rows:
        return
    _merge_engines(store, {r["eng"] for r in rows})
    store.execute(  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query -- Cypher text from internal label names; values are bound parameters
        f"UNWIND $rows AS r MATCH (n:{label} {{id: r.src}}), (e:Engine {{id: r.eng}}) "
        "MERGE (n)-[:AFFECTS {role: r.role}]->(e)",
        {"rows": rows},
    )


def populate_version_chain(lineage: list[dict], effective_version: int, store: GraphStore) -> None:
    """The assignment version chain (#306): one AssignmentVersion per version of the
    effective lineage, SUPERSEDES to the version before it, RESTORES from a
    rollback to the version it restates (the one the abandoned reroute
    ``rolled_back_version`` came from), and AFFECTS to the queries each version's
    Moves changed and the engines they left or landed on."""
    by_version = {int(a.get("version") or 0): a for a in lineage}
    node_rows: list[dict] = []
    query_rows: list[dict] = []
    engine_rows: list[dict] = []
    for version, assignment in sorted(by_version.items()):
        if version < 1:
            continue
        moves = version_moves(assignment)
        prev = assignment.get("previous_version")
        kind = _version_kind(assignment, by_version.get(prev) if isinstance(prev, int) else None)
        # Only a rollback names the version it rolled back; a later version that
        # copies the field forward (a source-of-truth reroute) does not.
        rolled = assignment.get("rolled_back_version") if kind == "rollback" else None
        node_rows.append(
            {
                "id": f"assignment-v{version}",
                "ver": version,
                "kind": kind,
                "source": assignment.get("source") or "",
                "status": assignment.get("status") or "",
                "eff": version == effective_version,
                "created": str(assignment.get("timestamp") or ""),
                "n": len(moves),
                "passes": json.dumps(sorted({m.pass_.value for m in moves})),
                "actors": json.dumps(sorted({m.actor.value for m in moves})),
                "prev": prev if isinstance(prev, int) else None,
                "rolled": rolled if isinstance(rolled, int) else None,
            }
        )
        vid = f"assignment-v{version}"
        # A query whose Moves changed its engine is 'moved'; one whose Moves only
        # cleared or changed a tie (no engine change) is 'tie' and moves no engine.
        roles: dict[str, str] = {}
        for m in moves:
            if m.from_engine == m.to_engine:
                roles.setdefault(m.query_id, "tie")
                continue
            roles[m.query_id] = "moved"
            engine_rows.append({"src": vid, "eng": m.to_engine, "role": "to"})
            if m.from_engine:
                engine_rows.append({"src": vid, "eng": m.from_engine, "role": "from"})
        query_rows.extend({"src": vid, "qid": q, "role": r} for q, r in roles.items())
    if not node_rows:
        return
    store.execute(
        "UNWIND $rows AS r MERGE (v:AssignmentVersion {id: r.id}) "
        "SET v.version = r.ver, v.kind = r.kind, v.source = r.source, v.status = r.status, "
        "v.effective = r.eff, v.created_at = r.created, v.move_count = r.n, "
        "v.passes_json = r.passes, v.actors_json = r.actors, "
        "v.previous_version = CAST(r.prev AS INT64), "
        "v.rolled_back_version = CAST(r.rolled AS INT64)",
        {"rows": node_rows},
    )
    chain_rows: list[dict] = []
    restore_rows: list[dict] = []
    for row in node_rows:
        if row["prev"] in by_version:
            chain_rows.append({"a": row["id"], "b": f"assignment-v{row['prev']}"})
        abandoned = by_version.get(row["rolled"]) if row["rolled"] else None
        restored = abandoned.get("previous_version") if abandoned else None
        if row["kind"] == "rollback" and restored in by_version:
            restore_rows.append({"a": row["id"], "b": f"assignment-v{restored}"})
    for rel, rows in (("SUPERSEDES", chain_rows), ("RESTORES", restore_rows)):
        if rows:
            store.execute(  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query -- Cypher text from internal rel names; values are bound parameters
                "UNWIND $rows AS r MATCH (a:AssignmentVersion {id: r.a}), "
                f"(b:AssignmentVersion {{id: r.b}}) MERGE (a)-[:{rel}]->(b)",
                {"rows": rows},
            )
    if query_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (v:AssignmentVersion {id: r.src}), (q:Query {id: r.qid}) "
            "MERGE (v)-[:AFFECTS {role: r.role}]->(q)",
            {"rows": query_rows},
        )
    _affects_engines(store, "AssignmentVersion", engine_rows)


def _recorded_dimensions(record: dict | None) -> tuple[dict, dict, dict]:
    """The score dimensions an access-pattern record holds, read through the record
    writer's own reader (``query_pattern.dimensions_of_record``), and each query's
    tables; all empty for a record written before them (schema version 3 or older)."""
    from src.agents.referee.query_pattern import dimensions_of_record

    recorded = dimensions_of_record(record)
    tables_of = {
        str(r.get("query_id")): list(r.get("tables") or [])
        for r in (record or {}).get("queries") or []
        if isinstance(r, dict)
    }
    return recorded["queries"], recorded["tables"], tables_of


def _rung_dimensions(
    qid: str, engine: str, score: float | None, by_query: dict, by_engine: dict, tables_of: dict
) -> str | None:
    """The recorded dimensions for ``qid`` on ``engine``, when they explain ``score``:
    the record's score (its ``fit``, else its ``base``) equals the edge's, the rule the
    detailed report's facts use (``report_facts.detail_round``). A reroute or a
    rollback carries the previous record's dimensions while its scores come from the
    router, so those edges get none rather than another score's breakdown."""
    from src.shared.report_facts import detail_round

    entry = (by_query.get(qid) or {}).get(engine)
    if not entry or score is None:
        return None
    if detail_round(entry.get("fit", entry.get("base"))) != detail_round(score):
        return None
    dims = dict(entry)
    rated = by_engine.get(engine) or {}
    common = rated.get("weights")
    tables = {}
    for table in tables_of.get(qid) or []:
        row = (rated.get("tables") or {}).get(table)
        if row:
            tables[table] = {**row, "weights": row.get("weights") or common}
    if tables:
        dims["tables"] = tables
    return json.dumps(dims, sort_keys=True)


def populate_scored(
    lineage: list[dict],
    effective_version: int,
    records: dict[int, dict | None],
    store: GraphStore,
) -> int:
    """SCORED edges (#306): each query's candidate ladder per assignment version.

    A query's ladder is the one its last Move with recorded candidates
    (``evidence.candidates``) holds, in that version or an earlier one: a customer
    pick, its group propagation, an auto pick or a source-of-truth reroute records
    none, so the query keeps its earlier ladder, and the edge says which version and
    Move it came from (``ladder_version``, ``move_seq``, ``pass``: that Move's own
    pass, as on its MOVED edge). A rollback's Moves copy the abandoned reroute's
    candidates: ``ladder_copied_from`` names the reroute version they came from.

    Edges are written for each version a query has a Move in, and for every query in
    the effective version (``current``). ``assigned`` is the version's own
    ``assigned_engine``, never read from the ladder. ``records`` maps a version to its
    access-pattern record; its score dimensions are attached when they explain the
    edge's score. One edge per (query, version, engine); the populator replaces every
    SCORED edge, so it can run again. Returns the edge count.
    """
    store.execute("MATCH ()-[s:SCORED]->() DELETE s")
    ladder: dict[str, tuple[int, Move, str, int | None]] = {}
    rows: list[dict] = []
    for assignment in lineage:
        version = int(assignment.get("version") or 0)
        if version < 1:
            continue
        rolled = assignment.get("rolled_back_version")
        rollback = assignment.get(
            "status"
        ) == AssignmentStatus.REROUTE_ROLLED_BACK.value and isinstance(rolled, int)
        moves = sorted(version_moves(assignment), key=lambda m: m.seq)
        for m in moves:
            if not m.evidence.candidates:
                continue
            copied = (
                rolled
                if rollback and m.reason_code is ReasonCode.ROLLBACK_REDESIGN_FAILED
                else None
            )
            ladder[m.query_id] = (version, m, m.pass_.value, copied)
        assigned: dict[str, str | None] = {
            str(qa.get("query_id")): qa.get("assigned_engine")
            for qa in assignment.get("query_assignments") or []
            if isinstance(qa, dict) and qa.get("query_id")
        }
        current = version == effective_version
        wanted = {m.query_id for m in moves}
        if current:
            wanted |= set(assigned)
        by_query, by_engine, tables_of = _recorded_dimensions(records.get(version))
        for qid in sorted(wanted):
            if qid not in ladder:
                continue
            ladder_version, m, pass_, copied = ladder[qid]
            seen: set[str] = set()
            for rung in m.evidence.candidates:
                if rung.engine in seen:
                    continue
                seen.add(rung.engine)
                rows.append(
                    {
                        "qid": qid,
                        "eng": rung.engine,
                        "ver": version,
                        "score": rung.score,
                        "gate": rung.gate,
                        "assigned": assigned.get(qid) == rung.engine,
                        "current": current,
                        "lver": ladder_version,
                        "seq": m.seq,
                        "pss": pass_,
                        "copied": copied,
                        "dims": _rung_dimensions(
                            qid, rung.engine, rung.score, by_query, by_engine, tables_of
                        ),
                    }
                )
    if not rows:
        return 0
    _merge_engines(store, {r["eng"] for r in rows})
    # CREATE after the DELETE above: one edge per row, and twice as fast as MERGE on a
    # large job. CAST: an all-null batch would otherwise type a column STRING.
    store.execute(
        "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (e:Engine {id: r.eng}) "
        "CREATE (q)-[:SCORED {version: r.ver, score: CAST(r.score AS DOUBLE), "
        "gate: r.gate, assigned: r.assigned, current: r.current, "
        "ladder_version: r.lver, move_seq: r.seq, pass: r.pss, "
        "ladder_copied_from: CAST(r.copied AS INT64), dimensions_json: r.dims}]->(e)",
        {"rows": rows},
    )
    return len(rows)


# The engines whose design is a set of relational tables (Aurora).
AURORA_TARGET_ENGINES = catalog.engine_ids(status=catalog.ACTIVE, role="relational")
# The labelled fallbacks of a query with no design object (#306): never a guess.
NOT_DESIGNED = ("not_designed", "not designed yet")
NOT_IN_DESIGN = ("not_in_design", "no target in the design")
OUT_OF_SCOPE = ("out_of_scope", "the design does not serve it")


def _names(rows: list | None, field: str) -> set[str]:
    return {str(r[field]) for r in rows or [] if isinstance(r, dict) and r.get(field)}


def _defined_objects(engine: str, design: dict) -> set[tuple[str, str, str | None]]:
    """The objects ``design`` defines, as ``(kind, name, parent)``: DynamoDB
    ``table_definitions`` and each table's ``gsis``, DocumentDB ``collections``,
    OpenSearch ``index_designs`` and ``data_stream_designs``, ElastiCache
    ``key_designs``, Aurora ``table_definitions``. An access pattern naming anything
    else names no object (#306)."""
    tables = [t for t in design.get("table_definitions") or [] if isinstance(t, dict)]
    if engine == "dynamodb":
        defined: set[tuple[str, str, str | None]] = {
            ("table", n, None) for n in _names(tables, "table_name")
        }
        for t in tables:
            for gsi in _names(t.get("gsis"), "gsi_name"):
                defined.add(("gsi", gsi, str(t.get("table_name"))))
        return defined
    if engine == "documentdb":
        return {
            ("collection", n, None) for n in _names(design.get("collections"), "collection_name")
        }
    if engine == "opensearch":
        return {
            ("index", n, None)
            for n in _names(design.get("index_designs"), "index_name")
            | _names(design.get("data_stream_designs"), "data_stream_name")
        }
    if engine == "elasticache":
        return {("key_pattern", n, None) for n in _names(design.get("key_designs"), "key_pattern")}
    return {("table", n, None) for n in _names(tables, "table_name")}


def _pattern_query_ids(ap: dict) -> list[str]:
    return [str(q) for q in ap.get("query_ids") or ap.get("source_query_ids") or []]


def _design_objects(
    engine: str, design: dict, schema_version: int
) -> tuple[dict[str, dict], set[str]]:
    """``({query_id: {object_id: (object row, [pattern ids])}}, not served)`` from one
    schema output: each access pattern's target object under the engine's own
    contract field (DynamoDB ``table_name`` or, with ``gsi_name``, that GSI on it;
    DocumentDB ``collection_name``; OpenSearch ``index_or_stream``; ElastiCache
    ``key_pattern``; Aurora, the ``table_definitions`` its ``source_tables`` name,
    matched as the Aurora derivation matches them), only when the design defines that
    object. An access pattern marked ``in_scope: false`` serves none of its queries
    (``schema_design.scope.covered_query_ids``' rule), and an ``unsupported_patterns``
    entry records a query the design does not serve: those queries are returned as
    not served, unless another pattern serves them."""
    from src.agents.schema_design.scope import normalize_table_name

    defined = _defined_objects(engine, design)
    aurora_tables = {normalize_table_name(n): n for kind, n, _ in defined if kind == "table"}
    by_query: dict[str, dict] = {}
    not_served: set[str] = set()
    for up in design.get("unsupported_patterns") or []:
        if isinstance(up, dict):
            not_served.update(_pattern_query_ids(up))
    for ap in design.get("access_patterns") or []:
        if not isinstance(ap, dict):
            continue
        if ap.get("in_scope") is False:
            not_served.update(_pattern_query_ids(ap))
            continue
        named: list[tuple[str, str, str | None]] = []
        if engine == "dynamodb" and ap.get("table_name"):
            if ap.get("gsi_name"):
                named.append(("gsi", str(ap["gsi_name"]), str(ap["table_name"])))
            else:
                named.append(("table", str(ap["table_name"]), None))
        elif engine == "documentdb" and ap.get("collection_name"):
            named.append(("collection", str(ap["collection_name"]), None))
        elif engine == "opensearch" and ap.get("index_or_stream"):
            named.append(("index", str(ap["index_or_stream"]), None))
        elif engine == "elasticache" and ap.get("key_pattern"):
            named.append(("key_pattern", str(ap["key_pattern"]), None))
        elif engine in AURORA_TARGET_ENGINES:
            for source in ap.get("source_tables") or []:
                name = aurora_tables.get(normalize_table_name(str(source)))
                if name:
                    named.append(("table", name, None))
        for kind, name, parent in named:
            if (kind, name, parent) not in defined:
                continue
            full = f"{parent}.{name}" if parent else name
            oid = f"{engine}:{kind}:{full}@v{schema_version}"
            row = {
                "id": oid,
                "engine": engine,
                "kind": kind,
                "name": name,
                "parent": parent,
                "sv": schema_version,
                "fallback": False,
            }
            for qid in _pattern_query_ids(ap):
                entry = by_query.setdefault(qid, {}).setdefault(oid, (row, []))
                if ap.get("pattern_id") and ap["pattern_id"] not in entry[1]:
                    entry[1].append(ap["pattern_id"])
    return by_query, not_served - set(by_query)


def populate_targets(
    lineage: list[dict],
    effective_version: int,
    designs: dict[tuple[str, int], dict],
    store: GraphStore,
) -> int:
    """TARGETS edges (#306): each query of each lineage version to its design's target
    objects for the engines it uses in that version (its ``assigned_engine`` and
    ``cache_engine``), from that version's schema output (``designs[(engine,
    version)]``, ``schema-<engine>/v<N>`` designed from assignment v<N>, ADR-028).
    A query can target several objects in one version. An engine with no design at
    that version gives the ``not designed yet`` fallback; a design with no access
    pattern for the query gives ``no target in the design``; a design that marks the
    query's patterns out of scope or unsupported gives ``the design does not serve
    it`` (``out_of_scope``). The effective version's
    edges are ``effective``. Returns the edge count."""
    active = catalog.engine_ids(status=catalog.ACTIVE)
    objects: dict[str, dict] = {}
    rows: list[dict] = []
    parsed: dict[tuple[str, int], tuple[dict, set[str]]] = {}
    for assignment in lineage:
        version = int(assignment.get("version") or 0)
        if version < 1:
            continue
        for qa in assignment.get("query_assignments") or []:
            if not isinstance(qa, dict) or not qa.get("query_id"):
                continue
            qid = str(qa["query_id"])
            used = {str(e) for e in (qa.get("assigned_engine"), qa.get("cache_engine")) if e}
            for engine in sorted(used & active):
                key = (engine, version)
                if key not in parsed:
                    design = designs.get(key)
                    parsed[key] = (
                        _design_objects(engine, design, version) if design else ({}, set())
                    )
                served, not_served = parsed[key]
                found = served.get(qid)
                if not found:
                    if not designs.get(key):
                        kind, label = NOT_DESIGNED
                    elif qid in not_served:
                        kind, label = OUT_OF_SCOPE
                    else:
                        kind, label = NOT_IN_DESIGN
                    oid = f"{engine}:{kind}"
                    row = {
                        "id": oid,
                        "engine": engine,
                        "kind": kind,
                        "name": label,
                        "parent": None,
                        "sv": None,
                        "fallback": True,
                    }
                    found = {oid: (row, [])}
                for oid, (row, pattern_ids) in found.items():
                    objects[oid] = row
                    rows.append(
                        {
                            "qid": qid,
                            "oid": oid,
                            "ver": version,
                            "sv": row["sv"],
                            "eff": version == effective_version,
                            "ids": json.dumps(pattern_ids),
                        }
                    )
    if not rows:
        return 0
    _merge_engines(store, {o["engine"] for o in objects.values()})
    store.execute(
        "UNWIND $rows AS r MERGE (o:TargetObject {id: r.id}) "
        "SET o.engine = r.engine, o.kind = r.kind, o.name = r.name, o.parent = r.parent, "
        "o.schema_version = CAST(r.sv AS INT64), o.fallback = r.fallback",
        {"rows": list(objects.values())},
    )
    store.execute(
        "UNWIND $rows AS r MATCH (o:TargetObject {id: r.id}), (e:Engine {id: r.engine}) "
        "MERGE (o)-[:HOSTED_ON]->(e)",
        {"rows": list(objects.values())},
    )
    store.execute("MATCH ()-[t:TARGETS]->() DELETE t")
    store.execute(
        "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (o:TargetObject {id: r.oid}) "
        "CREATE (q)-[:TARGETS {version: r.ver, schema_version: CAST(r.sv AS INT64), "
        "effective: r.eff, pattern_ids_json: r.ids}]->(o)",
        {"rows": rows},
    )
    return len(rows)


def populate_overlays(
    overlays_by_version: Mapping[int, object],
    effective_version: int,
    store: GraphStore,
    reported: set[int] | None = None,
) -> None:
    """Waves, move units, sources of truth, the cache layer and engine cost (#306),
    from each lineage version's explorer overlays (``explorer_overlays.build_overlays``,
    the code the graph explorer reads them with), so the graph and the explorer
    cannot disagree. Every node and edge carries its assignment version; the
    effective version's are ``effective``. Engine cost is the effective report's."""
    from src.graph.explorer_overlays import Overlays, query_path

    waves: list[dict] = []
    includes: list[dict] = []
    wave_edges: dict[str, list[dict]] = {"MOVES_IN": [], "CACHED_IN": [], "CUTS_OVER_IN": []}
    units: dict[str, dict] = {}
    members: list[dict] = []
    sources: list[dict] = []
    replicas: list[dict] = []
    cache: list[dict] = []
    engines: set[str] = set()
    for version, ov in sorted(overlays_by_version.items()):
        if not isinstance(ov, Overlays):
            continue
        eff = version == effective_version
        migrating = {w["wave"]: bool(w.get("migrating")) for w in ov.waves}
        wid = {}
        for w in ov.plan:
            wid[w["wave"]] = f"v{version}:wave-{w['wave']}"
            waves.append(
                {
                    "id": wid[w["wave"]],
                    "ver": version,
                    "eff": eff,
                    "wave": w["wave"],
                    "title": w.get("title"),
                    "rationale": w.get("rationale"),
                    "gate": w.get("gate"),
                    "engines": json.dumps(w["engines"]),
                    "migrating": migrating.get(w["wave"]),
                    "qc": w.get("query_count"),
                    "cutover": bool(ov.cutover and ov.cutover["wave"] == w["wave"]),
                    "groups": json.dumps(w.get("table_groups") or []),
                }
            )
            includes.extend({"w": wid[w["wave"]], "e": e} for e in w["engines"])
            engines.update(w["engines"])
        for qid, wave in ov.query_move_wave.items():
            if wave in wid:
                wave_edges["MOVES_IN"].append({"q": qid, "w": wid[wave], "eff": eff})
            for step in query_path(ov, qid):
                if step["kind"] == "cutover" and step["wave"] in wid:
                    wave_edges["CUTS_OVER_IN"].append(
                        {"q": qid, "w": wid[step["wave"]], "eff": eff}
                    )
        for qid, wave in ov.query_cached_in.items():
            if wave in wid:
                wave_edges["CACHED_IN"].append({"q": qid, "w": wid[wave], "eff": eff})
        for qid, (unit, size) in ov.query_units.items():
            uid = f"v{version}:{unit}"
            units[uid] = {"id": uid, "ver": version, "eff": eff, "unit": unit, "size": size}
            members.append({"q": qid, "u": uid})
        for table, ts in ov.table_sources.items():
            if ts.get("source_of_truth"):
                sources.append({"t": table, "e": ts["source_of_truth"], "ver": version, "eff": eff})
                engines.add(str(ts["source_of_truth"]))
            for engine, reads in (ts.get("replica_reads_by_engine") or {}).items():
                replicas.extend(
                    {"q": q, "t": table, "e": engine, "ver": version, "eff": eff} for q in reads
                )
        for qid, c in ov.query_cache.items():
            cache.append({"q": qid, "e": c["engine"], "ver": version, "eff": eff, **c})
            engines.add(c["engine"])
    _merge_engines(store, engines)
    if waves:
        store.execute(
            "UNWIND $rows AS r MERGE (w:Wave {id: r.id}) SET w.version = r.ver, "
            "w.effective = r.eff, w.wave = r.wave, w.title = r.title, w.rationale = r.rationale, "
            "w.gate = r.gate, w.engines_json = r.engines, w.migrating = r.migrating, "
            "w.query_count = CAST(r.qc AS INT64), w.cutover = r.cutover, "
            "w.table_groups_json = r.groups",
            {"rows": waves},
        )
    if includes:
        store.execute(
            "UNWIND $rows AS r MATCH (w:Wave {id: r.w}), (e:Engine {id: r.e}) "
            "MERGE (w)-[:INCLUDES]->(e)",
            {"rows": includes},
        )
    for rel, rows in wave_edges.items():
        if rows:
            store.execute(  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query -- Cypher text from internal rel names; values are bound parameters
                f"UNWIND $rows AS r MATCH (q:Query {{id: r.q}}), (w:Wave {{id: r.w}}) "
                f"MERGE (q)-[x:{rel}]->(w) SET x.effective = r.eff",
                {"rows": rows},
            )
    if units:
        store.execute(
            "UNWIND $rows AS r MERGE (u:MoveUnit {id: r.id}) SET u.version = r.ver, "
            "u.effective = r.eff, u.unit = r.unit, u.size = r.size",
            {"rows": list(units.values())},
        )
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.q}), (u:MoveUnit {id: r.u}) "
            "MERGE (q)-[:MEMBER_OF]->(u)",
            {"rows": members},
        )
    if sources:
        store.execute(
            "UNWIND $rows AS r MATCH (st:SourceTable {id: r.t}), (e:Engine {id: r.e}) "
            "MERGE (st)-[s:SOURCE_OF_TRUTH {version: r.ver}]->(e) SET s.effective = r.eff",
            {"rows": sources},
        )
    if replicas:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.q}), (st:SourceTable {id: r.t}) "
            "MERGE (q)-[x:READS_REPLICA {version: r.ver, engine: r.e}]->(st) "
            "SET x.effective = r.eff",
            {"rows": replicas},
        )
    if cache:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.q}), (e:Engine {id: r.e}) "
            "MERGE (q)-[c:CACHED_BY {version: r.ver}]->(e) SET c.effective = r.eff, "
            "c.pattern = r.pattern, c.reason = r.reason, c.dropped = r.dropped",
            {"rows": cache},
        )
    if overlays_by_version:
        store.execute(
            "UNWIND $rows AS r MATCH (v:AssignmentVersion {version: r.v}) "
            "SET v.has_report = r.has",
            {
                "rows": [
                    {"v": v, "has": v in (reported or set())} for v in sorted(overlays_by_version)
                ]
            },
        )
    effective = overlays_by_version.get(effective_version)
    if not isinstance(effective, Overlays):
        return
    # The effective report's engine facts, stamped with the version that report was
    # built from (it can be older than the effective version, before a re-synthesis).
    facts = effective.engine_facts
    if facts:
        _merge_engines(store, set(facts))
        store.execute(
            "UNWIND $rows AS r MATCH (e:Engine {id: r.e}) "
            "SET e.monthly_cost_usd = CAST(r.c AS DOUBLE), e.cost_version = CAST(r.v AS INT64), "
            "e.role = r.role, e.rationale = r.rationale, "
            "e.workload_percent = CAST(r.share AS DOUBLE)",
            {
                "rows": [
                    {
                        "e": e,
                        "c": effective.engine_costs.get(e),
                        "v": effective.report_version,
                        "role": f.get("role"),
                        "rationale": f.get("rationale"),
                        "share": (
                            f["workload_percent"]
                            if isinstance(f.get("workload_percent"), (int, float))
                            else None
                        ),
                    }
                    for e, f in facts.items()
                ]
            },
        )
    # Each query's access-pattern class and the reason routing gave, from the
    # effective version's access-pattern record.
    if effective.query_classes:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.q}) "
            "SET q.access_pattern_class = r.c, q.access_pattern_reason = r.why",
            {
                "rows": [
                    {"q": q, "c": c, "why": why} for q, (c, why) in effective.query_classes.items()
                ]
            },
        )


def link_consolidations(reality_check_output: dict, lineage: list[dict], store: GraphStore) -> None:
    """Link each ``consolidation-{i}`` Decision to its queries (INFORMED_BY) through the
    move ledger, and to the engines it moves from and to (AFFECTS) (#306).

    A consolidation's queries are the queries whose net engine change over the
    Moves of the version the reality check wrote (``output_assignment_version``;
    any reality-check version of the lineage when it is not recorded) goes from
    its ``from_engine`` to its ``to_engine``: the same per-query start-to-end
    count as the record's ``query_count`` (``reality_check_request.moved_queries``).
    A query consolidated A to B and then flagged on to C in the same version
    counts under A to C, not A to B.
    """
    written = reality_check_output.get("output_assignment_version")
    versions = [a for a in lineage if written is not None and a.get("version") == written]
    if not versions:
        versions = [a for a in lineage if a.get("source") == "reality_check"]
    net: dict[str, tuple[str | None, str]] = {}
    for a in versions:
        net.update(_net_moves(version_moves(a)))
    informed: list[dict] = []
    engines: list[dict] = []
    for i, cons in enumerate(reality_check_output.get("consolidations") or []):
        did = f"consolidation-{i}"
        frm, to = cons.get("from_engine"), cons.get("to_engine")
        informed.extend({"did": did, "qid": q} for q, pair in net.items() if pair == (frm, to))
        if frm:
            engines.append({"src": did, "eng": frm, "role": "from"})
        if to:
            engines.append({"src": did, "eng": to, "role": "to"})
    informed = [dict(t) for t in {tuple(sorted(r.items())) for r in informed}]
    if informed:
        store.execute(
            "UNWIND $rows AS r MATCH (d:Decision {id: r.did}), (q:Query {id: r.qid}) "
            "MERGE (d)-[:INFORMED_BY]->(q)",
            {"rows": informed},
        )
    _affects_engines(store, "Decision", engines)


def populate_from_reality_check(reality_check_output: dict, store: GraphStore) -> None:
    """Create Decision nodes (consolidation) from reality check output."""
    consolidations = reality_check_output.get("consolidations", [])

    rows: list[dict] = []
    for i, cons in enumerate(consolidations):
        metadata = {
            "from_engine": cons["from_engine"],
            "to_engine": cons["to_engine"],
            "saved_cost_estimate": cons.get("saved_cost_estimate", 0),
            "action": cons.get("action", "full"),
        }
        rows.append(
            {
                "id": f"consolidation-{i}",
                "description": (
                    f"Consolidated {cons['query_count']} queries "
                    f"from {cons['from_engine']} to {cons['to_engine']}"
                ),
                "rationale": cons["reason"],
                "meta": json.dumps(metadata),
            }
        )
    if rows:
        store.execute(
            "UNWIND $rows AS r MERGE (d:Decision {id: r.id}) "
            "SET d.category = 'consolidation', d.description = r.description, "
            "d.rationale = r.rationale, d.phase = 'REALITY_CHECK', d.metadata = r.meta",
            {"rows": rows},
        )
        _stamp_agent(
            store,
            "reality-check",
            "Reality Check",
            "REALITY_CHECK",
            "Decision",
            [r["id"] for r in rows],
        )


def populate_from_schema_design(
    schema_output: dict, engine: str, schema_version: int, store: GraphStore
) -> None:
    """Create Decision (trade_off) and AccessPattern nodes from schema design output."""
    trade_offs = schema_output.get("trade_offs", [])

    decision_rows: list[dict] = []
    informed_rows: list[dict] = []
    for i, trade_off in enumerate(trade_offs):
        if not isinstance(trade_off, dict):
            continue
        did = f"trade_off-{engine}-{i}"
        decision_rows.append(
            {
                "id": did,
                "description": trade_off.get("description", ""),
                "impact": trade_off.get("impact", ""),
                "meta": json.dumps({"engine": engine}),
            }
        )
        informed_rows.extend({"did": did, "qid": qid} for qid in trade_off.get("query_ids", []))

    if decision_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (d:Decision {id: r.id}) "
            "SET d.category = 'trade_off', d.description = r.description, "
            "d.rationale = r.impact, d.phase = 'SCHEMA_DESIGN', d.metadata = r.meta",
            {"rows": decision_rows},
        )
        _stamp_agent(
            store,
            f"schema-{engine}",
            f"Schema {engine}",
            "SCHEMA_DESIGN",
            "Decision",
            [r["id"] for r in decision_rows],
        )
    if informed_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (d:Decision {id: r.did}), (q:Query {id: r.qid}) "
            "MERGE (d)-[:INFORMED_BY]->(q)",
            {"rows": informed_rows},
        )

    ap_rows: list[dict] = []
    part_of_rows: list[dict] = []
    for ap in schema_output.get("access_patterns", []):
        pattern_id = ap.get("pattern_id")
        if not pattern_id:
            continue
        ap_rows.append(
            {
                "id": pattern_id,
                "engine": engine,
                "ver": schema_version,
                "description": ap.get("description", ""),
                "pattern_group": ap.get("pattern_group", ""),
                "op": ap.get("operation", ""),
                "rps": float(ap.get("design_rps", 0) or 0),
                "in_scope": ap.get("in_scope", True),
            }
        )
        # DynamoDB/OpenSearch use query_ids; DocumentDB/ElastiCache use source_query_ids.
        qids = ap.get("query_ids") or ap.get("source_query_ids") or []
        part_of_rows.extend({"qid": qid, "pid": pattern_id} for qid in qids)

    if ap_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (ap:AccessPattern {id: r.id}) "
            "SET ap.engine = r.engine, ap.schema_version = r.ver, "
            "ap.description = r.description, ap.pattern_group = r.pattern_group, "
            "ap.operation = r.op, ap.design_rps = r.rps, ap.in_scope = r.in_scope",
            {"rows": ap_rows},
        )
        _stamp_agent(
            store,
            f"schema-{engine}",
            f"Schema {engine}",
            "SCHEMA_DESIGN",
            "AccessPattern",
            [r["id"] for r in ap_rows],
        )
    if part_of_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (ap:AccessPattern {id: r.pid}) "
            "MERGE (q)-[:PART_OF]->(ap)",
            {"rows": part_of_rows},
        )


def populate_from_post_schema_router(router_output: dict, store: GraphStore) -> None:
    """Create Decision nodes (reroute) from post-schema router output."""
    routings = router_output.get("routings", [])

    decision_rows: list[dict] = []
    informed_rows: list[dict] = []
    engine_rows: list[dict] = []
    for i, routing in enumerate(routings):
        did = f"reroute-{i}"
        metadata = {
            "from_engine": routing["from_engine"],
            "to_engine": routing.get("to_engine"),
            "cascade_depth": routing.get("cascade_depth", 0),
        }
        decision_rows.append(
            {
                "id": did,
                "description": (
                    f"Rerouted {routing['query_id']} from {routing['from_engine']} "
                    f"to {routing.get('to_engine', 'application-layer')}"
                ),
                "rationale": routing["reason"],
                "meta": json.dumps(metadata),
            }
        )
        informed_rows.append({"did": did, "qid": routing["query_id"]})
        # The engines the reroute moves the query from and to (#306); none for a
        # query rerouted to the application layer.
        engine_rows.append({"src": did, "eng": routing["from_engine"], "role": "from"})
        if routing.get("to_engine"):
            engine_rows.append({"src": did, "eng": routing["to_engine"], "role": "to"})

    if decision_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (d:Decision {id: r.id}) "
            "SET d.category = 'reroute', d.description = r.description, "
            "d.rationale = r.rationale, d.phase = 'POST_SCHEMA_ROUTER', d.metadata = r.meta",
            {"rows": decision_rows},
        )
        _stamp_agent(
            store,
            "post-schema-router",
            "Post-Schema Router",
            "POST_SCHEMA_ROUTER",
            "Decision",
            [r["id"] for r in decision_rows],
        )
    if informed_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (d:Decision {id: r.did}), (q:Query {id: r.qid}) "
            "MERGE (d)-[:INFORMED_BY]->(q)",
            {"rows": informed_rows},
        )
    _affects_engines(store, "Decision", engine_rows)


_LATENCY_PERCENTILES = ("p50", "p90", "p95", "p99", "p999", "min", "max")


def _flatten_latency(latency: dict, prefix: str) -> dict[str, float]:
    """Expand a percentile dict into {prefix}_{p50..max} params.

    LadybugDB reformats strings that look like maps, so latency objects can't be
    stored as JSON. Flattening to DOUBLE columns keeps the values native and
    queryable. Missing percentiles default to 0.0.
    """
    latency = latency or {}
    return {f"{prefix}_{p}": float(latency.get(p, 0) or 0) for p in _LATENCY_PERCENTILES}


def populate_from_load_test(
    load_test_output: dict, engine: str, schema_version: int, store: GraphStore
) -> None:
    """Create LoadTestRun nodes and TESTED_IN/VALIDATES edges from load test output."""
    pattern_results = load_test_output.get("pattern_results", [])

    run_rows: list[dict] = []
    edge_rows: list[dict] = []
    for result in pattern_results:
        run_id = f"lt-{engine}-v{schema_version}-{result['query_id']}"
        row = {
            "id": run_id,
            "qid": result["query_id"],
            "engine": engine,
            "ver": schema_version,
            "imp": result.get("improvement_factor", 0),
            "thr": result.get("throughput_rps", 0),
            "err": result.get("error_rate_pct", 0),
            "cost": result.get("cost_per_operation_usd", 0),
        }
        row.update(_flatten_latency(result.get("source_latency_ms", {}), "source"))
        row.update(_flatten_latency(result.get("target_latency_ms", {}), "target"))
        run_rows.append(row)
        edge_rows.append({"qid": result["query_id"], "ltid": run_id})

    if run_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (lt:LoadTestRun {id: r.id}) "
            "SET lt.timestamp = '', lt.query_id = r.qid, "
            "lt.engine = r.engine, lt.schema_version = r.ver, "
            "lt.source_p50 = r.source_p50, lt.source_p90 = r.source_p90, "
            "lt.source_p95 = r.source_p95, lt.source_p99 = r.source_p99, "
            "lt.source_p999 = r.source_p999, lt.source_min = r.source_min, "
            "lt.source_max = r.source_max, "
            "lt.target_p50 = r.target_p50, lt.target_p90 = r.target_p90, "
            "lt.target_p95 = r.target_p95, lt.target_p99 = r.target_p99, "
            "lt.target_p999 = r.target_p999, lt.target_min = r.target_min, "
            "lt.target_max = r.target_max, "
            "lt.improvement_factor = r.imp, lt.throughput_rps = r.thr, "
            "lt.error_rate_pct = r.err, lt.cost_per_operation_usd = r.cost",
            {"rows": run_rows},
        )
        _stamp_agent(
            store,
            f"load-test-{engine}",
            f"Load Test {engine}",
            "LOAD_TEST",
            "LoadTestRun",
            [r["id"] for r in run_rows],
        )
    if edge_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (q:Query {id: r.qid}), (lt:LoadTestRun {id: r.ltid}) "
            "MERGE (q)-[:TESTED_IN]->(lt)",
            {"rows": edge_rows},
        )
        # VALIDATES: the run validates its own engine's Destination (#306), not the
        # query's assigned engine: a cache load test validates the cache layer.
        store.execute(
            "MERGE (e:Engine {id: $engine}) ON CREATE SET e.display_name = $name",
            {"engine": engine, "name": display_engine(engine)},
        )
        store.execute(
            "MERGE (d:Destination {id: $engine}) "
            "ON CREATE SET d.engine = $engine, d.artifact_type = 'engine', "
            "d.artifact_name = $name",
            {"engine": engine, "name": display_engine(engine)},
        )
        store.execute(
            "MATCH (d:Destination {id: $engine}), (e:Engine {id: $engine}) "
            "MERGE (d)-[:HOSTED_ON]->(e)",
            {"engine": engine},
        )
        store.execute(
            "UNWIND $rows AS r MATCH (lt:LoadTestRun {id: r.ltid}), "
            "(d:Destination {id: $engine}) MERGE (lt)-[:VALIDATES]->(d)",
            {"rows": edge_rows, "engine": engine},
        )


def _resolved_risk_ids(resolved: list, taken: set[str]) -> list[str]:
    """Ids for the resolved risks, which the report gives none: ``RESOLVED-001`` on,
    in report order, skipping any id an active risk already has."""
    ids: list[str] = []
    n = 0
    for _ in resolved:
        n += 1
        while f"RESOLVED-{n:03d}" in taken:
            n += 1
        ids.append(f"RESOLVED-{n:03d}")
    return ids


def populate_from_synthesis(synthesis_output: dict, store: GraphStore) -> None:
    """Create Risk nodes and IMPACTS/EVIDENCED_BY edges from synthesis output.

    Both the open risks (``risk_assessment.risks``, ``status = 'active'``) and the
    risks the assignment resolved (``risk_assessment.resolved_risks``, ``status =
    'resolved'``, #306): a resolved risk records the engine whose analysis raised
    it, the engine it is resolved on and why. EVIDENCED_BY links a risk to each
    query in its ``query_ids`` that has a Query node; the others are counted and
    logged, not created.
    """
    risk_assessment = synthesis_output.get("risk_assessment") or {}
    risks = [r for r in (risk_assessment.get("risks") or []) if isinstance(r, dict)]
    resolved = [r for r in (risk_assessment.get("resolved_risks") or []) if isinstance(r, dict)]

    active_ids = [risk.get("risk_id") or f"risk-{i}" for i, risk in enumerate(risks)]
    resolved_ids = _resolved_risk_ids(resolved, set(active_ids))

    node_rows: list[dict] = []
    impact_rows: list[dict] = []
    evidence: dict[tuple[str, str], None] = {}
    for risk_id, risk, status in [
        *((rid, r, "active") for rid, r in zip(active_ids, risks, strict=True)),
        *((rid, r, "resolved") for rid, r in zip(resolved_ids, resolved, strict=True)),
    ]:
        wave = risk.get("wave")
        node_rows.append(
            {
                "id": risk_id,
                "status": status,
                "type": risk.get("risk_type") or "",
                "sev": risk.get("severity") or "",
                "description": risk.get("description") or "",
                "mitigation": risk.get("mitigation") or "",
                "rule": risk.get("rule"),
                "wave": wave if isinstance(wave, int) and not isinstance(wave, bool) else None,
                "engine": risk.get("engine"),
                "resolved_on": risk.get("resolved_on"),
                "reason": risk.get("reason"),
            }
        )
        impact_rows.extend(
            {"rid": risk_id, "tid": tid} for tid in (risk.get("affected_tables") or [])
        )
        for qid in risk.get("query_ids") or []:
            evidence[(risk_id, str(qid))] = None

    if node_rows:
        store.execute(
            "UNWIND $rows AS r MERGE (rk:Risk {id: r.id}) "
            "SET rk.status = r.status, rk.risk_type = r.type, rk.severity = r.sev, "
            "rk.description = r.description, rk.mitigation = r.mitigation, "
            "rk.rule = r.rule, rk.wave = CAST(r.wave AS INT64), rk.engine = r.engine, "
            "rk.resolved_on = r.resolved_on, rk.reason = r.reason",
            {"rows": node_rows},
        )
        _stamp_agent(
            store,
            "referee-synthesis",
            "Referee Synthesis",
            "SYNTHESIS",
            "Risk",
            [r["id"] for r in node_rows],
        )
    if impact_rows:
        store.execute(
            "UNWIND $rows AS r MATCH (rk:Risk {id: r.rid}), (st:SourceTable {id: r.tid}) "
            "MERGE (rk)-[:IMPACTS]->(st)",
            {"rows": impact_rows},
        )
    if evidence:
        known = {r["id"] for r in store.query("MATCH (q:Query) RETURN q.id AS id")}
        evidence_rows = [{"rid": rid, "qid": qid} for rid, qid in evidence if qid in known]
        skipped = len(evidence) - len(evidence_rows)
        if skipped:
            logger.info("graph: skipped %d risk query id(s) with no Query node", skipped)
        if evidence_rows:
            store.execute(
                "UNWIND $rows AS r MATCH (rk:Risk {id: r.rid}), (q:Query {id: r.qid}) "
                "MERGE (rk)-[:EVIDENCED_BY]->(q)",
                {"rows": evidence_rows},
            )


def rebuild_graph(
    db_name: str,
    job_id: str,
    artifact_store: ArtifactStore,
    graph_store: GraphStore,
) -> dict:
    """Full rebuild: clear graph, initialize schema, read all artifacts, populate in order.

    Skips any artifact that doesn't exist yet (partial pipeline runs).
    Returns stats: {"nodes_created": int, "edges_created": int, "duration_ms": int}
    """
    start = time.time()
    graph_store.clear()
    initialize_schema(graph_store)

    prefix = f"{db_name}/{job_id}"

    # The digest of the inputs, taken before any is read: an artifact rewritten
    # during the build leaves the graph behind the digest, so the next read
    # rebuilds it (#847).
    try:
        inputs_digest: str | None = graph_inputs_digest(artifact_store, db_name, job_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable store: build unstamped
        logger.warning("graph rebuild could not digest the inputs of %s: %s", prefix, exc)
        inputs_digest = None

    # Artifacts that exist but could not be read (a throttled or failing
    # store). A missing artifact is a partial run, not a failure.
    failed_reads: list[str] = []

    def _read_safe(path: str) -> dict | None:
        try:
            return artifact_store.read_json(path)
        except ValueError as exc:  # malformed JSON: retrying would not help
            logger.warning("graph rebuild skipped malformed %s: %s", path, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            if not _is_missing(artifact_store, path, exc):
                logger.warning("graph rebuild could not read %s: %s", path, exc)
                failed_reads.append(path)
            return None

    # 1. Collector
    collector = _read_safe(f"{prefix}/collector/output.json")
    if collector:
        populate_from_collector(collector, graph_store)

    # 2. Triage
    triage = _read_safe(f"{prefix}/referee-triage/triage.json")
    if triage:
        populate_from_triage(triage, graph_store)

    # Every active engine in the rule catalog (#306): analysis, schema design and
    # load test read the same list, so a new engine is never skipped.
    engines = sorted(catalog.engine_ids(status=catalog.ACTIVE))

    # 3. Analysis (multiple engines)
    for engine in engines:
        analysis = _read_safe(f"{prefix}/analysis-{engine}/analysis.json")
        if analysis:
            populate_from_analysis(analysis, engine, graph_store)

    # 4. Assignment (effective version — ADR-028 shared resolver)
    from src.storage.assignment_versioning import (
        assignment_artifact_path,
        reality_check_output_candidates,
        resolve_downstream_assignment_version,
        resolve_effective_assignment_version,
    )

    assignment_version = resolve_effective_assignment_version(artifact_store, db_name, job_id)
    lineage: list[dict] = []
    if assignment_version > 0:
        lineage = assignment_lineage(
            lambda v: _read_safe(assignment_artifact_path(db_name, job_id, v)),
            assignment_version,
        )
        assignment = _read_safe(assignment_artifact_path(db_name, job_id, assignment_version))
        if assignment:
            # A version saved before #1373 kept a consolidated query's score on
            # the engine it left and has no ``pinned``: the journey reads what its
            # Moves set.
            from src.agents.referee.move_ledger import follow_moves

            follow_moves(assignment, lineage)
            populate_from_assignment(assignment, graph_store)
        # 4b. The move ledger of every version in the effective lineage (#339).
        for version in lineage:
            populate_from_moves(version, graph_store)
        # 4d. The version chain of the effective lineage (#306).
        populate_version_chain(lineage, assignment_version, graph_store)
        # 4e. Each query's candidate ladder per version, with the score dimensions
        # each version's access-pattern record holds (#306, #1471).
        populate_scored(
            lineage,
            assignment_version,
            {
                int(a["version"]): _read_safe(
                    f"{prefix}/assignment/v{int(a['version'])}/access_patterns.json"
                )
                for a in lineage
            },
            graph_store,
        )
        # 4c. The candidate keys of each query, from the effective version's
        # access-pattern record (#1361). An older job has none: a missing file is a
        # partial run, like any artifact.
        access_patterns = _read_safe(
            f"{prefix}/assignment/v{assignment_version}/access_patterns.json"
        )
        if access_patterns:
            populate_from_access_patterns(access_patterns, graph_store)
        # 4f. Each query's design target objects per lineage version (#306).
        populate_targets(
            lineage,
            assignment_version,
            {
                (engine, int(a["version"])): design
                for a in lineage
                for engine in engines
                if (
                    design := _read_safe(
                        f"{prefix}/schema-{engine}/v{int(a['version'])}/schema_output.json"
                    )
                )
            },
            graph_store,
        )

    # 5. Reality check
    # The run the effective assignment descends from (#193). The lineage is read
    # through _read_safe, so a failing read is recorded like every other here.
    safe_store = SimpleNamespace(
        list_prefix=artifact_store.list_prefix,
        exists=artifact_store.exists,
        read_json=lambda path: _read_safe(path) or {},
    )
    reality = next(
        (
            r
            for key in reality_check_output_candidates(
                safe_store, db_name, job_id, assignment_version
            )
            if (r := _read_safe(key))
        ),
        None,
    )
    if reality:
        populate_from_reality_check(reality, graph_store)
        link_consolidations(reality, lineage, graph_store)

    # 6. Schema design: every engine, at the effective assignment
    # version (#306). ``schema-<engine>/v<N>`` is designed from assignment v<N>
    # (ADR-028), and a rollback restates the designs of the version it restores on
    # its own version, so an older or abandoned version's design is never read.
    # With no assignment yet, schema design writes v1 (ADR-026).
    design_version = resolve_downstream_assignment_version(artifact_store, db_name, job_id)
    for engine in engines:
        schema = _read_safe(f"{prefix}/schema-{engine}/v{design_version}/schema_output.json")
        if schema:
            populate_from_schema_design(schema, engine, design_version, graph_store)

    # 7. Post-schema router
    router = _read_safe(f"{prefix}/post-schema-router/router_output.json")
    if router:
        populate_from_post_schema_router(router, graph_store)

    # 8. Load test: every engine, on the design it tested (the same version).
    for engine in engines:
        lt = _read_safe(f"{prefix}/load-test-{engine}/v{design_version}/results/summary.json")
        if lt:
            populate_from_load_test(lt, engine, design_version, graph_store)

    # 9. Synthesis
    # The effective version's report first, then the legacy keys (#1520).
    from src.storage.assignment_versioning import synthesis_report_candidates

    synthesis = None
    for synthesis_key in synthesis_report_candidates(artifact_store, db_name, job_id):
        synthesis = _read_safe(synthesis_key)
        if synthesis:
            populate_from_synthesis(synthesis, graph_store)
            break

    # 9b. Waves, move units, sources of truth, the cache layer and engine cost, per
    # lineage version (#306), by the explorer's overlay code on the inputs
    # ``explorer_overlays.read_overlays`` reads: the version's assignment and
    # access-pattern record, and its report (the effective version's is the one
    # read above, legacy keys included; an older version's only its own
    # ``synthesis/v<N>/report.json``).
    from src.graph.explorer_overlays import build_overlays

    overlays_by_version = {}
    reported: set[int] = set()
    for version_doc in lineage:
        v = int(version_doc.get("version") or 0)
        if v < 1:
            continue
        report = (
            synthesis
            if v == assignment_version
            else _read_safe(f"{prefix}/synthesis/v{v}/report.json")
        )
        if report:
            reported.add(v)
        overlays_by_version[v] = build_overlays(
            version_doc,
            report,
            _read_safe(f"{prefix}/assignment/v{v}/access_patterns.json"),
        )
    populate_overlays(
        overlays_by_version,
        assignment_version,
        graph_store,
        reported=reported,
    )

    # 10. The schema revision this graph was built with (a saved graph with
    # another revision is rebuilt before it is read) and its assignment version.
    # Only a complete build is stamped (#819): a graph missing an artifact that
    # exists must not count as current, so the next read rebuilds it.
    if not failed_reads and inputs_digest is not None:
        stamp_graph_meta(graph_store, assignment_version, inputs_digest)

    duration_ms = int((time.time() - start) * 1000)
    node_count = graph_store.query("MATCH (n) RETURN COUNT(n) AS c")[0]["c"]
    edge_count = graph_store.query("MATCH ()-[r]->() RETURN COUNT(r) AS c")[0]["c"]

    return {
        "nodes_created": node_count,
        "edges_created": edge_count,
        "duration_ms": duration_ms,
        "complete": not failed_reads and inputs_digest is not None,
        "failed_reads": len(failed_reads) + (inputs_digest is None),
    }


# A read error that means "this artifact does not exist": the local store's
# FileNotFoundError, a mapping's KeyError, an object store's NoSuchKey or 404.
_MISSING_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


def _is_missing(artifact_store: ArtifactStore, path: str, exc: Exception) -> bool:
    """True when ``exc`` reading ``path`` means the artifact is absent, not unreadable."""
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return True
    code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
    if code in _MISSING_CODES:
        return True
    try:
        return not artifact_store.exists(path)
    except Exception:  # noqa: BLE001 - the store is failing: treat it as unreadable
        return False
