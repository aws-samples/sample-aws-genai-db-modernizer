"""Per-query routing snapshot harness (see ``tests/e2e/routing_snapshot.py``).

Not an identity gate: a routing refactor is expected to move some queries.
The totals-only guard in ``test_routing_baseline.py`` missed #474/#487-sized
moves that happened to cancel out between engines; this test instead reports
every per-query change between a fresh deterministic run and
``baselines/routing_snapshot.json``, grouped by which step produced it, so a
no-op refactor shows no diff and a routing change shows exactly which
queries moved and why. A PR that changes routing regenerates the snapshot
with ``--reason`` and pastes the printed diff into the PR description, the
same convention ``routing_baseline.py`` uses.

Plain unit tests for the diff logic (``compare``/``format_diff``), using small
synthetic snapshots and no pipeline run, live in
``tests/unit/test_routing_snapshot_diff.py`` -- that module needs none of the
``e2e`` extra, so it runs under ``make test`` too, not just ``make e2e``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.contracts.assignment_models import Assignment
from tests.e2e.pipeline import PipelineResult
from tests.e2e.routing_snapshot import BASELINE, compare, format_diff, measure

pytestmark = pytest.mark.e2e


@pytest.mark.deterministic
def test_routing_snapshot_matches_the_baseline(run: PipelineResult) -> None:
    # Deterministic pipeline only: external runs deselect the `deterministic`
    # marker (a skip would fail under --fail-on-skip)
    baseline = json.loads(BASELINE.read_text())
    assert baseline.get("reason"), "the snapshot must say why it was last updated"
    actual = measure(run.job_dir())
    diff = compare(baseline["samples"][run.db], actual)
    if not diff:
        return
    collector = json.loads((run.job_dir() / "collector" / "output.json").read_text())
    query_text = {
        q["query_id"]: q.get("query_text", "")
        for q in collector.get("queries", {}).get("query_patterns", [])
    }
    pytest.fail(
        format_diff(run.db, diff, query_text)
        + "\nThe snapshot is out of date, not necessarily wrong -- if this PR means to "
        "change routing, regenerate it and paste this diff into the PR description: "
        'uv run python -m tests.e2e.routing_snapshot --write --reason "..."'
    )


@pytest.mark.deterministic
def test_v1_assignment_replays_from_its_moves(run: PipelineResult) -> None:
    """The move ledger (#616) rebuilds the pipeline's own assignment/v1 exactly,
    from v1's own Moves (moves are per-version).

    Every query has exactly one first Move (no ``from_engine``) carrying its
    candidate ladder, and replaying the Moves in ``seq`` order gives every
    query's ``assigned_engine`` and ``tied_engines``.
    """
    from src.agents.referee.move_ledger import replay
    from src.contracts.assignment_models import Assignment

    v1 = Assignment.model_validate_json(
        (run.job_dir() / "assignment" / "v1" / "assignment.json").read_text()
    )
    expected = {
        qa.query_id: (qa.assigned_engine, list(qa.tied_engines)) for qa in v1.query_assignments
    }
    assert replay(v1.moves) == expected
    firsts = [m.query_id for m in v1.moves if m.from_engine is None]
    assert sorted(firsts) == sorted(expected)
    assert [m.seq for m in v1.moves] == list(range(1, len(v1.moves) + 1))
    assert all(m.evidence.candidates for m in v1.moves)


@pytest.mark.deterministic
def test_every_group_member_carries_the_same_tie_after_the_first_assignment(
    run: PipelineResult,
) -> None:
    """#833: the first assignment gives every member of a co-dependency group
    the same engine and the same ``tied_engines``, so ``tie_break.group_tie``
    (the one rule later passes use for a group's tie) never meets members
    that disagree on a real run."""
    v1 = Assignment.model_validate_json(
        (run.job_dir() / "assignment" / "v1" / "assignment.json").read_text()
    )
    by_id = {qa.query_id: qa for qa in v1.query_assignments}
    for group in v1.co_dependency_groups:
        members = [by_id[q] for q in group if q in by_id]
        assert len({tuple(sorted(qa.tied_engines)) for qa in members}) <= 1, group
        assert len({qa.assigned_engine for qa in members}) <= 1, group


def _versions(run: PipelineResult) -> dict[int, Assignment]:
    root = run.job_dir() / "assignment"
    return {
        int(p.name[1:]): Assignment.model_validate_json((p / "assignment.json").read_text())
        for p in root.glob("v*")
        if p.name[1:].isdigit()
    }


def _state(assignment: Assignment) -> dict[str, tuple[str, list[str]]]:
    return {
        qa.query_id: (qa.assigned_engine, list(qa.tied_engines))
        for qa in assignment.query_assignments
    }


def _reality_check_output(run: PipelineResult) -> dict[str, Any]:
    output: dict[str, Any] = json.loads(
        (run.job_dir() / "reality-check" / "output.json").read_text()
    )
    return output


@pytest.mark.deterministic
def test_the_reality_check_version_replays_from_v1_plus_its_own_moves(run: PipelineResult) -> None:
    """Moves are per-version (#616): the reality check's version holds exactly
    the check's own Moves, and v1 replayed plus those Moves rebuilds it --
    every engine and every ``tied_engines``. Every other version derived from
    v1 holds no Moves yet (the customer gate and the post-schema router record
    theirs in later changes)."""
    from src.agents.referee.move_ledger import replay

    versions = _versions(run)
    rc_version = _reality_check_output(run)["output_assignment_version"]
    assert rc_version, "every sample's reality check consolidates something"
    revised = versions[rc_version]
    assert revised.previous_version == 1
    assert revised.moves, "the reality check's version must carry its own Moves"
    assert [m.seq for m in revised.moves] == list(range(1, len(revised.moves) + 1))
    assert replay(revised.moves, start=replay(versions[1].moves)) == _state(revised)
    # Only the check's own Moves: none is a first assignment.
    assert all(m.from_engine is not None for m in revised.moves)
    for n, later in versions.items():
        if n not in (1, rc_version):
            assert later.moves == [], f"v{n} carries {len(later.moves)} Moves it did not record"


@pytest.mark.deterministic
def test_the_reality_check_records_derive_from_its_moves(run: PipelineResult) -> None:
    """``consolidations``, ``sweep_decisions`` and ``flag_decisions`` are derived
    from the ledger (#616): the written consolidation records are exactly what
    ``consolidations_from_moves`` builds from the version's input and its own
    Moves (fed the written records for the reason text, the retention and the
    engine's first-assignment verdict, which a Move does not carry, and the
    analysed engine costs for the saving), each record's (from, to, count) is
    the ledger's net move for that pair, an engine the sweep removed is exactly
    where its sweep Moves came from, and each moved flag is one model-flag Move
    per query."""
    from collections import Counter

    from src.agents.referee.move_ledger import replay
    from src.agents.referee.reality_check_patterns import _engine_infra_cost
    from src.agents.referee.reality_check_records import consolidations_from_moves

    versions = _versions(run)
    output = _reality_check_output(run)
    revised = versions[output["output_assignment_version"]]
    assert revised.previous_version is not None
    start = _state(versions[revised.previous_version])
    final = replay(revised.moves, start=start)
    net = Counter(
        (start[qid][0], engine) for qid, (engine, _) in final.items() if start[qid][0] != engine
    )
    records: Counter[tuple[str, str]] = Counter()
    for c in output["consolidations"]:
        records[(c["from_engine"], c["to_engine"])] += c["query_count"]
    assert records == net

    analysis = {
        p.name.removeprefix("analysis-"): json.loads((p / "analysis.json").read_text())
        for p in run.job_dir().glob("analysis-*")
        if (p / "analysis.json").exists()
    }
    before = [
        qa.model_dump(mode="json") for qa in versions[revised.previous_version].query_assignments
    ]
    derived = consolidations_from_moves(
        before,
        revised.moves,
        output["consolidations"],
        output["unique_value_assessment"],
        _engine_infra_cost(analysis),
    )
    assert derived == output["consolidations"]

    sweep_from = {m.from_engine for m in revised.moves if m.pass_.value == "sweep"}
    removed = {d["engine"] for d in output["sweep_decisions"] if d["decision"] == "removed"}
    assert sweep_from == removed

    flag_moves = sorted(
        (m.query_id, m.from_engine, m.to_engine)
        for m in revised.moves
        if m.pass_.value == "model_flag"
    )
    flagged = sorted(
        (qid, d["flagged_engine"], d["destination_engine"])
        for d in output["flag_decisions"]
        if d["outcome"] == "moved"
        for qid in d["query_ids"] + d["moved_with_group"]
    )
    assert flag_moves == flagged


@pytest.mark.deterministic
def test_every_reality_check_move_states_its_scores(run: PipelineResult) -> None:
    """ADR-031 Decision 7: a moved query reports its before and after scores.
    A Move's score is ``None`` only when that engine was gated (named in the
    Move's candidate ladder)."""
    versions = _versions(run)
    revised = versions[_reality_check_output(run)["output_assignment_version"]]
    for move in revised.moves:
        gates = {c.engine: c.gate for c in move.evidence.candidates}
        assert move.evidence.candidates, move
        if move.score_before is None:
            assert move.from_engine is not None and gates.get(move.from_engine), move
        if move.score_after is None:
            assert gates.get(move.to_engine), move
