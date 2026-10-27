"""Pinned assess() results keep today's table figures (#1636).

The collector output now keeps a count and a size the source does not have as None. A
result pinned to contract 1.0, 1.1 or 1.2 states them as every collector wrote them
before: 0. Contract 1.3 (#1635) states them as unknown.
"""

from __future__ import annotations

import pytest

from src.assessment.result_builder import _row_count_through_1_2, _size_through_1_2


@pytest.mark.parametrize(
    ("table", "row_count", "size_mb"),
    [
        # A table with no statistics, an Oracle table with no segment found, a DDL table.
        ({"row_count": None, "size_mb": 3.5}, 0, 3.5),
        ({"row_count": 40, "size_mb": None}, 40, 0.0),
        ({"row_count": None, "size_mb": None}, 0, 0.0),
        # Known figures and a missing field are as collected.
        ({"row_count": 0, "size_mb": 0.0}, 0, 0.0),
        ({"row_count": 12, "size_mb": 1.25}, 12, 1.25),
        ({}, None, None),
    ],
)
def test_a_pinned_result_states_an_unknown_figure_as_0(
    table: dict, row_count: int | None, size_mb: float | None
) -> None:
    assert (_row_count_through_1_2(table), _size_through_1_2(table)) == (row_count, size_mb)
