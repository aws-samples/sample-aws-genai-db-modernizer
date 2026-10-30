# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The access-pattern totals the results page shows are one fact (#792, ADR-032).

On a designed wordpress job (DynamoDB, cache layer and Aurora MySQL designs, no model), the
synthesis ``facts.access_pattern_totals`` equals what the explorer lists: the access patterns
of the owner engines' schema designs, the cache layer's apart. The explorer is built from the schema designs, the facts from ``query_groups``, so
this ties the two together.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.shared.cache_engines import CACHE_ENGINES
from tests.e2e.designed_run import run_designed_wordpress
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def designed(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    return run_designed_wordpress(tmp_path_factory.mktemp("ap-totals"), "ap-totals")


def _design_patterns(job_dir: Path) -> dict[str, list[dict]]:
    """engine -> the access patterns of its newest schema design."""
    out: dict[str, list[dict]] = {}
    for engine_dir in sorted(job_dir.glob("schema-*")):
        newest = max(engine_dir.glob("v*/schema_output.json"), key=lambda p: int(p.parent.name[1:]))
        out[engine_dir.name.removeprefix("schema-")] = (
            json.loads(newest.read_text()).get("access_patterns") or []
        )
    return out


def test_the_facts_equal_what_the_explorer_lists(designed: PipelineResult) -> None:
    report = json.loads(designed.report_json().read_text())
    totals = report["facts"]["access_pattern_totals"]
    patterns = _design_patterns(designed.job_dir())

    owner = {e: aps for e, aps in patterns.items() if e not in CACHE_ENGINES and aps}
    assert owner, "the designed run must give at least one owner engine access patterns"
    assert totals["owned_pattern_count"] == sum(len(a) for a in owner.values())

    cache = sum(len(a) for e, a in patterns.items() if e in CACHE_ENGINES)
    assert cache > 0, "the designed run must carry a cache-layer design"
    assert totals["cache_layer_pattern_count"] == cache
    assert totals["listed_pattern_count"] == totals["owned_pattern_count"] + cache


def test_the_totals_do_not_depend_on_the_stored_block(designed: PipelineResult) -> None:
    """The reader recomputes the same figures from ``query_groups`` for a block written
    before contract 1.26, so an old report shows the same totals."""
    from src.report.facts import report_facts

    report = json.loads(designed.report_json().read_text())
    stored = report["facts"]["access_pattern_totals"]
    del report["facts"]["access_pattern_totals"]
    assert report_facts(report)["access_pattern_totals"] == stored
