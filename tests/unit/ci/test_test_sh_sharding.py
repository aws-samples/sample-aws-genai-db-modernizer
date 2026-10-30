"""ci/test.sh's coverage tracer and the test-shard matrix (#1718)."""

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
TEST_SH = REPO / "ci" / "test.sh"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"


def _run(tmp_path: Path, *args: str, env_extra: dict[str, str] | None = None) -> str:
    """Run ci/test.sh with a stub `uv` that prints the tracer pytest would get."""
    stub = tmp_path / "uv"
    stub.write_text('#!/usr/bin/env bash\necho "COVERAGE_CORE=${COVERAGE_CORE-<unset>}"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {k: v for k, v in os.environ.items() if k != "COVERAGE_CORE"}
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env.update(env_extra or {})
    out = subprocess.run(
        ["bash", str(TEST_SH), *args], cwd=REPO, env=env, capture_output=True, text=True, check=True
    )
    return out.stdout


def test_a_coverage_run_uses_the_sysmon_tracer(tmp_path: Path) -> None:
    assert "COVERAGE_CORE=sysmon" in _run(tmp_path, "--cov=src", "--cov-report=")


def test_a_bare_cov_flag_uses_the_sysmon_tracer(tmp_path: Path) -> None:
    assert "COVERAGE_CORE=sysmon" in _run(tmp_path, "--cov")


@pytest.mark.passes_on_main(reason="main never sets a tracer; this guards that only --cov does")
def test_a_run_without_coverage_leaves_the_tracer_alone(tmp_path: Path) -> None:
    assert "COVERAGE_CORE=<unset>" in _run(tmp_path, "-q")


@pytest.mark.passes_on_main(
    reason="main passes a preset tracer through; this keeps the override working"
)
def test_an_explicit_tracer_wins(tmp_path: Path) -> None:
    out = _run(tmp_path, "--cov=src", env_extra={"COVERAGE_CORE": "ctrace"})
    assert "COVERAGE_CORE=ctrace" in out


@pytest.mark.passes_on_main(
    reason="main's four shards agree; this keeps the matrix and TEST_SHARD in step"
)
def test_the_shard_matrix_and_the_shard_total_agree() -> None:
    job = yaml.safe_load(CI_YML.read_text())["jobs"]["test-shard"]
    shards = job["strategy"]["matrix"]["shard"]
    run_env = next(s["env"] for s in job["steps"] if s.get("name") == "Run tests")
    total = run_env["TEST_SHARD"].split("/")[1]
    assert shards == list(range(1, len(shards) + 1))
    assert total == str(len(shards))
    assert job["name"].endswith(f"/{len(shards)})")


@pytest.mark.passes_on_main(reason="main never sets a tracer; a report flag alone is not coverage")
def test_a_report_flag_without_cov_leaves_the_tracer_alone(tmp_path: Path) -> None:
    assert "COVERAGE_CORE=<unset>" in _run(tmp_path, "--cov-report=")
