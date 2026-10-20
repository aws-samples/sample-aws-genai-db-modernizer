"""The read I/O basis holds when an engine is sized for part of the collection (#1197, #1374).

The TCO sizes a kept DocumentDB for the queries it is assigned. Two things must not
change with that subset: the collection's cache figure (decided once, on the whole
collection, as routing does) and what the customer is told when the subset has no reads.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.agents.referee.assigned_cost import sized_cost_estimate
from src.agents.referee.query_cost import QueryCostModel
from src.shared import catalog
from src.shared.catalog.engines import EnginePricing
from src.shared.source_cache import NO_READS_BASIS, cost_basis_fields, cost_basis_sentence
from src.tools.analysis import documentdb_analysis_tools as docdb
from src.tools.analysis.documentdb_analysis_tools import estimate_documentdb_costs
from tests.unit.agents.referee.test_cache_hit_fallback import (
    TARGET_GB,
    _collector,
    _old_live,
    _query,
)


def _assignment(engine: str, *qids: str) -> dict:
    return {
        "query_assignments": [
            {
                "query_id": q,
                "assigned_engine": engine,
                "in_scope": True,
                "source_tables": ["d.topics"],
            }
            for q in qids
        ]
    }


def _row(estimate) -> dict:
    return cost_basis_fields({"cost_components": estimate.cost_components})


def test_a_writes_only_sizing_does_not_say_the_source_reported_no_ratio() -> None:
    collector = _collector(_query("r"), _query("w", "UPDATE"), hit=99.9)
    sized = sized_cost_estimate("documentdb", collector, _assignment("documentdb", "w"))
    assert sized is not None
    estimate, _ = sized
    assert estimate.cost_components["read_io_hit_ratio_basis"] == NO_READS_BASIS
    sentence = cost_basis_sentence("DocumentDB", _row(estimate))
    assert sentence is not None
    assert "no buffer-cache hit ratio" not in sentence
    assert "writes only" in sentence and "no read I/O" in sentence


def test_reads_without_any_ratio_still_say_none_was_reported() -> None:
    collector = _collector(_query("r"), hit=None)
    estimate, _ = sized_cost_estimate("documentdb", collector, _assignment("documentdb", "r"))
    sentence = cost_basis_sentence("DocumentDB", _row(estimate))
    assert sentence is not None and "the source reported no buffer-cache hit ratio" in sentence


def test_routing_and_the_sized_row_use_one_ratio_on_an_old_file() -> None:
    old = _old_live(99.5, 99.5, 99.5, memory_gb=64.0)
    q = old["queries"]["query_patterns"][0]
    routed = QueryCostModel.from_collector(old).variable_monthly(q, "documentdb")
    # Routing bills the instance ratio scaled by 16/64: 24.875% hit.
    assert routed > 0
    estimate, _ = sized_cost_estimate("documentdb", old, _assignment("documentdb", "q0"))
    row = _row(estimate)
    assert row["read_io_hit_ratio_basis"] == "collection"
    assert row["collection_hit_ratio_billed_pct"] == pytest.approx(99.5 * TARGET_GB / 64.0)
    # The whole-collection estimate and the sized row state the same ratio and bound.
    whole = _row(estimate_documentdb_costs(old))
    assert row == whole
    # Left to decide on its one query, the estimate would read 99.5% as that query's own.
    alone = _row(estimate_documentdb_costs({**old, "queries": {"query_patterns": [q]}}))
    assert alone["read_io_hit_ratio_basis"] == "query"


def test_a_subset_of_a_current_file_is_not_read_as_an_old_file() -> None:
    # Two queries report the same own ratio, but the whole collection has a third that
    # differs: they are their own ratios, not the instance's.
    current = _collector(
        _query("a", cache_hit_ratio_pct=95.0),
        _query("b", cache_hit_ratio_pct=95.0),
        _query("c", cache_hit_ratio_pct=50.0),
        hit=None,
    )
    estimate, _ = sized_cost_estimate("documentdb", current, _assignment("documentdb", "a", "b"))
    assert estimate.cost_components["read_io_hit_ratio_basis"] == "query"


def test_the_estimate_with_no_decided_cache_still_decides_for_itself() -> None:
    old = _old_live(100.0, 100.0)
    assert estimate_documentdb_costs(old).cost_components["read_io_hit_ratio_basis"] == "collection"


def test_a_missing_estimate_instance_class_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real = catalog.pricing("documentdb")
    monkeypatch.setattr(
        docdb.catalog,
        "pricing",
        lambda _e: real.model_copy(
            update={"estimate_instance_class": None, "estimate_instance_memory_gb": None}
        ),
    )
    with pytest.raises(ValueError, match="estimate_instance_class"):
        estimate_documentdb_costs(_collector(_query("r")))


def test_an_instance_class_the_price_table_does_not_price_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = catalog.pricing("documentdb")
    monkeypatch.setattr(
        docdb.catalog,
        "pricing",
        lambda _e: real.model_copy(update={"estimate_instance_class": "db.r9z.huge"}),
    )
    with pytest.raises(ValueError, match="db.r9z.huge"):
        estimate_documentdb_costs(_collector(_query("r")))


def test_an_instance_class_needs_its_memory() -> None:
    with pytest.raises(ValidationError, match="estimate_instance_memory_gb"):
        EnginePricing(estimate_instance_class="db.r6g.large")
    with pytest.raises(ValidationError, match="estimate_instance_class"):
        EnginePricing(estimate_instance_memory_gb=16)
