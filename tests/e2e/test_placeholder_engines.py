"""ADR-031 Decision 9 on real pipeline output (see tests/unit/agents/referee/test_placeholder_engines.py).

Neptune and Keyspaces are placeholders: on every bundled sample, triage defers them, nothing
analyzes them, no query or table is assigned to them, and no later artifact (reality check,
synthesis report) names either as an engine.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.e2e

PLACEHOLDERS = ("neptune", "keyspaces")
# A JSON string value that is exactly an engine id or display name, as in
# "assigned_engine": "neptune" or "Neptune", in any case.
ENGINE_ID = re.compile(r'"(?:neptune|keyspaces)"', re.IGNORECASE)
# Triage is the one stage allowed to name them: it lists what it deferred.
ALLOWED_DIRS = {"collector", "referee-triage"}


@pytest.mark.deterministic
def test_triage_defers_and_never_selects_a_placeholder(run: PipelineResult) -> None:
    triage = json.loads((run.job_dir() / "referee-triage" / "triage.json").read_text())
    selected = {a["agent_type"] for a in triage["selected_agents"]}
    skipped = {a["agent_type"] for a in triage.get("skipped_agents", [])}
    deferred = {a["agent_type"] for a in triage.get("deferred_agents", [])}
    assert not selected & set(PLACEHOLDERS)
    assert not skipped & set(PLACEHOLDERS)
    # Every placeholder a signal targeted was deferred, not dropped or selected.
    targeted = {
        t for s in triage.get("signals", []) for t in s.get("targets", []) if t in PLACEHOLDERS
    }
    assert targeted <= deferred, f"targeted {sorted(targeted)} but deferred {sorted(deferred)}"
    assert deferred <= set(PLACEHOLDERS)


@pytest.mark.deterministic
def test_no_stage_after_triage_analyzes_or_designs_a_placeholder(run: PipelineResult) -> None:
    job = run.job_dir()
    for engine in PLACEHOLDERS:
        assert not (job / f"analysis-{engine}").exists()
        assert not (job / f"schema-{engine}").exists()


@pytest.mark.deterministic
def test_no_assignment_or_report_artifact_names_a_placeholder_engine(run: PipelineResult) -> None:
    job = run.job_dir()
    checked = 0
    for path in sorted(job.rglob("*.json")):
        if path.relative_to(job).parts[0] in ALLOWED_DIRS:
            continue
        checked += 1
        hits = ENGINE_ID.findall(path.read_text())
        assert not hits, f"{path.relative_to(job)} names a placeholder engine: {hits[:3]}"
    assert checked, "no artifacts were checked"
    assert list(job.glob("assignment/v*/assignment.json")), "the assignment was not written"
