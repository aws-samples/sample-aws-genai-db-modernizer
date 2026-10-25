"""The standalone commands set, show and hold the customer's engine scope (#1574, item 6).

``/modernize`` passes ``--target-databases`` to ``run_assessment.py``; the standalone
path had no way to give a scope (``/collect`` then ``/triage``), ``/triage`` did not show
the engines the scope skipped, and its "modify the selection" gate let an engine outside
the scope reach ``/analyze``. ``run_triage.py --target-databases`` records the scope the
way ``run_assessment.py`` does and prints it with each skipped engine's reason, and
``run_analysis.py`` refuses an engine outside the recorded scope.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own scripts with fixed argv
import sys
from pathlib import Path

import pytest

from src.storage.local_store import LocalArtifactStore
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

REPO_ROOT = Path(__file__).resolve().parents[3]
DB, JOB = "ecommerce", "job-1574"


def _script(name: str, root: Path, *args: str) -> tuple[int, dict]:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / name),
            "--job-id",
            JOB,
            "--db",
            DB,
            "--artifact-root",
            str(root),
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "MODERNIZER_CI_SANDBOX"},
        timeout=120,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    return proc.returncode, json.loads(lines[-1]) if lines else {"stdout": proc.stdout}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", get_ecommerce_collector_output())
    return tmp_path


def test_triage_records_and_prints_the_scope_with_its_skips(root: Path) -> None:
    code, line = _script("run_triage.py", root, "--target-databases", "dynamodb,aurora_mysql")
    assert code == 0, line
    assert line["engine_scope"] == ["aurora_mysql", "dynamodb"]
    assert set(line["selected"]) <= {"aurora_mysql", "dynamodb"}
    reasons = {s["engine"]: s["reason"] for s in line["skipped_reasons"]}
    assert reasons["opensearch"] == "Not in the customer's engine scope"
    assert [s["engine"] for s in line["skipped_reasons"]] == line["skipped"]
    decision = json.loads((root / DB / JOB / "decisions" / "engine_scope.json").read_text())
    assert decision["engines"] == ["aurora_mysql", "dynamodb"]
    assert decision["via"] == "cli"


def test_triage_refuses_a_scope_the_setting_refuses(root: Path) -> None:
    code, line = _script("run_triage.py", root, "--target-databases", "neptune")
    assert code == 1
    assert line["status"] == "error" and "--target-databases" in line["message"]
    assert not (root / DB / JOB / "decisions" / "engine_scope.json").exists()


def test_analysis_refuses_an_engine_outside_the_recorded_scope(root: Path) -> None:
    assert _script("run_triage.py", root, "--target-databases", "dynamodb,aurora_mysql")[0] == 0
    code, line = _script("run_analysis.py", root, "--engine", "opensearch", "--llm-mode", "none")
    assert code == 1
    assert line["status"] == "error"
    assert "outside the customer's engine scope" in line["message"]
    assert not (root / DB / JOB / "analysis-opensearch").exists()
    # An engine inside the scope still runs.
    code, line = _script("run_analysis.py", root, "--engine", "dynamodb", "--llm-mode", "none")
    assert code == 0, line
