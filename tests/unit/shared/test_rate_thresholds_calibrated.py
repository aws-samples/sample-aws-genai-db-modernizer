# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The engine-analysis rate thresholds live in the rule catalog with their sources (#1994).

Rates are a statement's calls over its observed window (#1901), so a threshold that was a
fixed calls/s figure fired on an ordinary busy workload. Each one is now derived from the
engine's documented capacity for the access pattern, or from the other engine's floor for
the same queries, and both Aurora engines carry the same values.
"""

from __future__ import annotations

import pytest

from src.shared.catalog.engines import engine_entry, engine_threshold
from src.shared.catalog.patterns import pattern
from src.shared.catalog.prices import load_price_table

AURORA = ("aurora_mysql", "aurora_postgresql")
CAPACITY = "memory_read_capacity_calls_per_second"
TEXT_SEARCH = "high_volume_text_search_min_calls_per_second"

# The documented figure and the scaling the catalog states.
DOCUMENTED_SELECTS_PER_SECOND = 700_000
DOCUMENTED_VCPUS = 64  # db.r4.16xlarge
SMALLEST_CLASS = "db.r6g.large"
SMALLEST_VCPUS = 2


@pytest.mark.parametrize("engine", AURORA)
@pytest.mark.parametrize("name", [CAPACITY, TEXT_SEARCH])
def test_each_aurora_threshold_is_in_the_catalog_with_its_source(engine, name) -> None:
    entry = engine_entry(engine)
    assert entry is not None and name in entry.thresholds
    assert entry.thresholds[name].why


def test_both_aurora_engines_agree_on_every_rate_threshold() -> None:
    for name in (CAPACITY, TEXT_SEARCH):
        assert len({engine_threshold(e, name) for e in AURORA}) == 1, name


def test_the_capacity_threshold_is_the_documented_figure_scaled_to_the_smallest_class() -> None:
    value = engine_threshold("aurora_mysql", CAPACITY)
    assert value == DOCUMENTED_SELECTS_PER_SECOND * SMALLEST_VCPUS / DOCUMENTED_VCPUS
    why = engine_entry("aurora_mysql").thresholds[CAPACITY].why  # type: ignore[union-attr]
    assert "Amazon Aurora Performance Assessment Technical Guide" in why
    assert "700,000" in why and "db.r4.16xlarge" in why and SMALLEST_CLASS in why
    # The class it scales to is the smallest memory-optimized one the price table lists.
    prices = load_price_table()
    for engine in AURORA:
        classes = prices.section(f"{engine}.io_optimized")["instance_hourly"]
        memory_optimized = sorted(c for c in classes if c.startswith("db.r"))
        assert SMALLEST_CLASS in memory_optimized
        assert all(c.endswith(("large", "xlarge")) for c in memory_optimized)


def test_the_analysis_reads_the_catalog_values() -> None:
    from src.tools.analysis import aurora_common_pattern_catalog as catalog

    capacity = engine_threshold("aurora_mysql", CAPACITY)
    assert catalog.HIGH_FREQUENCY_PK_LOOKUP_CPS == capacity
    assert catalog.CACHE_READ_CPS_THRESHOLD == capacity
    assert catalog.HIGH_VOLUME_TEXT_SEARCH_CPS == engine_threshold("aurora_mysql", TEXT_SEARCH)


def test_text_search_agrees_with_opensearch_for_the_same_queries() -> None:
    """Aurora's high-volume-text-search points a table at OpenSearch from the same rate
    OpenSearch's own floor keeps a source-served text search (#326)."""
    assert engine_threshold("aurora_mysql", TEXT_SEARCH) == engine_threshold(
        "opensearch", "native_override_min_calls_per_second"
    )


@pytest.mark.parametrize(
    ("pattern_id", "engine"),
    [
        ("high-frequency-pk-lookup", "aurora_mysql"),
        ("high-frequency-pk-lookup", "aurora_postgresql"),
        ("simple-cache-read", "aurora_mysql"),
        ("simple-cache-read", "aurora_postgresql"),
    ],
)
def test_the_rule_text_states_the_catalog_rate(pattern_id, engine) -> None:
    capacity = engine_threshold(engine, CAPACITY)
    why = pattern(pattern_id).engines[engine].why
    assert f"above {capacity:,.0f} calls/second" in why


@pytest.mark.parametrize("engine", AURORA)
def test_the_key_lookup_concern_states_the_same_rate(engine) -> None:
    capacity = engine_threshold(engine, CAPACITY)
    (concern,) = pattern("key-value-lookup").engines[engine].concerns
    assert f"Above {capacity:,.0f} calls per second" in concern
