"""``--artifact-root`` falls back to ``ARTIFACT_DIR`` (#395).

Every script that writes local artifacts hard-coded ``default="./artifacts"``
for ``--artifact-root``, so ``ARTIFACT_DIR=/tmp/x run_assessment.py ...``
still wrote under ``./artifacts`` in the current directory even though
``src/storage/__init__.py`` (the local API) honors ``ARTIFACT_DIR``. The CLI
and the UI could then disagree about where a job lives.

The fix: ``default=os.environ.get("ARTIFACT_DIR", "./artifacts")``, with an
explicit ``--artifact-root`` still winning. ``run_assessment.py`` is checked
by actually constructing its ``LocalArtifactStore``; the other four scripts
(``run_collect.py``, ``run_schema_design.py``, ``run_report.py``,
``run_reality_check.py``) had the identical hard-coded default and are
checked the cheap way: under the CI sandbox (``scripts/_sandbox.py``) a
resolved ``--artifact-root`` outside the repo is rejected right after
argument parsing, before any real work, so the rejection message's path
proves which default the parser actually used.
"""

from __future__ import annotations

import json
import subprocess  # nosec B603 B404 -- fixed interpreter plus this repo's own script args
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


class _StopAfterTriage(Exception):
    pass


def test_run_assessment_artifact_root_defaults_to_artifact_dir_env(monkeypatch, tmp_path):
    from scripts import run_assessment
    from src.storage import local_store as local_store_module

    monkeypatch.chdir(tmp_path)
    env_root = tmp_path / "env-artifacts"
    monkeypatch.setenv("ARTIFACT_DIR", str(env_root))

    captured: dict[str, str] = {}
    orig_init = local_store_module.LocalArtifactStore.__init__

    def spy_init(self, base_dir, *args, **kwargs):
        captured["base_dir"] = base_dir
        orig_init(self, base_dir, *args, **kwargs)

    monkeypatch.setattr(local_store_module.LocalArtifactStore, "__init__", spy_init)
    monkeypatch.setattr(
        run_assessment, "phase_collect", lambda f, db, store: ("job-1", "wordpress")
    )

    def stop(*args, **kwargs):
        raise _StopAfterTriage

    monkeypatch.setattr(run_assessment, "phase_triage", stop)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_assessment.py", "--file", "collector.json", "--db", "wordpress"],
    )

    with pytest.raises(_StopAfterTriage):
        run_assessment.main()

    assert captured["base_dir"] == str(env_root)


def test_run_assessment_explicit_artifact_root_still_wins(monkeypatch, tmp_path):
    from scripts import run_assessment
    from src.storage import local_store as local_store_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "env-artifacts"))
    explicit_root = tmp_path / "explicit-artifacts"

    captured: dict[str, str] = {}
    orig_init = local_store_module.LocalArtifactStore.__init__

    def spy_init(self, base_dir, *args, **kwargs):
        captured["base_dir"] = base_dir
        orig_init(self, base_dir, *args, **kwargs)

    monkeypatch.setattr(local_store_module.LocalArtifactStore, "__init__", spy_init)
    monkeypatch.setattr(
        run_assessment, "phase_collect", lambda f, db, store: ("job-1", "wordpress")
    )

    def stop(*args, **kwargs):
        raise _StopAfterTriage

    monkeypatch.setattr(run_assessment, "phase_triage", stop)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_assessment.py",
            "--file",
            "collector.json",
            "--db",
            "wordpress",
            "--artifact-root",
            str(explicit_root),
        ],
    )

    with pytest.raises(_StopAfterTriage):
        run_assessment.main()

    assert captured["base_dir"] == str(explicit_root)


@pytest.mark.parametrize(
    "script_args",
    [
        ("run_collect.py", ["--file", "pyproject.toml", "--db", "testdb", "--job-id", "job1"]),
        (
            "run_schema_design.py",
            ["--job-id", "job1", "--db", "testdb", "--engine", "dynamodb"],
        ),
        ("run_report.py", ["--job-id", "job1", "--db", "testdb"]),
        ("run_reality_check.py", ["--job-id", "job1", "--db", "testdb"]),
    ],
    ids=["run_collect", "run_schema_design", "run_report", "run_reality_check"],
)
def test_other_scripts_also_default_artifact_root_to_artifact_dir_env(script_args, tmp_path):
    script, args = script_args
    outside_root = tmp_path / "outside-repo-artifacts"
    out = subprocess.run(  # nosec B603 -- fixed interpreter, this repo's own script, no shell
        [sys.executable, f"scripts/{script}", *args],
        cwd=REPO,
        env={
            "PATH": "/usr/bin:/bin",
            "MODERNIZER_CI_SANDBOX": "1",
            "ARTIFACT_DIR": str(outside_root),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 1, out.stdout + out.stderr
    message = json.loads(out.stdout.strip().splitlines()[-1])["message"]
    assert f"--artifact-root {str(outside_root)!r} resolves outside the repository root" in message
