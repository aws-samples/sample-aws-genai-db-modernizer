"""
PostgreSQL Collector Tools — SSM-based

All queries execute on a remote automation instance via SSM Run Command.
Mirrors MySQLRemoteCollector pattern but with PostgreSQL-specific queries
(pg_stat_statements, pg_stat_user_tables, pg_stats, information_schema).
"""

import hashlib
import logging
import re

from src.tools.aws.ssm_executor import SSMExecutor

logger = logging.getLogger(__name__)


class PostgreSQLRemoteCollector:
    """Collects PostgreSQL metadata via SSM Run Command."""

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
        return self.ssm.run_sql_json(
            engine="postgresql",
            host=self.host,
            port=self.port,
            database=self.database,
            secret_arn=self.secret_arn,
            sql=sql,
            region=self.region,
        )

    def _query_raw(self, sql: str) -> str:
        return self.ssm.run_sql(
            engine="postgresql",
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
        rows = self._query("SELECT version() AS version")
        return str(rows[0]["version"]) if rows else "unknown"

    def get_database_size_gb(self) -> float:
        rows = self._query(
            "SELECT pg_database_size(current_database()) / 1024.0 / 1024.0 / 1024.0 AS size_gb"
        )
        return float(rows[0]["size_gb"] or 0) if rows else 0

    # -------------------------------------------------------------------
    # Schema
    # -------------------------------------------------------------------

    def collect_tables(self) -> list[dict]:
        return self._query(
            """
            SELECT
                schemaname AS schema_name,
                relname AS table_name,
                n_live_tup AS row_count,
                pg_relation_size(schemaname || '.' || relname) / 1024.0 / 1024.0 AS data_size_mb,
                pg_indexes_size(schemaname || '.' || relname) / 1024.0 / 1024.0 AS index_size_mb,
                seq_scan AS full_table_scans,
                idx_scan AS index_scans
            FROM pg_stat_user_tables
            WHERE schemaname = 'public'
            ORDER BY relname
        """
        )

    def collect_columns(self, table_name: str) -> list[dict]:
        return self._query(
            f"""
            SELECT
                column_name, ordinal_position, data_type, udt_name,
                character_maximum_length AS max_length,
                is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = '{table_name}'
            ORDER BY ordinal_position
        """  # nosec B608 — table_name from information_schema, not user input
        )

    def collect_column_cardinality(self, table_name: str) -> dict[str, int]:
        rows = self._query(
            f"""
            SELECT attname AS col, n_distinct
            FROM pg_stats
            WHERE schemaname = 'public' AND tablename = '{table_name}'
        """  # nosec B608 — table_name from information_schema, not user input
        )
        result = {}
        for r in rows:
            n = r.get("n_distinct")
            if n is not None:
                result[r["col"]] = int(n) if float(n) >= 0 else abs(int(float(n) * 1000))
        return result

    def collect_indexes(self, table_name: str) -> list[dict]:
        raw = self._query(
            f"""
            SELECT
                i.relname AS index_name,
                a.attname AS column_name,
                ix.indisunique AS is_unique,
                ix.indisprimary AS is_primary,
                am.amname AS index_type
            FROM pg_class t
            JOIN pg_index ix ON t.oid = ix.indrelid
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
            JOIN pg_am am ON i.relam = am.oid
            WHERE t.relname = '{table_name}'
              AND t.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')
            ORDER BY i.relname, a.attnum
        """  # nosec B608 — table_name from information_schema, not user input
        )
        indexes: dict[str, dict] = {}
        for r in raw:
            name = r["index_name"]
            if name not in indexes:
                indexes[name] = {
                    "index_name": name,
                    "columns": [],
                    "is_unique": r.get("is_unique") in (True, "t", "true"),
                    "is_primary": r.get("is_primary") in (True, "t", "true"),
                    "index_type": str(r.get("index_type") or "btree").lower(),
                }
            indexes[name]["columns"].append(r["column_name"])
        return list(indexes.values())

    def collect_foreign_keys(self, table_name: str) -> list[dict]:
        raw = self._query(
            f"""
            SELECT
                tc.constraint_name, kcu.column_name,
                ccu.table_name AS referenced_table_name,
                ccu.column_name AS referenced_column_name,
                rc.update_rule AS on_update, rc.delete_rule AS on_delete
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
            JOIN information_schema.referential_constraints rc
              ON rc.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = 'public' AND tc.table_name = '{table_name}'
            ORDER BY tc.constraint_name
        """  # nosec B608 — table_name from information_schema, not user input
        )
        fks: dict[str, dict] = {}
        for r in raw:
            name = r["constraint_name"]
            if name not in fks:
                fks[name] = {
                    "constraint_name": name,
                    "columns": [],
                    "referenced_table": r["referenced_table_name"],
                    "referenced_columns": [],
                    "on_delete": r.get("on_delete"),
                    "on_update": r.get("on_update"),
                }
            fks[name]["columns"].append(r["column_name"])
            fks[name]["referenced_columns"].append(r["referenced_column_name"])
        return list(fks.values())

    def collect_primary_key(self, table_name: str) -> list[str]:
        rows = self._query(
            f"""
            SELECT a.attname AS column_name
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'public.{table_name}'::regclass AND i.indisprimary
            ORDER BY a.attnum
        """  # nosec B608 — table_name from information_schema, not user input
        )
        return [r["column_name"] for r in rows]

    # -------------------------------------------------------------------
    # Views, procedures, triggers
    # -------------------------------------------------------------------

    def collect_views(self) -> list[dict]:
        return self._query(
            """
            SELECT viewname AS view_name, definition, viewowner AS owner
            FROM pg_views WHERE schemaname = 'public' ORDER BY viewname
        """
        )

    def collect_procedures(self) -> list[dict]:
        return self._query(
            """
            SELECT
                p.proname AS routine_name,
                CASE p.prokind WHEN 'f' THEN 'FUNCTION' WHEN 'p' THEN 'PROCEDURE' END AS routine_type,
                pg_get_function_result(p.oid) AS return_type,
                l.lanname AS language
            FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            JOIN pg_language l ON p.prolang = l.oid
            WHERE n.nspname = 'public'
            ORDER BY routine_type, routine_name
        """
        )

    def collect_triggers(self) -> list[dict]:
        return self._query(
            """
            SELECT
                t.tgname AS trigger_name,
                c.relname AS table_name,
                CASE t.tgtype & 2 WHEN 2 THEN 'BEFORE' ELSE 'AFTER' END AS timing,
                CASE t.tgtype & 28
                    WHEN 4 THEN 'INSERT' WHEN 8 THEN 'DELETE'
                    WHEN 16 THEN 'UPDATE' ELSE 'MULTIPLE'
                END AS event_type,
                CASE t.tgtype & 1 WHEN 1 THEN 'ROW' ELSE 'STATEMENT' END AS for_each
            FROM pg_trigger t
            JOIN pg_class c ON t.tgrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname = 'public' AND NOT t.tgisinternal
            ORDER BY c.relname, t.tgname
        """
        )

    # -------------------------------------------------------------------
    # Query patterns (pg_stat_statements)
    # -------------------------------------------------------------------

    def collect_query_patterns(self, min_calls: int = 1, limit: int = 1000) -> list[dict]:
        raw = self._query(
            f"""
            SELECT
                queryid, query, calls,
                total_exec_time AS total_time_ms,
                mean_exec_time AS avg_time_ms,
                min_exec_time AS min_time_ms,
                max_exec_time AS max_time_ms,
                stddev_exec_time AS stddev_time_ms,
                rows,
                shared_blks_hit, shared_blks_read,
                blk_read_time AS io_read_time_ms,
                blk_write_time AS io_write_time_ms,
                temp_blks_read, temp_blks_written
            FROM pg_stat_statements
            WHERE calls >= {min_calls}
              AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
            ORDER BY total_exec_time DESC
            LIMIT {limit}
        """  # nosec B608 — min_calls and limit are internal constants, not user input
        )

        patterns = []
        for r in raw:
            query_text = str(r.get("query") or "")
            calls = r.get("calls") or 1
            avg_ms = float(r.get("avg_time_ms") or 0)
            stddev_ms = float(r.get("stddev_time_ms") or 0)
            shared_hit = int(r.get("shared_blks_hit") or 0)
            shared_read = int(r.get("shared_blks_read") or 0)
            total_blks = shared_hit + shared_read

            p50, p95, p99 = _estimate_percentiles(avg_ms, stddev_ms)

            patterns.append(
                {
                    "query_id": str(r.get("queryid") or _hash(query_text)),
                    "query_text": query_text,
                    "query_type": _extract_query_type(query_text),
                    "execution_count": calls,
                    "frequency_per_hour": calls / 24,
                    "calls_per_second": calls / (24 * 3600),
                    "execution_time_ms_avg": avg_ms,
                    "execution_time_ms_min": float(r.get("min_time_ms") or 0),
                    "execution_time_ms_max": float(r.get("max_time_ms") or 0),
                    "execution_time_ms_p50": p50,
                    "execution_time_ms_p95": p95,
                    "execution_time_ms_p99": p99,
                    "total_time_ms": float(r.get("total_time_ms") or 0),
                    "rows_returned_avg": (r.get("rows") or 0) / calls,
                    "rows_returned_p95": _estimate_rows_p95(
                        (r.get("rows") or 0) / calls, avg_ms, p95
                    ),
                    "rows_affected_avg": 0,
                    "rows_examined_avg": total_blks
                    * 8192.0
                    / max(calls, 1),  # blocks * 8KB as proxy for rows examined
                    "scan_efficiency_pct": min(
                        round((r.get("rows") or 0) / max(total_blks * 8192.0, 1) * 100, 2), 100.0
                    )
                    if total_blks > 0
                    else None,
                    "tables_accessed": _extract_tables(query_text),
                    "filter_columns": _extract_filter_columns(query_text),
                    "sort_columns": _extract_sort_columns(query_text),
                    "has_joins": " join " in query_text.lower(),
                    "join_count": len(re.findall(r"\bjoin\b", query_text, re.I)),
                    "has_aggregations": bool(
                        re.search(r"\b(count|sum|avg|min|max|group\s+by)\b", query_text, re.I)
                    ),
                    "has_subqueries": query_text.lower().count("select") > 1,
                    "has_text_search": _has_text_search(query_text),
                    "text_search_type": _text_search_type(query_text),
                    "has_time_range_filter": _has_time_range_filter(query_text),
                    # PostgreSQL-specific
                    "cache_hit_ratio_pct": round(shared_hit / max(total_blks, 1) * 100, 2),
                    "shared_blks_hit": shared_hit,
                    "shared_blks_read": shared_read,
                    "io_read_time_ms": float(r.get("io_read_time_ms") or 0),
                    "io_write_time_ms": float(r.get("io_write_time_ms") or 0),
                    "temp_blocks_read": int(r.get("temp_blks_read") or 0),
                    "temp_blocks_written": int(r.get("temp_blks_written") or 0),
                }
            )
        return patterns

    # -------------------------------------------------------------------
    # Global stats
    # -------------------------------------------------------------------

    def collect_global_stats(self) -> dict:
        rows = self._query(
            """
            SELECT
                numbackends AS active_connections,
                xact_commit + xact_rollback AS total_transactions,
                blks_hit, blks_read,
                tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted,
                temp_files, temp_bytes
            FROM pg_stat_database WHERE datname = current_database()
        """
        )
        if not rows:
            return {}
        r = rows[0]
        blks_hit = int(r.get("blks_hit") or 0)
        blks_read = int(r.get("blks_read") or 0)
        return {
            "cache_hit_ratio_pct": round(blks_hit / max(blks_hit + blks_read, 1) * 100, 2),
            "buffer_pool_hits": blks_hit,
            "buffer_pool_reads_from_disk": blks_read,
            "buffer_pool_read_requests": blks_hit + blks_read,
            "tmp_disk_tables": int(r.get("temp_files") or 0),
            "tmp_tables": int(r.get("temp_files") or 0),
            "active_connections": int(r.get("active_connections") or 0),
            "total_transactions": int(r.get("total_transactions") or 0),
        }

    # -------------------------------------------------------------------
    # Sample data
    # -------------------------------------------------------------------

    def collect_sample_data(self, table_name: str, limit: int = 10) -> list[dict]:
        return self._query(
            f'SELECT * FROM public."{table_name}" LIMIT {limit}'  # nosec B608 — table_name from information_schema, not user input
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUERY_TYPE_RE = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.I)
_TABLE_RE = re.compile(r"(?:FROM|JOIN|INTO|UPDATE)\s+(?:public\.)?\"?(\w+)\"?", re.I)
_WHERE_COL_RE = re.compile(r"WHERE\s+.*?\"?(\w+)\"?\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)", re.I)
_AND_COL_RE = re.compile(r"(?:AND|OR)\s+\"?(\w+)\"?\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)", re.I)
_ORDER_COL_RE = re.compile(r"ORDER\s+BY\s+(.*?)(?:LIMIT|OFFSET|$)", re.I)


def _extract_query_type(sql: str) -> str:
    m = _QUERY_TYPE_RE.match(sql)
    return m.group(1).upper() if m else "OTHER"


def _extract_tables(sql: str) -> list[str]:
    return list(dict.fromkeys(_TABLE_RE.findall(sql))) or ["unknown"]


def _extract_filter_columns(sql: str) -> list[str] | None:
    cols = _WHERE_COL_RE.findall(sql) + _AND_COL_RE.findall(sql)
    return list(dict.fromkeys(cols)) or None


def _extract_sort_columns(sql: str) -> list[str] | None:
    m = _ORDER_COL_RE.search(sql)
    if not m:
        return None
    raw = m.group(1)
    cols = [c.strip().strip('"').split()[0] for c in raw.split(",")]
    return [c for c in cols if c and c.upper() not in ("ASC", "DESC")] or None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _estimate_percentiles(mean: float, stddev: float) -> tuple[float, float, float]:
    """Estimate p50/p95/p99 from mean+stddev (normal approximation)."""
    p50 = max(0.0, mean)
    p95 = max(0.0, mean + 1.645 * stddev)
    p99 = max(0.0, mean + 2.326 * stddev)
    return round(p50, 3), round(p95, 3), round(p99, 3)


def _estimate_rows_p95(avg_rows: float, avg_ms: float, p95_ms: float) -> float | None:
    if not avg_ms or avg_ms == 0 or not p95_ms:
        return None
    return round(avg_rows * (p95_ms / avg_ms), 1)


def _has_text_search(query_text: str) -> bool:
    if re.search(r"\b(to_tsvector|to_tsquery|plainto_tsquery|@@)\b", query_text, re.I):
        return True
    if re.search(r"\s%\s", query_text):
        return True
    if re.search(r"\b[I]?LIKE\s+'%", query_text, re.I):
        return True
    # pg_stat_statements parameterizes: LIKE $1
    if re.search(r"\b[I]?LIKE\s+\$\d+", query_text, re.I):
        return True
    return False


def _text_search_type(query_text: str) -> str | None:
    if re.search(r"\b(to_tsvector|to_tsquery|@@)\b", query_text, re.I):
        return "tsvector"
    if re.search(r"\s%\s", query_text):
        return "trigram"
    if re.search(r"\b[I]?LIKE\s+'%", query_text, re.I):
        return "like_wildcard"
    if re.search(r"\b[I]?LIKE\s+\$\d+", query_text, re.I):
        return "like_wildcard"
    return None


def _has_time_range_filter(query_text: str) -> bool:
    if re.search(
        r"\b(now\(\)|CURRENT_TIMESTAMP|CURRENT_DATE)\s*[-+]\s*interval\b", query_text, re.I
    ):
        return True
    if re.search(r"\bBETWEEN\s+'?\d{4}-\d{2}-\d{2}", query_text, re.I):
        return True
    if re.search(r"[<>]=?\s+'?\d{4}-\d{2}-\d{2}", query_text, re.I):
        return True
    # pg_stat_statements: now() - $1 or date column > $1
    if re.search(r"\b(now\(\)|CURRENT_TIMESTAMP)\s*[-+]\s*\$\d+", query_text, re.I):
        return True
    if re.search(
        r"\b(date|time|created|updated|timestamp)\w*\b\s*[<>]=?\s*\$\d+", query_text, re.I
    ):
        return True
    return False
