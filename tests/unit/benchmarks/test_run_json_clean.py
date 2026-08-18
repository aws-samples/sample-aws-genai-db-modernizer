"""The --json runner output on stdout must be valid, parseable JSON only.

Regression guard: the assignment agent prints '[assignment] ...' lines; those
must not leak onto stdout in --json mode and corrupt machine-readable output.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_json_stdout_is_pure_json():
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.runner.run", "--stage", "assignment", "--json"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    # stdout must parse as JSON with the expected report keys — no log-line prefix.
    report = json.loads(proc.stdout)
    assert "complete" in report
    assert "aggregate_acceptable_accuracy" in report
    assert "per_case" in report
