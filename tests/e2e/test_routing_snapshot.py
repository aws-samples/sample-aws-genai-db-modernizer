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
