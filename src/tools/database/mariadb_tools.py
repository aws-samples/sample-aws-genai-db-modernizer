"""
MariaDB Collector Tools — SSM-based

Extends MySQLRemoteCollector with MariaDB-specific version detection.
MariaDB uses the same mysql CLI, performance_schema, and INFORMATION_SCHEMA
but has different version numbering and feature availability.
"""

import re

from src.tools.database.mysql_tools import MySQLRemoteCollector, _version_gte


def _is_mariadb(version: str) -> bool:
    return "mariadb" in version.lower()


def _mariadb_version_gte(version: str, minimum: str) -> bool:
    """Version check for MariaDB strings like '10.6.18-MariaDB'."""
    return _version_gte(version, minimum)


class MariaDBRemoteCollector(MySQLRemoteCollector):
    """MariaDB collector — overrides query pattern collection for correct feature flags."""

    def collect_query_patterns(self, min_executions: int = 10, limit: int = 1000) -> list[dict]:
        version = self.get_version()

        # MariaDB never has MySQL 8.0.25+ QUANTILE columns
        has_quantiles = False
        # SUM_ERRORS/SUM_WARNINGS: MariaDB 10.5.4+
        has_errors = _mariadb_version_gte(version, "10.5.4") if _is_mariadb(version) else False
        # FIRST_SEEN/LAST_SEEN: MariaDB 10.5.4+
        has_first_last_seen = (
            _mariadb_version_gte(version, "10.5.4") if _is_mariadb(version) else False
        )

        return self._collect_query_patterns_with_flags(
            has_quantiles=has_quantiles,
            has_errors=has_errors,
            has_first_last_seen=has_first_last_seen,
            min_executions=min_executions,
            limit=limit,
        )

    def _collect_query_patterns_with_flags(
        self,
        *,
        has_quantiles: bool,
        has_errors: bool,
        has_first_last_seen: bool,
        min_executions: int,
        limit: int,
    ) -> list[dict]:
        """Build and execute query patterns SQL with explicit feature flags."""
        from src.tools.database.mysql_tools import (
            _estimate_rows_p95,
            _extract_filter_columns,
            _extract_query_type,
            _extract_sort_columns,
            _extract_tables,
            _has_text_search,
            _has_time_range_filter,
            _hash,
            _text_search_type,
        )

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
        """  # nosec B608
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
                    "execution_time_ms_p50": float(r.get("avg_time_ms") or 0),
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
