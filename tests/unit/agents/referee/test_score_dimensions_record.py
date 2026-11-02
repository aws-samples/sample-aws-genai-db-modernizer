# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each query's score dimensions, recorded from the scorer call routing made (#1471).

``assignment/v<N>/access_patterns.json`` (schema version 4) holds, per query and scored
engine, the pattern, cost, base, applied penalty and score the resolver's own scorer
call computed (``score_query_for_engine(..., explain=True)``), and per engine and table
the complexity, performance and weights that call combined. Routing reads none of it.
These checks tie the record to the move ledger on the samples: every scored rung of every
query's latest ladder has its dimensions recorded, with the same score, and the figures
are the scorer's own (its anti-pattern map, cost dimension and pattern model).
"""

from __future__ import annotations

from functools import cache
from typing import Any

import pytest

from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.query_pattern import (
    SCORE_DIMENSIONS_SCHEMA_VERSION,
    access_patterns_record,
    recorded_dimensions,
    write_access_patterns,
)
from src.agents.referee.query_scoring import ScoringContext
from src.shared.engine_names import without_placeholder_engines
from src.shared.report_facts import class_pattern_score, recorded_pattern_score
from src.storage.local_store import LocalArtifactStore
from src.tools.analysis.score_weights import table_weights
from src.tools.sql.query_facts import remember_collected_facts
from tests.unit.assessment.conftest import PLACED_JOB, placed_run

SAMPLES = ("wordpress", "discourse")


@cache
def _job(sample: str) -> tuple[Any, Any, list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """``(state, store, lineage, {version: record})`` of the sample's placed run."""
    _, state, store = placed_run(sample)
    root = f"{sample}/{PLACED_JOB}"
    records = {
        int(a["version"]): store.read_json(
            f"{root}/assignment/v{a['version']}/access_patterns.json"
        )
        for a in state.lineage
    }
    return state, store, state.lineage, records


def _ladders(lineage: list[dict[str, Any]]) -> dict[int, dict[str, dict[str, Any]]]:
    """``{version: {query: {engine: rung}}}``: each query's latest ladder as of each version."""
    out: dict[int, dict[str, dict[str, Any]]] = {}
    current: dict[str, dict[str, Any]] = {}
    for assignment in lineage:
        for move in sorted(assignment.get("moves") or [], key=lambda m: m["seq"]):
            rungs = (move.get("evidence") or {}).get("candidates") or []
            if rungs:
                current[move["query_id"]] = {r["engine"]: r for r in rungs}
        out[int(assignment["version"])] = dict(current)
    return out


@pytest.mark.parametrize("sample", SAMPLES)
def test_every_scored_rung_has_the_dimensions_of_its_score(sample: str) -> None:
    """Every engine a query's latest ladder scored has its dimensions in that version's
    record, and the recorded score is the ladder's: the numbers never disagree."""
    _, _, lineage, records = _job(sample)
    ladders = _ladders(lineage)
    checked = 0
    for version, record in records.items():
        assert record["schema_version"] == SCORE_DIMENSIONS_SCHEMA_VERSION
        rows = {r["query_id"]: r for r in record["queries"]}
        for qid, ladder in ladders[version].items():
            dims = rows[qid]["score_dimensions"]
            for engine, rung in ladder.items():
                if rung.get("score") is None:
                    assert engine not in dims, (version, qid, engine)
                    continue
                entry = dims[engine]
                # A score equal to the base, and a penalty of 0, are left out.
                fit, penalty = entry.get("fit", entry["base"]), entry.get("penalty", 0)
                assert fit == rung["score"], (version, qid, engine)
                assert fit == max(0, entry["base"] - penalty)
                assert ("fit" in entry) == (penalty > 0 and entry["base"] > 0)
                assert entry.get("penalty") != 0
                checked += 1
    assert checked


@pytest.mark.parametrize("sample", SAMPLES)
def test_the_recorded_dimensions_are_the_scorers_own(sample: str) -> None:
    """For a few queries, the recorded pattern, cost and penalty are the scorer's own
    (the pattern model, ``ScoringContext.cost_score`` and the anti-pattern map), and each
    table's complexity, performance and weights are the engine analysis's."""
    state, _, lineage, records = _job(sample)
    analyses = without_placeholder_engines(state.analyses)
    remember_collected_facts(state.collector)
    ctx = ScoringContext.from_pipeline(analyses, state.triage, state.collector)
    record = records[max(records)]
    queries = {q["query_id"]: q for q in state.query_patterns}
    recorded = [r for r in record["queries"] if r["score_dimensions"]]
    # A few queries, some of them penalised on an engine.
    with_penalty = [
        r for r in recorded if any(e.get("penalty") for e in r["score_dimensions"].values())
    ]
    rows = recorded[:15] + with_penalty[:10]
    assert rows and with_penalty
    penalised = 0
    for row in rows:
        query = queries[row["query_id"]]
        for engine, entry in row["score_dimensions"].items():
            penalty = entry.get("penalty", 0)
            assert penalty == ctx.anti_pattern_map.get((row["query_id"], engine), 0)
            penalised += penalty > 0
            if ctx.pattern_model.has_class_score(query):
                # Left out where it is the row's class score; the reader falls back to it.
                assert recorded_pattern_score(row, engine) == ctx.pattern_model.pattern_score(
                    query, engine
                )
                assert entry["cost"] == ctx.cost_score(query, engine)
    assert penalised
    for engine, rated in record["table_dimensions"].items():
        recs = {r["table_id"]: r for r in analyses[engine]["table_recommendations"]}
        assert rated["tables"]
        for table, dims in rated["tables"].items():
            breakdown = recs[table]["score_breakdown"]
            assert dims["complexity"] == breakdown["complexity_score"]
            assert dims["performance"] == breakdown["performance_score"]
            weights = table_weights(engine, recs[table])
            assert dims.get("weights", rated["weights"]) == {
                "pattern": weights["pattern_match"],
                "complexity": weights["complexity"],
                "performance": weights["performance"],
                "cost": weights["cost"],
            }
    assert record["table_dimensions"]


def test_recording_the_dimensions_changes_no_routing() -> None:
    """The resolver with the sink gives the same assignment as without: routing reads none
    of what it records."""
    state, _, _, _ = _job("wordpress")
    remember_collected_facts(state.collector)
    plain = AssignmentResolver().resolve(state.triage, state.analyses, state.collector)
    sink: dict[str, dict[str, Any]] = {}
    recorded = AssignmentResolver().resolve(
        state.triage, state.analyses, state.collector, score_dimensions=sink
    )
    drop = {"timestamp", "created_at"}
    assert recorded.model_dump(exclude=drop) == plain.model_dump(exclude=drop)
    assert sink and all(isinstance(v, dict) for v in sink.values())


def test_a_later_version_carries_its_predecessors_dimensions(tmp_path) -> None:
    """A writer that did not score (a customer edit, a rollback) carries the dimensions of
    the version its assignment came from; from a record written before them, none."""
    state, _, _, records = _job("wordpress")
    store = LocalArtifactStore(base_dir=str(tmp_path))
    root = "wordpress/job"
    store.write_json(f"{root}/assignment/v1/access_patterns.json", records[1])
    store.write_json(f"{root}/assignment/v2/assignment.json", {"version": 2, "previous_version": 1})
    write_access_patterns(store, "wordpress", "job", 2, state.collector, state.analyses)
    carried = store.read_json(f"{root}/assignment/v2/access_patterns.json")
    assert carried["table_dimensions"] == records[1]["table_dimensions"]
    assert [r["score_dimensions"] for r in carried["queries"]] == [
        r["score_dimensions"] for r in records[1]["queries"]
    ]
    old = {**records[1], "schema_version": 3}
    old.pop("table_dimensions")
    for row in old["queries"]:
        row.pop("score_dimensions")
    store.write_json(f"{root}/assignment/v1/access_patterns.json", old)
    write_access_patterns(store, "wordpress", "job", 2, state.collector, state.analyses)
    bare = store.read_json(f"{root}/assignment/v2/access_patterns.json")
    assert bare["table_dimensions"] == {}
    assert all(r["score_dimensions"] == {} for r in bare["queries"])


def test_a_record_built_without_a_scorer_run_holds_no_dimensions() -> None:
    state, _, _, _ = _job("wordpress")
    record = access_patterns_record(state.collector, state.analyses)
    assert record["schema_version"] == SCORE_DIMENSIONS_SCHEMA_VERSION
    assert record["table_dimensions"] == {}
    assert all(r["score_dimensions"] == {} for r in record["queries"])


def test_the_reality_checks_version_records_its_own_run(tmp_path) -> None:
    """The reality check re-runs the resolver: its revised version's record holds that
    run's dimensions, never the input version's (here stripped, so none could carry)."""
    import shutil

    from src.agents.referee.reality_check_handler import (
        run_reality_check_deterministic,
        write_reality_check_result,
    )

    _, store, _, records = _job("wordpress")
    shutil.copytree(store.base_dir, tmp_path / "artifacts")
    copy = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    root = f"wordpress/{PLACED_JOB}"
    stripped = {**records[1], "table_dimensions": {}}
    stripped["queries"] = [{**r, "score_dimensions": {}} for r in records[1]["queries"]]
    copy.write_json(f"{root}/assignment/v1/access_patterns.json", stripped)
    det = run_reality_check_deterministic(PLACED_JOB, "wordpress", copy, 1)
    assert det["score_dimensions"]
    version = write_reality_check_result(copy, PLACED_JOB, "wordpress", det, 1)
    assert version is not None
    written = copy.read_json(f"{root}/assignment/v{version}/access_patterns.json")
    assert written["table_dimensions"] == records[2]["table_dimensions"]
    assert any(r["score_dimensions"] for r in written["queries"])


def test_the_homogeneous_guarantee_records_the_score_it_gives(monkeypatch) -> None:
    """A homogeneous source's own Aurora engine is scored past the gates (#474): when the
    plain scorer gates it for a query, the record holds the guarantee's own dimensions,
    and their score is the one routing compared."""
    from src.agents.referee import assignment_resolver
    from src.agents.referee.query_scoring import EngineScore

    state, _, _, _ = _job("wordpress")
    remember_collected_facts(state.collector)
    real = assignment_resolver.score_query_for_engine
    target = state.query_patterns[0]["query_id"]

    def gating(query, engine, ctx, **kwargs):
        if query["query_id"] == target and engine == "aurora_mysql":
            return EngineScore(None, "gated in this test", "capability")
        return real(query, engine, ctx, **kwargs)

    monkeypatch.setattr(assignment_resolver, "score_query_for_engine", gating)
    sink: dict[str, dict[str, Any]] = {}
    assignment = AssignmentResolver().resolve(
        state.triage, state.analyses, state.collector, score_dimensions=sink
    )
    entry = recorded_dimensions(sink)["queries"][target]["aurora_mysql"]
    rung = next(
        c
        for m in assignment.moves
        if m.query_id == target and m.evidence.candidates
        for c in m.evidence.candidates
        if c.engine == "aurora_mysql"
    )
    assert rung.score is not None
    assert entry.get("fit", entry["base"]) == rung.score


def test_a_pattern_equal_to_the_class_score_is_left_out_and_read_back() -> None:
    """The record writes ``pattern`` only where it is not the row's class score (the better
    of as written and after redesign); the reader gives the class score back."""
    from src.agents.referee.query_pattern import _without_class_pattern

    row = {
        "class_scores": {"dynamodb": 40, "aurora_mysql": 80},
        "redesign": {"class_scores": {"dynamodb": 100}},
    }
    dims = {
        "dynamodb": {"pattern": 100, "cost": 90, "base": 96},
        "aurora_mysql": {"pattern": 80, "cost": 55, "base": 74},
        "opensearch": {"pattern": 30, "cost": 50, "base": 20},
    }
    kept = _without_class_pattern(row, dims)
    assert "pattern" not in kept["dynamodb"] and "pattern" not in kept["aurora_mysql"]
    assert kept["opensearch"]["pattern"] == 30
    stored = {**row, "score_dimensions": kept}
    assert [recorded_pattern_score(stored, e) for e in dims] == [100.0, 80.0, 30.0]
    assert class_pattern_score({"class_scores": {}}, "dynamodb") is None
    utility = {"class_scores": {}, "score_dimensions": {"dynamodb": {"base": 10}}}
    assert recorded_pattern_score(utility, "dynamodb") is None


@pytest.mark.parametrize("sample", SAMPLES)
def test_the_sample_records_leave_most_patterns_out(sample: str) -> None:
    _, _, _, records = _job(sample)
    entries = [
        (r, e, d)
        for r in records[max(records)]["queries"]
        for e, d in r["score_dimensions"].items()
        if "cost" in d
    ]
    written = [(r, e, d) for r, e, d in entries if "pattern" in d]
    assert len(written) < len(entries) / 2
    assert all(d["pattern"] != class_pattern_score(r, e) for r, e, d in written)
