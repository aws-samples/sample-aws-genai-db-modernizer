"""Unit tests for ``scripts/contract_value_snapshot.py`` (#975): the bump
rule for published enum, literal and id values, on synthetic snapshots."""

from __future__ import annotations

import enum
import json
import subprocess  # nosec B404 -- runs git in a scratch repository
import types
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from scripts import contract_value_snapshot as cvs
from scripts.contract_value_snapshot import evaluate_against_base, evaluate_snapshot, module_version


def _entry(version: str | None, *values: str) -> dict[str, Any]:
    return {"module": "m", "version": version, "values": sorted(values)}


def test_identical_snapshots_have_no_problems() -> None:
    snap = {"m.Reason": _entry("1.0", "a", "b")}
    assert evaluate_snapshot(snap, dict(snap)) == []


def test_removed_value_without_a_bump_fails() -> None:
    problems = evaluate_snapshot(
        {"m.Reason": _entry("1.0", "a", "b")}, {"m.Reason": _entry("1.0", "a")}
    )
    assert len(problems) == 1 and "MAJOR" in problems[0] and "'b'" in problems[0]


def test_renamed_value_is_a_removal_and_needs_major() -> None:
    before = {"capability_ids": _entry("1.1", "inverted_index")}
    after = {"capability_ids": _entry("1.2", "text_pattern_match")}
    problems = evaluate_snapshot(before, after)
    assert len(problems) == 1 and "MAJOR" in problems[0] and "inverted_index" in problems[0]


def test_removed_value_with_only_a_minor_bump_fails() -> None:
    problems = evaluate_snapshot(
        {"m.Reason": _entry("1.0", "a", "b")}, {"m.Reason": _entry("1.1", "a")}
    )
    assert len(problems) == 1 and "only moved to '1.1'" in problems[0]


def test_removed_value_with_a_major_bump_asks_only_for_regeneration() -> None:
    problems = evaluate_snapshot(
        {"m.Reason": _entry("1.4", "a", "b")}, {"m.Reason": _entry("2.0", "a")}
    )
    assert len(problems) == 1 and "wasn't regenerated" in problems[0]


def test_added_value_without_a_bump_fails_as_minor() -> None:
    problems = evaluate_snapshot(
        {"m.Reason": _entry("1.0", "a")}, {"m.Reason": _entry("1.0", "a", "b")}
    )
    assert len(problems) == 1 and "MINOR" in problems[0]


def test_added_value_with_a_minor_bump_asks_only_for_regeneration() -> None:
    problems = evaluate_snapshot(
        {"m.Reason": _entry("1.0", "a")}, {"m.Reason": _entry("1.1", "a", "b")}
    )
    assert len(problems) == 1 and "wasn't regenerated" in problems[0]


def test_version_move_alone_asks_for_regeneration() -> None:
    problems = evaluate_snapshot({"m.R": _entry("1.0", "a")}, {"m.R": _entry("1.1", "a")})
    assert len(problems) == 1 and "wasn't regenerated" in problems[0]


def test_value_sets_added_or_removed_wholesale_are_reported() -> None:
    committed = {"m.Old": _entry("1.0", "a")}
    actual = {"m.New": _entry("1.0", "a")}
    problems = evaluate_snapshot(committed, actual)
    assert any("m.New" in p and "new value set" in p for p in problems)
    assert any("m.Old" in p and "MAJOR" in p for p in problems)


def test_a_set_with_no_version_cannot_change() -> None:
    problems = evaluate_snapshot({"m.R": _entry(None, "a")}, {"m.R": _entry(None, "a", "b")})
    assert len(problems) == 1 and "no version to bump" in problems[0]


def test_contract_value_sets_finds_enums_and_literals(monkeypatch: Any, tmp_path: Any) -> None:
    class Color(str, enum.Enum):
        RED = "red"
        BLUE = "blue"

    Color.__module__ = "src.contracts.fake"
    Imported = enum.Enum("Imported", {"X": "x"})  # defined elsewhere: ignored
    fake = types.ModuleType("src.contracts.fake")
    fake.Color = Color  # type: ignore[attr-defined]
    fake.Size = Literal["s", "m"]  # type: ignore[attr-defined]
    fake.Imported = Imported  # type: ignore[attr-defined]
    (tmp_path / "fake.py").write_text("")
    monkeypatch.setattr(cvs, "_module", lambda stem: fake)
    monkeypatch.setattr(cvs, "CONTRACTS_DIR", tmp_path)
    assert cvs.contract_value_sets() == {"fake.Color": ["blue", "red"], "fake.Size": ["m", "s"]}


def test_module_version_reads_the_real_sources() -> None:
    # A versioned model's contract_version, a Version History, and an override.
    from src.contracts.triage_output import TriageOutputContract

    triage_default = TriageOutputContract.model_fields["contract_version"].default
    assert module_version("triage_output") == triage_default
    assert module_version("move_ledger") is not None
    assert module_version("query_trace") == module_version("synthesis_output")


def test_a_pending_bump_covers_an_added_value() -> None:
    """ADR-008 Decision 8 (#1158): a pending bump of the module stands in for the
    version move until it is resolved."""
    before, after = {"m.Reason": _entry("1.0", "a")}, {"m.Reason": _entry("1.0", "a", "b")}
    assert evaluate_snapshot(before, after) != []
    assert evaluate_snapshot(before, after, {"m": "minor"}) == []
    assert evaluate_snapshot(before, after, {"other": "major"}) != []


def test_a_pending_minor_bump_does_not_cover_a_removed_value() -> None:
    before, after = {"m.Reason": _entry("1.0", "a", "b")}, {"m.Reason": _entry("1.0", "a")}
    (problem,) = evaluate_snapshot(before, after, {"m": "minor"})
    assert "MAJOR" in problem and "declares only MINOR" in problem
    assert evaluate_snapshot(before, after, {"m": "major"}) == []


def test_a_value_change_with_a_regenerated_snapshot_but_no_bump_fails_against_main() -> None:
    main, branch = {"m.Reason": _entry("1.0", "a")}, {"m.Reason": _entry("1.0", "a", "b")}
    assert evaluate_snapshot(branch, branch) == []
    (problem,) = evaluate_against_base(main, branch)
    assert "no pending bump covers it" in problem
    assert evaluate_against_base(main, branch, {"m": "minor"}) == []
    assert evaluate_against_base(main, {"m.Reason": _entry("1.1", "a", "b")}) == []
    (problem,) = evaluate_against_base(
        {"m.Reason": _entry("1.0", "a", "b")}, {"m.Reason": _entry("1.0", "a")}, {"m": "minor"}
    )
    assert "no pending bump covers it" in problem


# --- a pending bump after main moved the same module (#1393) -------------------------


def _snapshot_text(snapshot: dict[str, Any]) -> str:
    # The layout write_snapshot() commits, so git merges it the way it merges the real file.
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


def _rebased_snapshot(tmp_path: Path) -> dict[str, Any]:
    """Two bumps of module ``q``: branch A adds a value set and declares a pending bump
    (its snapshot records main's 1.53); branch B's bump lands first and moves every
    ``q`` entry on main to 1.54. Returns A's committed snapshot after a clean rebase."""
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / "value_set_snapshot.json"

    def git(*args: str) -> None:
        subprocess.run(  # nosec B603 B607 -- fixed argv in a scratch repository
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    def commit(snapshot: dict[str, Any], message: str) -> None:
        path.write_text(_snapshot_text(snapshot))
        git("add", path.name)
        git("commit", "-q", "-m", message)

    git("init", "-q", "-b", "main")
    commit({"q.A": _q("1.53", "a"), "q.C": _q("1.53", "x")}, "main")
    git("checkout", "-q", "-b", "a")
    commit({"q.A": _q("1.53", "a"), "q.B": _q("1.53", "k"), "q.C": _q("1.53", "x")}, "A")
    git("checkout", "-q", "main")
    commit({"q.A": _q("1.54", "a", "b"), "q.C": _q("1.54", "x")}, "B resolved")
    git("checkout", "-q", "a")
    git("rebase", "-q", "main")  # clean: check=True raises on a conflict
    return cast(dict[str, Any], json.loads(path.read_text()))


def _q(version: str, *values: str) -> dict[str, Any]:
    return {"module": "q", "version": version, "values": sorted(values)}


# After the rebase the tree holds main's q at 1.54 plus A's new set.
_AFTER_REBASE = {"q.A": _q("1.54", "a", "b"), "q.B": _q("1.54", "k"), "q.C": _q("1.54", "x")}


def test_a_pending_bump_passes_after_main_moved_the_module(tmp_path: Path) -> None:
    """#1393: the rebase is clean, but A's new set still records 1.53. The module has a
    pending bump, so the version is the resolver's to write: the entry passes."""
    committed = _rebased_snapshot(tmp_path)
    assert committed["q.B"]["version"] == "1.53"  # the stale number the rebase keeps
    assert evaluate_snapshot(committed, _AFTER_REBASE, {"q": "minor"}) == []


@pytest.mark.passes_on_main
def test_a_stale_version_with_no_pending_bump_still_fails(tmp_path: Path) -> None:
    """A resolved branch (no pending file left) must match exactly: once the resolver
    moved q to 1.55, a snapshot that still says 1.53 or 1.54 fails."""
    committed = _rebased_snapshot(tmp_path)
    resolved = {key: {**entry, "version": "1.55"} for key, entry in _AFTER_REBASE.items()}
    problems = evaluate_snapshot(committed, resolved)
    assert len(problems) == 3 and all("wasn't regenerated" in p for p in problems)
    (problem,) = evaluate_snapshot(committed, _AFTER_REBASE, {"other": "minor"})
    assert problem.startswith("q.B:") and "wasn't regenerated" in problem


@pytest.mark.passes_on_main
def test_a_value_change_with_no_bump_still_fails_after_a_rebase(tmp_path: Path) -> None:
    committed = _rebased_snapshot(tmp_path)
    changed = {**_AFTER_REBASE, "q.B": _q("1.54", "k", "l")}
    (problem,) = evaluate_snapshot(committed, changed)
    assert problem.startswith("q.B:") and "wasn't regenerated" in problem
    same_version = {**committed, "q.B": _q("1.53", "k", "l")}
    (problem,) = evaluate_snapshot(committed, same_version, {"other": "minor"})
    assert "ADR-008 Decision 3 requires a MINOR bump" in problem


def test_a_pending_bump_still_checks_the_values_of_a_stale_entry() -> None:
    """Only the version is left to the resolver: a removed value under a pending MINOR
    still fails, whatever version the committed entry records."""
    committed = {"q.B": _q("1.53", "k", "l")}
    (problem,) = evaluate_snapshot(committed, {"q.B": _q("1.54", "k")}, {"q": "minor"})
    assert "MAJOR" in problem and "declares only MINOR" in problem
    assert evaluate_snapshot(committed, {"q.B": _q("1.54", "k", "l", "m")}, {"q": "minor"}) == []


def test_a_pending_bump_of_the_governing_module_covers_an_override_module() -> None:
    """The #1365 case: ``query_trace`` takes its version from ``synthesis_output``
    (VERSION_SOURCE_OVERRIDES), and the pending file names a synthesis_output contract,
    so the pending kind arrives keyed on synthesis_output."""

    def trace(version: str, *values: str) -> dict[str, Any]:
        return {"module": "query_trace", "version": version, "values": sorted(values)}

    pending = {"synthesis_output": "minor"}
    stale = {"query_trace.KeyRefusal": trace("1.54", "a")}
    assert evaluate_snapshot(stale, {"query_trace.KeyRefusal": trace("1.55", "a")}, pending) == []
    added = {"query_trace.KeyRefusal": trace("1.54", "a", "b")}
    assert evaluate_snapshot(stale, added, pending) == []
    assert evaluate_snapshot(stale, added) != []
    assert evaluate_against_base(stale, added, pending) == []
    assert evaluate_against_base(stale, added) != []


def _trace(version: str, *values: str) -> dict[str, Any]:
    return {"module": "query_trace", "version": version, "values": sorted(values)}


def test_a_pending_minor_of_the_governing_module_still_fails_a_removed_value() -> None:
    """A pending MINOR of synthesis_output does not cover a value removed from a
    query_trace set, and the messages name the module a bump is declared on."""
    pending = {"synthesis_output": "minor"}
    before = {"query_trace.KeyRefusal": _trace("1.54", "a", "b")}
    after = {"query_trace.KeyRefusal": _trace("1.54", "a")}
    (problem,) = evaluate_snapshot(before, after, pending)
    assert "MAJOR bump of synthesis_output" in problem and "declares only MINOR" in problem
    (problem,) = evaluate_against_base(before, after, pending)
    assert "no pending bump covers it" in problem
    assert "bump_contract.py synthesis_output major" in problem
    (problem,) = evaluate_snapshot(before, after)
    assert "bump_contract.py synthesis_output major" in problem


def test_the_override_applies_only_to_a_module_with_no_version_of_its_own(
    monkeypatch: Any,
) -> None:
    """The rule module_version applies: if an overridden module had its own version,
    a pending bump of the other module would not cover its sets."""
    assert cvs.version_source("query_trace") == "synthesis_output"
    assert cvs.pending_kind("query_trace", {"synthesis_output": "minor"}) == "minor"
    monkeypatch.setitem(cvs.VERSION_SOURCE_OVERRIDES, "triage_output", "synthesis_output")
    assert cvs.version_source("triage_output") == "triage_output"
    assert cvs.module_version("triage_output") == cvs._own_version("triage_output")
    assert cvs.pending_kind("triage_output", {"synthesis_output": "minor"}) is None
    before = {"triage_output.R": {"module": "triage_output", "version": "1.0", "values": ["a"]}}
    after = {"triage_output.R": {"module": "triage_output", "version": "1.0", "values": ["a", "b"]}}
    (problem,) = evaluate_snapshot(before, after, {"synthesis_output": "minor"})
    assert "bump_contract.py triage_output minor" in problem
