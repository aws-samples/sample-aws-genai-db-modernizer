# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Hook 2: no hand edits of generated baselines."""

from __future__ import annotations

import pytest

from scripts.hooks.block_baseline_edit import PROTECTED, PROTECTED_DIRS
from tests.unit.hooks.conftest import REPO_ROOT, edit


@pytest.mark.parametrize("rel", sorted(PROTECTED))
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
def test_protected_file_blocked(rel: str, tool: str, run_hook) -> None:
    res = run_hook("block_baseline_edit.py", edit(str(REPO_ROOT / rel), tool))
    assert res.blocked
    assert PROTECTED[rel] in res.stderr and "--write" in res.stderr
    assert res.stderr.count("\n") == 1


def test_relative_path_resolved_against_cwd(run_hook) -> None:
    event = edit("tests/e2e/baselines/routing_snapshot.json")
    assert run_hook("block_baseline_edit.py", event).blocked
    event["tool_input"]["file_path"] = "baselines/../baselines/routing_baseline.json"
    event["cwd"] = str(REPO_ROOT / "tests" / "e2e")
    assert run_hook("block_baseline_edit.py", event).blocked


@pytest.mark.parametrize(
    "rel",
    [
        "tests/e2e/routing_snapshot.py",
        "tests/e2e/baselines/other.json",
        "tests/contract/test_schema_snapshot.py",
        "docs/routing_snapshot.json",
        "src/contracts/schema_snapshot.json",
    ],
)
def test_other_files_allowed(rel: str, run_hook) -> None:
    assert run_hook("block_baseline_edit.py", edit(str(REPO_ROOT / rel))).code == 0


@pytest.mark.parametrize(
    "path",
    [
        "C:\\work\\repo\\tests\\e2e\\baselines\\routing_snapshot.json",
        "tests\\contract\\schema_snapshot.json",
        "tests\\e2e\\baselines/routing_baseline.json",
    ],
)
def test_backslash_paths_are_normalised(path: str, run_hook) -> None:
    event = edit(path)
    event["cwd"] = ""
    assert run_hook("block_baseline_edit.py", event).blocked


def test_protected_files_exist() -> None:
    for rel in PROTECTED:
        assert (REPO_ROOT / rel).exists(), rel


@pytest.mark.parametrize("event", ["", "{", '{"tool_input": {"file_path": 3}}', "{}"])
def test_bad_input_fails_open(event: str, run_hook) -> None:
    assert run_hook("block_baseline_edit.py", event).code == 0


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
def test_value_set_snapshot_is_protected(tool: str, run_hook) -> None:
    """#1408: the value-set snapshot is regenerated like the schema snapshot."""
    path = str(REPO_ROOT / "tests/contract/value_set_snapshot.json")
    res = run_hook("block_baseline_edit.py", edit(path, tool))
    assert res.blocked
    assert "uv run python scripts/contract_value_snapshot.py --write" in res.stderr


@pytest.mark.parametrize("prefix", sorted(PROTECTED_DIRS))
def test_a_file_in_a_protected_directory_is_blocked(prefix: str, run_hook) -> None:
    res = run_hook("block_baseline_edit.py", edit(str(REPO_ROOT / prefix / "v1" / "a.toml")))
    assert res.blocked and PROTECTED_DIRS[prefix] in res.stderr
    assert (REPO_ROOT / prefix).is_dir()


@pytest.mark.parametrize(
    "name", ["wordpress_analysis.json", "wordpress_reality_check.json", "trailgear_analysis.json"]
)
def test_saved_assess_results_are_never_regenerated(name: str, run_hook) -> None:
    """#1648: the saved 1.3 results are a record every later release reads back."""
    path = REPO_ROOT / "tests/fixtures/assess_results_v1_3" / name
    assert path.is_file()
    res = run_hook("block_baseline_edit.py", edit(str(path), "Write"))
    assert res.blocked
    assert "historical record" in res.stderr and "never regenerate it (#1648)" in res.stderr
    assert "--write" not in res.stderr


# #1941 review: the routine repair loop's state and result are written by the script only,
# so a session can never forge a model_repaired routine by hand.
@pytest.mark.parametrize(
    "rel",
    [
        "artifacts/shop/job-1/routines/v1/repair/loop_state.json",
        "artifacts/shop/job-1/routines/v12/routine_repair.json",
        "/abs/elsewhere/artifacts/db/j/routines/v3/repair/loop_state.json",
    ],
)
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
def test_the_routine_repair_state_and_result_are_never_edited_by_hand(
    rel: str, tool: str, run_hook
) -> None:
    res = run_hook("block_baseline_edit.py", edit(rel, tool))
    assert res.blocked
    assert "run_routine_repair.py" in res.stderr


@pytest.mark.parametrize(
    "rel",
    [
        "artifacts/shop/job-1/routines/v1/repair/llm_response.json",
        "artifacts/shop/job-1/routines/v1/repair/llm_input.json",
        "artifacts/shop/job-1/routines/v1/routine_migration.json",
    ],
)
@pytest.mark.passes_on_main  # the negative control: what stays writable
def test_the_repair_response_stays_writable(rel: str, run_hook) -> None:
    assert run_hook("block_baseline_edit.py", edit(rel)).code == 0


def test_the_ci_settings_deny_hand_edits_of_the_repair_state_and_result() -> None:
    import json

    deny = json.loads((REPO_ROOT / ".claude/settings.ci.json").read_text())["permissions"]["deny"]
    for tool in ("Edit", "Write"):
        assert f"{tool}(artifacts/**/routines/**/repair/loop_state.json)" in deny
        assert f"{tool}(artifacts/**/routines/**/routine_repair.json)" in deny
