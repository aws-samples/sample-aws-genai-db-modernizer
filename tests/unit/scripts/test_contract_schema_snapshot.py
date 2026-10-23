"""Unit tests for ``scripts/contract_schema_snapshot.py``'s canonicalization
and classification engine (#574 review), against small synthetic models
defined in this file rather than the 16 real contracts -- a failure here
always points at the engine itself, never at which real contract happened
to change shape. Each test is exactly one case from CONTRIBUTING.md's
Contract Changes bump rule.

These go through ``field_summary()``/``diff_fields()``/``evaluate_snapshot()``
the same way ``tests/contract/test_schema_snapshot.py`` does against the
real contracts, so both exercise the identical comparison.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from scripts.contract_schema_snapshot import (
    diff_fields,
    evaluate_against_base,
    evaluate_snapshot,
    field_summary,
    overall_bump,
    schema_hash,
)


def _bump_for(before: type[BaseModel], after: type[BaseModel]) -> tuple[str, list]:
    changes = diff_fields(field_summary(before), field_summary(after))
    return overall_bump(changes), changes


def test_nested_required_field_removed_is_major() -> None:
    class Inner(BaseModel):
        a: str
        b: str

    class InnerAfter(BaseModel):
        a: str

    class Before(BaseModel):
        inner: Inner

    class After(BaseModel):
        inner: InnerAfter

    bump, changes = _bump_for(Before, After)
    assert bump == "major"
    assert any(path == "inner.b" and b == "major" for path, b, _ in changes)


def test_nested_optional_field_added_is_minor() -> None:
    class Inner(BaseModel):
        a: str

    class InnerAfter(BaseModel):
        a: str
        b: str | None = None

    class Before(BaseModel):
        inner: Inner

    class After(BaseModel):
        inner: InnerAfter

    bump, changes = _bump_for(Before, After)
    assert bump == "minor"
    assert any(path == "inner.b" and b == "minor" for path, b, _ in changes)


def test_enum_value_added_is_minor() -> None:
    from enum import Enum

    class ColorBefore(str, Enum):
        RED = "red"

    class ColorAfter(str, Enum):
        RED = "red"
        BLUE = "blue"

    class Before(BaseModel):
        color: ColorBefore

    class After(BaseModel):
        color: ColorAfter

    bump, changes = _bump_for(Before, After)
    assert bump == "minor"
    assert any(path == "color" and "blue" in desc.lower() for path, _, desc in changes)


def test_enum_value_removed_is_major() -> None:
    from enum import Enum

    class ColorBefore(str, Enum):
        RED = "red"
        BLUE = "blue"

    class ColorAfter(str, Enum):
        RED = "red"

    class Before(BaseModel):
        color: ColorBefore

    class After(BaseModel):
        color: ColorAfter

    bump, changes = _bump_for(Before, After)
    assert bump == "major"
    assert any(path == "color" and "blue" in desc.lower() for path, _, desc in changes)


def test_constraint_tightened_is_major() -> None:
    class Before(BaseModel):
        n: int = Field(ge=0)

    class After(BaseModel):
        n: int = Field(ge=5)

    bump, changes = _bump_for(Before, After)
    assert bump == "major"
    assert any(path == "n" and "tightened" in desc for path, _, desc in changes)


def test_constraint_loosened_is_minor() -> None:
    class Before(BaseModel):
        n: int = Field(ge=5)

    class After(BaseModel):
        n: int = Field(ge=0)

    bump, changes = _bump_for(Before, After)
    assert bump == "minor"
    assert any(path == "n" and "loosened" in desc for path, _, desc in changes)


def test_cosmetic_description_change_is_ignored() -> None:
    class Before(BaseModel):
        n: int = Field(description="old description")

    class After(BaseModel):
        n: int = Field(description="a totally different description")

    assert field_summary(Before) == field_summary(After)
    assert schema_hash(field_summary(Before)) == schema_hash(field_summary(After))
    assert diff_fields(field_summary(Before), field_summary(After)) == []


def test_nested_ref_with_sibling_description_is_still_resolved() -> None:
    """The bug this review flagged: pydantic emits a nested model's ``$ref``
    together with a sibling ``description`` key for a plain (non-Optional)
    submodel field. A change inside that submodel must still be detected.
    """

    class Inner(BaseModel):
        a: str

    class InnerAfter(BaseModel):
        a: str
        b: str

    class Before(BaseModel):
        inner: Inner = Field(description="a nested thing")

    class After(BaseModel):
        inner: InnerAfter = Field(description="a nested thing")

    bump, changes = _bump_for(Before, After)
    assert bump == "major"
    assert any(path == "inner.b" for path, _, _ in changes)


def test_new_contract_missing_from_snapshot_fails() -> None:
    class Model(BaseModel):
        contract_version: str = Field(default="1.0")
        x: str

    fields = field_summary(Model)
    actual = {
        "synthetic.Model": {"version": "1.0", "schema_hash": schema_hash(fields), "fields": fields}
    }
    problems = evaluate_snapshot(committed={}, actual=actual)
    assert len(problems) == 1
    assert "synthetic.Model" in problems[0]
    assert "new contract" in problems[0]


# --- contract_version's own MAJOR.MINOR pattern (#1006) ------------------------------
# Every published contract_version default is MAJOR.MINOR, so adding exactly that
# pattern to the field (or turning a Literal version into that patterned string)
# rejects no file ever written: MINOR. Any other pattern change stays MAJOR.

_VERSION_PATTERN = r"^\d+\.\d+$"


def test_adding_the_version_pattern_to_contract_version_is_minor() -> None:
    class Before(BaseModel):
        contract_version: str = Field(default="1.0")

    class After(BaseModel):
        contract_version: str = Field(default="1.1", pattern=_VERSION_PATTERN)

    bump, changes = _bump_for(Before, After)
    assert bump == "minor", changes
    assert [path for path, _, _ in changes] == ["contract_version"]


def test_a_literal_version_becoming_the_patterned_string_is_minor() -> None:
    from typing import Literal

    class Before(BaseModel):
        contract_version: Literal["1.0"] = "1.0"

    class After(BaseModel):
        contract_version: str = Field(default="1.1", pattern=_VERSION_PATTERN)

    bump, changes = _bump_for(Before, After)
    assert bump == "minor", changes


def test_a_literal_version_becoming_an_unpatterned_string_is_still_major() -> None:
    from typing import Literal

    class Before(BaseModel):
        contract_version: Literal["1.0"] = "1.0"

    class After(BaseModel):
        contract_version: str = Field(default="1.1")

    assert _bump_for(Before, After)[0] == "major"


def test_a_literal_that_is_not_major_minor_becoming_the_patterned_string_is_major() -> None:
    from typing import Literal

    class Before(BaseModel):
        contract_version: Literal["v1"] = "v1"

    class After(BaseModel):
        contract_version: str = Field(default="1.1", pattern=_VERSION_PATTERN)

    assert _bump_for(Before, After)[0] == "major"


def test_another_pattern_on_contract_version_is_still_major() -> None:
    class Before(BaseModel):
        contract_version: str = Field(default="1.0")

    class After(BaseModel):
        contract_version: str = Field(default="1.1", pattern=r"^\d+\.\d+\.\d+$")

    assert _bump_for(Before, After)[0] == "major"


def test_changing_the_version_pattern_is_still_major() -> None:
    class Before(BaseModel):
        contract_version: str = Field(default="1.0", pattern=_VERSION_PATTERN)

    class After(BaseModel):
        contract_version: str = Field(default="1.1", pattern=r"^1\.\d+$")

    assert _bump_for(Before, After)[0] == "major"


def test_the_version_pattern_on_another_field_is_still_major() -> None:
    class Before(BaseModel):
        schema_version: str = Field(default="1.0")

    class After(BaseModel):
        schema_version: str = Field(default="1.0", pattern=_VERSION_PATTERN)

    assert _bump_for(Before, After)[0] == "major"


def test_the_version_pattern_on_a_nested_contract_version_is_still_major() -> None:
    class Inner(BaseModel):
        contract_version: str = Field(default="1.0")

    class InnerAfter(BaseModel):
        contract_version: str = Field(default="1.0", pattern=_VERSION_PATTERN)

    class Before(BaseModel):
        inner: Inner

    class After(BaseModel):
        inner: InnerAfter

    assert _bump_for(Before, After)[0] == "major"


def test_the_pattern_with_a_minor_bump_needs_no_major_bump() -> None:
    """``evaluate_snapshot`` compares a committed snapshot with a fresh one: with the
    pattern added and a MINOR bump, the only problem left is "regenerate the
    snapshot"; with no bump it asks for a MINOR one, never a MAJOR one."""

    class Before(BaseModel):
        contract_version: str = Field(default="1.0")

    class After(BaseModel):
        contract_version: str = Field(default="1.1", pattern=_VERSION_PATTERN)

    def entry(model: type[BaseModel], version: str) -> dict:
        fields = field_summary(model)
        return {"version": version, "schema_hash": schema_hash(fields), "fields": fields}

    committed = {"m.C": entry(Before, "1.0")}
    (bumped,) = evaluate_snapshot(committed, {"m.C": entry(After, "1.1")})
    assert "wasn't regenerated" in bumped and "MAJOR" not in bumped
    (unbumped,) = evaluate_snapshot(committed, {"m.C": entry(After, "1.0")})
    assert "requires a MINOR version bump" in unbumped


def _snapshot_entry(model: type[BaseModel], version: str) -> dict:
    fields = field_summary(model)
    return {"version": version, "schema_hash": schema_hash(fields), "fields": fields}


class _Base(BaseModel):
    contract_version: str = Field(default="1.0")
    name: str


class _Added(BaseModel):
    contract_version: str = Field(default="1.0")
    name: str
    extra: str | None = None


class _Removed(BaseModel):
    contract_version: str = Field(default="1.0")


def test_a_pending_minor_bump_covers_an_added_optional_field() -> None:
    """ADR-008 Decision 8 (#1158): with a pending bump declared, the version and the
    snapshot stay at main's and the shape change is accepted."""
    committed = {"m.C": _snapshot_entry(_Base, "1.0")}
    actual = {"m.C": _snapshot_entry(_Added, "1.0")}
    assert evaluate_snapshot(committed, actual) != []
    assert evaluate_snapshot(committed, actual, {"m.C": "minor"}) == []
    assert evaluate_snapshot(committed, actual, {"m.C": "major"}) == []


def test_a_pending_minor_bump_does_not_cover_a_removed_field() -> None:
    committed = {"m.C": _snapshot_entry(_Base, "1.0")}
    actual = {"m.C": _snapshot_entry(_Removed, "1.0")}
    (problem,) = evaluate_snapshot(committed, actual, {"m.C": "minor"})
    assert "needs a MAJOR bump" in problem and "declares only MINOR" in problem
    assert evaluate_snapshot(committed, actual, {"m.C": "major"}) == []


def test_a_pending_bump_covers_only_its_own_contract() -> None:
    committed = {"m.C": _snapshot_entry(_Base, "1.0"), "m.D": _snapshot_entry(_Base, "1.0")}
    actual = {"m.C": _snapshot_entry(_Added, "1.0"), "m.D": _snapshot_entry(_Added, "1.0")}
    (problem,) = evaluate_snapshot(committed, actual, {"m.C": "minor"})
    assert problem.startswith("m.D:") and "bump_contract.py m.D minor" in problem


def test_a_pending_contract_is_compared_without_its_version() -> None:
    """#1393: the snapshot test cannot tell main's move of a pending contract from a
    hand bump, so it leaves the version to ``resolve_contract_versions.py --check
    --against``, which refuses the hand bump (``test_contract_bumps.py``)."""
    committed = {"m.C": _snapshot_entry(_Base, "1.0")}
    actual = {"m.C": _snapshot_entry(_Added, "1.1")}
    assert evaluate_snapshot(committed, actual, {"m.C": "minor"}) == []
    (problem,) = evaluate_snapshot(committed, actual)
    assert "wasn't regenerated" in problem


def test_a_change_with_a_regenerated_snapshot_but_no_bump_fails_against_main() -> None:
    """Reviewer case (#1209): a field added, the bump not declared, and the snapshot
    regenerated: the branch's own snapshot matches, so ``evaluate_snapshot`` passes;
    compared with main's snapshot it fails, unless a pending bump covers it or the
    version moved."""
    main = {"m.C": _snapshot_entry(_Base, "1.0")}
    branch = {"m.C": _snapshot_entry(_Added, "1.0")}
    assert evaluate_snapshot(branch, branch) == []
    (problem,) = evaluate_against_base(main, branch)
    assert "schema changed since main" in problem and "no pending bump covers it" in problem
    assert evaluate_against_base(main, branch, {"m.C": "minor"}) == []
    assert evaluate_against_base(main, {"m.C": _snapshot_entry(_Added, "1.1")}) == []
    assert evaluate_against_base(main, main) == []


def test_against_main_a_removal_needs_a_major_move_or_pending_major() -> None:
    main = {"m.C": _snapshot_entry(_Base, "1.0")}
    (problem,) = evaluate_against_base(main, {"m.C": _snapshot_entry(_Removed, "1.1")})
    assert "needs a MAJOR bump" in problem
    (problem,) = evaluate_against_base(
        main, {"m.C": _snapshot_entry(_Removed, "1.0")}, {"m.C": "minor"}
    )
    assert "declares only MINOR" in problem
    assert evaluate_against_base(main, {"m.C": _snapshot_entry(_Removed, "2.0")}) == []


def test_a_pending_bump_leaves_the_version_to_the_resolver() -> None:
    """#1393: main moved m.C (1.0 -> 1.1) after the branch declared a pending bump and
    committed its entry at 1.0. The shape matches, so the entry passes: the resolver
    writes the version after the last rebase."""
    committed = {"m.C": _snapshot_entry(_Added, "1.0")}
    rebased = {"m.C": _snapshot_entry(_Added, "1.1")}
    assert evaluate_snapshot(committed, rebased, {"m.C": "minor"}) == []


@pytest.mark.passes_on_main
def test_a_stale_version_fails_without_a_pending_bump_of_that_contract() -> None:
    """A resolved branch has no pending file left and must match exactly; a pending bump
    of another contract does not excuse it."""
    committed = {"m.C": _snapshot_entry(_Added, "1.0")}
    rebased = {"m.C": _snapshot_entry(_Added, "1.1")}
    for pending in ({}, {"m.D": "minor"}):
        (problem,) = evaluate_snapshot(committed, rebased, pending)
        assert "wasn't regenerated" in problem
