"""Hook 2: no hand edits of generated baselines."""

from __future__ import annotations

import pytest

from scripts.hooks.block_baseline_edit import PROTECTED
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
