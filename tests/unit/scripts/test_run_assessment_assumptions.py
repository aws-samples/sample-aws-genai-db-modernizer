# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1991: ``run_assessment.py --assumptions <file>`` sets the job's assumptions in one
place, and ``--growth`` stays the one-value flag.

A refused file or value stops the run before anything is written, with the file's own
message; a value set twice (the flag and the file) is refused rather than one winning
silently; both are refused on a run past triage, as ``--target-databases`` is. A run
records the file's values under ``decisions/assumptions.json`` with origin ``file``, and
the approval gate states them so.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own scripts with fixed argv
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "run_assessment.py"


def _run(tmp_path: Path, *args: str, script: Path = SCRIPT, **env_extra: str):
    # No AWS access: --llm-mode none never calls a model, and no credential may reach it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    env.update(env_extra)
    return subprocess.run(  # nosec B603 -- fixed interpreter and this repo's script
        [sys.executable, str(script), "--artifact-root", str(tmp_path / "a"), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )


def _errors(proc) -> list[dict]:
    lines = [json.loads(x) for x in proc.stdout.splitlines() if x.startswith("{")]
    return [x for x in lines if x.get("status") == "error"]


def _file(tmp_path: Path, body: dict | str) -> str:
    path = tmp_path / "assumptions.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ({"growth": 0}, "--assumptions: growth must be between"),
        ({"grwth": 3}, '--assumptions: unknown key "grwth"'),
        ({"pricing": "eu-west-1"}, "--assumptions: pricing is collected"),
        ("{not json", "--assumptions: the file is not valid JSON"),
    ],
)
def test_a_refused_file_stops_before_anything_is_written(tmp_path, body, says) -> None:
    proc = _run(
        tmp_path, "--file", "missing.json", "--llm-mode", "none",
        "--assumptions", _file(tmp_path, body),
    )  # fmt: skip
    assert proc.returncode != 0
    errors = _errors(proc)
    assert errors and errors[0]["phase"] == "init" and says in errors[0]["message"], proc.stdout
    assert not (tmp_path / "a").exists()


def test_a_growth_flag_out_of_range_stops_before_anything_is_written(tmp_path) -> None:
    proc = _run(tmp_path, "--file", "missing.json", "--llm-mode", "none", "--growth", "1000")
    errors = _errors(proc)
    assert errors and "growth must be between" in errors[0]["message"]
    assert not (tmp_path / "a").exists()


def test_growth_set_by_the_flag_and_the_file_is_refused(tmp_path) -> None:
    proc = _run(
        tmp_path, "--file", "missing.json", "--llm-mode", "none", "--growth", "3",
        "--assumptions", _file(tmp_path, {"growth": 4}),
    )  # fmt: skip
    errors = _errors(proc)
    assert errors and "set growth in one place" in errors[0]["message"]


@pytest.mark.parametrize(
    "past_triage",
    [["--resume-reality-check"], ["--decide-for-me"], ["--choose", "t=dynamodb"]],
)
@pytest.mark.parametrize("flag", ["--growth", "--assumptions"])
def test_the_assumptions_cannot_be_changed_past_triage(tmp_path, past_triage, flag) -> None:
    value = "3" if flag == "--growth" else _file(tmp_path, {"growth": 3})
    proc = _run(tmp_path, "--job-id", "j1", "--db", "wordpress", *past_triage, flag, value)
    errors = _errors(proc)
    assert errors and "when a run starts at triage" in errors[0]["message"], proc.stdout


def test_a_file_outside_the_repo_is_refused_in_the_sandbox(tmp_path) -> None:
    """Under the headless sandbox the file must be inside the repository, as --file is.
    The file sets nothing, so a run the sandbox let through would stop at the check
    before writing anything."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env |= {"AWS_DEFAULT_REGION": "us-east-1", "MODERNIZER_CI_SANDBOX": "1"}
    proc = subprocess.run(  # nosec B603 -- fixed interpreter and this repo's script
        [
            sys.executable, str(SCRIPT), "--file", "docs/examples/wordpress/wordpress-collection.json",
            "--llm-mode", "none", "--assumptions", _file(tmp_path, "{}"),
        ],
        cwd=REPO, capture_output=True, text=True, timeout=300, env=env,
    )  # fmt: skip
    errors = _errors(proc)
    assert errors and "--assumptions" in errors[0]["message"], proc.stdout
    assert "outside the repository root" in errors[0]["message"]


def test_the_files_values_are_recorded_and_the_gate_states_them(tmp_path) -> None:
    """The file's peak and --growth together: one write keeps both origins and the
    file's hash (#1991 review), and the gate states each."""
    import zipfile

    with zipfile.ZipFile(REPO / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        z.extractall(tmp_path / "in")
    proc = _run(
        tmp_path, "--file", str(tmp_path / "in" / "wordpress-collection.json"), "--db", "wordpress",
        "--llm-mode", "none", "-y", "--assumptions", _file(tmp_path, {"peak": 2}), "--growth", "3",
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    lines = {x["phase"]: x for x in map(json.loads, proc.stdout.splitlines()) if "phase" in x}
    job = lines["collect"]["job_id"]
    decision = json.loads(
        (tmp_path / "a" / "wordpress" / job / "decisions" / "assumptions.json").read_text()
    )
    assert decision["growth_multiplier"] == 3.0 and decision["peak_to_avg_ratio"] == 2.0
    assert decision["origins"] == {"growth_multiplier": "flag", "peak_to_avg_ratio": "file"}
    assert decision["via"] == "file" and len(decision["file_sha256"]) == 64
    gate = _run(
        tmp_path, "--job-id", job, "--db", "wordpress", "--gate", "--mode", "chat",
        script=REPO / "scripts" / "print_summary.py",
    )  # fmt: skip
    assert gate.returncode == 0, gate.stdout[-2000:] + gate.stderr[-2000:]
    growth, peak = (
        next(x for x in gate.stdout.splitlines() if x.startswith(f"- {name}: "))
        for name in ("Growth", "Peak")
    )
    assert growth.startswith("- Growth: 3×") and "set in the run's options" in growth, growth
    assert peak.startswith("- Peak: 2× peak-to-average ratio"), peak
    assert "set in the assumptions file" in peak, peak
