"""The pipeline finishes after a customer pick on every sample (#859).

A customer edit at the assignment gate is a normal flow: the customer pins
queries, the reality check re-runs on that version, then synthesis and the
reports. On a copy of each sample's deterministic job, pin the first query
(by id) of every engine the first assignment used onto that engine, then run
the rest of the pipeline. Pinning every first-version engine keeps engines the
reality check would otherwise remove, so the routed workloads (and their
evidence labels) differ from the plain run's.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from pathlib import Path

import pytest

from tests.e2e.pipeline import REPO, PipelineResult, _env

JOB = "pick"


def _script(*args: str) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=_env(), timeout=600
    )
    assert proc.returncode == 0, f"{args[0]} failed:\n{proc.stderr[-3000:]}"
    payload: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


def _first_query_per_engine(assignment: dict) -> dict[str, str]:
    """The first in-scope query id of every engine that owns a query, by id."""
    picks: dict[str, str] = {}
    for qa in sorted(assignment["query_assignments"], key=lambda q: str(q["query_id"])):
        if qa.get("in_scope", True) and qa.get("assigned_engine"):
            picks.setdefault(qa["assigned_engine"], str(qa["query_id"]))
    return picks


@pytest.fixture(scope="module")
def picked(run: PipelineResult, tmp_path_factory: pytest.TempPathFactory) -> dict:
    from src.agents.referee.assignment_overrides import (
        QueryOverrideInput,
        apply_assignment_overrides,
    )
    from src.storage.local_store import LocalArtifactStore

    if run.external:
        pytest.skip("needs the deterministic job's first assignment version")
    root = Path(tmp_path_factory.mktemp(f"pick-{run.db}")) / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / JOB)
    first = json.loads((root / run.db / JOB / "assignment" / "v1" / "assignment.json").read_text())
    picks = _first_query_per_engine(first)
    result = apply_assignment_overrides(
        LocalArtifactStore(base_dir=str(root)),
        run.db,
        JOB,
        [QueryOverrideInput(query_id=q, assigned_engine=e) for e, q in sorted(picks.items())],
    )
    common = ["--job-id", JOB, "--db", run.db, "--artifact-root", str(root)]
    _script("scripts/run_reality_check.py", *common, "--llm-mode", "none")
    _script("scripts/run_synthesis.py", *common, "--llm-mode", "none")
    report_step = _script("scripts/run_report.py", *common)
    return {
        "picks": picks,
        "customer_version": result.assignment.version,
        "report": json.loads((root / report_step["report"]).read_text()),
        "deliverables": report_step.get("deliverables", []),
    }


@pytest.mark.deterministic
def test_synthesis_and_reports_finish_after_a_customer_pick(picked: dict) -> None:
    report = picked["report"]
    assert report["assignment_summary"]["version"] >= picked["customer_version"]
    assert any(p.endswith("/executive-report.pdf") for p in picked["deliverables"])
    # Every pinned engine stays in the plan: a pin is the customer's choice.
    owners = {r["target"] for r in report["ranking"] if r.get("role") != "cache_layer"}
    assert set(picked["picks"]) <= owners
