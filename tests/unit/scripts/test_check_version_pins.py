"""scripts/check_version_pins.py: a resolved bump needs no hand edit in the tests (#1226)."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from scripts import check_version_pins as cvp
from scripts.contract_bumps import contract_default, history_newest

REPO = Path(__file__).resolve().parents[3]


def test_every_resolvable_contract_moves_one_minor_with_its_history(tmp_path: Path) -> None:
    """The simulation is the resolver's own edit: new default and Version History entry."""
    shutil.copytree(REPO / "src" / "contracts", tmp_path / "src" / "contracts")
    source = (REPO / "src/contracts/synthesis_output.py").read_text()
    before = contract_default(source, "SynthesisOutputContract")
    # A reason of its own: this test also runs inside the check's own bumped tree,
    # where SIMULATED_REASON is already a Version History entry.
    reason = "Bumped by test_check_version_pins."
    bumped = cvp.bump_everything(tmp_path, reason)
    assert "synthesis_output.SynthesisOutputContract" in bumped
    after_text = (tmp_path / "src/contracts/synthesis_output.py").read_text()
    major, minor = before.split(".")
    want = f"{major}.{int(minor) + 1}"
    assert contract_default(after_text, "SynthesisOutputContract") == want
    assert history_newest(after_text) == want
    assert reason in after_text
    # The contracts the resolver cannot edit are left alone, not half-edited.
    assert "aurora_mysql_model_output.AuroraMySQLModelOutputContract" not in bumped


def test_the_tests_it_runs_mention_a_version_and_skip_the_heavy_suites(tmp_path: Path) -> None:
    for rel, text in {
        "tests/unit/a/test_pin.py": 'assert x.contract_version == "1.0"\n',
        "tests/unit/a/test_const.py": "assert DELTA_VERSION\n",
        "tests/unit/a/test_other.py": "assert 1\n",
        "tests/e2e/test_pipe.py": "contract_version\n",
        "tests/integration/test_api.py": "contract_version\n",
        "tests/unit/a/helper.py": "contract_version\n",
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert cvp.candidate_tests(tmp_path) == [
        "tests/unit/a/test_const.py",
        "tests/unit/a/test_pin.py",
    ]


def test_validate_contracts_runs_it_as_a_blocking_step() -> None:
    """The step lives in ``validate-contracts`` (a required check) and can fail it:
    no ``continue-on-error`` and no ``if`` on the step or the job."""
    workflow = REPO / ".github/workflows/ci.yml"
    assert workflow.exists()
    loaded = yaml.safe_load(workflow.read_text())
    job = loaded["jobs"]["validate-contracts"]
    (step,) = [
        s
        for s in job["steps"]
        if s.get("run", "").startswith("uv run python scripts/check_version_pins.py")
    ]
    for scope in (job, step):
        assert not scope.get("continue-on-error"), scope
        assert "if" not in scope, scope
    assert "merge_group" in (loaded.get("on") or loaded[True])  # PyYAML reads `on:` as True


def test_a_collection_error_is_not_reported_as_a_pin() -> None:
    out = (
        "FAILED tests/a.py::test_pin - AssertionError: '1.45' == '1.44'\n"
        "ERROR tests/b.py - ImportError: no module named x\n"
        "ERROR tests/c.py::test_x - fixture 'y' not found\n"
    )
    assert cvp.parse_outcomes(out) == (
        ["tests/a.py::test_pin"],
        ["tests/b.py", "tests/c.py::test_x"],
    )
    assert cvp.parse_outcomes("1 passed\n") == ([], [])
