"""The facade-vs-regex cross-check writes its per-job artifact (#557, #553).

The facade's answer drives ``tables_accessed`` (#553; the routing snapshot
guards what that does to routing); this checks the collect phase leaves the
comparison with the regex behind.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.pipeline import PipelineResult


@pytest.mark.deterministic
def test_collect_writes_the_sql_shadow_artifact(run: PipelineResult) -> None:
    if run.external:
        pytest.skip("a job produced outside the deterministic runner")
    payload = json.loads((run.job_dir() / "collector" / "sql_shadow.json").read_text())
    summary = payload["summary"]
    assert payload["mode"] == "cross_check" and payload["drives_output"] == "facade"
    assert summary["queries"] == run.steps["collect"]["queries"]
    assert summary["agree"] + summary["disagree"] == summary["queries"]
    assert len(payload["disagreements"]) == summary["disagree"]
