# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The query-facts caches fit the collection being read (#384).

Both caches were bounded at 65,536 entries. A collection of 100,000 statements (each
read under its text and its lower-cased text) evicted every fact before its next
reader, so each step parsed the texts again, many times over: the assignment phase took
twice as long at 100,000 statements as with room for them. Loading a collection now
raises both bounds to fit it; a bound never falls.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.tools.sql import query_facts as qf


@pytest.fixture
def small_caches(monkeypatch):
    saved = (qf._query_facts.maxsize, qf._STORED_READINGS_MAX)
    qf._query_facts.cache_clear()
    monkeypatch.setattr(qf, "_FACT_CACHE_FLOOR", 8)
    qf._query_facts.maxsize = 8
    qf._STORED_READINGS_MAX = 8
    try:
        yield
    finally:
        qf._query_facts.cache_clear()
        qf._STORED_READINGS.clear()
        qf._query_facts.maxsize, qf._STORED_READINGS_MAX = saved


def _texts(n: int) -> list[str]:
    return [f"SELECT c{i} FROM t WHERE id = ?" for i in range(n)]


def test_the_cache_keeps_one_facts_object_per_text(small_caches):
    a = qf.query_facts("SELECT 1 FROM t", "mysql")
    assert qf.query_facts("SELECT 1 FROM t", "mysql") is a
    assert qf.query_facts("SELECT 1 FROM t", "postgresql") is not a


def test_past_its_bound_the_least_recently_used_is_forgotten(small_caches):
    texts = _texts(9)
    first = qf.query_facts(texts[0], "mysql")
    for t in texts[1:8]:
        qf.query_facts(t, "mysql")
    assert qf.query_facts(texts[0], "mysql") is first  # used again: now the newest
    qf.query_facts(texts[8], "mysql")  # forgets texts[1], the oldest
    assert len(qf._query_facts) == 8
    assert qf.query_facts(texts[0], "mysql") is first


def test_loading_a_collection_makes_room_for_all_of_it(small_caches):
    texts = _texts(50)
    collection = {
        "metadata": {"source_database": {"engine": "mysql"}},
        "queries": {"query_patterns": [{"query_text": t} for t in texts]},
    }
    qf.remember_collected_facts(collection)
    assert qf._query_facts.maxsize == 4 * 50 and qf._STORED_READINGS_MAX == 4 * 50
    with patch.object(qf, "QueryFacts", wraps=qf.QueryFacts) as made:
        for _ in range(3):
            for t in texts:
                qf.query_facts(t, "mysql")
    # Each text is read once, however many readers ask for it.
    assert made.call_count == 50


def test_a_bound_never_falls(small_caches):
    qf.reserve_fact_caches(1_000)
    assert qf._query_facts.maxsize == 4_000
    qf.reserve_fact_caches(10)
    assert qf._query_facts.maxsize == 4_000 and qf._STORED_READINGS_MAX == 4_000


def test_the_floor_stays_for_a_small_collection():
    assert qf._FACT_CACHE_FLOOR == 65536
    before = qf._query_facts.maxsize
    qf.reserve_fact_caches(3)
    assert qf._query_facts.maxsize == before >= qf._FACT_CACHE_FLOOR


def test_the_offline_parser_makes_room_before_it_reads(small_caches):
    from src.tools.database.offline_parser import parse_offline_collection

    raw = {
        "metadata": {"version": "8.0.36", "database_name": "app"},
        "tables": [{"table_name": "t", "row_count": 1}],
        "columns": [
            {"table_name": "t", "column_name": "id", "data_type": "int", "is_nullable": "NO"}
        ],
        "queries": [{"digest": f"d{i}", "query_text": t} for i, t in enumerate(_texts(30))],
    }
    parse_offline_collection(raw)
    assert qf._query_facts.maxsize == 4 * 30
