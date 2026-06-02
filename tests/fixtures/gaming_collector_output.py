"""
Realistic multiplayer gaming platform collector output fixture.

Simulates a large-scale online game running on PostgreSQL 15 on RDS.
This fixture is fully CollectorOutputContract v3.0 compliant and contains
query patterns designed to trigger ALL 5 implemented Redis analysis patterns
plus the large-result-set anti-pattern:

  - Caching: Player profile lookups, game config/item lookups (high frequency)
  - Session stores: Active game session reads/writes by player_id
  - Leaderboards: Top players ORDER BY score DESC LIMIT
  - Time series: Match stats grouped by date
  - Geospatial: ST_Distance queries for nearby players
  - Anti-pattern (large result sets): Full leaderboard dump

Schema:
  - game.players           (500k rows, 85 MB)  — player profiles
  - game.player_sessions   (1M rows, 120 MB)   — active game sessions
  - game.leaderboards      (100k rows, 15 MB)  — ranked scores
  - game.matches           (2M rows, 450 MB)   — match history
  - game.player_locations  (500k rows, 60 MB)  — geospatial positions
  - game.match_stats       (5M rows, 900 MB)   — per-match analytics
"""

from typing import Any


def get_gaming_collector_output() -> dict[str, Any]:
    """Return a complete, contract-validated CollectorOutputContract for a gaming DB."""
    return {
        "contract_version": "3.0",
        "job_id": "job-gaming-001",
        "metadata": _metadata(),
        "database_schema": _schema(),
        "queries": _queries(),
        "metrics": _metrics(),
    }


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def _metadata() -> dict[str, Any]:
    return {
        "collection_timestamp": "2026-02-18T10:00:00Z",
        "collector_version": "1.0.0",
        "collection_duration_seconds": 95.2,
        "source_database": {
            "engine": "postgresql",
            "version": "15.4",
            "hostname": "gaming-prod.cxyz9876wxyz.us-west-2.rds.amazonaws.com",
            "database_name": "game",
            "database_size_gb": 120.0,
            "deployment_type": "rds_instance",
            "rds_instance_metadata": {
                "db_instance_identifier": "gaming-prod",
                "instance_class": "db.r7g.2xlarge",
                "vcpu_count": 8,
                "memory_gb": 64.0,
                "storage_type": "gp3",
                "storage_size_gb": 500,
                "storage_iops": 6000,
                "storage_throughput_mbps": 250,
                "multi_az": True,
                "region": "us-west-2",
                "availability_zone": "us-west-2a",
                "read_replica_count": 2,
                "backup_retention_days": 14,
                "performance_insights_enabled": True,
                "enhanced_monitoring_interval": 30,
            },
        },
    }


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _schema() -> dict[str, Any]:
    return {
        "tables": [
            _players_table(),
            _player_sessions_table(),
            _leaderboards_table(),
            _matches_table(),
            _player_locations_table(),
            _match_stats_table(),
        ],
        "views": [
            {
                "view_id": "game.top_players_weekly",
                "view_name": "top_players_weekly",
                "schema_name": "game",
                "definition": (
                    "SELECT p.player_id, p.display_name, l.score, l.rank "
                    "FROM leaderboards l JOIN players p ON l.player_id = p.player_id "
                    "WHERE l.season = current_season() "
                    "ORDER BY l.score DESC LIMIT 100"
                ),
                "is_updatable": False,
                "referenced_tables": ["game.leaderboards", "game.players"],
                "column_list": ["player_id", "display_name", "score", "rank"],
            }
        ],
        "procedures": [
            {
                "procedure_id": "game.sp_record_match_result",
                "procedure_name": "sp_record_match_result",
                "schema_name": "game",
                "procedure_type": "PROCEDURE",
                "definition": (
                    "CREATE PROCEDURE sp_record_match_result(p_match_id INT, p_winner_id INT) "
                    "LANGUAGE plpgsql AS $$ BEGIN "
                    "  UPDATE matches SET winner_id = p_winner_id, status = 'completed' "
                    "  WHERE match_id = p_match_id; "
                    "  UPDATE leaderboards SET score = score + 25 "
                    "  WHERE player_id = p_winner_id; "
                    "END; $$"
                ),
                "language": "plpgsql",
                "parameters": [
                    {"parameter_name": "p_match_id", "data_type": "INT", "parameter_mode": "IN"},
                    {"parameter_name": "p_winner_id", "data_type": "INT", "parameter_mode": "IN"},
                ],
                "referenced_tables": ["game.matches", "game.leaderboards"],
            }
        ],
        "triggers": [
            {
                "trigger_id": "game.trg_match_completed",
                "trigger_name": "trg_match_completed",
                "schema_name": "game",
                "table_id": "game.matches",
                "event_type": "UPDATE",
                "timing": "AFTER",
                "for_each": "ROW",
                "definition": (
                    "CREATE TRIGGER trg_match_completed AFTER UPDATE ON matches "
                    "FOR EACH ROW WHEN (NEW.status = 'completed') "
                    "EXECUTE FUNCTION fn_update_match_stats()"
                ),
                "is_enabled": True,
            }
        ],
    }


def _players_table() -> dict[str, Any]:
    return {
        "table_id": "game.players",
        "table_name": "players",
        "schema_name": "game",
        "row_count": 500000,
        "size_mb": 85.0,
        "columns": [
            {
                "column_name": "player_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "is_auto_increment": True,
                "cardinality": 500000,
            },
            {
                "column_name": "display_name",
                "ordinal_position": 2,
                "data_type": "varchar(50)",
                "normalized_data_type": "string",
                "max_length": 50,
                "nullable": False,
                "cardinality": 498000,
            },
            {
                "column_name": "email",
                "ordinal_position": 3,
                "data_type": "varchar(255)",
                "normalized_data_type": "string",
                "max_length": 255,
                "nullable": False,
                "cardinality": 500000,
            },
            {
                "column_name": "avatar_url",
                "ordinal_position": 4,
                "data_type": "varchar(512)",
                "normalized_data_type": "string",
                "max_length": 512,
                "nullable": True,
            },
            {
                "column_name": "level",
                "ordinal_position": 5,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 1,
            },
            {
                "column_name": "xp",
                "ordinal_position": 6,
                "data_type": "bigint",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 0,
            },
            {
                "column_name": "is_online",
                "ordinal_position": 7,
                "data_type": "boolean",
                "normalized_data_type": "boolean",
                "nullable": False,
                "default_value": False,
            },
            {
                "column_name": "created_at",
                "ordinal_position": 8,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
            {
                "column_name": "last_login_at",
                "ordinal_position": 9,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": True,
            },
        ],
        "indexes": [
            {
                "index_name": "pk_players",
                "columns": ["player_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_players_email",
                "columns": ["email"],
                "is_unique": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_players_display_name",
                "columns": ["display_name"],
                "is_unique": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["player_id"],
    }


def _player_sessions_table() -> dict[str, Any]:
    return {
        "table_id": "game.player_sessions",
        "table_name": "player_sessions",
        "schema_name": "game",
        "row_count": 1000000,
        "size_mb": 120.0,
        "columns": [
            {
                "column_name": "session_id",
                "ordinal_position": 1,
                "data_type": "uuid",
                "normalized_data_type": "uuid",
                "nullable": False,
                "cardinality": 1000000,
            },
            {
                "column_name": "player_id",
                "ordinal_position": 2,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 450000,
            },
            {
                "column_name": "session_token",
                "ordinal_position": 3,
                "data_type": "varchar(256)",
                "normalized_data_type": "string",
                "max_length": 256,
                "nullable": False,
            },
            {
                "column_name": "game_server_id",
                "ordinal_position": 4,
                "data_type": "varchar(64)",
                "normalized_data_type": "string",
                "max_length": 64,
                "nullable": True,
            },
            {
                "column_name": "status",
                "ordinal_position": 5,
                "data_type": "varchar(20)",
                "normalized_data_type": "string",
                "max_length": 20,
                "nullable": False,
                "default_value": "active",
            },
            {
                "column_name": "created_at",
                "ordinal_position": 6,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
            {
                "column_name": "expires_at",
                "ordinal_position": 7,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
            },
            {
                "column_name": "last_heartbeat_at",
                "ordinal_position": 8,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
            },
        ],
        "indexes": [
            {
                "index_name": "pk_player_sessions",
                "columns": ["session_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_psess_player_id",
                "columns": ["player_id"],
                "is_unique": False,
                "index_type": "btree",
            },
            {
                "index_name": "idx_psess_expires",
                "columns": ["expires_at"],
                "is_unique": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["session_id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_psess_player",
                "columns": ["player_id"],
                "referenced_table": "game.players",
                "referenced_columns": ["player_id"],
                "on_delete": "CASCADE",
                "on_update": "CASCADE",
            }
        ],
    }


def _leaderboards_table() -> dict[str, Any]:
    return {
        "table_id": "game.leaderboards",
        "table_name": "leaderboards",
        "schema_name": "game",
        "row_count": 100000,
        "size_mb": 15.0,
        "columns": [
            {
                "column_name": "entry_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "is_auto_increment": True,
                "cardinality": 100000,
            },
            {
                "column_name": "player_id",
                "ordinal_position": 2,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 100000,
            },
            {
                "column_name": "score",
                "ordinal_position": 3,
                "data_type": "bigint",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 0,
            },
            {
                "column_name": "rank",
                "ordinal_position": 4,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": True,
            },
            {
                "column_name": "season",
                "ordinal_position": 5,
                "data_type": "varchar(20)",
                "normalized_data_type": "string",
                "max_length": 20,
                "nullable": False,
            },
            {
                "column_name": "updated_at",
                "ordinal_position": 6,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
        ],
        "indexes": [
            {
                "index_name": "pk_leaderboards",
                "columns": ["entry_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_lb_player_season",
                "columns": ["player_id", "season"],
                "is_unique": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_lb_score",
                "columns": ["score"],
                "is_unique": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["entry_id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_lb_player",
                "columns": ["player_id"],
                "referenced_table": "game.players",
                "referenced_columns": ["player_id"],
                "on_delete": "CASCADE",
                "on_update": "CASCADE",
            }
        ],
    }


def _matches_table() -> dict[str, Any]:
    return {
        "table_id": "game.matches",
        "table_name": "matches",
        "schema_name": "game",
        "row_count": 2000000,
        "size_mb": 450.0,
        "columns": [
            {
                "column_name": "match_id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "normalized_data_type": "integer",
                "nullable": False,
                "is_auto_increment": True,
                "cardinality": 2000000,
            },
            {
                "column_name": "game_mode",
                "ordinal_position": 2,
                "data_type": "varchar(30)",
                "normalized_data_type": "string",
                "max_length": 30,
                "nullable": False,
            },
            {
                "column_name": "status",
                "ordinal_position": 3,
                "data_type": "varchar(20)",
                "normalized_data_type": "string",
                "max_length": 20,
                "nullable": False,
                "default_value": "in_progress",
            },
            {
                "column_name": "winner_id",
                "ordinal_position": 4,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": True,
            },
            {
                "column_name": "duration_seconds",
                "ordinal_position": 5,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": True,
            },
            {
                "column_name": "server_id",
                "ordinal_position": 6,
                "data_type": "varchar(64)",
                "normalized_data_type": "string",
                "max_length": 64,
                "nullable": False,
            },
            {
                "column_name": "created_at",
                "ordinal_position": 7,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
            {
                "column_name": "completed_at",
                "ordinal_position": 8,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": True,
            },
        ],
        "indexes": [
            {
                "index_name": "pk_matches",
                "columns": ["match_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_matches_created_at",
                "columns": ["created_at"],
                "is_unique": False,
                "index_type": "btree",
            },
            {
                "index_name": "idx_matches_status",
                "columns": ["status"],
                "is_unique": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["match_id"],
    }


def _player_locations_table() -> dict[str, Any]:
    return {
        "table_id": "game.player_locations",
        "table_name": "player_locations",
        "schema_name": "game",
        "row_count": 500000,
        "size_mb": 60.0,
        "columns": [
            {
                "column_name": "player_id",
                "ordinal_position": 1,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 500000,
            },
            {
                "column_name": "latitude",
                "ordinal_position": 2,
                "data_type": "double precision",
                "normalized_data_type": "decimal",
                "nullable": False,
            },
            {
                "column_name": "longitude",
                "ordinal_position": 3,
                "data_type": "double precision",
                "normalized_data_type": "decimal",
                "nullable": False,
            },
            {
                "column_name": "geom",
                "ordinal_position": 4,
                "data_type": "geometry(Point,4326)",
                "normalized_data_type": "binary",
                "nullable": True,
            },
            {
                "column_name": "updated_at",
                "ordinal_position": 5,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
        ],
        "indexes": [
            {
                "index_name": "pk_player_locations",
                "columns": ["player_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_ploc_geom",
                "columns": ["geom"],
                "is_unique": False,
                "index_type": "gist",
            },
        ],
        "primary_key": ["player_id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_ploc_player",
                "columns": ["player_id"],
                "referenced_table": "game.players",
                "referenced_columns": ["player_id"],
                "on_delete": "CASCADE",
                "on_update": "CASCADE",
            }
        ],
    }


def _match_stats_table() -> dict[str, Any]:
    return {
        "table_id": "game.match_stats",
        "table_name": "match_stats",
        "schema_name": "game",
        "row_count": 5000000,
        "size_mb": 900.0,
        "columns": [
            {
                "column_name": "stat_id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "normalized_data_type": "integer",
                "nullable": False,
                "is_auto_increment": True,
                "cardinality": 5000000,
            },
            {
                "column_name": "match_id",
                "ordinal_position": 2,
                "data_type": "bigint",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 2000000,
            },
            {
                "column_name": "player_id",
                "ordinal_position": 3,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "cardinality": 480000,
            },
            {
                "column_name": "kills",
                "ordinal_position": 4,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 0,
            },
            {
                "column_name": "deaths",
                "ordinal_position": 5,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 0,
            },
            {
                "column_name": "assists",
                "ordinal_position": 6,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 0,
            },
            {
                "column_name": "damage_dealt",
                "ordinal_position": 7,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 0,
            },
            {
                "column_name": "score",
                "ordinal_position": 8,
                "data_type": "int",
                "normalized_data_type": "integer",
                "nullable": False,
                "default_value": 0,
            },
            {
                "column_name": "created_at",
                "ordinal_position": 9,
                "data_type": "timestamp",
                "normalized_data_type": "timestamp",
                "nullable": False,
                "default_value": "CURRENT_TIMESTAMP",
            },
        ],
        "indexes": [
            {
                "index_name": "pk_match_stats",
                "columns": ["stat_id"],
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            },
            {
                "index_name": "idx_mstats_match",
                "columns": ["match_id"],
                "is_unique": False,
                "index_type": "btree",
            },
            {
                "index_name": "idx_mstats_player",
                "columns": ["player_id"],
                "is_unique": False,
                "index_type": "btree",
            },
            {
                "index_name": "idx_mstats_created_at",
                "columns": ["created_at"],
                "is_unique": False,
                "index_type": "btree",
            },
        ],
        "primary_key": ["stat_id"],
        "foreign_keys": [
            {
                "constraint_name": "fk_mstats_match",
                "columns": ["match_id"],
                "referenced_table": "game.matches",
                "referenced_columns": ["match_id"],
                "on_delete": "CASCADE",
                "on_update": "CASCADE",
            },
            {
                "constraint_name": "fk_mstats_player",
                "columns": ["player_id"],
                "referenced_table": "game.players",
                "referenced_columns": ["player_id"],
                "on_delete": "CASCADE",
                "on_update": "CASCADE",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Query Patterns
# ---------------------------------------------------------------------------


def _queries() -> dict[str, Any]:
    return {
        "query_patterns": [
            # ── Caching: player profile lookup (high frequency) ──
            {
                "query_id": "gq01-player-profile",
                "query_text": "SELECT player_id, display_name, email, avatar_url, level, xp FROM players WHERE player_id = ?",
                "query_type": "SELECT",
                "frequency_per_hour": 72000.0,
                "calls_per_second": 20.0,
                "tables_accessed": ["game.players"],
                "rows_returned_avg": 1.0,
                "rows_returned_p95": 1.0,
                "execution_time_ms_avg": 0.6,
                "execution_time_ms_min": 0.2,
                "execution_time_ms_max": 10.0,
                "execution_time_ms_p50": 0.4,
                "execution_time_ms_p95": 1.8,
                "execution_time_ms_p99": 5.0,
                "total_time_ms": 43200.0,
                "db_load_contribution_percent": 15.0,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["player_id"],
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
            # ── Caching: game item/config lookup ──
            {
                "query_id": "gq02-player-by-name",
                "query_text": "SELECT player_id, display_name, level, xp FROM players WHERE display_name = ?",
                "query_type": "SELECT",
                "frequency_per_hour": 18000.0,
                "calls_per_second": 5.0,
                "tables_accessed": ["game.players"],
                "rows_returned_avg": 1.0,
                "rows_returned_p95": 1.0,
                "execution_time_ms_avg": 0.8,
                "execution_time_ms_min": 0.3,
                "execution_time_ms_max": 12.0,
                "execution_time_ms_p50": 0.6,
                "execution_time_ms_p95": 2.5,
                "execution_time_ms_p99": 7.0,
                "total_time_ms": 14400.0,
                "db_load_contribution_percent": 5.0,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["display_name"],
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
            # ── Session store: active session read ──
            {
                "query_id": "gq03-session-read",
                "query_text": "SELECT session_id, player_id, session_token, game_server_id, status FROM player_sessions WHERE session_id = ? AND expires_at > NOW()",
                "query_type": "SELECT",
                "frequency_per_hour": 108000.0,
                "calls_per_second": 30.0,
                "tables_accessed": ["game.player_sessions"],
                "rows_returned_avg": 1.0,
                "rows_returned_p95": 1.0,
                "execution_time_ms_avg": 0.3,
                "execution_time_ms_min": 0.1,
                "execution_time_ms_max": 5.0,
                "execution_time_ms_p50": 0.2,
                "execution_time_ms_p95": 0.8,
                "execution_time_ms_p99": 2.5,
                "total_time_ms": 32400.0,
                "db_load_contribution_percent": 11.2,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["session_id", "expires_at"],
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
            # ── Session store: session heartbeat update ──
            {
                "query_id": "gq04-session-heartbeat",
                "query_text": "UPDATE player_sessions SET last_heartbeat_at = NOW() WHERE session_id = ? AND player_id = ?",
                "query_type": "UPDATE",
                "frequency_per_hour": 72000.0,
                "calls_per_second": 20.0,
                "tables_accessed": ["game.player_sessions"],
                "rows_affected_avg": 1.0,
                "execution_time_ms_avg": 0.8,
                "execution_time_ms_min": 0.3,
                "execution_time_ms_max": 12.0,
                "execution_time_ms_p50": 0.6,
                "execution_time_ms_p95": 2.5,
                "execution_time_ms_p99": 6.0,
                "total_time_ms": 57600.0,
                "db_load_contribution_percent": 8.5,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["session_id", "player_id"],
                "lock_time_ms": 7200.0,
                "lock_time_pct": 12.5,
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
            # ── Leaderboard: top players by score ──
            {
                "query_id": "gq05-top-players",
                "query_text": "SELECT l.player_id, p.display_name, l.score, l.rank FROM leaderboards l JOIN players p ON l.player_id = p.player_id WHERE l.season = ? ORDER BY l.score DESC LIMIT 100",
                "query_type": "SELECT",
                "frequency_per_hour": 10800.0,
                "calls_per_second": 3.0,
                "tables_accessed": ["game.leaderboards", "game.players"],
                "rows_returned_avg": 100.0,
                "rows_returned_p95": 100.0,
                "execution_time_ms_avg": 5.0,
                "execution_time_ms_min": 2.0,
                "execution_time_ms_max": 35.0,
                "execution_time_ms_p50": 4.0,
                "execution_time_ms_p95": 12.0,
                "execution_time_ms_p99": 25.0,
                "total_time_ms": 54000.0,
                "db_load_contribution_percent": 8.0,
                "has_joins": True,
                "join_count": 1,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["season"],
                "sort_columns": ["score"],
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
            # ── Leaderboard: player rank lookup ──
            {
                "query_id": "gq06-player-rank",
                "query_text": "SELECT score, rank FROM leaderboards WHERE player_id = ? AND season = ? ORDER BY score DESC LIMIT 1",
                "query_type": "SELECT",
                "frequency_per_hour": 36000.0,
                "calls_per_second": 10.0,
                "tables_accessed": ["game.leaderboards"],
                "rows_returned_avg": 1.0,
                "rows_returned_p95": 1.0,
                "execution_time_ms_avg": 0.5,
                "execution_time_ms_min": 0.2,
                "execution_time_ms_max": 8.0,
                "execution_time_ms_p50": 0.4,
                "execution_time_ms_p95": 1.5,
                "execution_time_ms_p99": 4.0,
                "total_time_ms": 18000.0,
                "db_load_contribution_percent": 6.2,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["player_id", "season"],
                "sort_columns": ["score"],
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
            # ── Time series: daily match counts ──
            {
                "query_id": "gq07-daily-matches",
                "query_text": "SELECT DATE(created_at) AS match_date, game_mode, COUNT(*) AS match_count, AVG(duration_seconds) AS avg_duration FROM matches WHERE created_at >= ? GROUP BY DATE(created_at), game_mode ORDER BY match_date DESC",
                "query_type": "SELECT",
                "frequency_per_hour": 720.0,
                "calls_per_second": 0.2,
                "tables_accessed": ["game.matches"],
                "rows_returned_avg": 90.0,
                "rows_returned_p95": 120.0,
                "rows_examined_avg": 200000.0,
                "execution_time_ms_avg": 120.0,
                "execution_time_ms_min": 50.0,
                "execution_time_ms_max": 600.0,
                "execution_time_ms_p50": 100.0,
                "execution_time_ms_p95": 300.0,
                "execution_time_ms_p99": 500.0,
                "total_time_ms": 86400.0,
                "db_load_contribution_percent": 6.5,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": True,
                "has_subqueries": False,
                "filter_columns": ["created_at"],
                "sort_columns": ["match_date"],
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
            # ── Time series: hourly player stats ──
            {
                "query_id": "gq08-hourly-stats",
                "query_text": "SELECT date_trunc('hour', created_at) AS hour_bucket, SUM(kills) AS total_kills, SUM(deaths) AS total_deaths, COUNT(*) AS entries FROM match_stats WHERE created_at >= ? GROUP BY hour_bucket ORDER BY hour_bucket",
                "query_type": "SELECT",
                "frequency_per_hour": 360.0,
                "calls_per_second": 0.1,
                "tables_accessed": ["game.match_stats"],
                "rows_returned_avg": 24.0,
                "rows_returned_p95": 48.0,
                "rows_examined_avg": 500000.0,
                "execution_time_ms_avg": 200.0,
                "execution_time_ms_min": 80.0,
                "execution_time_ms_max": 1000.0,
                "execution_time_ms_p50": 160.0,
                "execution_time_ms_p95": 500.0,
                "execution_time_ms_p99": 800.0,
                "total_time_ms": 72000.0,
                "db_load_contribution_percent": 5.8,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": True,
                "has_subqueries": False,
                "filter_columns": ["created_at"],
                "sort_columns": ["hour_bucket"],
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
            # ── Geospatial: nearby players ──
            {
                "query_id": "gq09-nearby-players",
                "query_text": "SELECT pl.player_id, p.display_name, ST_Distance(pl.geom, ST_SetSRID(ST_MakePoint(?, ?), 4326)) AS dist FROM player_locations pl JOIN players p ON pl.player_id = p.player_id WHERE ST_DWithin(pl.geom, ST_SetSRID(ST_MakePoint(?, ?), 4326), 5000) ORDER BY dist LIMIT 50",
                "query_type": "SELECT",
                "frequency_per_hour": 14400.0,
                "calls_per_second": 4.0,
                "tables_accessed": ["game.player_locations", "game.players"],
                "rows_returned_avg": 25.0,
                "rows_returned_p95": 50.0,
                "execution_time_ms_avg": 8.0,
                "execution_time_ms_min": 2.0,
                "execution_time_ms_max": 60.0,
                "execution_time_ms_p50": 6.0,
                "execution_time_ms_p95": 20.0,
                "execution_time_ms_p99": 40.0,
                "total_time_ms": 115200.0,
                "db_load_contribution_percent": 12.0,
                "has_joins": True,
                "join_count": 1,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["geom"],
                "sort_columns": ["dist"],
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
            # ── Geospatial: update player location ──
            {
                "query_id": "gq10-update-location",
                "query_text": "UPDATE player_locations SET latitude = ?, longitude = ?, geom = ST_SetSRID(ST_MakePoint(?, ?), 4326), updated_at = NOW() WHERE player_id = ?",
                "query_type": "UPDATE",
                "frequency_per_hour": 36000.0,
                "calls_per_second": 10.0,
                "tables_accessed": ["game.player_locations"],
                "rows_affected_avg": 1.0,
                "execution_time_ms_avg": 1.2,
                "execution_time_ms_min": 0.4,
                "execution_time_ms_max": 15.0,
                "execution_time_ms_p50": 0.9,
                "execution_time_ms_p95": 3.5,
                "execution_time_ms_p99": 8.0,
                "total_time_ms": 43200.0,
                "db_load_contribution_percent": 7.5,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["player_id"],
                "lock_time_ms": 5400.0,
                "lock_time_pct": 12.5,
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
            # ── Write: record match ──
            {
                "query_id": "gq11-record-match",
                "query_text": "INSERT INTO matches (game_mode, status, server_id, created_at) VALUES (?, 'in_progress', ?, NOW())",
                "query_type": "INSERT",
                "frequency_per_hour": 7200.0,
                "calls_per_second": 2.0,
                "tables_accessed": ["game.matches"],
                "rows_affected_avg": 1.0,
                "execution_time_ms_avg": 2.0,
                "execution_time_ms_min": 0.8,
                "execution_time_ms_max": 20.0,
                "execution_time_ms_p50": 1.5,
                "execution_time_ms_p95": 5.0,
                "execution_time_ms_p99": 12.0,
                "total_time_ms": 14400.0,
                "db_load_contribution_percent": 2.5,
                "has_joins": False,
                "join_count": 0,
                "has_aggregations": False,
                "has_subqueries": False,
                "lock_time_ms": 1800.0,
                "lock_time_pct": 12.5,
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
            # ── Anti-pattern: full leaderboard dump ──
            {
                "query_id": "gq12-full-leaderboard-dump",
                "query_text": "SELECT l.player_id, p.display_name, l.score, l.rank FROM leaderboards l JOIN players p ON l.player_id = p.player_id WHERE l.season = ?",
                "query_type": "SELECT",
                "frequency_per_hour": 72.0,
                "calls_per_second": 0.02,
                "tables_accessed": ["game.leaderboards", "game.players"],
                "rows_returned_avg": 100000.0,
                "rows_returned_p95": 100000.0,
                "rows_examined_avg": 100000.0,
                "execution_time_ms_avg": 350.0,
                "execution_time_ms_min": 150.0,
                "execution_time_ms_max": 1500.0,
                "execution_time_ms_p50": 280.0,
                "execution_time_ms_p95": 800.0,
                "execution_time_ms_p99": 1200.0,
                "total_time_ms": 25200.0,
                "db_load_contribution_percent": 3.8,
                "has_joins": True,
                "join_count": 1,
                "has_aggregations": False,
                "has_subqueries": False,
                "filter_columns": ["season"],
                "full_table_scans": 72,
                "first_seen": "2026-02-17T00:00:00Z",
                "last_seen": "2026-02-18T10:00:00Z",
            },
        ],
        "total_queries_analyzed": 2450000,
        "query_log_source": "performance_insights",
        "collection_start_time": "2026-02-17T00:00:00Z",
        "collection_end_time": "2026-02-18T10:00:00Z",
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _metrics() -> dict[str, Any]:
    return {
        "performance_metrics": {
            "avg_query_time_ms": 6.5,
            "p50_query_time_ms": 0.8,
            "p95_query_time_ms": 25.0,
            "p99_query_time_ms": 150.0,
            "queries_per_second": 105.3,
            "connection_pool_usage_percent": 65.0,
            "active_connections_avg": 85.0,
            "active_connections_max": 180.0,
            "transactions_per_second": 82.0,
            "read_iops_avg": 5500.0,
            "write_iops_avg": 1200.0,
            "network_throughput_mbps_avg": 55.0,
        },
        "rds_cloudwatch_metrics": {
            "cpu_utilization": {"avg": 62.0, "max": 92.0, "min": 20.0, "p95": 85.0},
            "freeable_memory_gb": {"avg": 35.0, "max": 55.0, "min": 12.0, "p95": 20.0},
            "database_connections": {"avg": 85.0, "max": 180.0, "min": 25.0, "p95": 150.0},
            "read_iops": {"avg": 5500.0, "max": 12000.0, "min": 1500.0, "p95": 10000.0},
            "write_iops": {"avg": 1200.0, "max": 3500.0, "min": 300.0, "p95": 2800.0},
            "read_latency_ms": {"avg": 1.2, "max": 10.0, "min": 0.2, "p95": 5.0},
            "write_latency_ms": {"avg": 2.5, "max": 18.0, "min": 0.8, "p95": 8.0},
            "network_receive_throughput_mbps": {
                "avg": 35.0,
                "max": 85.0,
                "min": 8.0,
                "p95": 70.0,
            },
            "network_transmit_throughput_mbps": {
                "avg": 55.0,
                "max": 120.0,
                "min": 12.0,
                "p95": 100.0,
            },
            "free_storage_space_gb": 380.0,
        },
    }
