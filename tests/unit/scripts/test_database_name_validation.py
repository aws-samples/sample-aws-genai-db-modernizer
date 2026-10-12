"""Every local entry point that takes a database name must reject a
malformed or path-traversing one before writing any artifact, not only
under MODERNIZER_CI_SANDBOX=1 (#420). scripts/_sandbox.py already covers the
headless-CI case (tests/unit/scripts/test_sandbox.py); this file exercises
the general-purpose src.shared.database_name.validate_database_name check
each scripts/run_*.py script now runs on a plain, unsandboxed invocation.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own scripts with fixed argv
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# One safe argv per script, with "--db", "<value>" always last so a test can
# swap the value in.
_SCRIPT_ARGS: dict[str, list[str]] = {
    "run_triage.py": ["--job-id", "j1"],
    "run_assignment.py": ["--job-id", "j1"],
    "run_reality_check.py": ["--job-id", "j1"],
    "run_synthesis.py": ["--job-id", "j1"],
    "run_report.py": ["--job-id", "j1"],
    "run_analysis.py": ["--job-id", "j1", "--engine", "dynamodb"],
    "run_schema_design.py": ["--job-id", "j1", "--engine", "dynamodb"],
    "run_assessment.py": ["--job-id", "j1"],
}

_INVALID_NAMES = ["../escape", "..", "a/b", "a b", "", "café"]


def _status_line(stdout: str) -> dict:
    """Return the first JSON stdout line that carries a ``status`` key.

    Most scripts print exactly one JSON line. run_assessment.py's compact
    output (stdout isn't a terminal under subprocess) prints the phase status
    line and then a trailing ``{"log": ..., "output_tail": ...}`` pointer, so
    this skips straight to the status line."""
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
    """Run ``script`` with ``args`` plus an explicit ``--artifact-root`` under
    ``artifact_root`` (every script accepts the flag; not all of them also
    read $ARTIFACT_DIR, so the flag is what keeps this test off the real repo
    tree)."""
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
@pytest.mark.parametrize("bad_name", _INVALID_NAMES)
def test_rejects_invalid_db_outside_sandbox(
    script: str, base_args: list[str], bad_name: str, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    proc = _run(script, [*base_args, "--db", bad_name], artifact_root)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    out = _status_line(proc.stdout)
    assert out["status"] == "error"
    assert "database_name" in out["message"]
    # Nothing should have been written under the artifact root.
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def test_rejects_path_traversal_before_writing_anything(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    proc = _run("run_triage.py", ["--job-id", "j1", "--db", "../../outside"], artifact_root)
    assert proc.returncode != 0
    assert not any(tmp_path.rglob("outside"))


def test_run_collect_rejects_bad_explicit_db(tmp_path: Path) -> None:
    collector_file = tmp_path / "collection.json"
    collector_file.write_text(json.dumps({"collection_version": "1.0", "queries": []}))
    artifact_root = tmp_path / "artifacts"
    proc = _run(
        "run_collect.py",
        ["--file", str(collector_file), "--db", "../escape"],
        artifact_root,
    )
    assert proc.returncode != 0
    out = _status_line(proc.stdout)
    assert out["status"] == "error"
    assert "database_name" in out["message"]
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def test_run_collect_rejects_bad_derived_db(tmp_path: Path) -> None:
    """No --db given: the name is derived from the collector file's own
    metadata, which is untrusted input and must pass the same check. The
    message must say how to recover (pass --db) rather than just reject."""
    collector_file = tmp_path / "collection.json"
    collector_file.write_text(
        json.dumps(
            {
                "collection_version": "1.0",
                "metadata": {"database_name": "../evil"},
                "queries": [],
            }
        )
    )
    artifact_root = tmp_path / "artifacts"
    proc = _run("run_collect.py", ["--file", str(collector_file)], artifact_root)
    assert proc.returncode != 0
    out = _status_line(proc.stdout)
    assert out["status"] == "error"
    assert "metadata.database_name" in out["message"]
    assert "--db" in out["message"]
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def test_run_collect_rejects_bad_derived_db_in_contract_format(tmp_path: Path) -> None:
    """Same derivation, but through the "is_contract" branch (a file that is
    already in collector contract format), where the name comes from
    metadata.source_database.database_name instead."""
    collector_file = tmp_path / "collection.json"
    collector_file.write_text(
        json.dumps(
            {
                "contract_version": "3.0",
                "metadata": {"source_database": {"database_name": "../evil"}},
            }
        )
    )
    artifact_root = tmp_path / "artifacts"
    proc = _run("run_collect.py", ["--file", str(collector_file)], artifact_root)
    assert proc.returncode != 0
    out = _status_line(proc.stdout)
    assert out["status"] == "error"
    assert "metadata.database_name" in out["message"]
    assert "--db" in out["message"]
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def test_run_assessment_rejects_bad_derived_db(tmp_path: Path) -> None:
    """Same derivation case as run_collect.py, through run_assessment.py's
    phase_collect (triggered by the "is_raw" branch, --file with no --db)."""
    collector_file = tmp_path / "collection.json"
    collector_file.write_text(
        json.dumps(
            {
                "collection_version": "1.0",
                "metadata": {"database_name": "../evil"},
                "queries": [],
            }
        )
    )
    artifact_root = tmp_path / "artifacts"
    proc = _run("run_assessment.py", ["--file", str(collector_file)], artifact_root)
    assert proc.returncode != 0
    out = _status_line(proc.stdout)
    assert out["status"] == "error"
    assert "metadata.database_name" in out["message"]
    assert "--db" in out["message"]
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def test_run_assessment_rejects_bad_derived_db_in_contract_format(tmp_path: Path) -> None:
    """Same derivation, but through phase_collect's "is_contract" branch."""
    collector_file = tmp_path / "collection.json"
    collector_file.write_text(
        json.dumps(
            {
                "contract_version": "3.0",
                "metadata": {"source_database": {"database_name": "../evil"}},
            }
        )
    )
    artifact_root = tmp_path / "artifacts"
    proc = _run("run_assessment.py", ["--file", str(collector_file)], artifact_root)
    assert proc.returncode != 0
    out = _status_line(proc.stdout)
    assert out["status"] == "error"
    assert "metadata.database_name" in out["message"]
    assert "--db" in out["message"]
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def test_accepts_valid_db(tmp_path: Path) -> None:
    """A valid --db is not rejected by the new check (it still fails later,
    for the unrelated reason that there's no such job -- proving the
    validator itself let it through)."""
    proc = _run("run_triage.py", ["--job-id", "j1", "--db", "wordpress"], tmp_path)
    out = _status_line(proc.stdout)
    assert "database_name must contain" not in out.get("message", "")


def test_strips_trailing_newline_and_uses_the_stripped_value(tmp_path: Path) -> None:
    """A --db with a trailing newline is accepted (stripped), and the
    stripped value -- not the original with the newline -- is what ends up
    in the artifact path. Observed through run_triage.py's "not found"
    message, which embeds the (job-less) collector path it looked for."""
    artifact_root = tmp_path / "artifacts"
    proc = _run("run_triage.py", ["--job-id", "j1", "--db", "abc\n"], artifact_root)
    out = _status_line(proc.stdout)
    assert "database_name must contain" not in out.get("message", "")
    assert "abc/j1/collector/output.json" in out["message"]
    assert "abc\n" not in out["message"]
