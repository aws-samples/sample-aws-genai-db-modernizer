"""The collect phase's status line reports the table-reference parser (#553).

The facade's regex fallbacks and the regex cross-check are counted on the one
JSON status line the collect phase already prints, so they reach stdout (and,
for run_assessment.py, ``_logs/run_assessment.log``: see
``test_run_assessment_output.py``) without a log handler.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own scripts with fixed argv
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

# discourse on sqlglot 30.22.0: 13 fallbacks, 33 disagreements with the regex.
DISCOURSE_TABLE_REFS = {
    "regex_fallback": 13,
    "reasons": {"command": 11, "not_a_statement": 1, "parse_error": 1},
    "agree": 1621,
    "disagree": 33,
}


def _unzip(name: str, dest: Path) -> Path:
    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        z.extractall(dest)
    return dest / f"{name}-collection.json"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("MODERNIZER_CI_SANDBOX", None)
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    return env


def _lines(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_run_collect_status_line_has_the_table_refs_summary(tmp_path: Path) -> None:
    src = _unzip("discourse", tmp_path / "in")
    out = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [
            sys.executable,
            "scripts/run_collect.py",
            "--file",
            str(src),
            "--db",
            "discourse",
            "--job-id",
            "refs0001",
            "--artifact-root",
            str(tmp_path / "artifacts"),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        env=_env(),
    )
    status = _lines(out.stdout)[-1]
    assert status["status"] == "complete"
    assert status["table_refs"] == DISCOURSE_TABLE_REFS
