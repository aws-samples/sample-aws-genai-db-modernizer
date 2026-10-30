# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property: a table's write group moves as one (#992, ADR-031 Amendment 9).

Whatever the statements and their scores, the first assignment puts every
write and locking read of a table on one engine, never on an engine that is
not a system of record, and that engine is the best mean over the group's
members (or an exact tie the tie-break settled). Reads stay per query.
"""

from __future__ import annotations

from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.referee import assignment_resolver
from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.query_scoring import EngineScore
from src.agents.referee.write_groups import build_write_groups, placement_groups
from src.contracts.move_ledger import ReasonCode

ENGINES = ("aurora_postgresql", "dynamodb", "documentdb", "opensearch")
TABLES = ("t1", "t2", "t3")
SHAPES = {
    "insert": "INSERT INTO {t} (id, a) VALUES (?, ?)",
    "update_key": "UPDATE {t} SET a = ? WHERE id = ?",
    "update_filter": "UPDATE {t} SET a = ? WHERE a > ?",
    "delete": "DELETE FROM {t} WHERE id = ?",
    "lock": "SELECT * FROM {t} WHERE id = ? FOR UPDATE",
    "read_key": "SELECT * FROM {t} WHERE id = ?",
    "read_agg": "SELECT a, COUNT(*) FROM {t} GROUP BY a",
}
WRITES = {"insert", "update_key", "update_filter", "delete", "lock"}


@st.composite
def workloads(draw: st.DrawFn) -> tuple[list[dict], dict[tuple[str, str], float]]:
    n = draw(st.integers(min_value=1, max_value=12))
    queries = []
    scores: dict[tuple[str, str], float] = {}
    for i in range(n):
        shape = draw(st.sampled_from(sorted(SHAPES)))
        table = draw(st.sampled_from(TABLES))
        qid = f"q{i:02d}"
        queries.append(
            {
                "query_id": qid,
                "query_text": SHAPES[shape].format(t=table),
                "query_type": SHAPES[shape].split()[0],
                "tables_accessed": [f"db.{table}"],
                "calls_per_second": 1.0,
            }
        )
        for engine in ENGINES:
            scores[(qid, engine)] = float(draw(st.integers(min_value=0, max_value=100)))
    return queries, scores


def _resolve(queries: list[dict], scores: dict[tuple[str, str], float]):
    def fake(query, engine, ctx):
        return EngineScore(scores[(query["query_id"], engine)])

    tables = sorted({t for q in queries for t in q["tables_accessed"]})
    with mock.patch.object(assignment_resolver, "score_query_for_engine", fake):
        return AssignmentResolver().resolve(
            {"selected_agents": [{"agent_type": e} for e in ENGINES], "signals": []},
            {
                e: {
                    "table_recommendations": [
                        {"table_id": t, "confidence_score": 50} for t in tables
                    ],
                    "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
                }
                for e in ENGINES
            },
            {
                "job_id": "p",
                "metadata": {"source_database": {"engine": "sqlserver"}},
                "database_schema": {"tables": [{"table_id": t, "table_name": t} for t in tables]},
                "queries": {"query_patterns": queries},
            },
        )


@settings(deadline=None, max_examples=150)
@given(workloads())
def test_every_tables_writes_land_on_one_owning_engine(workload) -> None:
    queries, scores = workload
    result = _resolve(queries, scores)
    engine = {qa.query_id: qa.assigned_engine for qa in result.query_assignments}
    groups = build_write_groups(queries, "sqlserver")
    assert result.write_groups == groups
    for table, members in groups.items():
        assert len({engine[q] for q in members}) == 1, (table, members, engine)
        assert engine[members[0]] != "opensearch"  # not a system of record


@settings(deadline=None, max_examples=150)
@given(workloads())
def test_the_group_engine_is_the_best_mean_and_reads_stay_per_query(workload) -> None:
    queries, scores = workload
    result = _resolve(queries, scores)
    engine = {qa.query_id: qa.assigned_engine for qa in result.query_assignments}
    tied = {qa.query_id: qa.tied_engines for qa in result.query_assignments}
    units = placement_groups([], build_write_groups(queries, "sqlserver"))
    grouped = {q for unit in units for q in unit}
    owners = [e for e in ENGINES if e != "opensearch"]
    for unit in units:
        means = {e: sum(scores[(q, e)] for q in unit) / len(unit) for e in owners}
        best = max(means.values())
        assert means[engine[unit[0]]] == best, (unit, means, engine[unit[0]])
        moves = [m for m in result.moves if m.query_id in unit]
        if sum(1 for v in means.values() if v == best) == 1:
            assert {m.reason_code for m in moves} == {ReasonCode.WRITE_GROUP}
            assert not tied[unit[0]]
    by_id = {q["query_id"]: q for q in queries}
    for qid, q in by_id.items():
        if qid in grouped:
            continue
        candidates = (
            owners if q["query_type"] != "SELECT" or "FOR UPDATE" in q["query_text"] else ENGINES
        )
        best = max(scores[(qid, e)] for e in candidates)
        assert scores[(qid, engine[qid])] == best, (qid, engine[qid])


JOIN_SHAPES = {
    "join_read": "SELECT * FROM {t} a JOIN {u} b ON b.a_id = a.id JOIN {u} c ON c.id = b.id",
    "join_update": "UPDATE a SET a.x = ? FROM {t} a JOIN {u} b ON b.a_id = a.id",
}


@st.composite
def join_workloads(draw: st.DrawFn) -> tuple[list[dict], dict[tuple[str, str], float]]:
    """Single-table statements plus multi-table JOIN reads and writes, whose
    significant joins build co-dependency groups that share writes with write groups."""
    queries, scores = draw(workloads())
    for i in range(draw(st.integers(min_value=1, max_value=5))):
        shape = draw(st.sampled_from(sorted(JOIN_SHAPES)))
        t, u = draw(st.permutations(TABLES))[:2]
        qid = f"j{i:02d}"
        queries.append(
            {
                "query_id": qid,
                "query_text": JOIN_SHAPES[shape].format(t=t, u=u),
                "query_type": JOIN_SHAPES[shape].split()[0],
                "tables_accessed": [f"db.{t}", f"db.{u}"],
                "has_joins": True,
                "join_count": 2,
                "filter_tables": [],
                "calls_per_second": 1.0,
            }
        )
        for engine in ENGINES:
            scores[(qid, engine)] = float(draw(st.integers(min_value=0, max_value=100)))
    return queries, scores


@settings(deadline=None, max_examples=150)
@given(join_workloads())
def test_join_groups_and_write_groups_place_as_one_unit(workload) -> None:
    """A JOIN co-dependency group that shares a write with a table's write group
    places as one unit with it (ADR-031 Amendment 9): every unit, and so every
    table's writes and every JOIN group, sits on one engine, the unit's best mean."""
    from src.agents.referee.co_dependency import build_co_dependency_groups

    queries, scores = workload
    result = _resolve(queries, scores)
    engine = {qa.query_id: qa.assigned_engine for qa in result.query_assignments}
    write_groups = build_write_groups(queries, "sqlserver")
    co_groups = build_co_dependency_groups(queries, [])
    for table, members in write_groups.items():
        assert len({engine[q] for q in members}) == 1, (table, members)
    for group in co_groups:
        assert len({engine[q] for q in group}) == 1, group
    owners = [e for e in ENGINES if e != "opensearch"]
    for unit in placement_groups(co_groups, write_groups):
        assert len({engine[q] for q in unit}) == 1, unit
        candidates = (
            owners
            if any(
                build_write_groups([q for q in queries if q["query_id"] == m], "sqlserver")
                for m in unit
            )
            else list(ENGINES)
        )
        means = {e: sum(scores[(q, e)] for q in unit) / len(unit) for e in candidates}
        assert means[engine[unit[0]]] == max(means.values()), (unit, means)


@settings(deadline=None, max_examples=500)
@given(
    st.lists(st.tuples(st.integers(0, 100), st.integers(0, 100)), min_size=2, max_size=12),
    st.floats(min_value=1e-7, max_value=1e4, allow_nan=False, allow_infinity=False),
)
def test_equal_call_rates_keep_the_plain_mean_and_its_exact_ties(pairs, rate) -> None:
    """Under equal call rates a write group's weighted mean is the plain mean,
    exactly, so an exact plain-mean tie stays a tie (and goes through tie_break)
    whatever the rate (#992 round 4: float noise used to break 1 in 5)."""
    from src.agents.referee.co_dependency import group_engine_scores
    from src.agents.referee.write_groups import unit_call_weights

    members = [f"q{i}" for i in range(len(pairs))]
    by_id = {q: {"calls_per_second": rate} for q in members}
    weights = unit_call_weights([members], {"db.t": members}, by_id)
    assert set(weights.values()) == {1.0}
    score = {(q, "a"): float(a) for q, (a, _) in zip(members, pairs, strict=True)}
    score.update({(q, "b"): float(b) for q, (_, b) in zip(members, pairs, strict=True)})
    plain = group_engine_scores(members, ["a", "b"], lambda q, e: score[(q, e)])
    weighted = group_engine_scores(members, ["a", "b"], lambda q, e: score[(q, e)], weights)
    assert weighted == plain
    if sum(a for a, _ in pairs) == sum(b for _, b in pairs):
        assert weighted["a"] == weighted["b"]
