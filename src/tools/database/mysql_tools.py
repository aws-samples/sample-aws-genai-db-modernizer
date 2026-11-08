# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
MySQL Collector Tools — SSM-based

All queries execute on a remote automation instance via SSM Run Command.
No direct database connection from the collector.
"""

import functools
import hashlib
import logging
import re

from src.agents.collector.mysql_routine_create import quote_ident
from src.shared.query_capture import DEFAULT_MIN_EXECUTIONS, capture_settings
from src.shared.rate_window import (
    ServerWindow,
    first_row_figure,
    rate_sample_setting,
    sample_arguments,
    server_window,
    statement_rate_fields,
    take_live_sample,
)
from src.tools.aws.ssm_executor import SSMExecutor
from src.tools.database import live_sql
from src.tools.database.filter_columns import where_columns
from src.tools.database.index_keys import add_index_key
from src.tools.database.live_sql import LiveQueryReader
from src.tools.sql.query_type import statement_query_type

logger = logging.getLogger(__name__)


# #1652: the collection script's routine sections (``scripts/collect-mysql.sql``), for
# the live collector, with DATABASE() for ``@db``; ``{flag}`` is 1 or 0
# (``include_routine_bodies``). Catalog reads only: no SHOW, no setting changed.
_PROCEDURES_SQL = """
    SELECT routine_name AS routine_name,
           routine_schema AS schema_name,
           specific_name AS specific_name,
           routine_type AS routine_type,
           data_type AS return_type,
           definer AS definer,
           security_type AS security_type,
           is_deterministic AS is_deterministic,
           sql_data_access AS sql_data_access,
           routine_comment AS routine_comment,
           external_language AS external_language,
           sql_mode AS sql_mode,
           character_set_client AS character_set_client,
           collation_connection AS collation_connection,
           database_collation AS database_collation,
           CHAR_LENGTH(routine_definition) AS body_chars,
           IF({flag} = 1, routine_definition, NULL) AS definition,
           CASE WHEN {flag} = 0 THEN 'opted_out'
                WHEN routine_definition IS NULL THEN 'not_readable'
                ELSE 'collected' END AS body_status
    FROM information_schema.routines
    WHERE routine_schema = DATABASE()
    ORDER BY routine_type, routine_name
"""

_TRIGGERS_SQL = """
    SELECT trigger_name AS trigger_name,
           trigger_schema AS schema_name,
           event_manipulation AS event_type,
           event_object_table AS table_name,
           action_timing AS timing,
           action_orientation AS for_each,
           sql_mode AS sql_mode,
           definer AS definer,
           action_order AS action_order,
           character_set_client AS character_set_client,
           collation_connection AS collation_connection,
           database_collation AS database_collation,
           CHAR_LENGTH(action_statement) AS body_chars,
           IF({flag} = 1, action_statement, NULL) AS definition,
           IF({flag} = 1, 'collected', 'opted_out') AS body_status
    FROM information_schema.triggers
    WHERE trigger_schema = DATABASE()
    ORDER BY event_object_table, trigger_name
"""

_PARAMETERS_SQL = """
    SELECT specific_name AS routine_name,
           specific_schema AS schema_name,
           routine_type AS routine_type,
           ordinal_position AS ordinal,
           parameter_mode AS mode,
           parameter_name AS parameter_name,
           data_type AS data_type,
           dtd_identifier AS dtd_identifier,
           character_set_name AS character_set_name,
           collation_name AS collation_name
    FROM information_schema.parameters
    WHERE specific_schema = DATABASE()
    ORDER BY routine_type, specific_name, ordinal_position
"""

_EVENTS_SQL = """
    SELECT event_name AS event_name,
           event_schema AS schema_name,
           definer AS definer,
           time_zone AS time_zone,
           event_type AS event_type,
           DATE_FORMAT(execute_at, '%Y-%m-%d %H:%i:%s') AS execute_at,
           CAST(interval_value AS CHAR) AS interval_value,
           interval_field AS interval_field,
           DATE_FORMAT(starts, '%Y-%m-%d %H:%i:%s') AS starts,
           DATE_FORMAT(ends, '%Y-%m-%d %H:%i:%s') AS ends,
           status AS status,
           on_completion AS on_completion,
           event_comment AS event_comment,
           sql_mode AS sql_mode,
           character_set_client AS character_set_client,
           collation_connection AS collation_connection,
           database_collation AS database_collation,
           CHAR_LENGTH(event_definition) AS body_chars,
           IF({flag} = 1, event_definition, NULL) AS definition,
           IF({flag} = 1, 'collected', 'opted_out') AS body_status
    FROM information_schema.events
    WHERE event_schema = DATABASE()
    ORDER BY event_name
"""

_PARAMETERS_COUNTED_SQL = """
    SELECT COUNT(*) AS n FROM information_schema.parameters WHERE specific_schema = DATABASE()
"""

_ROUTINES_COUNTED_SQL = """
    SELECT (SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema = DATABASE())
         + (SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema = DATABASE())
         + (SELECT COUNT(*) FROM information_schema.events WHERE event_schema = DATABASE()) AS n
"""

_GRANTEE = (
    "CONCAT('''', SUBSTRING_INDEX(CURRENT_USER(), '@', 1), '''@''', "
    "SUBSTRING_INDEX(CURRENT_USER(), '@', -1), '''')"
)
_PRIVILEGES_NOT_SEEN_SQL = f"""
    SELECT p.privilege FROM (
      SELECT 'SHOW_ROUTINE' AS privilege UNION ALL SELECT 'TRIGGER' UNION ALL SELECT 'EVENT'
    ) AS p
    WHERE NOT EXISTS (
            SELECT 1 FROM information_schema.user_privileges u
             WHERE u.grantee = {_GRANTEE}
               AND (u.privilege_type = p.privilege
                    OR (p.privilege = 'SHOW_ROUTINE' AND u.privilege_type = 'SELECT')))
      AND NOT EXISTS (
            SELECT 1 FROM information_schema.schema_privileges s
             WHERE s.grantee = {_GRANTEE} AND s.table_schema = DATABASE()
               AND s.privilege_type = p.privilege)
"""  # nosec B608 -- fixed text, no input

_PROGRAM_STATS_SQL = """
    SELECT OBJECT_NAME AS routine_name, OBJECT_SCHEMA AS schema_name,
           OBJECT_TYPE AS object_type, COUNT_STAR AS execution_count,
           ROUND(SUM_TIMER_WAIT / 1000000000, 3) AS total_time_ms
    FROM performance_schema.events_statements_summary_by_program
    WHERE OBJECT_SCHEMA = DATABASE()
"""
# The routine text columns a live read decodes from the client's batch escapes (#1652).
_TEXT_COLUMNS = (
    "definition",
    "routine_comment",
    "event_comment",
    "create procedure",
    "create function",
    "sql original statement",
    "create event",
)
# The mysql client's batch-mode escapes (no --raw), read left to right.
_BATCH_ESCAPES = {"\\": "\\", "n": "\n", "t": "\t", "0": "\0", "r": "\r"}
_BATCH_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
# SHOW CREATE's statement and the column the client names it in (headers read lower case).
_SHOW_CREATE = {
    "procedure": ("PROCEDURE", "create procedure"),
    "function": ("FUNCTION", "create function"),
    "trigger": ("TRIGGER", "sql original statement"),
    "event": ("EVENT", "create event"),
}

_NO_PROGRAM_STATS = {
    "status": "not_tracked",
    "reason": "performance_schema program statistics are not available",
}


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
        self._statistics_expression: bool | None = None

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

    def get_settings(self) -> dict[str, str]:
        """The server settings that change how SQL text is read (#1482): the default
        ``sql_mode`` the application's sessions start with. Empty when it can't be read."""
        try:
            rows = self._query("SELECT @@GLOBAL.sql_mode AS sql_mode")
        except Exception as error:  # noqa: BLE001 -- a setting not read is the default
            logger.info("sql_mode not read (%s); the engine's defaults are read", error)
            return {}
        if not rows:
            logger.info("sql_mode not read (no row); the engine's defaults are read")
            return {}
        return {"sql_mode": str(rows[0]["sql_mode"] or "")}

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
                IF(engine IN ('MyISAM', 'Aria'), 'exact', 'estimate') AS row_count_basis,
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
                character_maximum_length AS max_length,
                numeric_precision, numeric_scale, is_nullable,
                column_default, column_key, extra
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = '{table_name}'
            ORDER BY ordinal_position
        """)  # nosec B608 — table_name from information_schema, not user input

    def _statistics_has_expression(self) -> bool:
        """Whether ``information_schema.statistics`` has the ``EXPRESSION`` column of a
        functional key part: MySQL 8.0.13+ (MariaDB's collector says no). Asked once per
        collector."""
        if self._statistics_expression is None:
            self._statistics_expression = _version_gte(self.get_version(), "8.0.13")
        return self._statistics_expression

    def collect_indexes(self, table_name: str) -> list[dict]:
        # A functional key part (MySQL 8.0.13+) has a NULL column_name; its text is in
        # EXPRESSION, a column older servers and MariaDB do not have (#295).
        expression = ", expression" if self._statistics_has_expression() else ""
        raw = self._query(f"""
            SELECT index_name, column_name, seq_in_index, non_unique, index_type, sub_part{expression}
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
                    "prefix_lengths": [],
                    "has_expressions": False,
                    "index_expressions": None,
                }
            add_index_key(indexes[name], r, "mysql")
        for index in indexes.values():
            if not any(n is not None for n in index["prefix_lengths"]):
                index["prefix_lengths"] = None  # whole-column index, or an unknown length
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

    def _text_rows(self, sql: str) -> list[dict]:
        """``sql``'s rows with each routine text column as the server holds it (#1652):
        the client's batch output escapes a backslash, newline, tab and NUL in a value,
        which a routine's body must not keep. A transport that returns values as they are
        (``batch_escaped = False``) is read unchanged."""
        rows = self._query(sql)
        if not getattr(getattr(self, "ssm", None), "batch_escaped", True):
            return rows
        for row in rows:
            for key in [k for k in row if str(k).lower() in _TEXT_COLUMNS]:
                if isinstance(row.get(key), str):
                    row[key] = _BATCH_ESCAPE.sub(
                        lambda m: _BATCH_ESCAPES.get(m.group(1), m.group(0)), row[key]
                    )
        return rows

    def collect_procedures(self, include_bodies: bool = True) -> list[dict]:
        """Routines with the catalog columns their CREATE is rebuilt from, and whether
        the user could read each body (#1652); ``include_bodies=False`` reads none."""
        flag = 1 if include_bodies else 0
        return self._text_rows(_PROCEDURES_SQL.format(flag=flag))  # nosec B608 -- flag is 0 or 1

    def collect_triggers(self, include_bodies: bool = True) -> list[dict]:
        """Triggers (listed only with the TRIGGER privilege) with their orientation,
        DEFINER and order (#1652); ``include_bodies=False`` reads no body."""
        flag = 1 if include_bodies else 0
        return self._text_rows(_TRIGGERS_SQL.format(flag=flag))  # nosec B608 -- flag is 0 or 1

    def show_create(self, kind: str, name: str) -> str | None:
        """The server's own CREATE for a routine, trigger or event of this database
        (#1652): ``SHOW CREATE PROCEDURE / FUNCTION / TRIGGER / EVENT``, a read that needs
        the privileges reading its body does. None when it is not permitted, returns no
        text, or ``kind`` is not one of those."""
        if kind not in _SHOW_CREATE:
            return None
        keyword, column = _SHOW_CREATE[kind]
        try:
            rows = self._text_rows(
                f"SHOW CREATE {keyword} {quote_ident(name)}"  # nosec B608 -- name quoted
            )
        except Exception:  # noqa: BLE001 -- not permitted: the caller rebuilds instead
            return None
        row = {str(k).lower(): v for k, v in rows[0].items()} if rows else {}
        value = row.get(column)
        return value if isinstance(value, str) and value.strip() else None

    def collect_routine_parameters(self) -> list[dict]:
        """Each routine's parameters and a function's return type, ordinal 0 (#1652)."""
        return self._query(_PARAMETERS_SQL)

    def collect_events(self, include_bodies: bool = True) -> list[dict]:
        """The scheduled events (listed only with the EVENT privilege, #1652)."""
        flag = 1 if include_bodies else 0
        return self._text_rows(_EVENTS_SQL.format(flag=flag))  # nosec B608 -- flag is 0 or 1

    def collect_routine_capture(self) -> dict:
        """The server's routine count and the visibility privileges not granted to the
        collection user directly (#1652): a hint, never a failure."""
        counted = self._query(_ROUTINES_COUNTED_SQL)
        missing = self._query(_PRIVILEGES_NOT_SEEN_SQL)
        parameters = self._query(_PARAMETERS_COUNTED_SQL)
        return {
            "routines_counted": int(counted[0]["n"]) if counted else None,
            "parameters_counted": int(parameters[0]["n"]) if parameters else None,
            "privileges_not_seen": sorted(
                {str(r["privilege"]) for r in missing if r.get("privilege")}
            ),
        }

    def collect_routine_stats(self) -> tuple[list[dict], dict]:
        """Per-program execution counts (#1652), read only when performance_schema has
        them, and how they were read; no setting is changed."""
        exists = self._query(
            "SELECT COUNT(*) AS n FROM information_schema.tables "
            "WHERE table_schema = 'performance_schema' "
            "AND table_name = 'events_statements_summary_by_program'"
        )
        if not exists or int(exists[0].get("n") or 0) == 0:
            return [], dict(_NO_PROGRAM_STATS)
        enabled = self._query("SELECT @@performance_schema AS ps")
        if not enabled or int(enabled[0].get("ps") or 0) != 1:
            return [], {"status": "not_tracked", "reason": "performance_schema is off"}
        return self._query(_PROGRAM_STATS_SQL), {"status": "tracked"}

    def stats_window(self) -> ServerWindow | None:
        """The window the server's statement counters cover (#1893): its ``Uptime``
        (``performance_schema`` resets with the server). ``SHOW GLOBAL STATUS`` answers
        on every MySQL and MariaDB version, where ``performance_schema.global_status``
        needs MySQL 5.7 (with ``show_compatibility_56`` off) or MariaDB 10.5. None when
        it can't be read."""
        try:
            rows = self._query("SHOW GLOBAL STATUS LIKE 'Uptime'")
        except Exception:  # noqa: BLE001 -- no uptime: the rates fall back to 24 hours
            return None
        return server_window(uptime_seconds=first_row_figure(rows, "Value"))

    def statements_since_start(self) -> object:
        """The server's own count of client statements since it started (#1991):
        ``Questions``, which no digest reset touches. A read, like ``stats_window``."""
        return first_row_figure(self._query("SHOW GLOBAL STATUS LIKE 'Questions'"), "Value")

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

    def rate_sample_snapshot_sql(self, has_first_seen: bool) -> str:
        """One snapshot of the digest counters (#1908): first a row with the server's UTC
        clock (``UTC_TIMESTAMP(6)``, so a daylight-saving change in the session's zone
        does not move it) and how many digests the snapshot holds, then each digest's
        ``COUNT_STAR`` and age. ``FIRST_SEEN`` is in the entry's key when the server has
        it, so a digest emptied and counted again in between (a ``TRUNCATE`` of the
        summary table) is a new entry, not a lower count."""
        first_seen = (
            "IFNULL(DATE_FORMAT(FIRST_SEEN, '%Y-%m-%d %H:%i:%s.%f'), '')"
            if has_first_seen
            else "''"
        )
        age = (
            "TIMESTAMPDIFF(MICROSECOND, FIRST_SEEN, NOW(6)) / 1000000" if has_first_seen else "NULL"
        )
        digests = (
            "FROM performance_schema.events_statements_summary_by_digest "
            "WHERE SCHEMA_NAME = DATABASE() AND DIGEST IS NOT NULL"
        )
        return f"""
            SELECT 0 AS row_kind, CAST(NULL AS CHAR) AS group_key,
                   CAST(NULL AS CHAR) AS entry_key, CAST(NULL AS UNSIGNED) AS execution_count,
                   NULL AS entry_age_seconds,
                   TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', UTC_TIMESTAMP(6)) / 1000000
                       AS sampled_at,
                   (SELECT COUNT(*) {digests}) AS entry_count
            UNION ALL
            SELECT 1, DIGEST, CONCAT(DIGEST, '/', {first_seen}), COUNT_STAR, {age}, NULL, NULL
            {digests}
            ORDER BY row_kind
        """  # nosec B608 -- first_seen, age and digests are fixed fragments

    def collect_query_patterns(
        self,
        min_executions: int = DEFAULT_MIN_EXECUTIONS,
        limit: int | None = None,
        rate_sample_seconds: int = 0,
    ) -> list[dict]:
        """The digests' statements, with their rates over ``rate_sample_seconds`` between
        two snapshots of the counters when it is above 0 (#1908, the collector passes
        ``CollectionOptions.rate_sample_seconds``)."""
        limit, min_executions = capture_settings(limit, min_executions)
        rate_sample_seconds = rate_sample_setting(rate_sample_seconds)
        # No cap (the default, #384): every statement that ran often enough.
        limit_clause = f"LIMIT {limit}" if limit is not None else ""
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

        statements_sql = f"""
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
            {limit_clause}
        """  # nosec B608 — extra_cols fixed; both settings are checked ints (#384)
        snapshot_sql = self.rate_sample_snapshot_sql(has_first_last_seen)
        raw, live = take_live_sample(
            lambda: self._query(snapshot_sql),
            rate_sample_seconds,
            lambda: self._query(statements_sql),
        )

        patterns = []
        reader = LiveQueryReader("mysql", "mysql_tools")
        # #1893: read once, and only if a statement has no usable window of its own (or
        # to check a sample's interval against it, #1908).
        counters_window = functools.cache(self.stats_window)
        sample = live.sample(counters_window) if live is not None else None
        for r in raw:
            query_text = str(r.get("query_text") or "")
            exec_count = r.get("execution_count") or 1
            query_id = r.get("digest") or _hash(query_text)
            shape = reader.fields(str(query_id), query_text)
            first_seen = r.get("first_seen") or r.get("FIRST_SEEN")
            last_seen = r.get("last_seen") or r.get("LAST_SEEN")
            first_seen = str(first_seen) if first_seen else None
            last_seen = str(last_seen) if last_seen else None
            patterns.append(
                {
                    "query_id": query_id,
                    "query_text": query_text,
                    "query_type": statement_query_type(query_text, "mysql"),
                    "execution_count": exec_count,
                    # #1893: over the digest's FIRST_SEEN to LAST_SEEN, else the uptime;
                    # #1908: over the interval between two snapshots, when sampled.
                    **statement_rate_fields(
                        exec_count,
                        first_seen,
                        last_seen,
                        counters_window,
                        **sample_arguments(live, sample, query_id),
                    ),
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
                    "tables_accessed": shape["tables_accessed"],
                    "filter_columns": _extract_filter_columns(query_text),
                    "sort_columns": _extract_sort_columns(query_text),
                    "has_joins": shape["has_joins"],
                    "join_count": shape["join_count"],
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
                    "has_aggregations": shape["has_aggregations"],
                    "has_subqueries": shape["has_subqueries"],
                    "has_text_search": shape["has_text_search"],
                    "text_search_type": shape["text_search_type"],
                    "parsed_facts": shape["parsed_facts"],
                    "query_tags": shape["query_tags"],
                    "query_tags_dropped": shape["query_tags_dropped"],
                    "has_time_range_filter": _has_time_range_filter(query_text),
                    "errors": r.get("sum_errors") or 0,
                    "warnings": r.get("sum_warnings") or 0,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                }
            )
        reader.log_fallbacks()
        return patterns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AND_COL_RE = re.compile(r"(?:AND|OR)\s+`?(\w+)`?\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)", re.I)
_ORDER_COL_RE = re.compile(r"ORDER\s+BY\s+(.*?)(?:LIMIT|$)", re.I)


def _extract_tables(sql: str) -> list[str]:
    """The live ``tables_accessed`` for ``sql``, read through the SQL facade (#795)."""
    return live_sql.extract_tables(sql, "mysql")


def _estimate_rows_p95(avg_rows: float, avg_ms: float, p95_ms: float) -> float | None:
    """Estimate p95 rows returned using latency ratio as proxy."""
    if not avg_ms or avg_ms == 0 or not p95_ms:
        return None
    ratio = p95_ms / avg_ms
    return round(avg_rows * ratio, 1)


def _extract_filter_columns(sql: str) -> list[str] | None:
    # Comments and literals are not SQL (#1420); a quoted name is one name (#1737).
    text = live_sql.column_text(sql, "mysql")
    cols = where_columns(text.text, "`") + _AND_COL_RE.findall(text.text)
    return list(dict.fromkeys(text.name(c) for c in cols)) or None


def _extract_sort_columns(sql: str) -> list[str] | None:
    # Comments and literals are not SQL (#1420); a quoted name is one name (#1737).
    text = live_sql.column_text(sql, "mysql")
    m = _ORDER_COL_RE.search(text.text)
    if not m:
        return None
    words = [c.split() for c in m.group(1).split(",")]
    cols = [w[0] for w in words if w]
    # A literal (a placeholder once blanked) sorts by a constant: it names no column.
    return [text.name(c) for c in cols if c != "?" and c.upper() not in ("ASC", "DESC")] or None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _has_time_range_filter(query_text: str) -> bool:
    query_text = live_sql.time_range_text(query_text, "mysql")  # (#1420)
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
