"""
Focused per-pattern fixtures for Redis analysis testing.

Each function returns a minimal CollectorOutputContract-compliant dict
designed to trigger exactly ONE specific pattern (or anti-pattern).
"""

from typing import Any


def _base_output(
    job_id: str,
    tables: list[dict],
    query_patterns: list[dict],
) -> dict[str, Any]:
    """Minimal scaffold with only the fields the analysis agent reads."""
    return {
        "job_id": job_id,
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": query_patterns},
    }


# ---------------------------------------------------------------------------
# Caching fixture
# ---------------------------------------------------------------------------


def get_caching_fixture() -> dict[str, Any]:
    """1 table, 3 high-frequency SELECTs. Triggers caching pattern only."""
    table = {
        "table_id": "app.configs",
        "table_name": "configs",
        "schema_name": "app",
        "row_count": 500,
        "size_mb": 1.0,
        "columns": [
            {
                "column_name": "config_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
            },
            {
                "column_name": "config_key",
                "ordinal_position": 2,
                "data_type": "varchar(100)",
                "normalized_data_type": "string",
                "max_length": 100,
                "nullable": False,
            },
            {
                "column_name": "config_value",
                "ordinal_position": 3,
                "data_type": "text",
                "normalized_data_type": "text",
                "nullable": True,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["config_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["config_id"],
    }

    queries = [
        {
            "query_id": "cache-q1",
            "query_text": "SELECT config_value FROM configs WHERE config_key = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 36000.0,
            "calls_per_second": 10.0,
            "tables_accessed": ["app.configs"],
            "rows_returned_avg": 1.0,
            "execution_time_ms_avg": 0.5,
        },
        {
            "query_id": "cache-q2",
            "query_text": "SELECT config_key, config_value FROM configs WHERE config_id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 21600.0,
            "calls_per_second": 6.0,
            "tables_accessed": ["app.configs"],
            "rows_returned_avg": 1.0,
            "execution_time_ms_avg": 0.4,
        },
        {
            "query_id": "cache-q3",
            "query_text": "SELECT config_id, config_key, config_value FROM configs WHERE config_id IN (?, ?, ?)",
            "query_type": "SELECT",
            "frequency_per_hour": 18000.0,
            "calls_per_second": 5.0,
            "tables_accessed": ["app.configs"],
            "rows_returned_avg": 3.0,
            "execution_time_ms_avg": 0.6,
        },
    ]

    return _base_output("job-caching-fixture", [table], queries)


# ---------------------------------------------------------------------------
# Session store fixture
# ---------------------------------------------------------------------------


def get_session_store_fixture() -> dict[str, Any]:
    """1 sessions table, 2 queries with session/user_id. Triggers session-store only."""
    table = {
        "table_id": "app.sessions",
        "table_name": "sessions",
        "schema_name": "app",
        "row_count": 50000,
        "size_mb": 15.0,
        "columns": [
            {
                "column_name": "session_id",
                "ordinal_position": 1,
                "data_type": "varchar(128)",
                "normalized_data_type": "string",
                "max_length": 128,
                "nullable": False,
            },
            {
                "column_name": "user_id",
                "ordinal_position": 2,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
            },
            {
                "column_name": "payload",
                "ordinal_position": 3,
                "data_type": "jsonb",
                "normalized_data_type": "json",
                "nullable": True,
            },
            {
                "column_name": "expires_at",
                "ordinal_position": 4,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["session_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["session_id"],
    }

    queries = [
        {
            "query_id": "sess-q1",
            "query_text": "SELECT session_id, user_id, payload FROM sessions WHERE session_id = ?",
            "query_type": "SELECT",
            "frequency_per_hour": 3600.0,
            "calls_per_second": 0.5,
            "tables_accessed": ["app.sessions"],
            "rows_returned_avg": 1.0,
            "execution_time_ms_avg": 0.8,
        },
        {
            "query_id": "sess-q2",
            "query_text": "INSERT INTO sessions (session_id, user_id, payload, expires_at) VALUES (?, ?, ?, ?)",
            "query_type": "INSERT",
            "frequency_per_hour": 1800.0,
            "calls_per_second": 0.3,
            "tables_accessed": ["app.sessions"],
            "rows_affected_avg": 1.0,
            "execution_time_ms_avg": 1.0,
        },
    ]

    return _base_output("job-session-fixture", [table], queries)


# ---------------------------------------------------------------------------
# Leaderboard fixture
# ---------------------------------------------------------------------------


def get_leaderboard_fixture() -> dict[str, Any]:
    """1 scores table, 2 queries with ORDER BY + LIMIT. Triggers leaderboard only."""
    table = {
        "table_id": "app.scores",
        "table_name": "scores",
        "schema_name": "app",
        "row_count": 100000,
        "size_mb": 20.0,
        "columns": [
            {
                "column_name": "score_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
            },
            {
                "column_name": "player_name",
                "ordinal_position": 2,
                "data_type": "varchar(100)",
                "normalized_data_type": "string",
                "max_length": 100,
                "nullable": False,
            },
            {
                "column_name": "points",
                "ordinal_position": 3,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["score_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["score_id"],
    }

    queries = [
        {
            "query_id": "lb-q1",
            "query_text": "SELECT player_name, points FROM scores ORDER BY points DESC LIMIT 10",
            "query_type": "SELECT",
            "frequency_per_hour": 3600.0,
            "calls_per_second": 0.5,
            "tables_accessed": ["app.scores"],
            "rows_returned_avg": 10.0,
            "execution_time_ms_avg": 3.0,
        },
        {
            "query_id": "lb-q2",
            "query_text": "SELECT player_name, points FROM scores WHERE points > 1000 ORDER BY points DESC LIMIT 50",
            "query_type": "SELECT",
            "frequency_per_hour": 1800.0,
            "calls_per_second": 0.3,
            "tables_accessed": ["app.scores"],
            "rows_returned_avg": 50.0,
            "execution_time_ms_avg": 5.0,
        },
    ]

    return _base_output("job-leaderboard-fixture", [table], queries)


# ---------------------------------------------------------------------------
# Geospatial fixture
# ---------------------------------------------------------------------------


def get_geospatial_fixture() -> dict[str, Any]:
    """1 locations table with lat/lng, 2 queries with ST_Distance/ST_Within. Triggers geospatial only."""
    table = {
        "table_id": "app.locations",
        "table_name": "locations",
        "schema_name": "app",
        "row_count": 200000,
        "size_mb": 30.0,
        "columns": [
            {
                "column_name": "location_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
            },
            {
                "column_name": "name",
                "ordinal_position": 2,
                "data_type": "varchar(200)",
                "normalized_data_type": "string",
                "max_length": 200,
                "nullable": False,
            },
            {
                "column_name": "latitude",
                "ordinal_position": 3,
                "data_type": "double",
                "normalized_data_type": "decimal",
                "nullable": False,
            },
            {
                "column_name": "longitude",
                "ordinal_position": 4,
                "data_type": "double",
                "normalized_data_type": "decimal",
                "nullable": False,
            },
            {
                "column_name": "geom",
                "ordinal_position": 5,
                "data_type": "geometry",
                "normalized_data_type": "binary",
                "nullable": True,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["location_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["location_id"],
    }

    queries = [
        {
            "query_id": "geo-q1",
            "query_text": "SELECT location_id, name, ST_Distance(geom, ST_MakePoint(?, ?)) AS dist FROM locations WHERE ST_DWithin(geom, ST_MakePoint(?, ?), 5000)",
            "query_type": "SELECT",
            "frequency_per_hour": 3600.0,
            "calls_per_second": 0.5,
            "tables_accessed": ["app.locations"],
            "rows_returned_avg": 12.0,
            "execution_time_ms_avg": 8.0,
        },
        {
            "query_id": "geo-q2",
            "query_text": "SELECT location_id, name FROM locations WHERE ST_Within(geom, ST_MakeEnvelope(?, ?, ?, ?, 4326))",
            "query_type": "SELECT",
            "frequency_per_hour": 1800.0,
            "calls_per_second": 0.3,
            "tables_accessed": ["app.locations"],
            "rows_returned_avg": 25.0,
            "execution_time_ms_avg": 10.0,
        },
    ]

    return _base_output("job-geospatial-fixture", [table], queries)


# ---------------------------------------------------------------------------
# Time-series fixture
# ---------------------------------------------------------------------------


def get_timeseries_fixture() -> dict[str, Any]:
    """1 events table, 2 queries with created_at + GROUP BY. Triggers time-series only."""
    table = {
        "table_id": "app.events",
        "table_name": "events",
        "schema_name": "app",
        "row_count": 5000000,
        "size_mb": 800.0,
        "columns": [
            {
                "column_name": "event_id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "normalized_data_type": "integer",
                "nullable": False,
            },
            {
                "column_name": "event_type",
                "ordinal_position": 2,
                "data_type": "varchar(50)",
                "normalized_data_type": "string",
                "max_length": 50,
                "nullable": False,
            },
            {
                "column_name": "created_at",
                "ordinal_position": 3,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
            },
            {
                "column_name": "payload",
                "ordinal_position": 4,
                "data_type": "jsonb",
                "normalized_data_type": "json",
                "nullable": True,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["event_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["event_id"],
    }

    queries = [
        {
            "query_id": "ts-q1",
            "query_text": "SELECT DATE(created_at) AS day, COUNT(*) AS cnt FROM events WHERE event_type = ? AND created_at >= ? GROUP BY DATE(created_at) ORDER BY day",
            "query_type": "SELECT",
            "frequency_per_hour": 720.0,
            "calls_per_second": 0.2,
            "tables_accessed": ["app.events"],
            "rows_returned_avg": 30.0,
            "execution_time_ms_avg": 50.0,
        },
        {
            "query_id": "ts-q2",
            "query_text": "SELECT date_trunc('hour', created_at) AS bucket, COUNT(*) FROM events WHERE created_at >= ? GROUP BY bucket ORDER BY bucket",
            "query_type": "SELECT",
            "frequency_per_hour": 360.0,
            "calls_per_second": 0.1,
            "tables_accessed": ["app.events"],
            "rows_returned_avg": 24.0,
            "execution_time_ms_avg": 35.0,
        },
    ]

    return _base_output("job-timeseries-fixture", [table], queries)


# ---------------------------------------------------------------------------
# Anti-pattern fixture (large result sets)
# ---------------------------------------------------------------------------


def get_anti_pattern_fixture() -> dict[str, Any]:
    """1 table, 1 query returning 50k rows. Triggers large-result-sets anti-pattern only."""
    table = {
        "table_id": "app.big_table",
        "table_name": "big_table",
        "schema_name": "app",
        "row_count": 500000,
        "size_mb": 200.0,
        "columns": [
            {
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
            },
            {
                "column_name": "data",
                "ordinal_position": 2,
                "data_type": "text",
                "normalized_data_type": "text",
                "nullable": True,
            },
        ],
        "indexes": [
            {
                "index_name": "PRIMARY",
                "columns": ["id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
        ],
        "primary_key": ["id"],
    }

    queries = [
        {
            "query_id": "ap-q1",
            "query_text": "SELECT id, data FROM big_table WHERE data IS NOT NULL",
            "query_type": "SELECT",
            "frequency_per_hour": 36.0,
            "calls_per_second": 0.01,
            "tables_accessed": ["app.big_table"],
            "rows_returned_avg": 50000.0,
            "execution_time_ms_avg": 500.0,
        },
    ]

    return _base_output("job-antipattern-fixture", [table], queries)
