"""ci/e2e-llm.sh records the judge first and keeps its test steps inside the job budget (#1593).

A scoped discourse run once spent 18 minutes on the report tests and 32 on the UI
tests, and the mirror's 90-minute job limit killed the job during the judge step:
no judge score, no results.json, no uploaded test results.
"""

from __future__ import annotations

import subprocess  # nosec B404 -- runs this repo's own ci/lib.sh with fixed argv
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ci" / "e2e-llm.sh"
LIB = REPO / "ci" / "lib.sh"


def _deadline(snippet: str, seconds_from_now: int) -> subprocess.CompletedProcess[str]:
    """Run ``snippet`` with lib.sh sourced and the deadline ``seconds_from_now`` away
    (negative: already past), the clock read by bash as ci/e2e-llm.sh reads it."""
    return subprocess.run(  # nosec B603 B607 -- fixed argv
        [
            "bash",
            "-c",
            'source "$1"; export E2E_LLM_DEADLINE_TS=$(($(date +%s) + $2)); ' + snippet,
            "_",
            str(LIB),
            str(seconds_from_now),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        stdin=subprocess.DEVNULL,
    )


def test_a_step_past_the_deadline_is_skipped_with_124() -> None:
    proc = _deadline("run_before_deadline echo ran", -1)
    assert proc.returncode == 124, proc
    assert "ran" not in proc.stdout
    assert "job budget" in proc.stderr


def test_a_step_that_outlasts_the_deadline_is_stopped_with_124() -> None:
    # A 30 s sleep left to finish would return 0 (and the 60 s subprocess timeout
    # bounds the test); 124 is the stop.
    proc = _deadline("run_before_deadline sleep 30", 2)
    assert proc.returncode == 124, proc
    assert "still running after" in proc.stderr


def test_a_step_inside_the_deadline_keeps_its_own_status() -> None:
    # A short deadline: a step that ends at once can leave the watchdog's sleep
    # behind for its length, holding the captured output open.
    assert _deadline("run_before_deadline true", 5).returncode == 0
    assert _deadline("run_before_deadline false", 5).returncode == 1


def _step_order() -> list[str]:
    text = SCRIPT.read_text()
    body = text[text.index("# --- deliverable checks + judge") :]
    return [line.split()[1] for line in body.splitlines() if line.strip().startswith("step ")]


def test_the_judge_runs_before_the_browser_tests() -> None:
    order = _step_order()
    assert order.index("render") < order.index("judge") < order.index("report-tests"), order
    assert order.index("report-tests") < order.index("ui-tests"), order


def test_the_browser_tests_run_inside_the_job_budget() -> None:
    text = SCRIPT.read_text()
    for step in ("report-tests", "ui-tests"):
        assert f"step {step} run_before_deadline " in text, step
    assert "E2E_LLM_JOB_BUDGET" in text
    # The default leaves the results step and the artifact upload time inside the
    # mirror's 90-minute job limit.
    assert "E2E_LLM_JOB_BUDGET:-4800" in text


def test_a_previous_runs_results_are_cleared_before_any_step(tmp_path: Path) -> None:
    """A skipped or stopped test step writes no junit: the merged results must not
    read a previous run's from a reused output folder. The run below stops at its
    first preflight check (no model access), before any step."""
    import os

    out = tmp_path / "llm-chat-wordpress"
    out.mkdir()
    stale = ["reports-junit.xml", "ui-junit.xml", "judge.json", "summary.json", "steps.json"]
    for name in stale:
        (out / name).write_text("from an earlier run")
    (out / "transcript.jsonl").write_text("{}\n")  # a dry run's input may live here
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "AWS_REGION"}
        and not k.startswith("E2E_LLM_")
    }
    env["E2E_OUTPUT"] = str(tmp_path)
    proc = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["bash", str(SCRIPT), "chat", "wordpress"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        stdin=subprocess.DEVNULL,
    )

    assert proc.returncode != 0
    assert [n for n in stale if (out / n).exists()] == []
    assert (out / "transcript.jsonl").read_text() == "{}\n"
