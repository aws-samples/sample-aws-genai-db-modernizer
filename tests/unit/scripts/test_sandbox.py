"""scripts/_sandbox.py: argument containment for headless CI runs.

Under MODERNIZER_CI_SANDBOX=1 (exported by ci/e2e-llm.sh) the allowlisted
scripts are the only Bash commands a headless /modernize run can execute, so
their arguments are the remaining way to reach outside the repo. The helper
rejects paths that resolve outside the repo root and job/db names that aren't
a single safe path component.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 -- runs this repo's own script with fixed argv
import sys
from pathlib import Path

import pytest

from scripts import _sandbox

REPO_ROOT = Path(__file__).resolve().parents[3]
ON = {"MODERNIZER_CI_SANDBOX": "1"}


def _ns(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def test_inactive_without_env_var() -> None:
    args = _ns(file="/etc/passwd", artifact_root="/", db="../x", job_id="a/b")
    assert _sandbox.sandbox_violation(args, environ={}) is None
    assert _sandbox.sandbox_violation(args, environ={"MODERNIZER_CI_SANDBOX": "0"}) is None


def test_accepts_in_repo_paths_and_safe_names() -> None:
    args = _ns(
        file=str(REPO_ROOT / "test-results" / "x.json"),
        artifact_root="./artifacts",
        db="wordpress",
        job_id="job_1.2-abc",
    )
    assert _sandbox.sandbox_violation(args, environ=ON) is None


def test_missing_and_none_attributes_are_ignored() -> None:
    assert _sandbox.sandbox_violation(_ns(), environ=ON) is None
    assert _sandbox.sandbox_violation(_ns(file=None, db=None), environ=ON) is None


@pytest.mark.parametrize(
    "attr,value",
    [
        ("file", "/etc/passwd"),
        ("file", "../outside.json"),
        ("artifact_root", "/tmp"),
        ("artifact_root", "./artifacts/../../elsewhere"),
    ],
)
def test_rejects_paths_outside_repo(attr: str, value: str) -> None:
    message = _sandbox.sandbox_violation(_ns(**{attr: value}), environ=ON)
    assert message is not None
    assert "--" + attr.replace("_", "-") in message
    assert "outside the repository root" in message


def test_rejects_symlink_escaping_repo(tmp_path: Path) -> None:
    link = REPO_ROOT / "test-results" / "_sandbox_escape_link"
    link.parent.mkdir(exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(tmp_path, target_is_directory=True)
    try:
        assert _sandbox.sandbox_violation(_ns(artifact_root=str(link)), environ=ON) is not None
    finally:
        link.unlink()


def test_unresolvable_paths_are_refused_not_raised(tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    for value in (str(loop), "x\0y"):
        message = _sandbox.sandbox_violation(_ns(file=value), environ=ON)
        assert message is not None and "--file" in message
    # An over-long in-repo name resolves (non-strict) and is not an escape.
    assert _sandbox.sandbox_violation(_ns(file="a" * 5000), environ=ON) is None


@pytest.mark.parametrize("value", ["../x", "a/b", "a b", "", ".", "..", "x;rm", "é"])
@pytest.mark.parametrize("attr", ["db", "job_id"])
def test_rejects_unsafe_names(attr: str, value: str) -> None:
    message = _sandbox.sandbox_violation(_ns(**{attr: value}), environ=ON)
    assert message is not None
    assert "--" + attr.replace("_", "-") in message


def test_extra_name_args_are_checked() -> None:
    args = _ns(decision="../../etc/x")
    assert _sandbox.sandbox_violation(args, environ=ON) is None
    assert (
        _sandbox.sandbox_violation(args, environ=ON, name_args=("db", "job_id", "decision"))
        is not None
    )


def test_run_triage_rejects_escape_with_its_normal_json_error() -> None:
    proc = subprocess.run(  # nosec B603 -- fixed argv
        [
            sys.executable,
            "scripts/run_triage.py",
            "--job-id",
            "job1",
            "--db",
            "wordpress",
            "--artifact-root",
            "/tmp",
        ],
        cwd=REPO_ROOT,
        env={**_sandbox_env(), **ON},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "--artifact-root" in out["message"]


def _sandbox_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "MODERNIZER_CI_SANDBOX"}
