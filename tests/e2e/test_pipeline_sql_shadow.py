"""Shadow mode for the sqlglot facade writes its per-job artifact (#557).

The facade's answer never drives the pipeline (the routing snapshot and the
deliverable tests guard that); this only checks the collect phase leaves the
comparison behind for #553 to read.
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
    assert payload["mode"] == "shadow" and payload["drives_output"] == "regex"
    assert summary["queries"] == run.steps["collect"]["queries"]
    assert summary["agree"] + summary["disagree"] == summary["queries"]
    assert len(payload["disagreements"]) == summary["disagree"]
