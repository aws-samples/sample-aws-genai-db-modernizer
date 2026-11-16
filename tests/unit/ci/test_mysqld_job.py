# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CI runs the mysqld integration tests for step 1's routine scripts (#2274).

``ci/test.sh`` deselects ``-m integration``, so the tests that run the generated MySQL
scripts on a real server need an entry point and a job of their own. These guards keep
the two in step: the script lists exactly the files that need a server, runs them under
``--fail-on-skip``, pins the server; the GitHub job calls the script and nothing else.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ci" / "test-mysqld.sh"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
TEST_SH = REPO / "ci" / "test.sh"


def _code(text: str) -> list[str]:
    """The commands of a shell script: comments dropped, backslash continuations joined."""
    out: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
            continue
        out.append((pending + line).strip())
        pending = ""
    return [c for c in out if c]


def _pytest_line(text: str, start: str) -> str:
    lines = [c for c in _code(text) if c.startswith(start)]
    assert len(lines) == 1, f"expected one `{start}` command, found {lines}"
    return lines[0]


def _files(text: str | None = None) -> list[str]:
    block = re.search(r"MYSQLD_TEST_FILES=\((.*?)\)", text or SCRIPT.read_text(), re.S)
    assert block, "ci/test-mysqld.sh must list its test files in MYSQLD_TEST_FILES=(...)"
    return block.group(1).split()


def script_problems(text: str) -> list[str]:
    """What is wrong with ci/test-mysqld.sh's commands (comments and log lines do not count)."""
    problems = []
    code = _code(text)
    cmd = next((c for c in code if c.startswith("uv run pytest ")), "")
    if not re.match(r"uv run pytest -m integration --fail-on-skip\b", cmd):
        problems.append("the pytest command must start `-m integration --fail-on-skip`")
    if "MYSQLD_TEST_FILES[@]" not in cmd:
        problems.append("the pytest command must run MYSQLD_TEST_FILES")
    url = next((c for c in code if c.startswith("MYSQL_URL=")), "")
    if not url.startswith('MYSQL_URL="https://cdn.mysql.com/'):
        problems.append("MYSQL_URL must be https from cdn.mysql.com")
    if not any(re.fullmatch(r'MYSQL_VERSION="8\.0\.\d+"', c) for c in code):
        problems.append("MYSQL_VERSION must be pinned to an exact 8.0.x")
    for name in ("MYSQL_SHA256", "MYSQLD_BIN_SHA256", "MYSQL_BIN_SHA256"):
        if not any(re.fullmatch(rf'{name}="[0-9a-f]{{64}}"', c) for c in code):
            problems.append(f"{name} must be a SHA-256")
    check = next(
        (
            n
            for n, c in enumerate(code)
            if re.fullmatch(r'if \[ "\$\(sha256_of "\$archive"\)" != "\$MYSQL_SHA256" \]; then', c)
        ),
        None,
    )
    untar = next((n for n, c in enumerate(code) if c.startswith("tar -x")), None)
    if check is None or untar is None or check > untar:
        problems.append("the archive's SHA-256 must be compared before it is unpacked")
    elif code[check + 1 : check + 4].count("exit 2") != 1:
        problems.append("a checksum mismatch must exit")
    if not any("binaries_ok" in c and c.startswith("if ! binaries_ok") for c in code):
        problems.append("the unpacked binaries must be checked on every run")
    return problems


def _shell_function(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.M | re.S)
    assert match, f"{name}() not found in ci/test-mysqld.sh"
    return match.group(0)


def _binaries_ok_exit(text: str, tmp_path: Path, mysqld_sha: str, mysql_sha: str) -> int:
    """Run the script's own binaries_ok() on a folder holding two stand-in binaries."""
    for name in ("mysqld", "mysql"):
        binary = tmp_path / "bin" / name
        binary.parent.mkdir(exist_ok=True)
        binary.write_text(f"stand-in {name}\n")
        binary.chmod(0o755)
    shell = "\n".join(
        [
            _shell_function(text, "sha256_of"),
            _shell_function(text, "binaries_ok"),
            f'MYSQLD_BIN_SHA256="{mysqld_sha}"',
            f'MYSQL_BIN_SHA256="{mysql_sha}"',
            f'binaries_ok "{tmp_path}"',
        ]
    )
    return subprocess.run(["bash", "-c", shell], capture_output=True).returncode


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _binaries_cases(text: str, tmp_path: Path) -> dict[str, int]:
    good_d, good_c = _sha("stand-in mysqld\n"), _sha("stand-in mysql\n")
    return {
        "both match": _binaries_ok_exit(text, tmp_path, good_d, good_c),
        "mysqld differs": _binaries_ok_exit(text, tmp_path, "0" * 64, good_c),
        "mysql differs": _binaries_ok_exit(text, tmp_path, good_d, "0" * 64),
    }


def test_binaries_ok_accepts_the_pinned_binaries_and_rejects_any_other(tmp_path: Path) -> None:
    cases = _binaries_cases(SCRIPT.read_text(), tmp_path)
    assert cases == {"both match": 0, "mysqld differs": 1, "mysql differs": 1}


def test_a_binaries_ok_that_accepts_anything_fails_the_behavioural_guard(tmp_path: Path) -> None:
    text = SCRIPT.read_text()
    body = _shell_function(text, "binaries_ok")
    mutated = text.replace(body, "binaries_ok() {\n  true\n}\n")
    assert mutated != text
    assert _binaries_cases(mutated, tmp_path)["mysqld differs"] == 0


def test_the_script_passes_its_own_guards() -> None:
    assert script_problems(SCRIPT.read_text()) == []


def _mutations() -> dict[str, Callable[[str], str]]:
    return {
        "no -m integration --fail-on-skip": lambda t: t.replace(
            "uv run pytest -m integration --fail-on-skip", "uv run pytest"
        ),
        "no --fail-on-skip": lambda t: t.replace(
            "uv run pytest -m integration --fail-on-skip", "uv run pytest -m integration"
        ),
        "checksum never fails": lambda t: t.replace(
            '"$(sha256_of "$archive")" != "$MYSQL_SHA256"', "false"
        ),
        "checksum does not exit": lambda t: t.replace(
            'does not match the pinned SHA-256 for MySQL $MYSQL_VERSION" >&2\n      exit 2',
            'does not match" >&2',
        ),
        "http url": lambda t: t.replace('MYSQL_URL="https://', 'MYSQL_URL="http://'),
        "other host": lambda t: t.replace("https://cdn.mysql.com/", "https://example.org/"),
        "unpinned version": lambda t: t.replace('MYSQL_VERSION="8.0.45"', 'MYSQL_VERSION="8.0"'),
        "binaries not rechecked": lambda t: t.replace("if ! binaries_ok", "if false"),
    }


@pytest.mark.parametrize("name", sorted(_mutations()))
def test_each_script_guard_fails_when_its_property_is_removed(name: str) -> None:
    mutated = _mutations()[name](SCRIPT.read_text())
    assert mutated != SCRIPT.read_text(), "the mutation must change the script"
    assert script_problems(mutated), name


def test_a_comment_does_not_satisfy_a_guard() -> None:
    text = SCRIPT.read_text().replace(
        "uv run pytest -m integration --fail-on-skip",
        "uv run pytest # -m integration --fail-on-skip",
    )
    assert script_problems(text)


def test_the_script_lists_the_step_1_routine_script_tests() -> None:
    assert _files() == [
        "tests/integration/test_routine_scripts_mysql.py",
        "tests/integration/test_triggers_scripts_mysql.py",
    ]


def test_every_listed_file_exists_and_is_marked_integration() -> None:
    for name in _files():
        path = REPO / name
        assert path.is_file(), name
        assert "pytest.mark.integration" in path.read_text(), name


def test_the_server_fixture_removes_its_data_folder_when_the_stop_times_out() -> None:
    text = (REPO / "tests/integration/test_routine_scripts_mysql.py").read_text()
    teardown = text[text.index("    finally:") : text.index("def _mysql(")]
    assert "TimeoutExpired" in teardown and "proc.kill()" in teardown
    assert re.search(r"finally:\s+shutil\.rmtree\(work", teardown)


def test_test_sh_keeps_deselecting_these_tests_and_failing_on_skip() -> None:
    cmd = _pytest_line(TEST_SH.read_text(), "uv run pytest tests/")
    assert '-m "not integration and not e2e"' in cmd
    assert "--fail-on-skip" in cmd


@pytest.mark.parametrize("marker", ['-m "not e2e"', "", '-m "not integration"'])
def test_the_test_sh_guard_fails_on_a_weaker_marker(marker: str) -> None:
    text = TEST_SH.read_text().replace('-m "not integration and not e2e"', marker)
    cmd = _pytest_line(text, "uv run pytest tests/")
    assert '-m "not integration and not e2e"' not in cmd


def _aggregate_exit(step: dict, results: dict[str, str]) -> int:
    """Run the aggregate step's shell with each `needs.<job>.result` set from `results`."""
    env = {"PATH": os.environ["PATH"]}
    for name, value in step["env"].items():
        match = re.fullmatch(r"\$\{\{ needs\.([\w-]+)\.result \}\}", str(value))
        env[name] = results[match.group(1)] if match else str(value)
    return subprocess.run(
        ["bash", "-e", "-c", step["run"]], env=env, capture_output=True
    ).returncode


def _aggregate_problems(step: dict) -> list[str]:
    """The step must succeed only when test-shard and mysqld-tests both succeeded."""
    problems = []
    states = ("success", "failure", "cancelled", "skipped")
    for shard, mysqld in itertools.product(states, states):
        code = _aggregate_exit(step, {"test-shard": shard, "mysqld-tests": mysqld})
        want_ok = shard == "success" and mysqld == "success"
        if (code == 0) != want_ok:
            problems.append(
                f"the aggregate step exits {code} for test-shard={shard}, mysqld-tests={mysqld}"
            )
    return problems


def _job_problems(workflow: dict) -> list[str]:
    problems = []
    jobs = workflow["jobs"]
    job = jobs.get("mysqld-tests", {})
    steps = job.get("steps", [])
    runs = [str(step["run"]).strip() for step in steps if "run" in step]
    if "./ci/test-mysqld.sh" not in runs:
        problems.append("the job must run ./ci/test-mysqld.sh")
    if any("pytest" in r or ("mysqld" in r and r != "./ci/test-mysqld.sh") for r in runs):
        problems.append("the job must inline no mysqld or pytest command")
    if "continue-on-error" in job or any("continue-on-error" in step for step in steps):
        problems.append("the job and its steps must not set continue-on-error")
    test = jobs.get("test", {})
    needs = test.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    if "mysqld-tests" not in needs:
        problems.append("the required `test` job must need mysqld-tests")
    aggregate = [
        step
        for step in test.get("steps", [])
        if "needs.mysqld-tests.result" in str(step.get("env", ""))
    ]
    if len(aggregate) != 1:
        problems.append("one step of the required `test` job must read needs.mysqld-tests.result")
    else:
        problems += _aggregate_problems(aggregate[0])
    return problems


def _workflow() -> dict:
    workflow: dict = yaml.safe_load(CI_YML.read_text())
    return workflow


def test_the_github_job_is_required_blocking_and_runs_only_the_script() -> None:
    assert _job_problems(_workflow()) == []


def test_the_job_guard_fails_on_each_weakening() -> None:
    import copy

    base = _workflow()

    def drop_need(w: dict) -> None:
        w["jobs"]["test"]["needs"] = ["test-shard"]

    def drop_result(w: dict) -> None:
        for step in w["jobs"]["test"]["steps"]:
            step.get("env", {}).pop("MYSQLD_RESULT", None)

    def job_continue(w: dict) -> None:
        w["jobs"]["mysqld-tests"]["continue-on-error"] = True

    def step_continue(w: dict) -> None:
        w["jobs"]["mysqld-tests"]["steps"][-1]["continue-on-error"] = True

    def inline(w: dict) -> None:
        w["jobs"]["mysqld-tests"]["steps"][-1]["run"] = "pytest -m integration"

    def ignore_mysqld_result(w: dict) -> None:
        for step in w["jobs"]["test"]["steps"]:
            if "MYSQLD_RESULT" in step.get("env", {}):
                step["run"] = '[ "$RESULT" = "success" ]'

    def accept_skipped(w: dict) -> None:
        for step in w["jobs"]["test"]["steps"]:
            if "MYSQLD_RESULT" in step.get("env", {}):
                step["run"] = step["run"].replace(
                    '[ "$MYSQLD_RESULT" = "success" ]', '[ "$MYSQLD_RESULT" != "failure" ]'
                )

    for weaken in (
        drop_need,
        drop_result,
        job_continue,
        step_continue,
        inline,
        ignore_mysqld_result,
        accept_skipped,
    ):
        w = copy.deepcopy(base)
        weaken(w)
        assert _job_problems(w), weaken.__name__
