"""
MySQL Collector Tools — SSM-based

All queries execute on a remote automation instance via SSM Run Command.
No direct database connection from the collector.
"""

import hashlib
import logging
import re

from src.tools.aws.ssm_executor import SSMExecutor

logger = logging.getLogger(__name__)


class MySQLRemoteCollector:
    """Collects MySQL metadata via SSM Run Command on an automation instance."""

    def __init__(
        self,
        ssm: SSMExecutor,
        host: str,
        port: int,
        database: str,
        secret_arn: str,
        region: str = "us-east-1",
    ):
        self.ssm = ssm
        self.host = host
        self.port = port
        self.database = database
        self.secret_arn = secret_arn
        self.region = region

    def _query(self, sql: str) -> list[dict]:
        """Execute SQL and return parsed rows."""
        return self.ssm.run_sql_json(
            engine="mysql",
            host=self.host,
            port=self.port,
            database=self.database,
            secret_arn=self.secret_arn,
            sql=sql,
            region=self.region,
        )

    def _query_raw(self, sql: str) -> str:
        """Execute SQL and return raw output."""
        return self.ssm.run_sql(
            engine="mysql",
            host=self.host,
            port=self.port,
            database=self.database,
            secret_arn=self.secret_arn,
            sql=sql,
            region=self.region,
        )

    # -------------------------------------------------------------------
    # Metadata
    # -------------------------------------------------------------------

    def get_version(self) -> str:
        rows = self._query("SELECT VERSION() AS version")
        return str(rows[0]["version"]) if rows else "unknown"

    def get_database_size_gb(self) -> float:
        rows = self._query("""
            SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024 / 1024, 4) AS size_gb
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
        """)
        return float(rows[0]["size_gb"] or 0) if rows else 0

    # -------------------------------------------------------------------
    # Schema
    # -------------------------------------------------------------------

    def collect_tables(self) -> list[dict]:
        return self._query("""
            SELECT
                table_name,
                table_rows AS row_count,
                ROUND(data_length / 1024 / 1024, 2) AS data_size_mb,
                ROUND(index_length / 1024 / 1024, 2) AS index_size_mb,
                engine,
                table_collation
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)

    def collect_columns(self, table_name: str) -> list[dict]:
        return self._query(f"""
            SELECT
                column_name, ordinal_position, data_type, column_type,
                character_maximum_length AS max_length, is_nullable,
                column_default, column_key, extra
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = '{table_name}'
            ORDER BY ordinal_position
        """)  # nosec B608 — table_name from information_schema, not user input

    def collect_indexes(self, table_name: str) -> list[dict]:
        raw = self._query(f"""
            SELECT index_name, column_name, seq_in_index, non_unique, index_type
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = '{table_name}'
            ORDER BY index_name, seq_in_index
        """)  # nosec B608 — table_name from information_schema, not user input
        indexes: dict[str, dict] = {}
        for r in raw:
            name = r["index_name"]
            if name not in indexes:
                indexes[name] = {
                    "index_name": name,
                    "columns": [],
                    "is_unique": not r["non_unique"],
                    "is_primary": name == "PRIMARY",
                    "index_type": (str(r.get("index_type") or "btree")).lower(),
                }
            indexes[name]["columns"].append(r["column_name"])
        return list(indexes.values())

    def collect_foreign_keys(self, table_name: str) -> list[dict]:
        raw = self._query(f"""
            SELECT
                kcu.constraint_name, kcu.column_name,
                kcu.referenced_table_name, kcu.referenced_column_name,
                rc.delete_rule AS on_delete, rc.update_rule AS on_update
            FROM information_schema.key_column_usage kcu
            JOIN information_schema.referential_constraints rc
              ON kcu.constraint_name = rc.constraint_name
             AND kcu.constraint_schema = rc.constraint_schema
            WHERE kcu.table_schema = DATABASE()
              AND kcu.table_name = '{table_name}'
              AND kcu.referenced_table_name IS NOT NULL
            ORDER BY kcu.constraint_name, kcu.ordinal_position
        """)  # nosec B608 — table_name from information_schema, not user input
        fks: dict[str, dict] = {}
        for r in raw:
            name = r["constraint_name"]
            if name not in fks:
                fks[name] = {
                    "constraint_name": name,
                    "columns": [],
                    "referenced_table": r["referenced_table_name"],
                    "referenced_columns": [],
                    "on_delete": r["on_delete"],
                    "on_update": r["on_update"],
                }
            fks[name]["columns"].append(r["column_name"])
            fks[name]["referenced_columns"].append(r["referenced_column_name"])
        return list(fks.values())

    def collect_primary_key(self, table_name: str) -> list[str]:
        rows = self._query(f"""
            SELECT column_name
            FROM information_schema.key_column_usage
            WHERE table_schema = DATABASE()
              AND table_name = '{table_name}'
              AND constraint_name = 'PRIMARY'
            ORDER BY ordinal_position
        """)  # nosec B608 — table_name from information_schema, not user input
        return [r["column_name"] for r in rows]

    # -------------------------------------------------------------------
    # Views, procedures, triggers
    # -------------------------------------------------------------------

    def collect_views(self) -> list[dict]:
        return self._query("""
            SELECT table_name AS view_name, view_definition AS definition, is_updatable
            FROM information_schema.views
            WHERE table_schema = DATABASE()
            ORDER BY table_name
        """)

    def collect_procedures(self) -> list[dict]:
        return self._query("""
            SELECT routine_name, routine_type, data_type AS return_type,
                   routine_definition AS definition
            FROM information_schema.routines
            WHERE routine_schema = DATABASE()
            ORDER BY routine_type, routine_name
        """)

    def collect_triggers(self) -> list[dict]:
        return self._query("""
            SELECT trigger_name, event_manipulation AS event_type,
                   event_object_table AS table_name,
                   action_timing AS timing, action_statement AS definition
            FROM information_schema.triggers
            WHERE trigger_schema = DATABASE()
            ORDER BY event_object_table, trigger_name
        """)

    # -------------------------------------------------------------------
    # Global status metrics (cache, temp tables)
    # -------------------------------------------------------------------

    def collect_global_stats(self) -> dict:
        """Collect InnoDB cache and temp table stats from GLOBAL STATUS."""
        rows = self._query("""
            SELECT VARIABLE_NAME AS name, VARIABLE_VALUE AS val
            FROM performance_schema.global_status
            WHERE VARIABLE_NAME IN (
                'Innodb_buffer_pool_read_requests',
                'Innodb_buffer_pool_reads',
                'Created_tmp_disk_tables',
                'Created_tmp_tables'
            )
        """)
        stats = {r["name"].lower(): int(r["val"] or 0) for r in rows}
        read_req = stats.get("innodb_buffer_pool_read_requests", 0)
        reads = stats.get("innodb_buffer_pool_reads", 0)
        return {
            "cache_hit_ratio_pct": round((read_req - reads) / max(read_req, 1) * 100, 2),
            "buffer_pool_hits": read_req - reads,
            "buffer_pool_reads_from_disk": reads,
            "buffer_pool_read_requests": read_req,
            "tmp_disk_tables": stats.get("created_tmp_disk_tables", 0),
            "tmp_tables": stats.get("created_tmp_tables", 0),
        }

    # -------------------------------------------------------------------
    # Query patterns (performance_schema)
    # -------------------------------------------------------------------

    def collect_query_patterns(self, min_executions: int = 10, limit: int = 1000) -> list[dict]:
        # Detect version to know which columns are available
        version = self.get_version()
        has_quantiles = _version_gte(version, "8.0.25")
        has_errors = _version_gte(version, "5.7.0")
        has_first_last_seen = _version_gte(version, "5.7.9")

        # Build optional column fragments (each ends with comma if present)
        extra_cols = ""
        if has_errors:
            extra_cols += "SUM_ERRORS AS sum_errors, SUM_WARNINGS AS sum_warnings, "
        if has_quantiles:
            extra_cols += "QUANTILE_95 / 1000000000 AS p95_ms, QUANTILE_99 / 1000000000 AS p99_ms, "
        if has_first_last_seen:
            extra_cols += "FIRST_SEEN, LAST_SEEN, "

        raw = self._query(
            f"""
            SELECT
                DIGEST AS digest, DIGEST_TEXT AS query_text, SCHEMA_NAME AS schema_name,
                COUNT_STAR AS execution_count,
                SUM_TIMER_WAIT / 1000000000 AS total_time_ms,
                AVG_TIMER_WAIT / 1000000000 AS avg_time_ms,
                MIN_TIMER_WAIT / 1000000000 AS min_time_ms,
                MAX_TIMER_WAIT / 1000000000 AS max_time_ms,
                SUM_ROWS_SENT AS total_rows_sent,
                SUM_ROWS_EXAMINED AS total_rows_examined,
                SUM_ROWS_AFFECTED AS total_rows_affected,
                SUM_SELECT_SCAN AS full_table_scans,
                SUM_SELECT_RANGE AS range_scans,
                SUM_NO_INDEX_USED AS no_index_used,
                SUM_NO_GOOD_INDEX_USED AS no_good_index_used,
                SUM_LOCK_TIME / 1000000000 AS lock_time_ms,
                {extra_cols}
                SUM_TIMER_WAIT AS _sort_key
            FROM performance_schema.events_statements_summary_by_digest
            WHERE SCHEMA_NAME = DATABASE()
              AND COUNT_STAR >= {min_executions}
            ORDER BY SUM_TIMER_WAIT DESC
            LIMIT {limit}
        """  # nosec B608 — extra_cols, min_executions, limit are internal constants, not user input
        )

        patterns = []
        for r in raw:
            query_text = str(r.get("query_text") or "")
            exec_count = r.get("execution_count") or 1
            first_seen = r.get("first_seen") or r.get("FIRST_SEEN")
            last_seen = r.get("last_seen") or r.get("LAST_SEEN")
            first_seen = str(first_seen) if first_seen else None
            last_seen = str(last_seen) if last_seen else None
            patterns.append(
                {
                    "query_id": r.get("digest") or _hash(query_text),
                    "query_text": query_text,
                    "query_type": _extract_query_type(query_text),
                    "execution_count": exec_count,
                    "frequency_per_hour": exec_count / 24,
                    "calls_per_second": exec_count / (24 * 3600),
                    "execution_time_ms_avg": float(r.get("avg_time_ms") or 0),
                    "execution_time_ms_min": float(r.get("min_time_ms") or 0),
                    "execution_time_ms_max": float(r.get("max_time_ms") or 0),
                    "execution_time_ms_p50": float(
                        r.get("avg_time_ms") or 0
                    ),  # approx: median ≈ avg for normal dist
                    "execution_time_ms_p95": (
                        float(r.get("p95_ms") or 0) if r.get("p95_ms") else None
                    ),
                    "execution_time_ms_p99": (
                        float(r.get("p99_ms") or 0) if r.get("p99_ms") else None
                    ),
                    "total_time_ms": float(r.get("total_time_ms") or 0),
                    "rows_returned_avg": (r.get("total_rows_sent") or 0) / exec_count,
                    "rows_returned_p95": _estimate_rows_p95(
                        avg_rows=(r.get("total_rows_sent") or 0) / exec_count,
                        avg_ms=float(r.get("avg_time_ms") or 0),
                        p95_ms=float(r.get("p95_ms") or 0),
                    ),
                    "rows_examined_avg": (r.get("total_rows_examined") or 0) / exec_count,
                    "rows_affected_avg": (r.get("total_rows_affected") or 0) / exec_count,
                    "full_table_scans": r.get("full_table_scans") or 0,
                    "range_scans": r.get("range_scans") or 0,
                    "queries_without_index": r.get("no_index_used") or 0,
                    "queries_with_bad_index": r.get("no_good_index_used") or 0,
                    "lock_time_ms": float(r.get("lock_time_ms") or 0),
                    "tables_accessed": _extract_tables(query_text),
                    "filter_columns": _extract_filter_columns(query_text),
                    "sort_columns": _extract_sort_columns(query_text),
                    "has_joins": " join " in query_text.lower(),
                    "join_count": len(re.findall(r"\bjoin\b", query_text, re.I)),
                    "scan_efficiency_pct": min(
                        round(
                            (r.get("total_rows_sent") or 0)
                            / max(r.get("total_rows_examined") or 1, 1)
                            * 100,
                            2,
                        ),
                        100.0,
                    ),
                    "lock_time_pct": round(
                        float(r.get("lock_time_ms") or 0)
                        / max(float(r.get("total_time_ms") or 1), 0.001)
                        * 100,
                        2,
                    ),
                    "has_aggregations": bool(
                        re.search(r"\b(count|sum|avg|min|max|group\s+by)\b", query_text, re.I)
                    ),
                    "has_subqueries": query_text.lower().count("select") > 1,
                    "has_text_search": _has_text_search(query_text),
                    "text_search_type": _text_search_type(query_text),
                    "has_time_range_filter": _has_time_range_filter(query_text),
                    "errors": r.get("sum_errors") or 0,
                    "warnings": r.get("sum_warnings") or 0,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                }
            )
        return patterns

    # -------------------------------------------------------------------
    # Sample data
    # -------------------------------------------------------------------

    def collect_sample_data(self, table_name: str, limit: int = 10) -> list[dict]:
        return self._query(
            f"SELECT * FROM `{table_name}` LIMIT {limit}"  # nosec B608 — table_name from information_schema, not user input
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUERY_TYPE_RE = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE|REPLACE)\b", re.I)
_TABLE_RE = re.compile(r"(?:FROM|JOIN|INTO|UPDATE)\s+`?(\w+)`?", re.I)
_WHERE_COL_RE = re.compile(r"WHERE\s+.*?`?(\w+)`?\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)", re.I)
_AND_COL_RE = re.compile(r"(?:AND|OR)\s+`?(\w+)`?\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)", re.I)
_ORDER_COL_RE = re.compile(r"ORDER\s+BY\s+(.*?)(?:LIMIT|$)", re.I)


def _extract_query_type(sql: str) -> str:
    m = _QUERY_TYPE_RE.match(sql)
    return m.group(1).upper() if m else "OTHER"


def _extract_tables(sql: str) -> list[str]:
    return list(dict.fromkeys(_TABLE_RE.findall(sql))) or ["unknown"]


def _estimate_rows_p95(avg_rows: float, avg_ms: float, p95_ms: float) -> float | None:
    """Estimate p95 rows returned using latency ratio as proxy."""
    if not avg_ms or avg_ms == 0 or not p95_ms:
        return None
    ratio = p95_ms / avg_ms
    return round(avg_rows * ratio, 1)


def _extract_filter_columns(sql: str) -> list[str] | None:
    cols = _WHERE_COL_RE.findall(sql) + _AND_COL_RE.findall(sql)
    return list(dict.fromkeys(cols)) or None


def _extract_sort_columns(sql: str) -> list[str] | None:
    m = _ORDER_COL_RE.search(sql)
    if not m:
        return None
    raw = m.group(1)
    cols = [c.strip().strip("`").split()[0] for c in raw.split(",")]
    return [c for c in cols if c and not c.upper().startswith(("ASC", "DESC"))] or None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _has_text_search(query_text: str) -> bool:
    """Detect text search patterns in MySQL (handles parameterized queries)."""
    if re.search(r"\bMATCH\s*\(.*\)\s*AGAINST\s*\(", query_text, re.I):
        return True
    if re.search(r"\b[I]?LIKE\s+'%", query_text, re.I):
        return True
    # MySQL performance_schema normalizes: LIKE '%foo%' → LIKE ?
    if re.search(r"\b[I]?LIKE\s+\?", query_text, re.I):
        return True
    return False


def _text_search_type(query_text: str) -> str | None:
    if re.search(r"\bMATCH\s*\(.*\)\s*AGAINST\s*\(", query_text, re.I):
        return "fulltext"
    if re.search(r"\b[I]?LIKE\s+'%", query_text, re.I):
        return "like_wildcard"
    if re.search(r"\b[I]?LIKE\s+\?", query_text, re.I):
        return "like_wildcard"
    return None


def _has_time_range_filter(query_text: str) -> bool:
    if re.search(
        r"\b(NOW\s*\(\s*\)|CURRENT_TIMESTAMP|CURRENT_DATE|CURDATE\s*\(\s*\))\s*[-+]\s*INTERVAL\b",
        query_text,
        re.I,
    ):
        return True
    if re.search(r"\bBETWEEN\s+'?\d{4}-\d{2}-\d{2}", query_text, re.I):
        return True
    if re.search(r"[<>]=?\s+'?\d{4}-\d{2}-\d{2}", query_text, re.I):
        return True
    # MySQL performance_schema normalizes dates: BETWEEN ? AND ?
    # Detect by column name heuristic (date/time columns with BETWEEN)
    if re.search(
        r"\b(date|time|created|updated|timestamp)\w*\b\s*(BETWEEN|[<>]=?)\s*\?", query_text, re.I
    ):
        return True
    return False


def _version_gte(version: str, minimum: str) -> bool:
    """Check if MySQL version >= minimum. Handles '8.0.43-log' style strings."""
    try:
        v_match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
        m_match = re.match(r"(\d+)\.(\d+)\.(\d+)", minimum)
        if not v_match or not m_match:
            return False
        v_parts = [int(x) for x in v_match.groups()]
        m_parts = [int(x) for x in m_match.groups()]
        return v_parts >= m_parts
    except Exception:
        return False
