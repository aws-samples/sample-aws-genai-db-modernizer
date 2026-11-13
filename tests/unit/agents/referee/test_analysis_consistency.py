# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The cross-engine consistency guard (#1994): detect, settle with the single scorer,
reconcile the notes."""

from __future__ import annotations

import copy
from dataclasses import dataclass

import pytest

from src.agents.referee import analysis_consistency as ac
from src.shared.catalog.patterns import patterns


def _analysis(*anti_patterns: tuple[str, list[str]]) -> dict:
    return {
        "workload_analysis": {
            "patterns_detected": [],
            "anti_patterns_detected": [
                {
                    "anti_pattern_id": f"ap-{i}",
                    "anti_pattern_type": kind,
                    "severity_weight": 0.5,
                    "query_ids": ids,
                    "description": kind,
                }
                for i, (kind, ids) in enumerate(anti_patterns)
            ],
        }
    }


# Aurora PostgreSQL points q1 and q2 at DynamoDB (no-relational-need); DynamoDB points q1
# and q3 back at the relational engines (ordered-scan).
ANALYSES = {
    "aurora_postgresql": _analysis(("no-relational-need", ["q1", "q2"])),
    "dynamodb": _analysis(("ordered-scan", ["q1", "q3"])),
}


def test_claims_come_from_the_catalog_better_served_by() -> None:
    claims = ac.analysis_claims(ANALYSES)
    assert ac.Claim("aurora_postgresql", "q1", "dynamodb", "no-relational-need") in claims
    assert ac.Claim("dynamodb", "q1", "aurora_postgresql", "ordered-scan") in claims
    # An anti-pattern whose entry names no other engine makes no claim.
    assert ac.analysis_claims({"dynamodb": _analysis(("complex-aggregation", ["q9"]))}) == []


def test_detects_only_queries_two_analyses_point_at_each_other() -> None:
    found = ac.find_contradictions(ANALYSES)
    assert [(c.query_id, c.engines) for c in found] == [("q1", ("aurora_postgresql", "dynamodb"))]


def test_one_way_claims_are_not_contradictions() -> None:
    # Aurora points q1 at DynamoDB; DynamoDB says nothing about q1.
    one_way = {
        "aurora_mysql": _analysis(("high-frequency-pk-lookup", ["q1"])),
        "dynamodb": _analysis(("ordered-scan", ["q2"])),
    }
    assert ac.find_contradictions(one_way) == []
    # Pointing at a third engine is no contradiction either.
    third = {
        "aurora_mysql": _analysis(("simple-cache-read", ["q1"])),
        "dynamodb": _analysis(("ordered-scan", ["q1"])),
    }
    assert ac.find_contradictions(third) == []
    assert ac.find_contradictions({}) == []


@dataclass
class _Score:
    score: float | None


def _scorer(monkeypatch: pytest.MonkeyPatch, scores: dict[str, float | None]) -> list[tuple]:
    calls: list[tuple] = []

    def fake(query, engine, ctx, *, exact=False, explain=False, apply_gates=True):
        calls.append((query["query_id"], engine, exact))
        return _Score(scores[engine])

    import src.agents.referee.query_scoring as qs

    monkeypatch.setattr(qs, "score_query_for_engine", fake)
    return calls


QUERIES = {"q1": {"query_id": "q1"}}


def test_the_single_scorer_settles_the_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scorer(monkeypatch, {"aurora_postgresql": 71.5, "dynamodb": 40.0})
    (settled,) = ac.settle(ac.find_contradictions(ANALYSES), QUERIES, object())
    assert settled.winner == "aurora_postgresql"
    assert settled.rule == ac.TIE_RULE_SCORE
    assert settled.scores == {"aurora_postgresql": 71.5, "dynamodb": 40.0}
    assert {(q, e) for q, e, _ in calls} == {("q1", "aurora_postgresql"), ("q1", "dynamodb")}
    assert all(exact for _, _, exact in calls)


def test_a_gated_engine_loses(monkeypatch: pytest.MonkeyPatch) -> None:
    _scorer(monkeypatch, {"aurora_postgresql": None, "dynamodb": 10.0})
    (settled,) = ac.settle(ac.find_contradictions(ANALYSES), QUERIES, object())
    assert settled.winner == "dynamodb"


def test_an_exact_tie_goes_through_break_tie(monkeypatch: pytest.MonkeyPatch) -> None:
    _scorer(monkeypatch, {"aurora_postgresql": 50.0, "dynamodb": 50.0})
    seen: list = []
    import src.agents.referee.tie_break as tb

    def fake_break_tie(tied, current, source, counts=None, query_id=""):
        seen.append((tied, set(current), source, dict(counts or {}), query_id))
        return "dynamodb", "tie_unresolved"

    monkeypatch.setattr(tb, "break_tie", fake_break_tie)
    (settled,) = ac.settle(
        ac.find_contradictions(ANALYSES),
        QUERIES,
        object(),
        current_engines={"dynamodb", "aurora_postgresql"},
        settled={"dynamodb": 3},
        source_engine="postgresql",
    )
    assert settled.winner == "dynamodb" and settled.rule == "tie_unresolved"
    assert seen == [
        (
            ["aurora_postgresql", "dynamodb"],
            {"dynamodb", "aurora_postgresql"},
            "aurora_postgresql",
            {"dynamodb": 3},
            "q1",
        )
    ]


def test_an_unknown_query_stays_unsettled(monkeypatch: pytest.MonkeyPatch) -> None:
    _scorer(monkeypatch, {"aurora_postgresql": 1.0, "dynamodb": 2.0})
    (settled,) = ac.settle(ac.find_contradictions(ANALYSES), {}, object())
    assert settled.winner is None
    # Nothing is reconciled for it.
    assert ac.reconcile(ANALYSES, [settled]) == ANALYSES


def test_reconcile_drops_only_the_winners_contradicting_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scorer(monkeypatch, {"aurora_postgresql": 70.0, "dynamodb": 40.0})
    before = copy.deepcopy(ANALYSES)
    settled = ac.settle(ac.find_contradictions(ANALYSES), QUERIES, object())
    out = ac.reconcile(ANALYSES, settled)
    # Aurora PostgreSQL won: its note pointing q1 at DynamoDB goes, q2 stays.
    (nrn,) = out["aurora_postgresql"]["workload_analysis"]["anti_patterns_detected"]
    assert nrn["query_ids"] == ["q2"]
    # DynamoDB's note pointing q1 at the relational engine is the one statement kept.
    (scan,) = out["dynamodb"]["workload_analysis"]["anti_patterns_detected"]
    assert scan["query_ids"] == ["q1", "q3"]
    # The input is not changed.
    assert ANALYSES == before


def test_reconcile_removes_an_entry_left_with_no_query(monkeypatch: pytest.MonkeyPatch) -> None:
    _scorer(monkeypatch, {"aurora_postgresql": 10.0, "dynamodb": 90.0})
    analyses = {
        "aurora_postgresql": _analysis(("no-relational-need", ["q1"])),
        "dynamodb": _analysis(("ordered-scan", ["q1"])),
    }
    out = ac.reconcile(analyses, ac.settle(ac.find_contradictions(analyses), QUERIES, object()))
    assert out["dynamodb"]["workload_analysis"]["anti_patterns_detected"] == []
    assert out["aurora_postgresql"] == analyses["aurora_postgresql"]


def test_check_without_contradictions_returns_the_analyses_unchanged() -> None:
    analyses = {"aurora_mysql": _analysis(("high-frequency-pk-lookup", ["q1"]))}
    out, found = ac.check_analysis_consistency(analyses, {}, {}, None)
    assert found == [] and out == analyses


def test_every_engine_hint_names_its_better_served_by_engines() -> None:
    """The catalog field the guard reads matches the entry's own advice (#1994)."""
    from src.agents.referee.synthesis_grounding import engine_mentions
    from src.shared.catalog.engines import engines

    known = {e.id for e in engines()}
    for entry in patterns():
        for engine, support in entry.engines.items():
            if support.kind != "anti_pattern":
                continue
            where = f"{entry.id}.toml [engines.{engine}]"
            assert engine not in support.better_served_by, where
            assert set(support.better_served_by) <= known, where
            named = {
                e for e, _, _ in engine_mentions(f"{support.engine_hint or ''} {support.guidance}")
            }
            if support.guidance_names_engine:
                assert support.better_served_by, where
            for target in support.better_served_by:
                relational = target.startswith("aurora_") and "relational" in support.guidance
                assert target in named or relational, f"{where}: {target} is not in its text"


# ---------------------------------------------------------------------------
# Wired into synthesis: load_synthesis_data runs the guard before any builder
# ---------------------------------------------------------------------------

DB, JOB = "shop", "job-1994"
TABLE = "public.t1"
FIRST_ROWS = {
    "query_id": "q1",
    "query_type": "SELECT",
    "query_text": "SELECT * FROM t1 ORDER BY id LIMIT $1",
    "tables_accessed": [TABLE],
    "calls_per_second": 2.0,
    "rows_returned_avg": 5.0,
}


def _engine_analysis(kind: str) -> dict:
    return {
        "table_recommendations": [
            {
                "table_id": TABLE,
                "confidence_score": 60,
                "score_breakdown": {
                    "pattern_match_score": 60,
                    "complexity_score": 60,
                    "performance_score": 60,
                    "cost_score": 60,
                },
            }
        ],
        "workload_analysis": {
            "patterns_detected": [],
            "anti_patterns_detected": [
                {
                    "anti_pattern_id": "x",
                    "anti_pattern_type": kind,
                    "severity_weight": 0.5,
                    "query_ids": ["q1"],
                    "table_ids": [TABLE],
                }
            ],
        },
    }


def _seed(store, assigned_engine: str = "aurora_postgresql") -> None:
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {
            "selected_agents": [{"agent_type": "aurora_postgresql"}, {"agent_type": "dynamodb"}],
            "query_capabilities": {},
        },
    )
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": "postgresql"}},
            "database_schema": {
                "tables": [
                    {
                        "table_id": TABLE,
                        "primary_key": ["id"],
                        "columns": [{"column_name": "id", "data_type": "integer"}],
                    }
                ]
            },
            "queries": {"query_patterns": [FIRST_ROWS]},
        },
    )
    store.write_json(
        f"{DB}/{JOB}/analysis-aurora_postgresql/analysis.json",
        _engine_analysis("no-relational-need"),
    )
    store.write_json(
        f"{DB}/{JOB}/analysis-dynamodb/analysis.json", _engine_analysis("ordered-scan")
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "version": 1,
            "query_assignments": [
                {
                    "query_id": "q1",
                    "assigned_engine": assigned_engine,
                    "in_scope": True,
                    "source_tables": [TABLE],
                }
            ],
        },
    )


def test_synthesis_settles_and_reconciles_before_any_builder(tmp_path) -> None:
    from src.storage.local_store import LocalArtifactStore
    from tests.design_records import load_synthesis_data

    store = LocalArtifactStore(base_dir=str(tmp_path))
    _seed(store)
    data = load_synthesis_data(store, JOB, DB, assignment_version=1)
    (found,) = data.analysis_contradictions
    assert found.query_id == "q1" and found.engines == ("aurora_postgresql", "dynamodb")
    # Routing settled it: the outcome is the query's assigned engine.
    assert found.rule == ac.RULE_ASSIGNED and found.winner == "aurora_postgresql"
    # Aurora PostgreSQL's note sending q1 to DynamoDB is gone from what synthesis reads.
    aurora = data.engines["aurora_postgresql"].analysis
    assert aurora["workload_analysis"]["anti_patterns_detected"] == []
    # It routes nothing: the assignment synthesis reads is the one routing wrote.
    (qa,) = data.assignment["query_assignments"]
    assert qa["assigned_engine"] == "aurora_postgresql" and not data.assignment.get("moves")
    # The stored analysis is never rewritten.
    stored = store.read_json(f"{DB}/{JOB}/analysis-aurora_postgresql/analysis.json")
    assert stored["workload_analysis"]["anti_patterns_detected"][0]["query_ids"] == ["q1"]


def test_synthesis_without_a_contradiction_keeps_every_note(tmp_path) -> None:
    from src.storage.local_store import LocalArtifactStore
    from tests.design_records import load_synthesis_data

    store = LocalArtifactStore(base_dir=str(tmp_path))
    _seed(store)
    store.write_json(
        f"{DB}/{JOB}/analysis-dynamodb/analysis.json", _engine_analysis("complex-aggregation")
    )
    data = load_synthesis_data(store, JOB, DB, assignment_version=1)
    assert data.analysis_contradictions == []
    aurora = data.engines["aurora_postgresql"].analysis
    assert aurora["workload_analysis"]["anti_patterns_detected"][0]["query_ids"] == ["q1"]


def _guarded(tmp_path, assigned_engine: str):
    from src.storage.local_store import LocalArtifactStore
    from tests.design_records import load_synthesis_data

    store = LocalArtifactStore(base_dir=str(tmp_path))
    _seed(store, assigned_engine)
    triage = store.read_json(f"{DB}/{JOB}/referee-triage/triage.json")
    triage["selected_agents"].append({"agent_type": "documentdb"})
    store.write_json(f"{DB}/{JOB}/referee-triage/triage.json", triage)
    store.write_json(
        f"{DB}/{JOB}/analysis-documentdb/analysis.json", _engine_analysis("complex-aggregation")
    )
    return load_synthesis_data(store, JOB, DB, assignment_version=1)


def test_the_outcome_is_the_assigned_engine_not_a_fresh_score(tmp_path) -> None:
    """#2040 review: routing put q1 on DynamoDB, although the pair's scores favour Aurora
    PostgreSQL; the guard follows routing and keeps DynamoDB's own note out."""
    data = _guarded(tmp_path, "dynamodb")
    (found,) = data.analysis_contradictions
    assert (found.winner, found.rule) == ("dynamodb", ac.RULE_ASSIGNED)
    assert data.engines["dynamodb"].analysis["workload_analysis"]["anti_patterns_detected"] == []


def test_a_third_assigned_engine_drops_both_notes(tmp_path) -> None:
    """#2040 review: routing put q1 on DocumentDB, neither engine of the pair. Each note
    points at an engine the plan does not use for q1, so neither reaches the deliverables."""
    data = _guarded(tmp_path, "documentdb")
    (found,) = data.analysis_contradictions
    assert (found.winner, found.rule) == ("documentdb", ac.RULE_ASSIGNED_THIRD)
    assert found.scores == {}
    analyses = {
        "aurora_postgresql": _engine_analysis("no-relational-need"),
        "dynamodb": _engine_analysis("ordered-scan"),
    }
    assignment = {"query_assignments": [{"query_id": "q1", "assigned_engine": "documentdb"}]}
    out, (settled,) = ac.check_analysis_consistency(analyses, {}, {}, assignment)
    assert settled.winner == "documentdb"
    for engine in analyses:
        assert out[engine]["workload_analysis"]["anti_patterns_detected"] == [], engine
    assert analyses["dynamodb"]["workload_analysis"]["anti_patterns_detected"], "input unchanged"


def test_a_query_routing_did_not_place_falls_back_to_the_scorer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scorer(monkeypatch, {"aurora_postgresql": 10.0, "dynamodb": 90.0})
    (settled,) = ac.settle(
        ac.find_contradictions(ANALYSES), QUERIES, object(), assigned={"other": "dynamodb"}
    )
    assert (settled.winner, settled.rule) == ("dynamodb", ac.TIE_RULE_SCORE)
