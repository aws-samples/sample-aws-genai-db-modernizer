# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
SQL Server Collector Tools — SSM-based.

All queries execute on a remote automation instance via SSM Run Command.
Mirrors PostgreSQLRemoteCollector pattern but with SQL Server-specific queries
(sys.dm_exec_query_stats, sys.tables, sys.columns, sys.indexes, etc.).

Identifier naming convention: tables are referenced as ``"schema.table"`` since
SQL Server uses schemas (typically ``dbo``) and the contract supports it via
``Table.schema_name``.
"""

import functools
import hashlib
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
from src.tools.database.index_keys import is_included_column, settle_included_columns
from src.tools.database.live_sql import LiveQueryReader
from src.tools.database.routine_links import routine_id
from src.tools.sql.query_type import statement_query_type

logger = logging.getLogger(__name__)

# System schemas that should be excluded from collection. ``sys`` and
# ``INFORMATION_SCHEMA`` are always system; ``guest`` is rarely used for
# user content; the ``db_*`` schemas are SQL Server's built-in security roles.
_SYSTEM_SCHEMAS_FILTER = (
    "s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
    "AND s.name NOT LIKE 'db[_]%' ESCAPE '['"
)


# #1652: the offline script's section 10b, the counts the engine keeps per routine.
_ROUTINE_STATS_SQL = """
    SELECT OBJECT_NAME(x.object_id, x.database_id) AS routine_name,
           OBJECT_SCHEMA_NAME(x.object_id, x.database_id) AS schema_name,
           x.object_type,
           SUM(x.execution_count) AS execution_count,
           CAST(SUM(x.total_elapsed_time) / 1000.0 AS DECIMAL(18, 3)) AS total_time_ms
    FROM (
        SELECT object_id, database_id, 'PROCEDURE' AS object_type,
               execution_count, total_elapsed_time
        FROM sys.dm_exec_procedure_stats WHERE database_id = DB_ID()
        UNION ALL
        SELECT object_id, database_id, 'FUNCTION',
               execution_count, total_elapsed_time
        FROM sys.dm_exec_function_stats WHERE database_id = DB_ID()
        UNION ALL
        SELECT object_id, database_id, 'TRIGGER',
               execution_count, total_elapsed_time
        FROM sys.dm_exec_trigger_stats WHERE database_id = DB_ID()
    ) x
    WHERE OBJECT_NAME(x.object_id, x.database_id) IS NOT NULL
    GROUP BY x.object_id, x.database_id, x.object_type
"""

# The plan cache entries the statement query and both sampling snapshots read (#1908):
# this database's statements (or unresolved ones), without RDS's and SQL Server's own.
_STATEMENT_FILTER = """
                    WHERE (st.dbid = DB_ID() OR st.dbid IS NULL OR st.dbid = 0)
                      AND st.text NOT LIKE '%rdsadmin%'
                      AND st.text NOT LIKE '%rds_configuration%'
                      AND st.text NOT LIKE '%rds_database_tracking%'
                      AND st.text NOT LIKE '%rds_is_db_writable%'
                      AND st.text NOT LIKE '%rds_component_version%'
                      AND st.text NOT LIKE '%dm_os_sys_info%'
                      AND st.text NOT LIKE '%dm_exec_query_stats%'
                      AND st.text NOT LIKE '%dm_exec_sql_text%'
                      AND st.text NOT LIKE '%information_schema%'
                      AND st.text NOT LIKE '%@@VERSION%'
                      AND st.text NOT LIKE '%msdb.%'
                      AND st.text NOT LIKE '%msdb..%'
                      AND st.text NOT LIKE '%sys.server_role_members%'
                      AND st.text NOT LIKE '%sys.server_principals%'
                      AND st.text NOT LIKE '%sys.server_triggers%'
                      AND st.text NOT LIKE '%sys.configurations%'
                      AND st.text NOT LIKE '%sys.databases%'
                      AND st.text NOT LIKE '%sys.dm_%'
                      -- SQL Server's own Query Store / automatic tuning code (#1876).
                      -- st.text is the whole batch or module: every statement of one
                      -- that reads sys.plan_persist_* goes (the reach of '%sys.dm_%').
                      AND st.text NOT LIKE '%sys.plan[_]persist[_]%'
"""


class SQLServerRemoteCollector:
    """Collects SQL Server metadata via SSM Run Command."""

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
            engine="sqlserver",
            host=self.host,
            port=self.port,
            database=self.database,
            secret_arn=self.secret_arn,
            sql=sql,
            region=self.region,
        )

    def _query_raw(self, sql: str) -> str:
        return self.ssm.run_sql(
            engine="sqlserver",
            host=self.host,
            port=self.port,
            database=self.database,
            secret_arn=self.secret_arn,
            sql=sql,
            region=self.region,
        )

    @staticmethod
    def _split_qualified(table_name: str) -> tuple[str, str]:
        """Split ``"schema.table"`` into ``(schema, table)`` with ``dbo`` default."""
        if "." in table_name:
            schema, table = table_name.split(".", 1)
            return schema, table
        return "dbo", table_name

    # -------------------------------------------------------------------
    # Metadata
    # -------------------------------------------------------------------

    def get_version(self) -> str:
        rows = self._query("SELECT CAST(@@VERSION AS NVARCHAR(MAX)) AS version")
        return str(rows[0]["version"]) if rows else "unknown"

    def get_database_size_gb(self) -> float:
        rows = self._query("""
            SELECT CAST(SUM(size) * 8.0 / 1024 / 1024 AS FLOAT) AS size_gb
            FROM sys.master_files
            WHERE database_id = DB_ID()
        """)
        return float(rows[0]["size_gb"] or 0) if rows else 0

    # -------------------------------------------------------------------
    # Schema
    # -------------------------------------------------------------------

    def collect_tables(self) -> list[dict]:
        return self._query(f"""
            SELECT
                s.name AS schema_name,
                t.name AS table_name,
                r.row_total AS row_count,
                'estimate' AS row_count_basis,
                CAST(SUM(CASE WHEN p.index_id IN (0, 1) THEN a.total_pages ELSE 0 END)
                     * 8.0 / 1024 AS FLOAT) AS data_size_mb,
                CAST(SUM(CASE WHEN p.index_id > 1 THEN a.total_pages ELSE 0 END)
                     * 8.0 / 1024 AS FLOAT) AS index_size_mb,
                MAX(u.full_table_scans) AS full_table_scans,
                MAX(u.index_scans) AS index_scans
            FROM sys.tables t
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            INNER JOIN (
                SELECT object_id, SUM(rows) AS row_total
                FROM sys.partitions
                WHERE index_id IN (0, 1)
                GROUP BY object_id
            ) r ON r.object_id = t.object_id
            -- Every index's partitions (#1873): index_id 0 or 1 is the data, > 1 the
            -- nonclustered indexes.
            INNER JOIN sys.partitions p ON t.object_id = p.object_id
            INNER JOIN sys.allocation_units a ON p.partition_id = a.container_id
            -- One usage row per table, so no page is summed once per used index.
            LEFT JOIN (
                SELECT
                    object_id,
                    MAX(user_scans) AS full_table_scans,
                    MAX(user_seeks + user_lookups) AS index_scans
                FROM sys.dm_db_index_usage_stats
                WHERE database_id = DB_ID()
                GROUP BY object_id
            ) u ON u.object_id = t.object_id
            WHERE {_SYSTEM_SCHEMAS_FILTER}
            GROUP BY s.name, t.name, r.row_total
            ORDER BY s.name, t.name
        """)  # nosec B608 — _SYSTEM_SCHEMAS_FILTER is a module constant, not user input

    def collect_columns(self, table_name: str) -> list[dict]:
        schema, table = self._split_qualified(table_name)
        return self._query(f"""
            SELECT
                c.name AS column_name,
                c.column_id AS ordinal_position,
                ty.name AS data_type,
                c.max_length,
                c.precision AS numeric_precision,
                c.scale AS numeric_scale,
                c.is_nullable,
                CASE WHEN c.is_identity = 1 THEN 'YES' ELSE 'NO' END AS is_identity,
                dc.definition AS column_default
            FROM sys.columns c
            INNER JOIN sys.tables t ON c.object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            INNER JOIN sys.types ty ON c.user_type_id = ty.user_type_id
            LEFT JOIN sys.default_constraints dc ON dc.parent_object_id = t.object_id
                AND dc.parent_column_id = c.column_id
            WHERE s.name = '{schema}' AND t.name = '{table}'
            ORDER BY c.column_id
        """)  # nosec B608 — schema/table from sys catalog, not user input

    def collect_indexes(self, table_name: str) -> list[dict]:
        schema, table = self._split_qualified(table_name)
        raw = self._query(f"""
            SELECT
                i.name AS index_name,
                c.name AS column_name,
                ic.key_ordinal,
                i.is_unique,
                i.is_primary_key,
                i.type_desc AS index_type
            FROM sys.indexes i
            INNER JOIN sys.tables t ON i.object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            INNER JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
            INNER JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
            WHERE s.name = '{schema}' AND t.name = '{table}'
              AND i.name IS NOT NULL
              AND i.type > 0
            ORDER BY i.name, ic.key_ordinal
        """)  # nosec B608 — schema/table from sys catalog, not user input

        indexes: dict[str, dict] = {}
        for r in raw:
            name = r["index_name"]
            if name not in indexes:
                indexes[name] = {
                    "index_name": name,
                    "columns": [],
                    "is_unique": r.get("is_unique") in (True, 1, "1", "True", "true"),
                    "is_primary": r.get("is_primary_key") in (True, 1, "1", "True", "true"),
                    "index_type": _normalize_index_type(r.get("index_type")),
                }
            if is_included_column(r):
                # #2227: a key_ordinal 0 column is stored in the index, not part of its key
                # (an INCLUDE column, or the partitioning column SQL Server adds), as offline.
                indexes[name].setdefault("included_columns", []).append(r["column_name"])
            else:
                indexes[name]["columns"].append(r["column_name"])
        for index in indexes.values():
            settle_included_columns(index)
        return list(indexes.values())

    def collect_foreign_keys(self, table_name: str) -> list[dict]:
        schema, table = self._split_qualified(table_name)
        raw = self._query(f"""
            SELECT
                fk.name AS constraint_name,
                col.name AS column_name,
                rt.name AS referenced_table_name,
                rcol.name AS referenced_column_name,
                fk.update_referential_action_desc AS on_update,
                fk.delete_referential_action_desc AS on_delete,
                fkc.constraint_column_id
            FROM sys.foreign_keys fk
            INNER JOIN sys.tables t ON fk.parent_object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            INNER JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
            INNER JOIN sys.columns col
                ON col.object_id = fkc.parent_object_id AND col.column_id = fkc.parent_column_id
            INNER JOIN sys.tables rt ON fk.referenced_object_id = rt.object_id
            INNER JOIN sys.columns rcol
                ON rcol.object_id = fkc.referenced_object_id
                AND rcol.column_id = fkc.referenced_column_id
            WHERE s.name = '{schema}' AND t.name = '{table}'
            ORDER BY fk.name, fkc.constraint_column_id
        """)  # nosec B608 — schema/table from sys catalog, not user input

        fks: dict[str, dict] = {}
        for r in raw:
            name = r["constraint_name"]
            if name not in fks:
                fks[name] = {
                    "constraint_name": name,
                    "columns": [],
                    "referenced_table": r["referenced_table_name"],
                    "referenced_columns": [],
                    "on_delete": _normalize_fk_action(r.get("on_delete")),
                    "on_update": _normalize_fk_action(r.get("on_update")),
                }
            fks[name]["columns"].append(r["column_name"])
            fks[name]["referenced_columns"].append(r["referenced_column_name"])
        return list(fks.values())

    def collect_primary_key(self, table_name: str) -> list[str]:
        schema, table = self._split_qualified(table_name)
        rows = self._query(f"""
            SELECT c.name AS column_name
            FROM sys.indexes i
            INNER JOIN sys.tables t ON i.object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            INNER JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
            INNER JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
            WHERE i.is_primary_key = 1
              AND s.name = '{schema}' AND t.name = '{table}'
            ORDER BY ic.key_ordinal
        """)  # nosec B608 — schema/table from sys catalog, not user input
        return [r["column_name"] for r in rows]

    # -------------------------------------------------------------------
    # Views, procedures, triggers
    # -------------------------------------------------------------------

    def collect_views(self) -> list[dict]:
        return self._query(f"""
            SELECT
                v.name AS view_name,
                s.name AS schema_name,
                m.definition,
                p.name AS owner
            FROM sys.views v
            INNER JOIN sys.schemas s ON v.schema_id = s.schema_id
            LEFT JOIN sys.sql_modules m ON v.object_id = m.object_id
            LEFT JOIN sys.database_principals p ON v.principal_id = p.principal_id
            WHERE {_SYSTEM_SCHEMAS_FILTER}
            ORDER BY s.name, v.name
        """)  # nosec B608 — _SYSTEM_SCHEMAS_FILTER is a module constant, not user input

    def collect_procedures(self, include_bodies: bool = True) -> list[dict]:
        """Procedures and functions, with each body unless ``include_bodies`` is False
        (#1652: then by name only, ``body_status`` opted_out)."""
        flag = 1 if include_bodies else 0
        return self._query(f"""
            SELECT
                o.name AS routine_name,
                s.name AS schema_name,
                CASE o.type
                    WHEN 'P' THEN 'PROCEDURE'
                    WHEN 'FN' THEN 'FUNCTION'
                    WHEN 'IF' THEN 'INLINE_TABLE_FUNCTION'
                    WHEN 'TF' THEN 'TABLE_FUNCTION'
                    ELSE 'OTHER'
                END AS routine_type,
                'TSQL' AS language,
                -- #1652: NULL for an encrypted module or one the user may not see
                CASE WHEN {flag} = 1 THEN OBJECT_DEFINITION(o.object_id) END AS definition,
                -- #1933: its size and whether it is encrypted, as the script reads them
                DATALENGTH(OBJECT_DEFINITION(o.object_id)) / 2 AS body_chars,
                OBJECTPROPERTY(o.object_id, 'IsEncrypted') AS is_encrypted,
                CASE WHEN {flag} = 0 THEN 'opted_out' END AS body_status
            FROM sys.objects o
            INNER JOIN sys.schemas s ON o.schema_id = s.schema_id
            WHERE o.type IN ('P', 'FN', 'IF', 'TF')
              AND {_SYSTEM_SCHEMAS_FILTER}
            ORDER BY routine_type, s.name, o.name
        """)  # nosec B608 — _SYSTEM_SCHEMAS_FILTER is a module constant, not user input

    def collect_routine_capture(self, include_bodies: bool = True) -> dict:
        """How routine code was read (#1933), as the script's metadata.routine_capture:
        whether bodies were kept, and VIEW_DEFINITION when the user lacks VIEW
        DEFINITION (sys.objects then hides the modules it has no permission on)."""
        rows = self._query(
            "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION') AS granted"
        )
        granted = bool(rows) and str(rows[0].get("granted")) == "1"
        return {
            "include_bodies": include_bodies,
            "privileges_not_seen": [] if granted else ["VIEW_DEFINITION"],
        }

    def collect_triggers(self, include_bodies: bool = True) -> list[dict]:
        """DML triggers with every event, state and body unless ``include_bodies`` is
        False (#1652)."""
        flag = 1 if include_bodies else 0
        return self._query(f"""
            SELECT
                tr.name AS trigger_name,
                t.name AS table_name,
                s.name AS schema_name,
                CASE WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsInsteadOfTrigger') = 1
                     THEN 'INSTEAD OF'
                     ELSE 'AFTER' END AS timing,
                CASE
                    WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsInsertTrigger') = 1 THEN 'INSERT'
                    WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsUpdateTrigger') = 1 THEN 'UPDATE'
                    WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsDeleteTrigger') = 1 THEN 'DELETE'
                    ELSE 'OTHER'
                END AS event_type,
                -- #1651: every event the trigger fires on, e.g. 'INSERT,UPDATE'
                STUFF((SELECT ',' + te.type_desc FROM sys.trigger_events te
                       WHERE te.object_id = tr.object_id ORDER BY te.type
                       FOR XML PATH('')), 1, 1, '') AS events,
                -- #1652: once per statement; its state and its body
                'STATEMENT' AS for_each,
                CASE WHEN tr.is_disabled = 1 THEN 0 ELSE 1 END AS is_enabled,
                CASE WHEN {flag} = 1 THEN OBJECT_DEFINITION(tr.object_id) END AS definition,
                -- #1933: its size and whether it is encrypted, as the script reads them
                DATALENGTH(OBJECT_DEFINITION(tr.object_id)) / 2 AS body_chars,
                OBJECTPROPERTY(tr.object_id, 'IsEncrypted') AS is_encrypted,
                CASE WHEN {flag} = 0 THEN 'opted_out' END AS body_status
            FROM sys.triggers tr
            -- sys.objects, not sys.tables: an INSTEAD OF trigger can sit on a view (#1651)
            INNER JOIN sys.objects t ON tr.parent_id = t.object_id AND t.type IN ('U', 'V')
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE tr.is_ms_shipped = 0
              AND {_SYSTEM_SCHEMAS_FILTER}
            ORDER BY s.name, t.name, tr.name
        """)  # nosec B608 — _SYSTEM_SCHEMAS_FILTER is a module constant, not user input

    # -------------------------------------------------------------------
    # Query patterns (DMV-based)
    # -------------------------------------------------------------------

    def rate_sample_snapshot_sql(self) -> str:
        """One snapshot of the plan cache's counters (#1908): first a row with the server's
        clock (``SYSUTCDATETIME()``) and how many entries the snapshot holds, then each
        cached statement's ``execution_count`` and age since ``creation_time`` under its
        ``query_hash``. An entry is its plan, statement
        offsets and ``creation_time``, so a plan evicted and cached again in between is a
        new entry, never a lower count."""
        return f"""
            WITH e AS (
                SELECT CONVERT(VARCHAR(40), qs.query_hash, 1) AS group_key,
                       CONVERT(VARCHAR(130), qs.plan_handle, 1) + '/'
                           + CAST(qs.statement_start_offset AS VARCHAR(12)) + '/'
                           + CAST(qs.statement_end_offset AS VARCHAR(12)) + '/'
                           + CONVERT(VARCHAR(30), qs.creation_time, 126) AS entry_key,
                       qs.execution_count,
                       DATEDIFF(SECOND, qs.creation_time, GETDATE()) AS entry_age_seconds
                FROM sys.dm_exec_query_stats qs
                CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
{_STATEMENT_FILTER}
            )
            SELECT 0 AS row_kind, CAST(NULL AS VARCHAR(40)) AS group_key,
                   CAST(NULL AS VARCHAR(200)) AS entry_key,
                   CAST(NULL AS BIGINT) AS execution_count,
                   CAST(NULL AS INT) AS entry_age_seconds,
                   CONVERT(VARCHAR(30), SYSUTCDATETIME(), 126) AS sampled_at,
                   (SELECT COUNT(*) FROM e) AS entry_count
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
        """Collect query stats from ``sys.dm_exec_query_stats``.

        With ``rate_sample_seconds`` above 0 (#1908, the collector passes
        ``CollectionOptions.rate_sample_seconds``), each statement's rate is the change in
        its count between two snapshots of the plan cache that far apart
        (:meth:`rate_sample_snapshot_sql`).

        Per-execution averages derive from totals divided by ``execution_count``.
        Times are converted from microseconds to milliseconds.
        Note: ``sys.dm_exec_query_stats`` reflects the plan cache only — long
        idle queries that aged out are not visible.

        ``sys.dm_exec_query_stats`` keeps one row per cached statement, so a
        non-parameterized query gets a new row per literal value, all sharing
        the same ``query_hash``. We group by ``qs.query_hash`` below so one
        row == one query shape (matching the queryid/DIGEST grouping already
        done for PostgreSQL and MySQL): the counters are summed across
        variants, timestamps take MIN/MAX across variants, and the query text
        comes from the variant with the highest ``total_elapsed_time``. The
        ``limit``/``min_executions`` thresholds apply after grouping, so they
        cap query shapes rather than literal-variant rows.

        Filtering note: ``sys.dm_exec_sql_text`` returns NULL for ``dbid`` on
        ad-hoc queries (only stored-procedure-bound statements get a real
        dbid). Filtering by ``dbid = DB_ID()`` would drop all user ad-hoc
        workload, so we don't filter on dbid here. RDS-internal noise
        (rds_configuration, rds_is_db_writable, etc.) is filtered via a
        ``NOT LIKE`` allow-list on the query text.
        """
        # sqlcmd's output is one row per line, tab-separated, so the query text is
        # sent with its backslashes, CR, LF and tabs written as \\, \r, \n and \t,
        # and live_sql.decode_query_text restores them: a line break that ends a -- comment
        # stays one (#1874).
        limit, min_executions = capture_settings(limit, min_executions)
        rate_sample_seconds = rate_sample_setting(rate_sample_seconds)
        # The routine each statement shape ran in (#1891), read just before the shapes
        # from the same plan cache.
        routines = self.collect_query_routines()
        # No cap (the default, #384): every statement that ran often enough.
        top_clause = f"TOP {limit}" if limit is not None else ""
        statements_sql = f"""
            SELECT {top_clause}
                CONVERT(VARCHAR(40), g.query_hash, 1) AS query_id,
                g.query_text,
                g.execution_count,
                g.total_logical_reads,
                g.total_physical_reads,
                g.total_worker_time,
                g.total_elapsed_time,
                g.total_rows,
                g.min_elapsed_time,
                g.max_elapsed_time,
                CONVERT(VARCHAR(30), g.creation_time, 121) AS creation_time,
                CONVERT(VARCHAR(30), g.last_execution_time, 121) AS last_execution_time
            FROM (
                SELECT
                    v.query_hash,
                    SUM(v.execution_count) AS execution_count,
                    SUM(v.total_logical_reads) AS total_logical_reads,
                    SUM(v.total_physical_reads) AS total_physical_reads,
                    SUM(v.total_worker_time) AS total_worker_time,
                    SUM(v.total_elapsed_time) AS total_elapsed_time,
                    SUM(v.total_rows) AS total_rows,
                    MIN(v.min_elapsed_time) AS min_elapsed_time,
                    MAX(v.max_elapsed_time) AS max_elapsed_time,
                    MIN(v.creation_time) AS creation_time,
                    MAX(v.last_execution_time) AS last_execution_time,
                    MAX(CASE WHEN v.variant_rank = 1 THEN v.query_text END) AS query_text
                FROM (
                    SELECT
                        qs.query_hash,
                        qs.execution_count,
                        qs.total_logical_reads,
                        qs.total_physical_reads,
                        qs.total_worker_time,
                        qs.total_elapsed_time,
                        qs.total_rows,
                        qs.min_elapsed_time,
                        qs.max_elapsed_time,
                        qs.creation_time,
                        qs.last_execution_time,
                        REPLACE(REPLACE(REPLACE(REPLACE(
                            SUBSTRING(st.text,
                                (qs.statement_start_offset/2)+1,
                                ((CASE qs.statement_end_offset
                                    WHEN -1 THEN DATALENGTH(st.text)
                                    ELSE qs.statement_end_offset
                                    END - qs.statement_start_offset)/2) + 1),
                            '\\', '\\\\'), CHAR(13), '\\r'), CHAR(10), '\\n'), CHAR(9), '\\t')
                            AS query_text,
                        ROW_NUMBER() OVER (
                            PARTITION BY qs.query_hash
                            ORDER BY qs.total_elapsed_time DESC, qs.plan_handle, qs.statement_start_offset
                        ) AS variant_rank
                    FROM sys.dm_exec_query_stats qs
                    CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
{_STATEMENT_FILTER}
                ) v
                GROUP BY v.query_hash
            ) g
            WHERE g.execution_count >= {min_executions}
            ORDER BY g.total_elapsed_time DESC
        """  # nosec B608 — both settings are checked ints (#384)
        snapshot_sql = self.rate_sample_snapshot_sql()
        raw, live = take_live_sample(
            lambda: self._query(snapshot_sql),
            rate_sample_seconds,
            lambda: self._query(statements_sql),
        )

        patterns = []
        reader = LiveQueryReader("sqlserver", "sqlserver_tools", empty=())
        # #1893: read once, and only if a statement has no usable window of its own (or
        # to check a sample's interval against it, #1908).
        counters_window = functools.cache(self.stats_window)
        sample = live.sample(counters_window) if live is not None else None
        for r in raw:
            query_text = live_sql.decode_query_text(str(r.get("query_text") or "")).strip()
            calls = int(r.get("execution_count") or 1)
            # The text-hash fallback hashes the text with its line breaks and tabs as
            # spaces, the form hashed before #1874. A statement of 256 characters or
            # fewer keeps its id; a longer one used to be cut at sqlcmd's old default
            # width before it was hashed, and now hashes whole (#1884). The id only
            # comes from the text when the row has no query_hash.
            query_id = str(r.get("query_id") or _hash(live_sql.flattened(query_text)))
            shape = reader.fields(query_id, query_text)

            total_elapsed_us = float(r.get("total_elapsed_time") or 0)
            total_worker_us = float(r.get("total_worker_time") or 0)
            total_rows = int(r.get("total_rows") or 0)
            total_logical = int(r.get("total_logical_reads") or 0)
            total_physical = int(r.get("total_physical_reads") or 0)

            avg_ms = total_elapsed_us / 1000.0 / max(calls, 1)
            min_ms = float(r.get("min_elapsed_time") or 0) / 1000.0
            max_ms = float(r.get("max_elapsed_time") or 0) / 1000.0
            avg_cpu_ms = total_worker_us / 1000.0 / max(calls, 1)

            # SQL Server DMVs do not expose per-query percentiles. Estimate
            # from min/max as a coarse proxy; downstream consumers know to
            # treat these as approximate when source == dmv_query_stats.
            p50, p95, p99 = _estimate_percentiles_from_min_max(avg_ms, min_ms, max_ms)

            avg_logical = total_logical / max(calls, 1)
            avg_physical = total_physical / max(calls, 1)
            avg_rows = total_rows / max(calls, 1)
            cache_hit_ratio = (
                (total_logical - total_physical) / total_logical * 100
                if total_logical > 0
                else None
            )

            patterns.append(
                {
                    "query_id": query_id,
                    "query_text": query_text,
                    "query_type": statement_query_type(query_text, "sqlserver"),
                    "execution_count": calls,
                    # #1893: over the plan's creation_time to last_execution_time, else
                    # the server's uptime.
                    **statement_rate_fields(
                        calls,
                        r.get("creation_time"),
                        r.get("last_execution_time"),
                        counters_window,
                        # #1908: over the interval between two snapshots, when sampled.
                        **sample_arguments(live, sample, r.get("query_id")),
                    ),
                    "execution_time_ms_avg": round(avg_ms, 3),
                    "execution_time_ms_min": round(min_ms, 3),
                    "execution_time_ms_max": round(max_ms, 3),
                    "execution_time_ms_p50": round(p50, 3),
                    "execution_time_ms_p95": round(p95, 3),
                    "execution_time_ms_p99": round(p99, 3),
                    "total_time_ms": round(total_elapsed_us / 1000.0, 3),
                    "rows_returned_avg": round(avg_rows, 2),
                    "rows_returned_p95": _estimate_rows_p95(avg_rows, avg_ms, p95),
                    "rows_affected_avg": 0,
                    "rows_examined_avg": round(avg_logical, 2),
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
                    # SQL Server-specific (semantically reusable for Oracle later)
                    "avg_logical_reads": round(avg_logical, 2),
                    "avg_physical_reads": round(avg_physical, 2),
                    "avg_cpu_time_ms": round(avg_cpu_ms, 3),
                    "cache_hit_ratio_pct": (
                        round(cache_hit_ratio, 2) if cache_hit_ratio is not None else None
                    ),
                    "first_seen": str(r.get("creation_time") or "") or None,
                    "last_seen": str(r.get("last_execution_time") or "") or None,
                    # None when the links could not be read (#1891).
                    "parent_routine_ids": (
                        None if routines is None else routines.get(str(r.get("query_id")), [])
                    ),
                }
            )
        reader.log_fallbacks()
        return patterns

    def stats_window(self) -> ServerWindow | None:
        """The window the plan cache's counters can cover (#1893): the server's uptime
        (``sys.dm_os_sys_info.sqlserver_start_time``; the cache empties on a restart).
        None when it can't be read."""
        try:
            rows = self._query(
                "SELECT DATEDIFF(SECOND, sqlserver_start_time, GETDATE()) AS uptime_seconds "
                "FROM sys.dm_os_sys_info"
            )
        except Exception:  # noqa: BLE001 -- no uptime: the rates fall back to 24 hours
            return None
        return server_window(uptime_seconds=first_row_figure(rows, "uptime_seconds"))

    def collect_routine_stats(self) -> tuple[list[dict], dict]:
        """The execution counts the engine keeps for each cached procedure, function and
        trigger (#1652), from in-memory DMVs, and how they were read: ``([],
        not_tracked)`` without VIEW SERVER STATE. The offline script's section 10b. No
        setting is changed."""
        perm = self._query("SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE') AS ok")
        if not perm or int(perm[0].get("ok") or 0) != 1:
            return [], {"status": "not_tracked", "reason": "VIEW SERVER STATE is not granted"}
        return self._query(_ROUTINE_STATS_SQL), {"status": "tracked"}

    def collect_query_routines(self) -> dict[str, list[str]] | None:
        """``{query_hash: [routine_id, ...]}``: the procedures, functions and triggers each
        cached statement shape ran in (#1891), as ``<schema>.<name>``, sorted.

        ``sys.dm_exec_sql_text(sql_handle).objectid`` names the module a cached statement
        belongs to; an ad hoc or prepared statement has none, so its shape is not listed.
        Only this database's modules count. The same read as the offline script's
        ``query_routines`` section (``scripts/collect-sqlserver.sql``). None when the read
        fails: the collection then records no links rather than an empty one.
        """
        try:
            rows = self._query("""
                SELECT DISTINCT
                    CONVERT(VARCHAR(40), qs.query_hash, 1) AS query_id,
                    OBJECT_SCHEMA_NAME(st.objectid, st.dbid) AS routine_schema,
                    OBJECT_NAME(st.objectid, st.dbid) AS routine_name
                FROM sys.dm_exec_query_stats qs
                CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
                WHERE st.objectid IS NOT NULL
                  AND st.dbid = DB_ID()
                  AND OBJECT_NAME(st.objectid, st.dbid) IS NOT NULL
            """)
        except Exception as exc:  # noqa: BLE001 - the links are optional, the shapes are not
            logger.warning("sqlserver_tools: statement routines not read: %s", exc)
            return None
        out: dict[str, set[str]] = {}
        for r in rows or []:
            rid = routine_id(r.get("routine_schema"), r.get("routine_name"))
            if r.get("query_id") and rid:
                out.setdefault(str(r["query_id"]), set()).add(rid)
        return {qid: sorted(ids) for qid, ids in out.items()}

    # -------------------------------------------------------------------
    # Global stats
    # -------------------------------------------------------------------

    def collect_global_stats(self) -> dict:
        rows = self._query("""
            SELECT
                (SELECT COUNT(*) FROM sys.dm_exec_connections) AS active_connections,
                (SELECT cntr_value FROM sys.dm_os_performance_counters
                 WHERE counter_name = 'Buffer cache hit ratio'
                   AND object_name LIKE '%Buffer Manager%') AS buffer_hit_raw,
                (SELECT cntr_value FROM sys.dm_os_performance_counters
                 WHERE counter_name = 'Buffer cache hit ratio base'
                   AND object_name LIKE '%Buffer Manager%') AS buffer_hit_base,
                (SELECT cntr_value FROM sys.dm_os_performance_counters
                 WHERE counter_name = 'Transactions/sec'
                   AND instance_name = '_Total') AS total_transactions
        """)
        if not rows:
            return {}
        r = rows[0]
        hit = float(r.get("buffer_hit_raw") or 0)
        base = float(r.get("buffer_hit_base") or 0)
        ratio = (hit / base * 100) if base > 0 else 0.0
        return {
            "cache_hit_ratio_pct": round(ratio, 2),
            "active_connections": int(r.get("active_connections") or 0),
            "total_transactions": int(r.get("total_transactions") or 0),
        }

    def collect_os_stats(self) -> dict:
        """Host CPU and memory from ``sys.dm_os_sys_info`` (#1224).

        Needs VIEW SERVER STATE (VIEW SERVER PERFORMANCE STATE on SQL Server 2022+).
        ``physical_memory_kb`` and ``committed_kb`` exist from SQL Server 2012; an
        older server fails the query and the figures stay empty. Reads no table rows.
        """
        rows = self._query("""
            SELECT cpu_count, physical_memory_kb, committed_kb
            FROM sys.dm_os_sys_info
        """)
        if not rows:
            return {}
        r = rows[0]
        return {
            "cpu_count": _int_or_none(r.get("cpu_count")),
            "physical_memory_gb": _kb_to_gb(r.get("physical_memory_kb")),
            "committed_memory_gb": _kb_to_gb(r.get("committed_kb")),
        }


# -----------------------------------------------------------------------
# Module-level helpers (mirrors the duplicated pattern in mysql_tools and
# postgres_tools — to be extracted to a shared module in a follow-up PR).
# -----------------------------------------------------------------------


def _int_or_none(value: object) -> int | None:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _kb_to_gb(value: object) -> float | None:
    try:
        return round(float(value) / 1024 / 1024, 2)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _extract_tables(sql: str) -> list[str]:
    """The live ``tables_accessed`` for ``sql``, read through the SQL facade (#795).

    Every table the statement reads or writes (``FROM``/``JOIN``, ``INTO``,
    ``UPDATE``, ``MERGE`` target and source), schema-qualified as written, in
    first-seen order; references into ``sys``/``INFORMATION_SCHEMA`` are
    skipped, and no table is ``[]``.
    """
    return live_sql.extract_tables(sql, "sqlserver", empty=())


def _extract_filter_columns(sql: str) -> list[str] | None:
    """Best-effort extraction of column names appearing in WHERE clauses."""
    # Comments and literals are not SQL (#1420); a quoted name is one name (#1737).
    text = live_sql.column_text(sql, "sqlserver")
    where_match = re.search(
        r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|$)",
        text.text,
        re.IGNORECASE | re.DOTALL,
    )
    if not where_match:
        return None
    where_clause = where_match.group(1)
    # A column is a whole word (``JOIN`` is no ``JO`` before ``IN``), and so is an operator.
    cols = re.findall(r"\b(\w+)\b\s*(?:=|<|>|<=|>=|<>|!=|LIKE\b|IN\b)", where_clause, re.IGNORECASE)
    return sorted({text.name(c) for c in cols}) if cols else None


def _extract_sort_columns(sql: str) -> list[str] | None:
    # Comments and literals are not SQL (#1420); a quoted name is one name (#1737).
    text = live_sql.column_text(sql, "sqlserver")
    order_match = re.search(
        r"\bORDER\s+BY\b(.*?)(?:\bLIMIT\b|\bOFFSET\b|$)",
        text.text,
        re.IGNORECASE | re.DOTALL,
    )
    if not order_match:
        return None
    # A qualified name is one sort column (``[t].[x]`` reads ``t.x``, as ``t.x`` does and as
    # the other collectors read it).
    cols = re.findall(r"\w+(?:\.\w+)*", order_match.group(1))
    return (
        sorted(
            {text.name(c) for c in cols if text.is_quoted(c) or c.upper() not in ("ASC", "DESC")}
        )
        if cols
        else None
    )


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _normalize_index_type(raw: object) -> str:
    """Map SQL Server index type names to the contract's IndexType enum.

    SQL Server's sys.indexes.type_desc returns values like ``CLUSTERED``,
    ``NONCLUSTERED``, ``XML``, ``SPATIAL``, ``CLUSTERED COLUMNSTORE``, etc.
    The contract enum is engine-neutral (``IndexType`` in src.contracts.collector_output).
    Most SQL Server B-tree indexes (clustered + nonclustered + non-unique +
    unique) map cleanly to ``btree``. Specialized types map to spatial,
    fulltext, or fall through to ``other``.
    """
    s = str(raw or "").upper()
    if "SPATIAL" in s:
        return "spatial"
    if "FULLTEXT" in s or "FULL-TEXT" in s:
        return "fulltext"
    # Hash indexes (memory-optimized in-memory OLTP) are rare in OLTP workloads
    if "HASH" in s:
        return "hash"
    # CLUSTERED COLUMNSTORE and NONCLUSTERED COLUMNSTORE (sys.indexes.type 5 and 6, #2227)
    # store columns, not a key: never a B-tree
    if "COLUMNSTORE" in s:
        return "columnstore"
    # CLUSTERED, NONCLUSTERED, UNIQUE — all B-tree internally
    if "CLUSTERED" in s or "NONCLUSTERED" in s:
        return "btree"
    return "other"


def _normalize_fk_action(raw: object) -> str:
    """Map SQL Server foreign-key action descriptors to the contract enum.

    SQL Server returns ``update_referential_action_desc``/``delete_referential_action_desc``
    as ``NO_ACTION``, ``CASCADE``, ``SET_NULL``, ``SET_DEFAULT`` (underscored).
    The contract's ``ForeignKeyAction`` enum uses spaces: ``NO ACTION``,
    ``SET NULL``, ``SET DEFAULT``. Convert underscores to spaces; pass through
    cleanly-formed values; default to ``NO ACTION``.
    """
    s = str(raw or "NO_ACTION").upper().replace("_", " ")
    if s in ("CASCADE", "SET NULL", "NO ACTION", "RESTRICT", "SET DEFAULT"):
        return s
    return "NO ACTION"


def _estimate_percentiles_from_min_max(
    avg: float, min_v: float, max_v: float
) -> tuple[float, float, float]:
    """Coarse percentile estimate when only avg/min/max are known.

    SQL Server DMVs do not expose per-query latency percentiles. We approximate:
      - p50 ≈ avg
      - p95 ≈ midpoint between avg and max
      - p99 ≈ closer to max
    These are intentionally approximate; consumers should check
    ``query_log_source == "dmv_query_stats"`` to know percentiles are estimated.
    """
    p50 = avg
    p95 = avg + (max_v - avg) * 0.7 if max_v > avg else avg
    p99 = avg + (max_v - avg) * 0.95 if max_v > avg else avg
    return p50, p95, p99


def _estimate_rows_p95(avg_rows: float, avg_ms: float, p95_ms: float) -> float | None:
    """Estimate p95 rows using the same latency-ratio proxy as MySQL/PG collectors."""
    if avg_ms <= 0:
        return None
    return round(avg_rows * (p95_ms / avg_ms), 2)


def _has_time_range_filter(query_text: str) -> bool:
    query_text = live_sql.time_range_text(query_text, "sqlserver")  # (#1420)
    lower = query_text.lower()
    if re.search(r"\b(getdate|sysdatetime|current_timestamp)\b", lower):
        return True
    if re.search(r"\bbetween\b.*\b(date|datetime|datetime2|date_)", lower):
        return True
    if re.search(r"(date|datetime|datetime2|created|updated|_at|_date)\s*[<>]=?", lower):
        return True
    # A date or timestamp literal after BETWEEN or a comparison (#1420).
    if re.search(r"(?:\bbetween\s+|[<>]=?\s*)n?'\d{4}-\d{2}-\d{2}", lower):
        return True
    return False
