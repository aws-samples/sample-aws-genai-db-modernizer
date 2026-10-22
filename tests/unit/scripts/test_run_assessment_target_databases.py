"""#1328: ``run_assessment.py --target-databases`` is the CLI's engine scope.

A refused value stops the run before anything is written, with the setting's own
message; a scope passed to a run past triage (resuming the reality check, settling
choices) is refused rather than ignored. The scoped run itself is in
``tests/e2e/test_engine_scope_api.py``.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own script with fixed argv
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "run_assessment.py"


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # No AWS access: --llm-mode none never calls a model, and no credential may reach it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    return subprocess.run(  # nosec B603 -- fixed interpreter and this repo's script
        [sys.executable, str(SCRIPT), "--artifact-root", str(tmp_path / "a"), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )


@pytest.mark.parametrize(
    ("value", "says"),
    [
        (" , ", "--target-databases is empty"),
        ("dynamodb,aurora", "'aurora' is legacy and not accepted; use aurora_mysql or"),
        ("neptune", "Neptune (neptune) is not available yet"),
        ("dynamodb,redis", "--target-databases: unknown engine id(s) redis"),
    ],
)
def test_a_refused_scope_stops_before_anything_is_written(tmp_path, value, says) -> None:
    proc = _run(
        tmp_path, "--file", "missing.json", "--llm-mode", "none", "--target-databases", value
    )
    assert proc.returncode == 2
    assert says in proc.stderr
    assert not (tmp_path / "a").exists()


@pytest.mark.parametrize(
    "past_triage",
    [["--resume-reality-check"], ["--decide-for-me"], ["--choose", "t=dynamodb"]],
)
def test_a_scope_cannot_be_changed_past_triage(tmp_path, past_triage) -> None:
    proc = _run(
        tmp_path, "--job-id", "j1", "--db", "wordpress", *past_triage,
        "--target-databases", "dynamodb",
    )  # fmt: skip
    assert proc.returncode != 0
    lines = [json.loads(x) for x in proc.stdout.splitlines() if x.startswith("{")]
    errors = [x for x in lines if x.get("status") == "error"]
    assert errors and "cannot change the scope of a job past triage" in errors[0]["message"]


def test_the_scope_is_recorded_and_routes_wordpress_within_it(tmp_path) -> None:
    import zipfile

    with zipfile.ZipFile(REPO / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        z.extractall(tmp_path / "in")
    proc = _run(
        tmp_path, "--file", str(tmp_path / "in" / "wordpress-collection.json"), "--db", "wordpress",
        "--llm-mode", "none", "-y", "--target-databases", "DynamoDB,aurora_postgresql",
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    lines = {x["phase"]: x for x in map(json.loads, proc.stdout.splitlines()) if "phase" in x}
    assert lines["triage"]["engine_scope"] == ["aurora_postgresql", "dynamodb"]
    assert lines["assignment"]["distribution"] == {"dynamodb": 40, "aurora_postgresql": 67}
    job = lines["collect"]["job_id"]
    decision = json.loads(
        (tmp_path / "a" / "wordpress" / job / "decisions" / "engine_scope.json").read_text()
    )
    assert (decision["engines"], decision["via"]) == (["aurora_postgresql", "dynamodb"], "cli")
