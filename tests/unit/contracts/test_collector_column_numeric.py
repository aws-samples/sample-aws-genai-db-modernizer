"""Tests for numeric precision/scale on the collector column contract.

Collectors shell out to database CLIs and parse the text output, so these two
values arrive in whatever shape the CLI printed them — usually strings, and
empty strings for types that have neither. The coercion lives on the contract
rather than in each collector so four collectors do not carry four copies of the
same try/except.

The distinction worth preserving is ``None`` versus ``0``. ``None`` means "the
source did not tell us", which is a residual for the designer to resolve;
``0`` means "no digits right of the point", which is a fact. Coercing a
misparsed value to 0 would convert the first into the second and fabricate a
constraint.
"""

from __future__ import annotations

import pytest

from src.contracts.collector_output import Column


def _column(**kwargs: object) -> Column:
    return Column(column_name="amount", data_type="decimal", nullable=True, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw_precision", "raw_scale", "precision", "scale"),
    [
        # Driver-returned integers (psycopg, pymysql).
        (10, 2, 10, 2),
        # CLI-parsed text, which is the common case for these collectors.
        ("10", "2", 10, 2),
        # Oracle emits empty strings for columns that have neither.
        ("", "", None, None),
        (None, None, None, None),
        # SQL Server reports 0/0 for non-numeric types; 0 is a real value and
        # is preserved, and the type resolver rejects a zero precision.
        (0, 0, 0, 0),
        # Scale 0 with a real precision is DECIMAL(p,0), not missing data.
        ("18", "0", 18, 0),
    ],
)
def test_precision_and_scale_are_coerced_from_cli_shapes(
    raw_precision: object, raw_scale: object, precision: int | None, scale: int | None
) -> None:
    col = _column(numeric_precision=raw_precision, numeric_scale=raw_scale)

    assert col.numeric_precision == precision
    assert col.numeric_scale == scale


@pytest.mark.parametrize("garbage", ["N/A", "unknown", "abc", object()])
def test_unparseable_values_become_unknown_not_zero(garbage: object) -> None:
    """An unparseable value is unknown, which is a question, not a zero."""
    col = _column(numeric_precision=garbage, numeric_scale=garbage)

    assert col.numeric_precision is None
    assert col.numeric_scale is None


@pytest.mark.parametrize("negative", [-1, "-5"])
def test_negative_values_are_dropped_rather_than_clamped(negative: object) -> None:
    """A negative precision means the source was misparsed.

    Clamping to 0 would assert "no decimal digits" on evidence that says only
    that parsing failed, and the field carries ``ge=0`` so a negative cannot be
    stored regardless.
    """
    col = _column(numeric_precision=negative, numeric_scale=negative)

    assert col.numeric_precision is None
    assert col.numeric_scale is None


def test_defaults_are_absent_so_existing_payloads_stay_valid() -> None:
    """Both fields are optional; collector output predating them still parses."""
    col = Column(column_name="id", data_type="int", nullable=False)

    assert col.numeric_precision is None
    assert col.numeric_scale is None


def test_precision_is_independent_of_max_length() -> None:
    """The two describe different things and must not be conflated.

    Oracle historically returned NUMBER precision from its max_length
    normalizer, which made a character count mean two things.
    """
    col = Column(
        column_name="amount",
        data_type="decimal",
        nullable=True,
        max_length=22,
        numeric_precision=10,
        numeric_scale=2,
    )

    assert col.max_length == 22
    assert col.numeric_precision == 10
