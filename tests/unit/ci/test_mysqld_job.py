# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CI runs the mysqld integration tests for step 1's routine scripts (#2274).

``ci/test.sh`` deselects ``-m integration``, so the tests that run the generated MySQL
scripts on a real server need an entry point and a job of their own. These guards keep
the two in step: the script lists exactly the files that need a server, runs them under
``--fail-on-skip``, pins the server; the GitHub job calls the script and nothing else.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ci" / "test-mysqld.sh"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
TEST_SH = REPO / "ci" / "test.sh"


def _files() -> list[str]:
    text = SCRIPT.read_text()
    block = re.search(r"MYSQLD_TEST_FILES=\((.*?)\)", text, re.S)
    assert block, "ci/test-mysqld.sh must list its test files in MYSQLD_TEST_FILES=(...)"
    return block.group(1).split()


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


def test_the_script_fails_on_a_skip_so_a_missing_server_cannot_pass() -> None:
    text = SCRIPT.read_text()
    assert "--fail-on-skip" in text
    assert re.search(r"pytest .*-m integration", text)


def test_the_server_version_is_pinned_exactly_with_a_checksum() -> None:
    text = SCRIPT.read_text()
    assert re.search(r'^MYSQL_VERSION="8\.0\.\d+"$', text, re.M)
    assert re.search(r'^MYSQL_SHA256="[0-9a-f]{64}"$', text, re.M)


@pytest.mark.passes_on_main(reason="guards that ci/test.sh keeps deselecting these tests")
def test_the_normal_test_run_still_excludes_these_tests() -> None:
    assert '-m "not integration and not e2e"' in TEST_SH.read_text()


def test_the_github_job_runs_the_script_and_inlines_nothing_else() -> None:
    jobs = yaml.safe_load(CI_YML.read_text())["jobs"]
    job = jobs["mysqld-tests"]
    runs = [step["run"].strip() for step in job["steps"] if "run" in step]
    assert "./ci/test-mysqld.sh" in runs
    assert not any("mysqld" in r and r != "./ci/test-mysqld.sh" for r in runs)
    assert not any("pytest" in r for r in runs)
