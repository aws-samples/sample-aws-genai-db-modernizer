"""A read with no hit ratio of its own uses the collection's buffer-cache hit ratio (#1197).

DocumentDB bills a read I/O only when the page misses its buffer cache
(``reads_billed_on_cache_miss_only``). A raw collection reports the source's hit ratio
for the whole instance, not per query; before #1197 it was dropped, so every read was
billed as a miss. Both DocumentDB estimates (the per-query cost dimension routing scores
with, and the analysis's monthly estimate) now fall back to it, bounded (maintainer
decision on #1222, 2026-10-22): scaled by the target instance's memory over the
source's, and with at least 1% of the reads billed as misses, the floor alone when the
source's memory is unknown. The analysis estimate states the basis and the bound.
"""

from __future__ import annotations

import pytest

from src.agents.referee.query_cost import QueryCostModel
from src.shared import catalog, source_cache
from src.shared.catalog.prices import SECONDS_PER_MONTH
from src.shared.source_cache import (
    CollectionCache,
    collection_hit_ratio_pct,
    combined_basis,
    read_hit_ratio,
)
from src.tools.analysis.documentdb_analysis_tools import estimate_documentdb_costs

TABLES = [
    {
        "table_id": "d.topics",
        "table_name": "topics",
        "row_count": 100_000,
        "size_mb": 100.0,
        "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
    }
]


def _query(qid, qtype="SELECT", cps=100.0, **extra):
    verb = {"SELECT": "SELECT * FROM topics WHERE id = 1", "UPDATE": "UPDATE topics SET a = 1"}
    return {
        "query_id": qid,
        "query_text": verb[qtype],
        "query_type": qtype,
        "tables_accessed": ["d.topics"],
        "calls_per_second": cps,
        "rows_returned_avg": 1.0,
        "rows_affected_avg": 1.0,
        **extra,
    }


def _collector(*queries, hit=None, source_host=True, memory_gb=None):
    metrics: dict = {"performance_metrics": {}}
    if source_host:
        metrics["source_host"] = {
            "buffer_cache_hit_ratio_pct": hit,
            "physical_memory_gb": memory_gb,
        }
    return {
        "metadata": {"source_database": {"engine": "mysql"}},
        "database_schema": {"tables": TABLES},
        "queries": {"query_patterns": list(queries)},
        "metrics": metrics,
    }


TARGET_GB = 16.0  # db.r6g.large, the instance DocumentDB's estimate prices


def _cache(pct: float, memory_gb: float | None = None) -> CollectionCache:
    return CollectionCache(hit_ratio_pct=pct, source_memory_gb=memory_gb)


def test_documentdb_bills_reads_on_a_cache_miss_only() -> None:
    assert catalog.pricing("documentdb").reads_billed_on_cache_miss_only


def test_the_target_memory_is_the_instance_the_estimate_prices() -> None:
    prices = catalog.pricing("documentdb")
    assert prices.estimate_instance_class == "db.r6g.large"
    assert prices.estimate_instance_memory_gb == TARGET_GB
    assert estimate_documentdb_costs(_collector()).cost_components["instance_type"] == (
        "db.r6g.large"
    )


# --- the helper -------------------------------------------------------------------


def test_a_query_s_own_ratio_wins_over_the_collection_s() -> None:
    assert read_hit_ratio({"cache_hit_ratio_pct": 50.0}, _cache(90.0)) == (50.0, "query")


def test_a_query_s_own_zero_is_its_own_ratio_not_missing() -> None:
    assert read_hit_ratio({"cache_hit_ratio_pct": 0.0}, _cache(90.0)) == (0.0, "query")


def test_no_own_ratio_falls_back_to_the_collection() -> None:
    assert read_hit_ratio({"cache_hit_ratio_pct": None}, _cache(90.0)) == (90.0, "collection")
    assert read_hit_ratio({}, _cache(90.0), TARGET_GB) == (90.0, "collection")


def test_neither_bills_every_read() -> None:
    assert read_hit_ratio({}, None) == (0.0, "none")
    assert read_hit_ratio({}, None, TARGET_GB) == (0.0, "none")


# --- the bound (maintainer decision on #1222, 2026-10-22) ---------------------------


def test_the_floor_is_one_constant_of_one_percent() -> None:
    assert source_cache.MIN_BILLED_MISS_SHARE == 0.01


def test_unknown_source_memory_applies_the_floor_only() -> None:
    # wordpress: 99.99993% collected, no host memory reported -> 1% of reads billed.
    assert read_hit_ratio({}, _cache(99.99993), TARGET_GB) == (99.0, "collection")
    # Below the floor's cap, an unscaled ratio is used as collected.
    assert read_hit_ratio({}, _cache(90.0), TARGET_GB) == (90.0, "collection")


def test_unknown_target_memory_applies_the_floor_only() -> None:
    assert read_hit_ratio({}, _cache(99.99993, 64.0), None) == (99.0, "collection")
    assert read_hit_ratio({}, _cache(90.0, 64.0), None) == (90.0, "collection")


def test_a_larger_source_scales_the_ratio_down() -> None:
    # trailgear: 99.551608% on a 64 GiB host; a 16 GiB target keeps a quarter.
    hit, basis = read_hit_ratio({}, _cache(99.551608, 64.0), TARGET_GB)
    assert (hit, basis) == (pytest.approx(24.887902), "collection")


def test_a_smaller_source_never_raises_the_ratio() -> None:
    # Scale down only, min(1, target / source): a hit ratio does not grow linearly with
    # memory, so an 8 GB source's 40% stays 40% on the 16 GiB target.
    assert read_hit_ratio({}, _cache(40.0, 8.0), TARGET_GB) == (40.0, "collection")
    assert read_hit_ratio({}, _cache(60.0, 1.0), TARGET_GB) == (60.0, "collection")
    assert read_hit_ratio({}, _cache(90.0, 16.0), TARGET_GB) == (90.0, "collection")
    # The floor still caps it.
    assert read_hit_ratio({}, _cache(99.9, 8.0), TARGET_GB) == (99.0, "collection")
    assert _cache(40.0, 8.0).memory_scale(TARGET_GB) == 1.0
    assert _cache(40.0, 64.0).memory_scale(TARGET_GB) == 0.25


def test_the_source_cache_never_makes_a_fallback_read_free() -> None:
    for memory in (None, 1.0, 16.0, 512.0):
        hit, _ = read_hit_ratio({}, _cache(100.0, memory), TARGET_GB)
        assert hit <= 99.0


def test_zero_or_nonsense_memory_is_unknown_not_a_division() -> None:
    for memory in (0, -4, "lots", float("nan"), True):
        cache = CollectionCache.from_collector(_collector(hit=99.99993, memory_gb=memory))
        assert cache is not None and cache.source_memory_gb is None
        assert read_hit_ratio({}, cache, TARGET_GB) == (99.0, "collection")


def test_a_query_s_own_ratio_is_floored_but_never_scaled() -> None:
    # The floor applies to every read: a query's own 100% still bills 1% of its reads.
    assert read_hit_ratio({"cache_hit_ratio_pct": 100.0}, None) == (99.0, "query")
    assert read_hit_ratio({"cache_hit_ratio_pct": 99.9}, _cache(99.0, 64.0), TARGET_GB) == (
        99.0,
        "query",
    )
    # Below the cap it is used as reported, never scaled by the memories.
    assert read_hit_ratio({"cache_hit_ratio_pct": 80.0}, _cache(99.0, 64.0), TARGET_GB) == (
        80.0,
        "query",
    )


def test_a_query_s_own_full_ratio_is_never_free_in_either_estimate() -> None:
    own = _query("o", cache_hit_ratio_pct=100.0)
    full = QueryCostModel.from_collector(_collector(_query("o"), source_host=False))
    model = QueryCostModel.from_collector(_collector(own, source_host=False))
    assert model.variable_monthly(own, "documentdb") == pytest.approx(
        0.01 * full.variable_monthly(_query("o"), "documentdb")
    )
    est = estimate_documentdb_costs(_collector(own, source_host=False))
    assert est.cost_components["read_io_hit_ratio_basis"] == "query"
    assert est.cost_components["min_billed_miss_pct"] == 1.0
    assert est.cost_components["io_requests"] == pytest.approx(
        _io(_collector(_query("o"), source_host=False)) * 0.01, abs=0.01
    )
    assert "At least 1% of every read is billed as a miss." in est.pricing_assumptions[3]


def test_the_floor_constant_is_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_cache, "MIN_BILLED_MISS_SHARE", 0.05)
    assert read_hit_ratio({}, _cache(99.99993), TARGET_GB) == (pytest.approx(95.0), "collection")
    assert read_hit_ratio({}, _cache(50.0), TARGET_GB) == (50.0, "collection")


def test_a_collection_without_source_host_has_no_ratio() -> None:
    assert collection_hit_ratio_pct(_collector(source_host=False)) is None
    assert collection_hit_ratio_pct(_collector(hit=None)) is None
    assert collection_hit_ratio_pct({}) is None
    assert collection_hit_ratio_pct(None) is None
    assert collection_hit_ratio_pct(_collector(hit=97.5)) == 97.5


def test_the_basis_names_every_source_used() -> None:
    assert combined_basis(["query", "query"]) == "query"
    assert combined_basis(["collection"]) == "collection"
    assert combined_basis(["collection", "query"]) == "query+collection"
    assert combined_basis(["none", "query"]) == "query+none"
    assert combined_basis([]) == "none"


# --- the per-query cost dimension ---------------------------------------------------


def test_the_cost_dimension_falls_back_to_the_collection_ratio() -> None:
    q = _query("q")
    billed_all = QueryCostModel.from_collector(_collector(q, source_host=False))
    with_host = QueryCostModel.from_collector(_collector(q, hit=90.0))
    assert with_host.collection_cache == CollectionCache(hit_ratio_pct=90.0)
    assert with_host.variable_monthly(q, "documentdb") == pytest.approx(
        0.1 * billed_all.variable_monthly(q, "documentdb")
    )


def test_the_query_s_own_ratio_still_wins_in_the_cost_dimension() -> None:
    own = _query("q", cache_hit_ratio_pct=50.0)
    plain = _query("p")
    model = QueryCostModel.from_collector(_collector(own, plain, hit=90.0))
    billed_all = QueryCostModel.from_collector(_collector(plain, source_host=False))
    full = billed_all.variable_monthly(plain, "documentdb")
    assert model.variable_monthly(own, "documentdb") == pytest.approx(0.5 * full)
    assert model.variable_monthly(plain, "documentdb") == pytest.approx(0.1 * full)


def test_the_cost_dimension_bounds_the_collection_ratio() -> None:
    q = _query("q")
    full = QueryCostModel.from_collector(_collector(q, source_host=False)).variable_monthly(
        q, "documentdb"
    )
    # Floor only: wordpress' 99.99993% bills 1% of the reads, never $0.
    floored = QueryCostModel.from_collector(_collector(q, hit=99.99993))
    assert floored.variable_monthly(q, "documentdb") == pytest.approx(0.01 * full)
    # Scaled: a 64 GiB source and the 16 GiB target keep a quarter of 99.551608%.
    scaled = QueryCostModel.from_collector(_collector(q, hit=99.551608, memory_gb=64.0))
    assert scaled.variable_monthly(q, "documentdb") == pytest.approx(
        (1 - 0.24887902) * full, rel=1e-6
    )


def test_writes_and_other_engines_ignore_the_ratio() -> None:
    write = _query("w", qtype="UPDATE")
    read = _query("r")
    a = QueryCostModel.from_collector(_collector(write, read, source_host=False))
    b = QueryCostModel.from_collector(_collector(write, read, hit=99.0))
    assert b.variable_monthly(write, "documentdb") == a.variable_monthly(write, "documentdb")
    assert b.variable_monthly(read, "dynamodb") == a.variable_monthly(read, "dynamodb")


# --- the DocumentDB monthly estimate ------------------------------------------------


def _io(collector) -> float:
    return float(estimate_documentdb_costs(collector).cost_components["io_requests"])


def test_without_any_ratio_the_estimate_is_unchanged_and_says_so() -> None:
    est = estimate_documentdb_costs(_collector(_query("r"), _query("w", "UPDATE"), hit=None))
    # 200 calls a second, one I/O each, at the engine file's price.
    price = catalog.pricing("documentdb").read_unit_price_per_million or 0.0
    assert est.cost_components["io_requests"] == pytest.approx(
        round(200 * SECONDS_PER_MONTH / 1e6 * price, 2)
    )
    assert est.cost_components["read_io_hit_ratio_basis"] == "none"
    assert est.cost_components["collection_hit_ratio_pct"] is None
    assert any("every read billed" in a for a in est.pricing_assumptions or [])


def test_the_estimate_bills_only_read_misses_at_the_collection_ratio() -> None:
    read, write = _query("r"), _query("w", "UPDATE")
    before = _io(_collector(read, write, source_host=False))
    est = estimate_documentdb_costs(_collector(read, write, hit=90.0))
    # Reads drop to a tenth; writes are billed in full.
    assert est.cost_components["io_requests"] == pytest.approx(before * (0.5 + 0.05), abs=0.01)
    assert est.cost_components["read_io_hit_ratio_basis"] == "collection"
    assert est.cost_components["collection_hit_ratio_pct"] == 90.0
    assert est.cost_components["collection_hit_ratio_billed_pct"] == 90.0
    assert any("instance-wide buffer-cache hit ratio (90%)" in a for a in est.pricing_assumptions)


def test_the_estimate_states_the_bound_it_applied() -> None:
    read = _query("r")
    full = _io(_collector(read, source_host=False))
    est = estimate_documentdb_costs(_collector(read, hit=99.551608, memory_gb=64.0))
    components = est.cost_components
    assert components["collection_hit_ratio_pct"] == 99.551608
    assert components["collection_hit_ratio_billed_pct"] == pytest.approx(24.887902)
    assert components["source_memory_gb"] == 64.0
    assert components["target_memory_gb"] == TARGET_GB
    assert components["min_billed_miss_pct"] == 1.0
    assert components["io_requests"] == pytest.approx(full * (1 - 0.24887902), abs=0.01)
    assert (
        "Read I/O: only cache misses billed, at the source's instance-wide buffer-cache hit "
        "ratio (99.551608%) scaled by the target's 16 GiB over the source's 64 GiB of memory, "
        "billed at 24.887902%. At least 1% of every read is billed as a miss."
    ) in est.pricing_assumptions


def test_the_estimate_floors_an_unscaled_ratio() -> None:
    read = _query("r")
    full = _io(_collector(read, source_host=False))
    est = estimate_documentdb_costs(_collector(read, hit=99.99993))
    assert est.cost_components["collection_hit_ratio_billed_pct"] == 99.0
    assert est.cost_components["source_memory_gb"] is None
    assert est.cost_components["io_requests"] == pytest.approx(full * 0.01, abs=0.01)
    assert any(
        "not scaled to the target's memory (the source reported no memory size)" in a
        for a in est.pricing_assumptions
    )


def test_the_estimate_uses_a_query_s_own_ratio_first() -> None:
    own = _query("o", cache_hit_ratio_pct=0.0)
    est = estimate_documentdb_costs(_collector(own, _query("p"), hit=100.0))
    # The query that reports 0% is billed in full; the other falls back to 100%,
    # floored: 1% of its reads are billed.
    assert est.cost_components["io_requests"] == pytest.approx(
        _io(_collector(own, hit=None)) * 1.01, abs=0.01
    )
    assert est.cost_components["read_io_hit_ratio_basis"] == "query+collection"


def test_a_workload_with_no_reads_says_so() -> None:
    est = estimate_documentdb_costs(_collector(_query("w", "UPDATE"), hit=99.0))
    assert est.cost_components["read_io_hit_ratio_basis"] == "no_reads"
    assert "Read I/O: the workload has no reads" in (est.pricing_assumptions or [])


def test_the_stated_ratio_keeps_its_precision() -> None:
    from src.shared.source_cache import format_pct

    assert format_pct(99.99993) == "99.99993"
    assert format_pct(99.989231) == "99.989231"
    assert format_pct(90.0) == "90"


# --- a collector output saved before #1197 by a live or agent path -----------------


def _old_live(*ratios, memory_gb=None):
    """The old collector copied the instance ratio, rounded to 2 places, onto every query
    and wrote no ``source_host`` ratio."""
    queries = [_query(f"q{i}", cache_hit_ratio_pct=r) for i, r in enumerate(ratios)]
    return _collector(*queries, hit=None, memory_gb=memory_gb)


def test_one_ratio_on_every_query_of_an_old_file_is_the_instance_figure() -> None:
    old = _old_live(100.0, 100.0, 100.0)
    cache = CollectionCache.from_collector(old)
    assert cache == CollectionCache(hit_ratio_pct=100.0, from_queries=True)
    # Billed and stated as the instance ratio (floored), not as each query's own.
    assert read_hit_ratio(old["queries"]["query_patterns"][0], cache, TARGET_GB) == (
        99.0,
        "collection",
    )
    est = estimate_documentdb_costs(old)
    assert est.cost_components["read_io_hit_ratio_basis"] == "collection"
    assert est.cost_components["collection_hit_ratio_pct"] == 100.0
    assert any("instance-wide buffer-cache hit ratio (100%)" in a for a in est.pricing_assumptions)


def test_an_old_file_s_copied_ratio_is_scaled_by_the_host_memory() -> None:
    old = _old_live(99.55, 99.55, memory_gb=64.0)
    cache = CollectionCache.from_collector(old)
    assert cache is not None and cache.from_queries and cache.source_memory_gb == 64.0
    q = old["queries"]["query_patterns"][1]
    hit, basis = read_hit_ratio(q, cache, TARGET_GB)
    assert (hit, basis) == (pytest.approx(24.8875), "collection")
    model = QueryCostModel.from_collector(old)
    full = QueryCostModel.from_collector(_collector(_query("q1"), source_host=False))
    assert model.variable_monthly(q, "documentdb") == pytest.approx(
        (1 - 0.248875) * full.variable_monthly(_query("q1"), "documentdb"), rel=1e-6
    )


def test_differing_or_partial_per_query_ratios_stay_each_query_s_own() -> None:
    for old in (
        _old_live(90.0, 80.0),  # real per-query figures differ
        _old_live(90.0, None),  # one query reports none
        _old_live(90.0),  # one query: nothing to tell it apart from its own
    ):
        assert CollectionCache.from_collector(old) is None
        q = old["queries"]["query_patterns"][0]
        assert read_hit_ratio(q, None, TARGET_GB) == (90.0, "query")


def test_a_source_host_ratio_wins_over_copied_query_ratios() -> None:
    queries = [_query("a", cache_hit_ratio_pct=100.0), _query("b", cache_hit_ratio_pct=100.0)]
    current = _collector(*queries, hit=99.99993)
    cache = CollectionCache.from_collector(current)
    assert cache == CollectionCache(hit_ratio_pct=99.99993)
    # A current file's per-query figure is that query's own.
    assert read_hit_ratio(queries[0], cache, TARGET_GB) == (99.0, "query")
