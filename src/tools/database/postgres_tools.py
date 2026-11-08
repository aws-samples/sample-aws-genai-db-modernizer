# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
PostgreSQL Collector Tools — SSM-based

All queries execute on a remote automation instance via SSM Run Command.
Mirrors MySQLRemoteCollector pattern but with PostgreSQL-specific queries
(pg_stat_statements, pg_stat_user_tables, pg_stats, information_schema).
"""

import contextlib
import functools
import hashlib
import json
import logging
import re

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
from src.tools.database.index_keys import add_index_key, expression_key_fields
from src.tools.database.live_sql import LiveQueryReader
from src.tools.sql.query_type import statement_query_type

logger = logging.getLogger(__name__)


# One pg_stat_statements entry (#1908): its user, query id and, on PostgreSQL 14+, whether
# it is a top-level statement. The statement query returns it with each row, and both
# sampling snapshots key each entry's count by it.
_SAMPLE_KEY = "s.userid::text || '/' || s.queryid::text || '/' || " + (
    "coalesce(to_jsonb(s) ->> 'toplevel', '')"
)


# #1652: the offline script's sections 8 and 9 (``scripts/collect-postgresql.sql``),
# for schema public as every live read here; ``{flag}`` is 1 or 0
# (``include_routine_bodies``). ``tests/unit/scripts/test_collect_postgresql_routines_sql.py``
# keeps the two the same.
_PROCEDURES_SQL = """
    SELECT
      pr.proname AS routine_name,
      n.nspname AS schema_name,
      pr.proname || '_' || pr.oid AS specific_name,
      pg_get_function_identity_arguments(pr.oid) AS identity_arguments,
      CASE pr.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS routine_type,
      pg_get_function_result(pr.oid) AS return_type,
      upper(l.lanname) AS language,
      pg_get_userbyid(pr.proowner) AS owner,
      pr.prosecdef AS security_definer,
      coalesce((
        SELECT json_agg(json_build_object('kind', dep.kind, 'name', dep.name) ORDER BY dep.kind, dep.name)
        FROM (
          SELECT DISTINCT
            CASE
              WHEN d.refclassid = 'pg_proc'::regclass THEN 'function'
              WHEN d.refclassid = 'pg_type'::regclass THEN 'type'
              WHEN c.relkind IN ('r', 'p', 'f') THEN 'table'
              WHEN c.relkind IN ('v', 'm') THEN 'view'
              WHEN c.relkind = 'S' THEN 'sequence'
            END AS kind,
            CASE
              WHEN d.refclassid = 'pg_proc'::regclass THEN d.refobjid::regprocedure::text
              WHEN d.refclassid = 'pg_type'::regclass THEN d.refobjid::regtype::text
              ELSE d.refobjid::regclass::text
            END AS name
          FROM pg_depend d
          LEFT JOIN pg_class c ON d.refclassid = 'pg_class'::regclass AND c.oid = d.refobjid
          WHERE d.classid = 'pg_proc'::regclass AND d.objid = pr.oid AND d.deptype = 'n'
            AND d.refclassid IN ('pg_proc'::regclass, 'pg_type'::regclass, 'pg_class'::regclass)
        ) dep
        WHERE dep.kind IS NOT NULL
      ), '[]'::json) AS depends_on,
      length(pg_get_functiondef(pr.oid)) AS body_chars,
      CASE WHEN {flag} = 1 THEN pg_get_functiondef(pr.oid) END AS definition,
      CASE WHEN {flag} = 1 THEN 'collected' ELSE 'opted_out' END AS body_status
    FROM pg_proc pr
    JOIN pg_namespace n ON n.oid = pr.pronamespace
    JOIN pg_language l ON l.oid = pr.prolang
    WHERE pr.prokind IN ('f', 'p')
      AND n.nspname = 'public'
      AND NOT EXISTS (
        SELECT 1 FROM pg_depend x
        WHERE x.classid = 'pg_proc'::regclass AND x.objid = pr.oid
          AND x.refclassid = 'pg_extension'::regclass AND x.deptype = 'e')
    ORDER BY routine_type, routine_name, specific_name
"""

_TRIGGERS_SQL = """
    -- pg_trigger, not information_schema.triggers: that view lists only the triggers
    -- on tables the user owns or holds more than SELECT on, so a read-only user saw
    -- none (#1651). One row per trigger, with every event it fires on. #1652: its
    -- orientation, state and function, and its whole CREATE (pg_get_triggerdef).
    -- tgtype bits: 1 ROW, 2 BEFORE, 4 INSERT, 8 DELETE, 16 UPDATE, 32 TRUNCATE,
    -- 64 INSTEAD OF.
    SELECT
      t.tgname AS trigger_name,
      n.nspname AS schema_name,
      c.relname AS table_name,
      concat_ws(' OR ',
        CASE WHEN t.tgtype & 4 <> 0 THEN 'INSERT' END,
        CASE WHEN t.tgtype & 16 <> 0 THEN 'UPDATE' END,
        CASE WHEN t.tgtype & 8 <> 0 THEN 'DELETE' END,
        CASE WHEN t.tgtype & 32 <> 0 THEN 'TRUNCATE' END) AS event_type,
      CASE t.tgtype & 66 WHEN 2 THEN 'BEFORE' WHEN 64 THEN 'INSTEAD OF' ELSE 'AFTER' END
        AS timing,
      CASE WHEN t.tgtype & 1 <> 0 THEN 'ROW' ELSE 'STATEMENT' END AS for_each,
      t.tgenabled::text AS is_enabled,
      t.tgfoid::regprocedure::text AS function_signature,
      length(pg_get_triggerdef(t.oid)) AS body_chars,
      CASE WHEN {flag} = 1 THEN pg_get_triggerdef(t.oid) END AS definition,
      CASE WHEN {flag} = 1 THEN 'collected' ELSE 'opted_out' END AS body_status
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT t.tgisinternal
      -- a partition's clone of its parent's trigger is the parent's trigger: one row
      -- (tgparentid is PostgreSQL 13+; on 12 the clones are internal)
      AND coalesce(to_jsonb(t) ->> 'tgparentid', '0') = '0'
      AND n.nspname = 'public'
    ORDER BY c.relname, t.tgname
"""


def _nested(toplevel: object) -> bool | None:
    """``pg_stat_statements.toplevel`` read backwards (#1652): True for a statement that
    ran inside a function or procedure; None when the server does not report it."""
    if toplevel is None or toplevel == "":
        return None
    if isinstance(toplevel, bool):
        return not toplevel
    return str(toplevel).strip().lower() in ("false", "f")


def n_distinct_count(n_distinct: object, reltuples: object) -> int | None:
    """A column's distinct values from ``pg_stats.n_distinct`` (#1989 review).

    A positive value is the count; a negative one is minus the fraction of the table's
    rows (-1: every row distinct), so the count is ``-n_distinct × reltuples``; 0 means
    PostgreSQL does not know. None when there is no count: 0, a negative value with no
    (or a non-positive) row estimate, or a value that is not a number."""
    try:
        n = float(n_distinct)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if n > 0:
        return int(round(n))
    if n < 0:
        try:
            rows = float(reltuples)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        count = int(round(-n * rows))
        return count if count > 0 else None
    return None


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

    def get_settings(self) -> dict[str, str]:
        """The server settings that change how SQL text is read (#1482):
        ``standard_conforming_strings``. Empty when it can't be read."""
        try:
            rows = self._query(
                "SELECT current_setting('standard_conforming_strings') "
                "AS standard_conforming_strings"
            )
        except Exception as error:  # noqa: BLE001 -- a setting not read is the default
            logger.info(
                "standard_conforming_strings not read (%s); the engine's default is read", error
            )
            return {}
        if not rows:
            logger.info("standard_conforming_strings not read (no row); the default is read")
            return {}
        return {"standard_conforming_strings": str(rows[0]["standard_conforming_strings"] or "")}

    def get_database_size_gb(self) -> float:
        rows = self._query(
            "SELECT pg_database_size(current_database()) / 1024.0 / 1024.0 / 1024.0 AS size_gb"
        )
        return float(rows[0]["size_gb"] or 0) if rows else 0

    # -------------------------------------------------------------------
    # Schema
    # -------------------------------------------------------------------

    def collect_tables(self) -> list[dict]:
        # #1636: the same catalogue estimate and unknown rule as
        # scripts/collect-postgresql.sql (pg_class.reltuples; no table is read).
        return self._query("""
            SELECT
                s.schemaname AS schema_name,
                s.relname AS table_name,
                CASE
                    WHEN c.reltuples < 0 THEN NULL
                    WHEN c.reltuples = 0 AND c.relpages = 0
                         AND current_setting('server_version_num')::int < 140000
                         AND s.last_analyze IS NULL AND s.last_autoanalyze IS NULL
                         AND s.last_vacuum IS NULL AND s.last_autovacuum IS NULL THEN NULL
                    ELSE c.reltuples::bigint
                END AS row_count,
                'estimate' AS row_count_basis,
                greatest(s.last_analyze, s.last_autoanalyze, s.last_vacuum, s.last_autovacuum)
                    AS stats_analyzed_at,
                pg_relation_size(s.relid) / 1024.0 / 1024.0 AS data_size_mb,
                pg_indexes_size(s.relid) / 1024.0 / 1024.0 AS index_size_mb,
                s.seq_scan AS full_table_scans,
                s.idx_scan AS index_scans
            FROM pg_stat_user_tables s
            JOIN pg_class c ON c.oid = s.relid
            WHERE s.schemaname = 'public'
            ORDER BY s.relname
        """)

    def collect_columns(self, table_name: str) -> list[dict]:
        return self._query(f"""
            SELECT
                column_name, ordinal_position, data_type, udt_name,
                character_maximum_length AS max_length,
                numeric_precision, numeric_scale, datetime_precision,
                is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = '{table_name}'
            ORDER BY ordinal_position
        """)  # nosec B608 — table_name from information_schema, not user input

    def collect_column_cardinality(self, table_name: str) -> dict[str, int]:
        rows = self._query(f"""
            SELECT s.attname AS col, s.n_distinct, c.reltuples
            FROM pg_stats s
            LEFT JOIN pg_namespace n ON n.nspname = s.schemaname
            LEFT JOIN pg_class c ON c.relname = s.tablename AND c.relnamespace = n.oid
            WHERE s.schemaname = 'public' AND s.tablename = '{table_name}'
        """)  # nosec B608 — table_name from information_schema, not user input
        return {
            r["col"]: count
            for r in rows
            if (count := n_distinct_count(r.get("n_distinct"), r.get("reltuples"))) is not None
        }

    def collect_indexes(self, table_name: str) -> list[dict]:
        raw = self._query(f"""
            SELECT
                i.relname AS index_name,
                a.attname AS column_name,
                ix.indisunique AS is_unique,
                ix.indisprimary AS is_primary,
                am.amname AS index_type,
                pg_get_expr(ix.indpred, ix.indrelid) AS predicate,
                (ix.indexprs IS NOT NULL) AS has_expressions,
                pg_get_expr(ix.indexprs, ix.indrelid) AS index_expressions,
                pg_get_indexdef(ix.indexrelid) AS index_def
            FROM pg_class t
            JOIN pg_index ix ON t.oid = ix.indrelid
            JOIN pg_class i ON i.oid = ix.indexrelid
            -- LEFT, not INNER: an expression key has attnum 0 and matches no
            -- pg_attribute row, and an inner join would drop the whole index.
            LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
            JOIN pg_am am ON i.relam = am.oid
            WHERE t.relname = '{table_name}'
              AND t.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')
            ORDER BY i.relname, array_position(ix.indkey, a.attnum)
        """)  # nosec B608 — table_name from information_schema, not user input
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
                    "predicate": r.get("predicate") or None,
                    **expression_key_fields(r),
                }
            add_index_key(indexes[name], r, "postgresql")
        return list(indexes.values())

    def collect_foreign_keys(self, table_name: str) -> list[dict]:
        # pg_constraint, not information_schema (#1868): see collect-postgresql.sql. One row
        # per column pair, in constraint order; conparentid = 0 drops partition copies.
        raw = self._query(f"""
            SELECT
                c.conname AS constraint_name, a.attname AS column_name,
                k.ordinal_position,
                rcl.relname AS referenced_table_name,
                ra.attname AS referenced_column_name,
                CASE c.confupdtype
                    WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE'
                    WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_update,
                CASE c.confdeltype
                    WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE'
                    WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_delete
            FROM pg_catalog.pg_constraint c
            JOIN pg_catalog.pg_class cl ON cl.oid = c.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = cl.relnamespace
            JOIN pg_catalog.pg_class rcl ON rcl.oid = c.confrelid
            CROSS JOIN LATERAL unnest(c.conkey, c.confkey)
                WITH ORDINALITY AS k(attnum, rattnum, ordinal_position)
            JOIN pg_catalog.pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            JOIN pg_catalog.pg_attribute ra
              ON ra.attrelid = c.confrelid AND ra.attnum = k.rattnum
            WHERE c.contype = 'f' AND c.conparentid = 0
              AND n.nspname = 'public' AND cl.relname = '{table_name}'
            ORDER BY c.conname, k.ordinal_position
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
                    "on_delete": r.get("on_delete"),
                    "on_update": r.get("on_update"),
                }
            fks[name]["columns"].append(r["column_name"])
            fks[name]["referenced_columns"].append(r["referenced_column_name"])
        return list(fks.values())

    def collect_primary_key(self, table_name: str) -> list[str]:
        rows = self._query(f"""
            SELECT a.attname AS column_name
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'public.{table_name}'::regclass AND i.indisprimary
            ORDER BY array_position(i.indkey, a.attnum)
        """)  # nosec B608 — table_name from information_schema, not user input
        return [r["column_name"] for r in rows]

    # -------------------------------------------------------------------
    # Views, procedures, triggers
    # -------------------------------------------------------------------

    def collect_views(self) -> list[dict]:
        return self._query("""
            SELECT viewname AS view_name, definition, viewowner AS owner
            FROM pg_views WHERE schemaname = 'public' ORDER BY viewname
        """)

    def collect_procedures(self, include_bodies: bool = True) -> list[dict]:
        """Every function and procedure in schema public with its whole CREATE (#1652),
        from pg_proc so a routine whose EXECUTE the user lacks is still listed;
        ``include_bodies=False`` reads names and sizes only."""
        flag = 1 if include_bodies else 0
        return self._query(_PROCEDURES_SQL.format(flag=flag))  # nosec B608 -- flag is 0 or 1

    def collect_extensions(self) -> list[dict]:
        """Every extension installed in the database and its version (#1078)."""
        return self._query("""
            SELECT extname AS name, extversion AS version
            FROM pg_extension
            ORDER BY extname
        """)

    def collect_enum_types(self) -> list[dict]:
        """Every enum type outside the system schemas, its labels in sort order (#1834).
        psql prints the labels as a JSON array: each row's ``labels`` is read back into a
        list (a row whose labels do not read as JSON keeps the text, and is dropped later)."""
        rows = self._query("""
            SELECT n.nspname AS schema_name, t.typname AS type_name,
                   json_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
            GROUP BY n.nspname, t.typname
            ORDER BY n.nspname, t.typname
        """)
        for row in rows:
            if isinstance(row.get("labels"), str):
                with contextlib.suppress(ValueError):
                    row["labels"] = json.loads(row["labels"])
        return rows

    def collect_triggers(self, include_bodies: bool = True) -> list[dict]:
        """Every trigger in schema public, one row each with every event (#1651), its
        orientation, state and function, and its whole CREATE (#1652), from pg_trigger:
        information_schema.triggers hides them from a read-only user."""
        flag = 1 if include_bodies else 0
        return self._query(_TRIGGERS_SQL.format(flag=flag))  # nosec B608 -- flag is 0 or 1

    def collect_routine_stats(self) -> tuple[list[dict], dict]:
        """The call counts PostgreSQL keeps per function (#1652, ``pg_stat_user_functions``,
        filled only when ``track_functions`` is pl or all) and how they were read; the
        setting is never changed."""
        stats = self._query("""
            SELECT funcname AS routine_name, schemaname AS schema_name,
                   'FUNCTION' AS object_type, calls AS execution_count,
                   round(total_time::numeric, 3) AS total_time_ms
            FROM pg_stat_user_functions
            WHERE schemaname = 'public'
        """)
        setting = self._query("SELECT current_setting('track_functions') AS track_functions")
        if setting and str(setting[0].get("track_functions") or "") == "none":
            return [], {"status": "not_tracked", "reason": "track_functions is none"}
        return stats, {"status": "tracked"}

    # -------------------------------------------------------------------
    # Query patterns (pg_stat_statements)
    # -------------------------------------------------------------------

    def rate_sample_snapshot_sql(self) -> str:
        """One snapshot of ``pg_stat_statements``' counters (#1908): first a row with the
        server's clock (``clock_timestamp()``, not the transaction's ``now()``) and how many
        entries the snapshot holds, then each entry's ``calls`` and, on PostgreSQL 17, its
        age since ``stats_since``. An entry is its user, query id and top-level flag
        (:data:`_SAMPLE_KEY`); PostgreSQL 17's ``stats_since`` is in the entry's key, so
        an entry reset and counted again in between is a new one, not a lower count."""
        return f"""
            WITH e AS (
                SELECT {_SAMPLE_KEY} AS group_key,
                       {_SAMPLE_KEY} || '/' || coalesce(to_jsonb(s) ->> 'stats_since', '')
                           AS entry_key,
                       s.calls AS execution_count,
                       extract(epoch FROM clock_timestamp()
                           - (to_jsonb(s) ->> 'stats_since')::timestamptz)::float8
                           AS entry_age_seconds
                FROM pg_stat_statements s
                WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
            )
            SELECT 0 AS row_kind, CAST(NULL AS text) AS group_key, CAST(NULL AS text) AS entry_key,
                   CAST(NULL AS bigint) AS execution_count,
                   CAST(NULL AS float8) AS entry_age_seconds,
                   extract(epoch FROM clock_timestamp())::float8 AS sampled_at,
                   (SELECT count(*) FROM e) AS entry_count
            UNION ALL
            SELECT 1, group_key, entry_key, execution_count, entry_age_seconds, NULL, NULL
            FROM e
            ORDER BY row_kind
        """  # nosec B608 -- fixed text

    def collect_query_patterns(
        self,
        min_executions: int = DEFAULT_MIN_EXECUTIONS,
        limit: int | None = None,
        rate_sample_seconds: int = 0,
    ) -> list[dict]:
        """The ``pg_stat_statements`` entries, with their rates over
        ``rate_sample_seconds`` between two snapshots of the counters when it is above 0
        (#1908, the collector passes ``CollectionOptions.rate_sample_seconds``)."""
        limit, min_executions = capture_settings(limit, min_executions)
        rate_sample_seconds = rate_sample_setting(rate_sample_seconds)
        # No cap (the default, #384): every statement that ran often enough.
        limit_clause = f"LIMIT {limit}" if limit is not None else ""
        statements_sql = f"""
            SELECT
                queryid, query, calls,
                {_SAMPLE_KEY} AS rate_sample_key,
                total_exec_time AS total_time_ms,
                mean_exec_time AS avg_time_ms,
                min_exec_time AS min_time_ms,
                max_exec_time AS max_time_ms,
                stddev_exec_time AS stddev_time_ms,
                rows,
                shared_blks_hit, shared_blks_read,
                blk_read_time AS io_read_time_ms,
                blk_write_time AS io_write_time_ms,
                temp_blks_read, temp_blks_written,
                -- #1893: PostgreSQL 17's per-entry start (NULL before 17, no such column).
                round(extract(epoch FROM now() - (to_jsonb(s) ->> 'stats_since')::timestamptz)
                    ::numeric, 3) AS stats_since_age_seconds,
                -- #1652: false inside a function or procedure (14+, track = all)
                (to_jsonb(s) ->> 'toplevel')::boolean AS toplevel
            FROM pg_stat_statements s
            WHERE calls >= {min_executions}
              AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
            ORDER BY total_exec_time DESC
            {limit_clause}
        """  # nosec B608 — both settings are checked ints (#384)
        snapshot_sql = self.rate_sample_snapshot_sql()
        raw, live = take_live_sample(
            lambda: self._query(snapshot_sql),
            rate_sample_seconds,
            lambda: self._query(statements_sql),
        )

        patterns = []
        reader = LiveQueryReader("postgresql", "postgres_tools")
        # #1893: read once, and only if a statement has no usable window of its own (or
        # to check a sample's interval against it, #1908).
        counters_window = functools.cache(self.stats_window)
        sample = live.sample(counters_window) if live is not None else None
        for r in raw:
            query_text = str(r.get("query") or "")
            calls = r.get("calls") or 1
            query_id = str(r.get("queryid") or _hash(query_text))
            shape = reader.fields(query_id, query_text)
            avg_ms = float(r.get("avg_time_ms") or 0)
            stddev_ms = float(r.get("stddev_time_ms") or 0)
            shared_hit = int(r.get("shared_blks_hit") or 0)
            shared_read = int(r.get("shared_blks_read") or 0)
            total_blks = shared_hit + shared_read

            p50, p95, p99 = _estimate_percentiles(avg_ms, stddev_ms)

            patterns.append(
                {
                    "query_id": query_id,
                    "query_text": query_text,
                    "query_type": statement_query_type(query_text, "postgresql"),
                    # #1652: PostgreSQL's toplevel read backwards; None when not reported.
                    "nested": _nested(r.get("toplevel")),
                    "execution_count": calls,
                    # #1893: PostgreSQL 17's time since the entry started counting,
                    # else the time since pg_stat_statements was reset, else 24 hours.
                    **statement_rate_fields(
                        calls,
                        None,
                        None,
                        counters_window,
                        observed_seconds=r.get("stats_since_age_seconds"),
                        # #1908: over the interval between two snapshots, when sampled.
                        **sample_arguments(live, sample, r.get("rate_sample_key")),
                    ),
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
                    "scan_efficiency_pct": (
                        min(
                            round((r.get("rows") or 0) / max(total_blks * 8192.0, 1) * 100, 2),
                            100.0,
                        )
                        if total_blks > 0
                        else None
                    ),
                    "tables_accessed": shape["tables_accessed"],
                    "filter_columns": _extract_filter_columns(query_text),
                    "sort_columns": _extract_sort_columns(query_text),
                    "has_joins": shape["has_joins"],
                    "join_count": shape["join_count"],
                    "has_aggregations": shape["has_aggregations"],
                    "has_subqueries": shape["has_subqueries"],
                    "has_text_search": shape["has_text_search"],
                    "text_search_type": shape["text_search_type"],
                    "parsed_facts": shape["parsed_facts"],
                    "query_tags": shape["query_tags"],
                    "query_tags_dropped": shape["query_tags_dropped"],
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
        reader.log_fallbacks()
        return patterns

    def stats_window(self) -> ServerWindow | None:
        """The window ``pg_stat_statements``' counters cover (#1893): the time since it was
        last reset (``pg_stat_statements_info.stats_reset``, PostgreSQL 14+). Never the
        uptime: with ``pg_stat_statements.save`` on (the default) the counters survive a
        clean restart. None before 14, or when it can't be read."""
        try:
            rows = self._query(
                "SELECT extract(epoch FROM now() - stats_reset) AS stats_reset_seconds "
                "FROM pg_stat_statements_info"
            )
        except Exception:  # noqa: BLE001 -- no reset time: the rates fall back to 24 hours
            return None
        return server_window(stats_reset_seconds=first_row_figure(rows, "stats_reset_seconds"))

    def server_uptime(self) -> object:
        """The server's uptime in seconds (#1991), ``pg_postmaster_start_time()``, a read.
        Never a rate's window (see ``stats_window``): the rate window check compares the
        window with it."""
        return first_row_figure(
            self._query(
                "SELECT extract(epoch FROM now() - pg_postmaster_start_time()) AS uptime_seconds"
            ),
            "uptime_seconds",
        )

    # -------------------------------------------------------------------
    # Global stats
    # -------------------------------------------------------------------

    def collect_global_stats(self) -> dict:
        rows = self._query("""
            SELECT
                numbackends AS active_connections,
                xact_commit + xact_rollback AS total_transactions,
                blks_hit, blks_read,
                tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted,
                temp_files, temp_bytes
            FROM pg_stat_database WHERE datname = current_database()
        """)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AND_COL_RE = re.compile(r"(?:AND|OR)\s+\"?(\w+)\"?\s*(?:=|<|>|!=|LIKE|IN|BETWEEN|IS)", re.I)
_ORDER_COL_RE = re.compile(r"ORDER\s+BY\s+(.*?)(?:LIMIT|OFFSET|$)", re.I)


def _extract_tables(sql: str) -> list[str]:
    """The live ``tables_accessed`` for ``sql``, read through the SQL facade (#795)."""
    return live_sql.extract_tables(sql, "postgresql")


def _extract_filter_columns(sql: str) -> list[str] | None:
    # Comments and literals are not SQL (#1420); a quoted name is one name, as written, and
    # an unquoted one is folded to lower case, as PostgreSQL folds it (#1737).
    text = live_sql.column_text(sql, "postgresql")
    cols = where_columns(text.text, '"') + _AND_COL_RE.findall(text.text)
    return list(dict.fromkeys(text.name(c, str.lower) for c in cols)) or None


def _extract_sort_columns(sql: str) -> list[str] | None:
    # Comments and literals are not SQL (#1420); a quoted name is one name, as written, and
    # an unquoted one is folded to lower case (#1737).
    text = live_sql.column_text(sql, "postgresql")
    m = _ORDER_COL_RE.search(text.text)
    if not m:
        return None
    words = [c.split() for c in m.group(1).split(",")]
    cols = [w[0] for w in words if w]
    # A literal (a placeholder once blanked) sorts by a constant: it names no column.
    return [
        text.name(c, str.lower) for c in cols if c != "?" and c.upper() not in ("ASC", "DESC")
    ] or None


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


def _has_time_range_filter(query_text: str) -> bool:
    query_text = live_sql.time_range_text(query_text, "postgresql")  # (#1420)
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
