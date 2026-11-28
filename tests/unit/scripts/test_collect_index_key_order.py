# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2289: whether an index's key order is known reaches collector/output.json.

The offline parser marks each index whose rows gave their key positions (#1927); every
collector rebuilds the index for the contract and must carry the mark, or the design
comparison takes every collection as one of unknown order and never reports a real
reordering of a key. This runs the collector on the bundled WordPress sample and then
the comparison on its output.
"""

from __future__ import annotations

import copy
import json
import subprocess  # nosec B404 -- runs this repo's own run_collect.py script with fixed argv
import sys
import zipfile
from pathlib import Path

import pytest

from src.tools.schema.aurora_common.ddl_generator import MYSQL, render_index
from src.tools.schema.aurora_common.saved_design import index_shape
from src.tools.schema.aurora_common.source_changes import design_source_changes

REPO = Path(__file__).resolve().parents[3]
DESIGN = (
    REPO / "tests/fixtures/real_runs/wordpress-both/job/schema-aurora_mysql/v2/schema_output.json"
)


@pytest.fixture(scope="module")
def collected(tmp_path_factory: pytest.TempPathFactory) -> dict:
    tmp = tmp_path_factory.mktemp("collect")
    with zipfile.ZipFile(REPO / "docs/examples/wordpress/wordpress.zip") as z:
        z.extractall(tmp / "in")
    root = tmp / "artifacts"
    subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, "scripts/run_collect.py", "--file",
         str(tmp / "in" / "wordpress-collection.json"), "--db", "wordpress",
         "--job-id", "ord00001", "--artifact-root", str(root)],
        cwd=REPO, capture_output=True, text=True, check=True,
    )  # fmt: skip
    output: dict = json.loads((root / "wordpress/ord00001/collector/output.json").read_text())
    return output


def test_every_index_in_the_output_says_its_key_order_is_known(collected: dict) -> None:
    marks = [
        i.get("key_order_known")
        for t in collected["database_schema"]["tables"]
        for i in t.get("indexes") or []
    ]
    assert marks and set(marks) == {True}


def test_a_real_key_reordering_is_then_reported(collected: dict) -> None:
    """On that output, a design index whose key columns are the source's in another order
    is changed in its key columns; the same index in the source's order is the same."""
    design = json.loads(DESIGN.read_text())
    rows = design_source_changes("aurora_mysql", design, collected)
    assert rows is not None
    tables = {t["table_name"]: t for t in collected["database_schema"]["tables"]}
    # A multi-column source index the design creates as collected.
    picked = None
    for i, table in enumerate(design["table_definitions"]):
        source = {
            x["index_name"]: x for x in (tables.get(table["table_name"]) or {}).get("indexes") or []
        }
        for n, statement in enumerate(table.get("indexes") or []):
            shape = index_shape("aurora_mysql", statement)
            src = source.get(shape["name"]) if shape else None
            row = next(
                (r for r in rows[i]["indexes"] if shape and r["name"] == shape["name"]), None
            )
            if (
                src
                and len(src["columns"]) > 1
                and not src.get("prefix_lengths")
                and row
                and row["change"] == "same"
            ):
                picked = (i, n, table["table_name"], src)
                break
        if picked:
            break
    assert picked, "no multi-column index the design creates as collected"
    i, n, name, index = picked
    reordered = copy.deepcopy(design)
    reordered["table_definitions"][i]["indexes"][n] = render_index(
        MYSQL, name, index["index_name"], list(reversed(index["columns"])),
        unique=bool(index["is_unique"]),
    )  # fmt: skip
    rows = design_source_changes("aurora_mysql", reordered, collected)
    assert rows is not None
    (row,) = [r for r in rows[i]["indexes"] if r["name"] == index["index_name"]]
    assert (row["change"], row["differences"]) == ("changed", ["columns"])
    # The same reordering on a collection that does not give key positions is no claim.
    unknown = copy.deepcopy(collected)
    for t in unknown["database_schema"]["tables"]:
        for x in t.get("indexes") or []:
            x["key_order_known"] = None
    rows = design_source_changes("aurora_mysql", reordered, unknown)
    assert rows is not None
    (row,) = [r for r in rows[i]["indexes"] if r["name"] == index["index_name"]]
    assert row["change"] == "same"
