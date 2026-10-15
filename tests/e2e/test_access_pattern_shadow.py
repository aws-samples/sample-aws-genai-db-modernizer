"""The access-pattern shadow on the bundled samples (#728, see ``tests/e2e/access_pattern_shadow.py``).

Every query of every sample gets exactly one class, and the shadow's "current"
side is the pipeline's own first assignment: computing the shadow changes no
engine (the routing snapshot guards the rest).
"""

from __future__ import annotations

import json

import pytest

from src.agents.referee.access_pattern import SCORED_CLASSES, UNPARSED, UTILITY
from tests.e2e.access_pattern_shadow import format_shadow, load, moved
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.e2e


@pytest.mark.deterministic
def test_every_query_has_one_class_and_the_current_side_is_v1(run: PipelineResult) -> None:
    job_dir = run.job_dir()
    shadow = load(job_dir)
    collector = json.loads((job_dir / "collector" / "output.json").read_text())
    query_ids = [q["query_id"] for q in collector["queries"]["query_patterns"]]
    assert [r["query_id"] for r in shadow["queries"]] == query_ids
    allowed = {*SCORED_CLASSES, UTILITY, UNPARSED}
    assert {r["class"] for r in shadow["queries"]} <= allowed
    v1 = json.loads((job_dir / "assignment" / "v1" / "assignment.json").read_text())
    engines = {qa["query_id"]: qa["assigned_engine"] for qa in v1["query_assignments"]}
    assert {r["query_id"]: r["current"]["engine"] for r in shadow["queries"]} == engines
    assert shadow["summary"]["engine_changes"] == len(moved(shadow))
    assert format_shadow(run.db, shadow)
