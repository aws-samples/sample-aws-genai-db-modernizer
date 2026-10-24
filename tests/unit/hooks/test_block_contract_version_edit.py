"""Hook 6: no hand edits of a contract's version default or Version History (#1408)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts import contract_bumps
from scripts.hooks import block_contract_version_edit as hook
from src.contracts.decision_record import DecisionRecord
from src.shared.contract_versioning import current_version
from tests.unit.hooks.conftest import REPO_ROOT

SCRIPT = "block_contract_version_edit.py"

CONTRACT = '''"""A contract.

Some prose about it.

Version History:
- 1.1 (2026-10-20, #2): Added ``b``.
- 1.0 (2026-10-01, #1): Initial version, with a
  continuation line.

More prose after the history.
"""

from pydantic import BaseModel, Field


class Thing(BaseModel):
    contract_version: str = Field(
        default="1.1", pattern=r"^\\d+\\.\\d+$", description="Contract version (MAJOR.MINOR format)"
    )
    a: str = Field(description="The a")
    b: int = 0
'''


@pytest.fixture
def contract(tmp_path: Path) -> Path:
    path = tmp_path / "src" / "contracts" / "thing.py"
    path.parent.mkdir(parents=True)
    path.write_text(CONTRACT)
    return path


def _edit(path: Path | str, old: str, new: str, replace_all: bool = False) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(path),
            "old_string": old,
            "new_string": new,
            "replace_all": replace_all,
        },
        "cwd": str(REPO_ROOT),
    }


def _write(path: Path | str, content: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": content},
        "cwd": str(REPO_ROOT),
    }


def _multi(path: Path | str, edits: list[tuple[str, str]]) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": str(path),
            "edits": [{"old_string": o, "new_string": n} for o, n in edits],
        },
        "cwd": str(REPO_ROOT),
    }


def _assert_refused(res) -> None:
    assert res.blocked
    assert "uv run python scripts/bump_contract.py" in res.stderr
    assert "uv run python scripts/resolve_contract_versions.py" in res.stderr
    assert res.stderr.count("\n") == 1


def test_changing_the_version_default_is_refused(contract: Path, run_hook) -> None:
    _assert_refused(run_hook(SCRIPT, _edit(contract, 'default="1.1"', 'default="1.2"')))


def test_adding_a_history_entry_is_refused(contract: Path, run_hook) -> None:
    event = _edit(
        contract, "Version History:\n", "Version History:\n- 1.2 (2026-10-24, #3): New.\n"
    )
    _assert_refused(run_hook(SCRIPT, event))


def test_rewording_a_history_continuation_line_is_refused(contract: Path, run_hook) -> None:
    event = _edit(contract, "  continuation line.", "  reworded continuation.")
    _assert_refused(run_hook(SCRIPT, event))


def test_removing_a_history_entry_is_refused(contract: Path, run_hook) -> None:
    _assert_refused(run_hook(SCRIPT, _edit(contract, "- 1.1 (2026-10-20, #2): Added ``b``.\n", "")))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('description="The a"', 'description="The a field"'),
        ("b: int = 0", "b: int = 1\n    c: str | None = None"),
        ("Some prose about it.", "Some other prose."),
        ("More prose after the history.", "Different closing prose."),
    ],
)
def test_other_edits_to_the_same_file_are_allowed(contract: Path, old, new, run_hook) -> None:
    assert run_hook(SCRIPT, _edit(contract, old, new)).code == 0


def test_a_write_that_keeps_the_version_lines_is_allowed(contract: Path, run_hook) -> None:
    content = CONTRACT.replace("b: int = 0", "b: int = 0\n    c: int = 0")
    assert run_hook(SCRIPT, _write(contract, content)).code == 0


def test_a_write_that_moves_the_version_is_refused(contract: Path, run_hook) -> None:
    _assert_refused(run_hook(SCRIPT, _write(contract, CONTRACT.replace('"1.1"', '"2.0"'))))


def test_a_multi_edit_with_one_version_change_is_refused(contract: Path, run_hook) -> None:
    event = _multi(contract, [('description="The a"', 'description="A"'), ('"1.1"', '"1.2"')])
    _assert_refused(run_hook(SCRIPT, event))


def test_a_multi_edit_without_a_version_change_is_allowed(contract: Path, run_hook) -> None:
    event = _multi(contract, [('description="The a"', 'description="A"'), ("b: int", "b: float")])
    assert run_hook(SCRIPT, event).code == 0


def test_a_new_contract_file_is_allowed(tmp_path: Path, run_hook) -> None:
    path = tmp_path / "src" / "contracts" / "new_thing.py"
    assert run_hook(SCRIPT, _write(path, CONTRACT)).code == 0


def test_files_outside_src_contracts_are_allowed(tmp_path: Path, run_hook) -> None:
    path = tmp_path / "tests" / "contract" / "thing.py"
    path.parent.mkdir(parents=True)
    path.write_text(CONTRACT)
    assert run_hook(SCRIPT, _edit(path, '"1.1"', '"1.2"')).code == 0


def _real(module: str) -> Path:
    return REPO_ROOT / "src" / "contracts" / f"{module}.py"


def _bump_default(module: str, cls: str) -> dict:
    """An Edit that moves ``cls``'s literal default in the real module by one MINOR."""
    text = _real(module).read_text()
    default = hook.class_defaults(text)[cls]
    lines = text.splitlines(keepends=True)
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == cls)
    start = sum(len(line) for line in lines[: node.lineno - 1])
    end = sum(len(line) for line in lines[: node.end_lineno])
    old = text[start:end]
    major, minor = default.split(".")
    new = old.replace(f'"{default}"', f'"{major}.{int(minor) + 1}"', 1)
    return _edit(_real(module), old, new)


@pytest.mark.parametrize(
    ("module", "cls"),
    [
        ("aurora_mysql_model_output", "AuroraMySQLModelOutputContract"),
        ("aurora_postgresql_model_output", "AuroraPostgresqlModelOutputContract"),
        ("assignment_models", "ValidationResult"),
    ],
)
def test_a_contract_bumped_by_hand_is_editable(module: str, cls: str, run_hook) -> None:
    """bump_contract.py sends these to a hand bump (the resolver cannot edit them)."""
    assert run_hook(SCRIPT, _bump_default(module, cls)).code == 0


@pytest.mark.parametrize(
    ("module", "constant"),
    [("aurora_design_delta", "DELTA_VERSION"), ("model_flag", "MODEL_FLAG_SCHEMA_VERSION")],
)
def test_a_module_with_a_version_constant_is_editable(module: str, constant: str, run_hook) -> None:
    text = _real(module).read_text()
    old = next(line for line in text.splitlines() if line.startswith(f"{constant} = "))
    assert run_hook(SCRIPT, _edit(_real(module), old, f'{constant} = "1.1"')).code == 0
    if module == "model_flag":  # its Version History is edited by hand too
        event = _edit(_real(module), "Version History:\n", "Version History:\n- 1.1 (x): y.\n")
        assert run_hook(SCRIPT, event).code == 0


def test_a_resolvable_contract_next_to_a_hand_bumped_one_is_still_guarded(run_hook) -> None:
    _assert_refused(run_hook(SCRIPT, _bump_default("assignment_models", "Assignment")))


def test_a_hand_bumped_contract_allows_history_edits_module_wide(run_hook) -> None:
    """assignment_models has ValidationResult (hand-bumped), so any Version History
    line in the module may be edited, even though Assignment is resolvable."""
    event = _edit(
        _real("assignment_models"), "Version History:\n", "Version History:\n- 9.9 (x): y.\n"
    )
    assert run_hook(SCRIPT, event).code == 0
    _assert_refused(run_hook(SCRIPT, _bump_default("assignment_models", "Assignment")))


def test_an_edit_that_does_not_parse_is_let_through(contract: Path, run_hook) -> None:
    event = _edit(contract, 'default="1.1", pattern', 'default="1.2", pattern(')
    assert run_hook(SCRIPT, event).code == 0


def test_adding_a_versioned_model_is_allowed(contract: Path, run_hook) -> None:
    new_model = (
        "\n\nclass Other(BaseModel):\n"
        '    contract_version: str = Field(default="1.0", pattern=r"^\\d+\\.\\d+$")\n'
    )
    assert run_hook(SCRIPT, _write(contract, CONTRACT + new_model)).code == 0
    path = _real("load_test_models")
    text = path.read_text()
    assert (
        run_hook(
            SCRIPT, _edit(path, "class SeedManifest(BaseModel):", "class Seeds(BaseModel):")
        ).code
        == 0
    )
    assert run_hook(SCRIPT, _write(path, text + new_model)).code == 0


def test_removing_a_versioned_model_is_allowed(tmp_path: Path, run_hook) -> None:
    path = tmp_path / "src" / "contracts" / "two.py"
    path.parent.mkdir(parents=True)
    extra = '\n\nclass Other(BaseModel):\n    contract_version: str = Field(default="3.0")\n'
    path.write_text(CONTRACT + extra)
    assert run_hook(SCRIPT, _edit(path, extra, "\n")).code == 0


def test_a_file_with_conflict_markers_is_editable(contract: Path, run_hook) -> None:
    conflicted = CONTRACT.replace(
        "- 1.1 (2026-10-20, #2): Added ``b``.\n",
        "<<<<<<< HEAD\n- 1.1 (2026-10-20, #2): Added ``b``.\n=======\n- 1.1 (2026-10-21, #9): Other.\n>>>>>>> branch\n",
    )
    contract.write_text(conflicted)
    assert run_hook(SCRIPT, _write(contract, CONTRACT)).code == 0


def test_hand_bumped_set_matches_bump_contract() -> None:
    """The hook's exemption is exactly the contracts bump_contract.py refuses to declare."""
    from scripts.bump_contract import check_resolvable, versioned_keys
    from scripts.contract_bumps import BumpError
    from scripts.resolve_contract_versions import hand_bumped_contracts, module_contracts

    keys = versioned_keys()
    refused: set[str] = set()
    found: set[str] = set()
    for path in sorted((REPO_ROOT / "src" / "contracts").glob("*.py")):
        module, text = path.stem, path.read_text()
        contracts = module_contracts(module, text)
        snapshot = [k for k in keys if k.split(".", 1)[0] == module]
        if snapshot:
            assert sorted(contracts) == sorted(snapshot), module
        for contract in contracts:
            try:
                check_resolvable(contract, "minor", "(check)")
            except BumpError:
                refused.add(contract)
        found |= hand_bumped_contracts(module, text)
    assert found == refused
    assert {
        "aurora_mysql_model_output.AuroraMySQLModelOutputContract",
        "aurora_postgresql_model_output.AuroraPostgresqlModelOutputContract",
        "assignment_models.ValidationResult",
    } <= found


def test_an_old_string_that_is_not_there_is_allowed(contract: Path, run_hook) -> None:
    assert run_hook(SCRIPT, _edit(contract, "not in the file", '"9.9"')).code == 0


def test_replace_all_is_applied(contract: Path, run_hook) -> None:
    assert run_hook(SCRIPT, _edit(contract, "1.1", "1.2", replace_all=True)).blocked


def _real_version_edit(new: str) -> tuple[str, str]:
    """The version line as decision_record.py has it today, and the same line at ``new``."""
    old = current_version(DecisionRecord)
    return f'default="{old}", pattern', f'default="{new}", pattern'


def test_a_real_contract_is_guarded(run_hook) -> None:
    path = REPO_ROOT / "src" / "contracts" / "decision_record.py"
    event = _edit(path, *_real_version_edit("99.1"))
    _assert_refused(run_hook(SCRIPT, event))
    event = _edit(path, '"What was decided', '"What was recorded')
    assert run_hook(SCRIPT, event).code == 0


def test_a_relative_path_is_resolved_against_cwd(run_hook) -> None:
    event = _edit("src/contracts/decision_record.py", *_real_version_edit("99.0"))
    _assert_refused(run_hook(SCRIPT, event))


@pytest.mark.parametrize(
    "event",
    [
        "",
        "{",
        '{"tool_input": {"file_path": 3}}',
        "{}",
        '{"tool_input": {"file_path": "src/contracts/x.py"}}',
    ],
)
def test_bad_input_fails_open(event: str, run_hook) -> None:
    assert run_hook(SCRIPT, event).code == 0


def test_patterns_match_the_resolvers() -> None:
    """The hook is stdlib-only, so it keeps copies of the resolver's patterns."""
    assert hook.DEFAULT_RE.pattern == contract_bumps._DEFAULT_RE.pattern
    assert hook.HEADING_RE.pattern == contract_bumps._HEADING_RE.pattern
    assert hook.ENTRY_RE.pattern == contract_bumps._ENTRY_RE.pattern


def test_every_contract_version_is_seen() -> None:
    """Each real contract's defaults and Version History are what the hook watches."""
    for path in sorted((REPO_ROOT / "src" / "contracts").glob("*.py")):
        text = path.read_text()
        defaults, history = hook.version_lines(text)
        assert defaults == tuple(m.group(2) for m in contract_bumps._DEFAULT_RE.finditer(text))
        if contract_bumps.history_newest(text) is not None:  # class docstrings count too
            assert history, path.name
