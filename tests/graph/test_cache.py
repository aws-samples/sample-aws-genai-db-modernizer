"""Tests for GraphStoreCache — LRU eviction of open graph connections."""

from src.graph import GraphStoreCache


def test_cache_returns_same_store_on_repeated_access(tmp_path):
    """Same (db_name, job_id) returns the same GraphStore instance."""
    cache = GraphStoreCache(max_size=3, base_dir=str(tmp_path))
    store1 = cache.get("mydb", "job-1")
    store2 = cache.get("mydb", "job-1")
    assert store1 is store2
    cache.close_all()


def test_cache_returns_different_stores_for_different_jobs(tmp_path):
    """Different job_ids return different GraphStore instances."""
    cache = GraphStoreCache(max_size=3, base_dir=str(tmp_path))
    store1 = cache.get("mydb", "job-1")
    store2 = cache.get("mydb", "job-2")
    assert store1 is not store2
    cache.close_all()


def test_cache_evicts_lru_when_full(tmp_path):
    """When cache exceeds max_size, least-recently-used entry is evicted."""
    cache = GraphStoreCache(max_size=2, base_dir=str(tmp_path))
    cache.get("db", "job-1")
    cache.get("db", "job-2")
    # Access job-1 again to make job-2 the LRU
    cache.get("db", "job-1")
    # Adding job-3 should evict job-2
    cache.get("db", "job-3")
    assert ("db", "job-2") not in cache._stores
    assert ("db", "job-1") in cache._stores
    assert ("db", "job-3") in cache._stores
    cache.close_all()
