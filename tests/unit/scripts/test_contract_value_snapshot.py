"""Unit tests for ``scripts/contract_value_snapshot.py`` (#975): the bump
rule for published enum, literal and id values, on synthetic snapshots."""

from __future__ import annotations

import enum
import types
from typing import Any, Literal

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
