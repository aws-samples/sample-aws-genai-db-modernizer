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

from pydantic import BaseModel, Field

from scripts.contract_schema_snapshot import (
    diff_fields,
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
