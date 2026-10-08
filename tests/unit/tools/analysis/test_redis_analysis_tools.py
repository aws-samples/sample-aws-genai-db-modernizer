"""Unit tests for Redis analysis tools (src/tools/analysis/redis_analysis_tools.py).

Verifies pattern detection and frequency_percent calculation (issue #453).
"""

from src.tools.analysis.redis_analysis_tools import analyze_redis_use_cases


def _make_collector(queries: list[dict]) -> dict:
    return {
        "queries": {
            "query_patterns": queries,
        }
    }


def test_redis_pattern_frequency_percent_independent_counts():
    """Each detected pattern must compute frequency_percent from its own query count,

    not from geospatial_queries (issue #453).
    """
    queries = [
        # 3 Caching queries (SELECT with calls_per_second > 1)
        {
            "query_id": "c1",
            "query_type": "SELECT",
            "calls_per_second": 10.0,
            "query_text": "SELECT * FROM products WHERE id = ?",
            "tables_accessed": ["products"],
        },
        {
            "query_id": "c2",
            "query_type": "SELECT",
            "calls_per_second": 5.0,
            "query_text": "SELECT * FROM categories WHERE id = ?",
            "tables_accessed": ["categories"],
        },
        {
            "query_id": "c3",
            "query_type": "SELECT",
            "calls_per_second": 2.0,
            "query_text": "SELECT * FROM settings WHERE k = ?",
            "tables_accessed": ["settings"],
        },
        # 2 Session queries (query_text contains 'session')
        {
            "query_id": "s1",
            "query_type": "SELECT",
            "calls_per_second": 0.5,
            "query_text": "SELECT * FROM user_sessions WHERE session_token = ?",
            "tables_accessed": ["user_sessions"],
        },
        {
            "query_id": "s2",
            "query_type": "UPDATE",
            "calls_per_second": 0.5,
            "query_text": "UPDATE user_sessions SET last_seen = NOW() WHERE session_id = ?",
            "tables_accessed": ["user_sessions"],
        },
        # 1 Leaderboard query ('order by' and 'limit')
        {
            "query_id": "l1",
            "query_type": "SELECT",
            "calls_per_second": 0.5,
            "query_text": "SELECT player_name, score FROM scores ORDER BY score DESC LIMIT 10",
            "tables_accessed": ["scores"],
        },
        # 1 Timeseries query ('created_at' and 'group by')
        {
            "query_id": "t1",
            "query_type": "SELECT",
            "calls_per_second": 0.5,
            "query_text": "SELECT count(*) FROM events WHERE created_at > ? GROUP BY event_type",
            "tables_accessed": ["events"],
        },
        # 1 Geospatial query (contains 'latitude')
        {
            "query_id": "g1",
            "query_type": "SELECT",
            "calls_per_second": 0.5,
            "query_text": "SELECT * FROM stores WHERE latitude BETWEEN ? AND ?",
            "tables_accessed": ["stores"],
        },
        # 2 Other queries that match no pattern
        {
            "query_id": "o1",
            "query_type": "INSERT",
            "calls_per_second": 0.1,
            "query_text": "INSERT INTO audit_log (msg) VALUES (?)",
            "tables_accessed": ["audit_log"],
        },
        {
            "query_id": "o2",
            "query_type": "DELETE",
            "calls_per_second": 0.1,
            "query_text": "DELETE FROM temp_data WHERE expired = 1",
            "tables_accessed": ["temp_data"],
        },
    ]

    # Total queries = 10
    total = len(queries)
    assert total == 10

    collector_output = _make_collector(queries)
    workload = analyze_redis_use_cases(collector_output)

    patterns_by_type = {p.pattern_type: p for p in workload.patterns_detected}

    # Verify each pattern was detected
    assert "caching" in patterns_by_type
    assert "session-store" in patterns_by_type
    assert "leaderboard" in patterns_by_type
    assert "time-series" in patterns_by_type
    assert "geospatial" in patterns_by_type

    # Verify each pattern's frequency_percent matches its own query count / total * 100
    # 3 / 10 * 100 = 30.0%
    assert patterns_by_type["caching"].frequency_percent == 30.0
    # 2 / 10 * 100 = 20.0%
    assert patterns_by_type["session-store"].frequency_percent == 20.0
    # 1 / 10 * 100 = 10.0%
    assert patterns_by_type["leaderboard"].frequency_percent == 10.0
    # 1 / 10 * 100 = 10.0%
    assert patterns_by_type["time-series"].frequency_percent == 10.0
    # 1 / 10 * 100 = 10.0%
    assert patterns_by_type["geospatial"].frequency_percent == 10.0


def test_redis_pattern_frequency_percent_zero_geospatial():
    """When geospatial queries are 0, other patterns must NOT report 0% frequency."""
    queries = [
        # 2 Caching queries
        {
            "query_id": "c1",
            "query_type": "SELECT",
            "calls_per_second": 10.0,
            "query_text": "SELECT * FROM items WHERE id = ?",
            "tables_accessed": ["items"],
        },
        {
            "query_id": "c2",
            "query_type": "SELECT",
            "calls_per_second": 5.0,
            "query_text": "SELECT * FROM items WHERE sku = ?",
            "tables_accessed": ["items"],
        },
        # 2 Session queries
        {
            "query_id": "s1",
            "query_type": "SELECT",
            "calls_per_second": 0.5,
            "query_text": "SELECT * FROM sessions WHERE session_token = ?",
            "tables_accessed": ["sessions"],
        },
        {
            "query_id": "s2",
            "query_type": "SELECT",
            "calls_per_second": 0.5,
            "query_text": "SELECT * FROM accounts WHERE user_id = ?",
            "tables_accessed": ["accounts"],
        },
    ]

    total = len(queries)
    assert total == 4

    collector_output = _make_collector(queries)
    workload = analyze_redis_use_cases(collector_output)

    patterns_by_type = {p.pattern_type: p for p in workload.patterns_detected}

    # Geospatial is 0 so not detected
    assert "geospatial" not in patterns_by_type

    # Caching and session must be 50.0% each, NOT 0.0%
    assert patterns_by_type["caching"].frequency_percent == 50.0
    assert patterns_by_type["session-store"].frequency_percent == 50.0
