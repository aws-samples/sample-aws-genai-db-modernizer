"""Every local entry point that takes a job_id must reject a malformed or
path-traversing one before writing any artifact, not only under
MODERNIZER_CI_SANDBOX=1 (#508) -- the job_id counterpart to #420's
test_database_name_validation.py. scripts/run_schema_design.py already had
a narrower, path-containment-only check for --check-costs
(_contained_draft_path); this exercises the general-purpose
src.shared.job_id.validate_job_id check every scripts/run_*.py script (and
scripts/check_decision.py) now runs on a plain, unsandboxed invocation.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own scripts with fixed argv
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# One safe argv per script, with "--job-id", "<value>" always last so a test
# can swap the value in.
_SCRIPT_ARGS: dict[str, list[str]] = {
    "run_triage.py": ["--db", "wordpress"],
    "run_assignment.py": ["--db", "wordpress"],
    "run_reality_check.py": ["--db", "wordpress"],
    "run_synthesis.py": ["--db", "wordpress"],
    "run_report.py": ["--db", "wordpress"],
    "run_analysis.py": ["--db", "wordpress", "--engine", "dynamodb"],
    "run_schema_design.py": ["--db", "wordpress", "--engine", "dynamodb"],
    "run_assessment.py": ["--db", "wordpress"],
    "check_decision.py": ["--db", "wordpress", "--decision", "triage_approval"],
}

_INVALID_JOB_IDS = ["../escape", "..", "a/b", "a b", "", "café"]


def _status_line(stdout: str) -> dict:
    """Return the first JSON stdout line that carries a ``status`` key (see
    test_database_name_validation.py for why: run_assessment.py's compact
    output prints a phase status line, then a trailing log pointer)."""
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "status" in data:
            return data
    raise AssertionError(f"no JSON line with a 'status' key in stdout:\n{stdout}")


def _run(script: str, args: list[str], artifact_root: Path):
    env = {k: v for k, v in os.environ.items() if k != "MODERNIZER_CI_SANDBOX"}
    return subprocess.run(  # nosec B603 -- fixed argv, repo-owned script
        [sys.executable, f"scripts/{script}", *args, "--artifact-root", str(artifact_root)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("script,base_args", list(_SCRIPT_ARGS.items()))
@pytest.mark.parametrize("bad_job_id", _INVALID_JOB_IDS)
def test_rejects_invalid_job_id_outside_sandbox(
    script: str, base_args: list[str], bad_job_id: str, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    proc = _run(script, [*base_args, "--job-id", bad_job_id], artifact_root)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    out = _status_line(proc.stdout)
    assert out["status"] == "error"
    assert "job_id" in out["message"]
    # Nothing should have been written under the artifact root.
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def test_rejects_path_traversal_before_writing_anything(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    proc = _run("run_triage.py", ["--db", "wordpress", "--job-id", "../../outside"], artifact_root)
    assert proc.returncode != 0
    assert not any(tmp_path.rglob("outside"))


def test_run_collect_rejects_bad_explicit_job_id(tmp_path: Path) -> None:
    collector_file = tmp_path / "collection.json"
    collector_file.write_text(json.dumps({"collection_version": "1.0", "queries": []}))
    artifact_root = tmp_path / "artifacts"
    proc = _run(
        "run_collect.py",
        ["--file", str(collector_file), "--job-id", "../escape"],
        artifact_root,
    )
    assert proc.returncode != 0
    out = _status_line(proc.stdout)
    assert out["status"] == "error"
    assert "job_id" in out["message"]
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def test_run_assessment_rejects_bad_explicit_job_id(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    proc = _run(
        "run_assessment.py",
        ["--db", "wordpress", "--job-id", "../escape"],
        artifact_root,
    )
    assert proc.returncode != 0
    out = _status_line(proc.stdout)
    assert out["status"] == "error"
    assert "job_id" in out["message"]
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def test_accepts_valid_job_id(tmp_path: Path) -> None:
    """A valid --job-id is not rejected by this check (it still fails
    later, for the unrelated reason that there's no such job -- proving the
    validator itself let it through)."""
    proc = _run("run_triage.py", ["--db", "wordpress", "--job-id", "j1"], tmp_path)
    out = _status_line(proc.stdout)
    assert "job_id must contain" not in out.get("message", "")


def test_accepts_an_omitted_job_id_for_scripts_where_its_optional(tmp_path: Path) -> None:
    """run_collect.py and run_assessment.py auto-generate a job_id when
    --job-id is omitted; that generated value is always valid. Uses the
    collector-contract-format branch (contract_version "3.0"), which writes
    the input through as-is rather than requiring a fully valid raw
    collection with real tables."""
    collector_file = tmp_path / "collection.json"
    collector_file.write_text(
        json.dumps(
            {
                "contract_version": "3.0",
                "metadata": {"source_database": {"database_name": "wordpress"}},
            }
        )
    )
    artifact_root = tmp_path / "artifacts"
    proc = _run("run_collect.py", ["--file", str(collector_file)], artifact_root)
    out = _status_line(proc.stdout)
    assert "job_id must contain" not in out.get("message", "")


def test_strips_trailing_newline_and_uses_the_stripped_value(tmp_path: Path) -> None:
    """A --job-id with a trailing newline is accepted (stripped), and the
    stripped value -- not the original with the newline -- is what ends up
    in the artifact path. Observed through run_triage.py's "not found"
    message, which embeds the collector path it looked for."""
    artifact_root = tmp_path / "artifacts"
    proc = _run("run_triage.py", ["--db", "wordpress", "--job-id", "j1\n"], artifact_root)
    out = _status_line(proc.stdout)
    assert "job_id must contain" not in out.get("message", "")
    assert "wordpress/j1/collector/output.json" in out["message"]
    assert "j1\n" not in out["message"]
