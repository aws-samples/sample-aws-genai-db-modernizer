# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
        ["bash", str(TEST_SH), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
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


def _pytest_argv(tmp_path: Path, *args: str) -> list[str]:
    """The arguments ci/test.sh hands to `uv run pytest`, read from a stub `uv`."""
    stub = tmp_path / "uv"
    stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    out = subprocess.run(
        ["bash", str(TEST_SH), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return out.stdout.splitlines()


def _timeout_flag(argv: list[str]) -> int:
    return int(next(a for a in argv if a.startswith("--timeout=")).split("=", 1)[1])


def test_every_run_has_a_per_test_time_limit(tmp_path: Path) -> None:
    """A hung test fails under its own name instead of stalling the job (#1835)."""
    argv = _pytest_argv(tmp_path, "-q")
    assert _timeout_flag(argv) == 300
    # The thread method also stops a test stuck in native code or a blocking
    # call, where a SIGALRM handler would never run, and prints every stack.
    assert "--timeout-method=thread" in argv


def test_a_caller_can_still_override_the_time_limit(tmp_path: Path) -> None:
    """The caller's arguments come last, and pytest keeps the last value."""
    argv = _pytest_argv(tmp_path, "--timeout=0")
    assert argv.index("--timeout=0") > argv.index("--timeout=300")


def test_the_time_limit_leaves_room_for_the_slowest_recorded_test(tmp_path: Path) -> None:
    """Five times the slowest test in .test_durations, so a slow runner or the
    coverage tracer cannot trip it, while a hang still fails in minutes."""
    import json

    slowest = max(json.loads((REPO / ".test_durations").read_text()).values())
    assert _timeout_flag(_pytest_argv(tmp_path)) >= 5 * slowest


def test_the_time_limit_plugin_is_a_dev_dependency() -> None:
    import tomllib

    dev = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    assert any(req.startswith("pytest-timeout") for req in dev["dev"])
