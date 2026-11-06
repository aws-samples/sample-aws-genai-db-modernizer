# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``generate_rows`` keeps the rows ADR-033's shape check reads (#1942).

``generate_rows_golden.json`` was recorded from the generator as it was before #1942
extended it, over ``golden_tables.py``'s tables (every column kind, both dialects,
three seeds, two row caps). A caller that passes none of the new arguments or column
fields must get exactly those rows; a change here changes the shape check's results.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from src.tools.synthetic_data.generator import generate_rows
from tests.unit.tools.synthetic_data.golden_tables import GOLDEN_CASES, GOLDEN_TABLES

GOLDEN = json.loads((Path(__file__).parent / "generate_rows_golden.json").read_text())


def _plain(value: object) -> object:
    return {"decimal": str(value)} if isinstance(value, Decimal) else value


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", sorted(GOLDEN_TABLES))
def test_generate_rows_gives_the_rows_recorded_before_the_extension(name):
    for dialect, seed, max_rows in GOLDEN_CASES:
        t = generate_rows(GOLDEN_TABLES[name], dialect=dialect, seed=seed, max_rows=max_rows)
        got = {
            "rows": [{k: _plain(v) for k, v in r.items()} for r in t.rows],
            "edge_rows": t.edge_rows,
            "basis": t.row_count_basis,
            "unchecked": list(t.unchecked_source_columns),
        }
        assert got == GOLDEN[f"{name}|{dialect}|{seed}|{max_rows}"], (dialect, seed, max_rows)
