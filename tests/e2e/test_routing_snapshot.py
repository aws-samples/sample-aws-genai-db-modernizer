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

import pytest

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
def test_a_derived_version_does_not_inherit_v1_moves(run: PipelineResult) -> None:
    """``Assignment.moves`` is per-version (#616, maintainer decision 2026-10-14).

    A version derived from an earlier one (the reality check's, here) holds
    only the Moves that produced it, never a copy of v1's. Its writer does not
    record its own Moves yet (later #616 changes), so today the list is empty.
    """
    from src.contracts.assignment_models import Assignment

    root = run.job_dir() / "assignment"
    versions = sorted(int(p.name[1:]) for p in root.glob("v*") if p.name[1:].isdigit())
    assert versions[0] == 1 and len(versions) > 1, versions
    v1 = Assignment.model_validate_json((root / "v1" / "assignment.json").read_text())
    assert v1.moves
    for n in versions[1:]:
        later = Assignment.model_validate_json((root / f"v{n}" / "assignment.json").read_text())
        assert later.previous_version is not None
        assert later.moves == [], f"v{n} carries {len(later.moves)} Moves it did not record"
