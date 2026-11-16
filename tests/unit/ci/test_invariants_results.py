# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2143: e2e-llm's ``invariants`` check, ``scripts/check_invariants.py`` on the run's job.

``ci/e2e-llm.sh`` runs the script over the headless run's own job and passes its verdict
to ``ci/llm/run.py results --invariants``; a failing invariant fails the run, and a step
that wrote nothing fails it too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ci.llm import run

REPO = Path(__file__).resolve().parents[3]


def _results(tmp_path: Path, invariants: Path | None) -> dict:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"job_id": "abc12345", "db": "wordpress", "mode": "chat"}))
    junit = tmp_path / "junit.xml"
    junit.write_text(
        "<testsuite>"
        + "".join(
            f'<testcase classname="tests.e2e.{m}" name="t"/>'
            for m in ("test_pipeline", "test_report_engineering", "test_report_pdf")
        )
        + "</testsuite>"
    )
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps({"pass": True, "mean": 5.0, "scores": {}}))
    return run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[junit],
        judge_path=judge,
        git_sha="abc",
        invariants_path=invariants,
    )


@pytest.mark.parametrize(("status", "passed"), [("pass", True), ("fail", False), ("error", False)])
def test_results_require_the_invariants_check_once_it_ran(
    tmp_path: Path, status: str, passed: bool
) -> None:
    verdict = tmp_path / "invariants.json"
    failures = {"facts_recount": ["synthesis v2: facts.engine_count is 4, recounted 3"]}
    verdict.write_text(
        json.dumps({"status": status, "failures": failures if status == "fail" else {}})
    )
    row = _results(tmp_path, verdict)
    assert row["checks"]["invariants"] is passed
    assert row["pass"] is passed
    assert row["invariants"]["status"] == status
    if status == "fail":
        assert row["invariants"]["failures"] == failures
        assert "check invariants is false" in row["fail_reasons"]


def test_results_fail_when_the_invariants_step_wrote_nothing(tmp_path: Path) -> None:
    row = _results(tmp_path, tmp_path / "missing.json")
    assert row["checks"]["invariants"] is None and row["pass"] is False
    assert "missing or unreadable input: missing.json" in row["fail_reasons"]


def test_results_without_the_invariants_step_leave_it_null_and_unrequired(
    tmp_path: Path,
) -> None:
    row = _results(tmp_path, None)
    assert row["checks"]["invariants"] is None and row["invariants"] is None
    assert row["pass"] is True


def test_e2e_llm_runs_the_invariant_queries_on_the_runs_job_and_requires_them() -> None:
    script = (REPO / "ci" / "e2e-llm.sh").read_text()
    assert (
        'scripts/check_invariants.py "$ARTIFACT_ROOT/$DB/$JOB" --out "$OUT/invariants.json"'
        in script
    )
    assert "step invariants run_invariants" in script
    assert 'INVARIANTS_ARGS=(--invariants "$OUT/invariants.json")' in script
    assert '"${INVARIANTS_ARGS[@]+"${INVARIANTS_ARGS[@]}"}"' in script
    # A reused output folder's old verdict never stands in for this run's.
    assert '"$OUT/invariants.json"' in script.split('DRY_RUN="${E2E_LLM_DRY_RUN')[0]
    # The step runs on the job the deliverable checks read, before the results row.
    assert script.index("step invariants") < script.index("step results run_results")
    assert script.index("step render run_render") < script.index("step invariants")
