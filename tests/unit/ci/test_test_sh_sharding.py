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


SDK_REQUIREMENTS = REPO / "src" / "atx_orchestrator" / "requirements-sdk.txt"


def _sdk_modules() -> list[str]:
    """The import names of the pinned SDK packages: agent-builder-X-aws-transform -> agent_builder_X."""
    import re

    return [
        m.group(1).replace("-", "_")
        for line in SDK_REQUIREMENTS.read_text().splitlines()
        if (m := re.match(r"(agent-builder-[a-z-]+)-aws-transform==", line))
    ]


def _run_with_stub_uv(tmp_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    """Run ci/test.sh with a stub `uv`.

    mode "ok": every check passes. "uv_broken": `uv run python -c pass` fails with a
    message. "sdk_missing": the import check fails with an ImportError line. The
    stub records the code of each `python -c` call in $tmp/calls.
    """
    stub = tmp_path / "uv"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1 $2 $3" = "run python -c" ]; then\n'
        f'  echo "$4" >> "{tmp_path}/calls"\n'
        '  if [ "$4" = "pass" ] && [ "' + mode + '" = "uv_broken" ]; then\n'
        '    echo "first line" >&2; echo "No interpreter found for Python 3.99" >&2; exit 2\n'
        "  fi\n"
        '  if [ "$4" != "pass" ] && [ "' + mode + '" = "sdk_missing" ]; then\n'
        '    echo "Traceback" >&2; echo "ModuleNotFoundError: No module named agent_builder_x" >&2; exit 1\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'echo "PYTEST_REACHED"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(TEST_SH), "-q"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _error_lines(out: subprocess.CompletedProcess[str]) -> list[str]:
    return [ln for ln in out.stderr.splitlines() if ln.startswith("error:")]


def test_a_missing_sdk_stops_the_run_with_one_actionable_line(tmp_path: Path) -> None:
    """Without the AWS Transform SDK, dozens of import errors hide real failures (#1848)."""
    out = _run_with_stub_uv(tmp_path, "sdk_missing")
    assert out.returncode == 2
    assert "PYTEST_REACHED" not in out.stdout
    errors = _error_lines(out)
    assert len(errors) == 1
    assert "uv pip install -r src/atx_orchestrator/requirements-sdk.txt" in errors[0]
    assert "after any uv sync" in errors[0]
    # The real import error is shown, not swallowed.
    assert "ModuleNotFoundError: No module named agent_builder_x" in errors[0]
    assert "uv could not run" not in errors[0]


def test_a_uv_failure_is_reported_as_uv_not_as_a_missing_sdk(tmp_path: Path) -> None:
    out = _run_with_stub_uv(tmp_path, "uv_broken")
    assert out.returncode == 2
    assert "PYTEST_REACHED" not in out.stdout
    errors = _error_lines(out)
    assert len(errors) == 1
    assert "uv could not run" in errors[0]
    assert "No interpreter found for Python 3.99" in errors[0]
    assert "not fully installed" not in errors[0]
    # The SDK import is never tried once uv itself cannot run.
    assert (tmp_path / "calls").read_text().split() == ["pass"]


@pytest.mark.passes_on_main(reason="main always runs pytest; this keeps the installed case working")
def test_an_installed_sdk_lets_the_run_reach_pytest(tmp_path: Path) -> None:
    out = _run_with_stub_uv(tmp_path, "ok")
    assert out.returncode == 0
    assert "PYTEST_REACHED" in out.stdout


def test_the_sdk_check_imports_every_pinned_package(tmp_path: Path) -> None:
    """The checked modules come from requirements-sdk.txt, including the agentic MCP one."""
    modules = _sdk_modules()
    assert "agent_builder_agentic_mcp" in modules  # test_iam_templates.py needs it
    assert len(modules) == 4
    _run_with_stub_uv(tmp_path, "ok")
    import_code = [c for c in (tmp_path / "calls").read_text().splitlines() if c != "pass"]
    assert import_code == ["import " + ", ".join(modules)]


def test_the_sdk_check_follows_the_requirements_file(tmp_path: Path) -> None:
    """A pin added to requirements-sdk.txt is checked without editing ci/test.sh."""
    import shutil

    repo = tmp_path / "repo"
    (repo / "ci").mkdir(parents=True)
    (repo / "src" / "atx_orchestrator").mkdir(parents=True)
    for name in ("test.sh", "lib.sh"):
        shutil.copy(REPO / "ci" / name, repo / "ci" / name)
    (repo / SDK_REQUIREMENTS.relative_to(REPO)).write_text(
        SDK_REQUIREMENTS.read_text() + "agent-builder-newthing-aws-transform==1.0.0\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(f'#!/usr/bin/env bash\necho "$4" >> "{tmp_path}/calls"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    subprocess.run(
        ["bash", str(repo / "ci" / "test.sh"), "-q"], cwd=repo, env=env, check=True, timeout=60
    )
    import_code = (tmp_path / "calls").read_text().splitlines()[1]
    assert import_code.endswith(", agent_builder_newthing")


def test_the_dev_setup_script_installs_the_sdk_requirements() -> None:
    setup = (REPO / "scripts" / "setup_dev.sh").read_text()
    assert "uv pip install -r src/atx_orchestrator/requirements-sdk.txt" in setup
    assert setup.index("uv sync --extra dev") < setup.index("requirements-sdk.txt")


def test_the_setup_verify_step_runs_the_same_command_the_docs_require() -> None:
    setup = (REPO / "scripts" / "setup_dev.sh").read_text()
    assert "./ci/test.sh -q" in setup
    assert "uv run pytest tests/ -v" not in setup
