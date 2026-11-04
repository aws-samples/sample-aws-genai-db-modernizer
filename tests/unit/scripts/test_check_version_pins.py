# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/check_version_pins.py: a resolved bump needs no hand edit in the tests (#1226)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
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
    assert "assignment_models.ValidationResult" not in bumped
    # #1221: the Aurora design outputs have a Version History the resolver edits.
    assert "aurora_mysql_model_output.AuroraMySQLModelOutputContract" in bumped


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


# --- #1845: --base limits the check to what the branch touched, and splits the failures ---


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


_TREE = {
    "tests/unit/a/test_changed.py": 'assert x.contract_version == "1.0"\n',
    "tests/unit/a/test_names_it.py": "from src.contracts.synthesis_output import M\nM_VERSION\n",
    "tests/unit/a/test_names_other.py": "from src.contracts.triage_output import T\nT_VERSION\n",
    "tests/unit/a/test_prefix_only.py": "from src.contracts.synthesis_output_v2 import M\nM_VERSION\n",
    "tests/unit/a/test_untouched.py": "current_version(X)\n",
}


def test_base_selection_takes_changed_tests_and_tests_naming_a_touched_contract(
    tmp_path: Path,
) -> None:
    _write(tmp_path, _TREE)
    changed = ["tests/unit/a/test_changed.py", "src/contracts/synthesis_output.py", "README.md"]
    assert cvp.select_tests(tmp_path, changed) == [
        "tests/unit/a/test_changed.py",
        "tests/unit/a/test_names_it.py",
    ]


def test_base_selection_is_empty_when_the_branch_touched_no_test_or_contract(
    tmp_path: Path,
) -> None:
    _write(tmp_path, _TREE)
    assert cvp.select_tests(tmp_path, ["src/ui/src/x.js", "docs/a.md"]) == []
    assert cvp.select_tests(tmp_path, []) == []


def test_base_selection_skips_changed_tests_that_name_no_version_or_were_deleted(
    tmp_path: Path,
) -> None:
    _write(tmp_path, {**_TREE, "tests/unit/a/test_plain.py": "assert 1\n"})
    changed = ["tests/unit/a/test_plain.py", "tests/unit/a/test_gone.py", "tests/e2e/test_x.py"]
    assert cvp.select_tests(tmp_path, changed) == []


def test_base_selection_reads_the_contract_a_pending_bump_names(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            **_TREE,
            "src/contracts/pending/triage_output.T.x.toml": (
                'contract = "triage_output.T"\nkind = "minor"\nreason = "r"\n'
            ),
        },
    )
    changed = ["src/contracts/pending/triage_output.T.x.toml"]
    assert cvp.select_tests(tmp_path, changed) == ["tests/unit/a/test_names_other.py"]
    # A pending file the branch deleted (resolved, or stacked and merged) names nothing.
    assert cvp.select_tests(tmp_path, ["src/contracts/pending/gone.toml"]) == []


def test_base_selection_runs_every_candidate_when_the_check_itself_changed(tmp_path: Path) -> None:
    _write(tmp_path, _TREE)
    for path in (
        "scripts/check_version_pins.py",
        "scripts/resolve_contract_versions.py",
        "scripts/contract_bumps.py",
        "scripts/contract_schema_snapshot.py",
        "scripts/contract_value_snapshot.py",
        "src/shared/contract_versioning.py",
        "src/contracts/__init__.py",
    ):
        assert cvp.select_tests(tmp_path, [path]) == cvp.candidate_tests(tmp_path), path


def test_split_failures_separates_pins_from_tests_that_fail_without_the_bump() -> None:
    pins, broken = cvp.split_failures(["t::a", "t::b", "t::c"], ["t::b", "t::z"])
    assert pins == ["t::a", "t::c"]
    assert broken == ["t::b"]
    assert cvp.split_failures([], []) == ([], [])


class _FakeGit:
    """Stands in for ``_run``: a fake worktree, then canned pytest results."""

    def __init__(self, tmp: Path, changed: list[str], bumped: str, plain: str) -> None:
        self.tmp, self.changed, self.bumped, self.plain = tmp, changed, bumped, plain
        self.calls: list[list[str]] = []
        self.reset = False

    def __call__(self, cmd, cwd, env=None):
        import subprocess

        self.calls.append(list(cmd))

        def done(out: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, rc, out, "")

        if cmd[:3] == ["git", "diff", "--name-only"]:
            return done("\n".join(self.changed) + "\n")
        if cmd[:3] == ["git", "worktree", "add"]:
            tree = Path(cmd[5])
            _write(tree, _TREE)
            (tree / "src").mkdir(exist_ok=True)
            return done()
        if cmd[:2] == ["git", "worktree"]:
            return done()
        if cmd[:2] == ["git", "reset"] or cmd[2:4] == ["reset", "-q"]:
            self.reset = True
            return done()
        if "-c" in cmd and "src.__path__" in cmd[-1]:
            import os

            return done(os.path.realpath(cwd / "src") + "\n")
        if "-c" in cmd:
            return done("3\n")
        if "-m" in cmd and "pytest" in cmd:
            out = self.plain if self.reset else self.bumped
            return done(out, 1 if "FAILED" in out else 0)
        return done()  # the snapshot scripts

    def pytest_args(self) -> list[list[str]]:
        return [c[c.index("pytest") + 1 :] for c in self.calls if "pytest" in c]


def test_main_with_base_runs_the_selection_then_reruns_failures_without_the_bump(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    fake = _FakeGit(
        tmp_path,
        ["src/contracts/synthesis_output.py", "tests/unit/a/test_changed.py"],
        bumped="FAILED tests/unit/a/test_changed.py::t_pin - x\n"
        "FAILED tests/unit/a/test_names_it.py::t_broken - y\n",
        plain="FAILED tests/unit/a/test_names_it.py::t_broken - y\n",
    )
    monkeypatch.setattr(cvp, "_run", fake)
    assert cvp.main(["--base", "origin/main"]) == 1
    first, rerun = fake.pytest_args()
    assert "tests/unit/a/test_changed.py" in first and "tests/unit/a/test_names_it.py" in first
    assert "tests/unit/a/test_untouched.py" not in first
    assert "tests/unit/a/test_changed.py::t_pin" in rerun
    assert "tests/unit/a/test_names_it.py::t_broken" in rerun
    out = capsys.readouterr()
    text = out.out + out.err
    pins_part, broken_part = text.split("fail without the bump too")
    assert "t_pin" in pins_part and "fail only under the bump" in pins_part
    assert "t_broken" in broken_part and "t_pin" not in broken_part
    assert ["git", "diff", "--name-only", "origin/main...HEAD"] in fake.calls


def test_main_reports_a_failure_without_the_bump_as_no_pin(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """#1746: a test that fails on HEAD as committed is not a version pin."""
    failing = "FAILED tests/unit/a/test_changed.py::t_broken - y\n"
    fake = _FakeGit(tmp_path, ["tests/unit/a/test_changed.py"], bumped=failing, plain=failing)
    monkeypatch.setattr(cvp, "_run", fake)
    assert cvp.main(["--base", "origin/main"]) == 2
    out = capsys.readouterr()
    text = out.out + out.err
    assert "not a version pin" in text
    assert "fail only under the bump" not in text


def test_main_with_base_and_nothing_touched_makes_no_worktree(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    fake = _FakeGit(tmp_path, ["docs/a.md", "src/ui/src/x.js", "ci/x.sh"], bumped="", plain="")
    monkeypatch.setattr(cvp, "_run", fake)
    assert cvp.main(["--base", "origin/main"]) == 0
    assert not [c for c in fake.calls if c[:3] == ["git", "worktree", "add"]]
    assert "no changed test file or contract module" in capsys.readouterr().out


def test_main_with_base_fails_when_git_diff_fails(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    def broken(cmd, cwd, env=None):
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: bad revision")

    monkeypatch.setattr(cvp, "_run", broken)
    assert cvp.main(["--base", "nope/main"]) == 2


@pytest.mark.passes_on_main
def test_main_without_base_still_runs_every_candidate(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeGit(tmp_path, [], bumped="1 passed\n", plain="")
    monkeypatch.setattr(cvp, "_run", fake)
    assert cvp.main([]) == 0
    (args,) = fake.pytest_args()
    for rel in _TREE:
        assert rel in args
    assert not [c for c in fake.calls if c[:3] == ["git", "diff", "--name-only"]]


# --- Round 1: importers of a changed contract, and changes the selection cannot place ---

_CONTRACTS = {
    "src/contracts/base_m.py": "X = 1\n",
    "src/contracts/mid_m.py": "from src.contracts.base_m import X\n",
    "src/contracts/top_m.py": "from . import mid_m\n",
    "src/contracts/rel_m.py": "def f():\n    from .base_m import X\n",
    "src/contracts/alias_m.py": "from src.contracts import base_m as b\n",
    "src/contracts/plain_m.py": "import src.contracts.top_m\n",
    "src/contracts/other_m.py": "import os\n",
}


def test_importers_of_a_changed_contract_are_found_transitively(tmp_path: Path) -> None:
    _write(tmp_path, _CONTRACTS)
    graph = cvp.contract_imports(tmp_path)
    assert graph["mid_m"] == {"base_m"}
    assert graph["top_m"] == {"mid_m"}
    assert graph["rel_m"] == {"base_m"}  # relative, inside a function
    assert graph["alias_m"] == {"base_m"}
    assert graph["plain_m"] == {"top_m"}
    assert graph["other_m"] == set()
    assert cvp.with_importers({"base_m"}, graph) == {
        "base_m", "mid_m", "top_m", "rel_m", "alias_m", "plain_m",
    }  # fmt: skip
    assert cvp.with_importers({"top_m"}, graph) == {"top_m", "plain_m"}
    assert cvp.with_importers({"other_m"}, graph) == {"other_m"}


def test_a_test_naming_only_an_importer_is_selected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            **_CONTRACTS,
            "tests/unit/a/test_top.py": "from src.contracts.top_m import T\nT_VERSION\n",
            "tests/unit/a/test_other.py": "from src.contracts.other_m import O\nO_VERSION\n",
        },
    )
    assert cvp.select_tests(tmp_path, ["src/contracts/base_m.py"]) == ["tests/unit/a/test_top.py"]


def test_the_reviewers_real_cases_select_tests_of_the_importing_modules() -> None:
    """Round 1 on the real tree: tests that name only a module built on the changed one."""
    for changed, want in (
        ("decision_trace_base", "tests/unit/contracts/test_dynamodb_decision_trace.py"),
        ("decision_trace_base", "tests/unit/contracts/test_schema_design_trace.py"),
        ("move_ledger", "tests/contract/test_synthesis_output.py"),
        ("analysis_output", "tests/contract/test_synthesis_output.py"),
    ):
        text = (REPO / want).read_text(encoding="utf-8")
        assert f"contracts.{changed}" not in text, (changed, want)  # names an importer only
        assert want in cvp.select_tests(REPO, [f"src/contracts/{changed}.py"]), (changed, want)


@pytest.mark.parametrize(
    "path",
    [
        "tests/conftest.py",
        "tests/unit/contracts/conftest.py",
        "tests/unit/a/helpers.py",
        "tests/unit/fixtures/old_job.json",
        "src/agents/referee/synthesis.py",
        "src/report/writer.py",
        "scripts/run_synthesis.py",
    ],
)
def test_a_change_the_selection_cannot_place_runs_every_candidate(tmp_path: Path, path) -> None:
    """Round 1: test support files and the code that writes contracts reach tests through
    any number of calls, so the selection falls back to the full run for them."""
    _write(tmp_path, _TREE)
    assert cvp.might_touch([path])
    assert cvp.select_tests(tmp_path, [path, "docs/a.md"]) == cvp.candidate_tests(tmp_path)


def test_changes_outside_python_code_and_tests_stay_scoped(tmp_path: Path) -> None:
    _write(tmp_path, _TREE)
    changed = ["docs/a.md", "ci/x.sh", "src/ui/src/x.js", ".github/workflows/ci.yml"]
    assert not cvp.might_touch(changed)
    assert cvp.select_tests(tmp_path, changed) == []
