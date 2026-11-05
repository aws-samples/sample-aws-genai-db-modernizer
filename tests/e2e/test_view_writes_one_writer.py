# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1875: a write through a view keeps one writer per table, through routing.

The deterministic pipeline runs on the synthetic SQL Server collection of
``tests/unit/tools/sql/test_view_lineage.py`` (views, and writes and reads through
them). In every assignment version, a write through a view is in its base table's
write group, never in the group of a table it only reads, and every written table's
writes sit on one engine (ADR-031 Decision 1 Amendments 9 and 19).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.referee.write_groups import collector_write_groups
from tests.e2e.pipeline import PipelineResult, run_pipeline_from_file
from tests.unit.tools.sql.test_view_lineage import _collection


@pytest.fixture(scope="module")
def view_run(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    root = tmp_path_factory.mktemp("view-writes")
    src = root / "shop-collection.json"
    src.write_text(json.dumps(_collection()), encoding="utf-8")
    return run_pipeline_from_file(src, "shop", root / "artifacts", "views")


def _versions(run: PipelineResult) -> list[dict]:
    paths = sorted(
        (run.job_dir() / "assignment").glob("v*/assignment.json"),
        key=lambda p: int(p.parent.name[1:]),
    )
    return [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]


def test_a_write_through_a_view_keeps_one_writer_per_table(view_run: PipelineResult) -> None:
    run = view_run
    collector = json.loads((run.job_dir() / "collector" / "output.json").read_text("utf-8"))
    groups = collector_write_groups(collector)
    # INSERT INTO vw_Orders (...) SELECT ... FROM Products writes Orders, not Products.
    assert "w_insert" in groups["shop.Orders"]
    assert "shop.Products" not in groups
    # UPDATE vw_ActiveUsers SET Username lands on Users through two views.
    assert "w_nested" in groups["shop.Users"]
    # UPDATE vw_Users SET IsDeleted = 1, Username = @n: its columns come from both of
    # the view's tables, so the fallback writes every base table, and the write is in
    # both tables' groups (and places them as one unit).
    assert "w_mixed" in groups["shop.Users"]
    assert "w_mixed" in groups["shop.UserPortals"]
    versions = _versions(run)
    assert versions
    for assignment in versions:
        engine = {qa["query_id"]: qa["assigned_engine"] for qa in assignment["query_assignments"]}
        for table, members in groups.items():
            assert len({engine[q] for q in members}) == 1, (assignment["version"], table)
