"""
SQL Server Collector Tools — SSM-based.

All queries execute on a remote automation instance via SSM Run Command.
Mirrors PostgreSQLRemoteCollector pattern but with SQL Server-specific queries
(sys.dm_exec_query_stats, sys.tables, sys.columns, sys.indexes, etc.).

Identifier naming convention: tables are referenced as ``"schema.table"`` since
SQL Server uses schemas (typically ``dbo``) and the contract supports it via
``Table.schema_name``.
"""

import hashlib
import logging
import re

from src.tools.aws.ssm_executor import SSMExecutor

logger = logging.getLogger(__name__)

# System schemas that should be excluded from collection. ``sys`` and
# ``INFORMATION_SCHEMA`` are always system; ``guest`` is rarely used for
# user content; the ``db_*`` schemas are SQL Server's built-in security roles.
_SYSTEM_SCHEMAS_FILTER = (
    "s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
    "AND s.name NOT LIKE 'db[_]%' ESCAPE '['"
)


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
                p.rows AS row_count,
                CAST(SUM(a.total_pages) * 8.0 / 1024 AS FLOAT) AS data_size_mb,
                CAST(SUM(CASE WHEN i.index_id > 1 THEN a.total_pages ELSE 0 END) * 8.0 / 1024 AS FLOAT) AS index_size_mb,
                MAX(ius.user_scans) AS full_table_scans,
                MAX(ius.user_seeks + ius.user_lookups) AS index_scans
            FROM sys.tables t
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            INNER JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1)
            INNER JOIN sys.allocation_units a ON p.partition_id = a.container_id
            LEFT JOIN sys.indexes i ON t.object_id = i.object_id AND p.index_id = i.index_id
            LEFT JOIN sys.dm_db_index_usage_stats ius
                ON ius.object_id = t.object_id AND ius.database_id = DB_ID()
            WHERE {_SYSTEM_SCHEMAS_FILTER}
            GROUP BY s.name, t.name, p.rows
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
            indexes[name]["columns"].append(r["column_name"])
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

    def collect_procedures(self) -> list[dict]:
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
                'TSQL' AS language
            FROM sys.objects o
            INNER JOIN sys.schemas s ON o.schema_id = s.schema_id
            WHERE o.type IN ('P', 'FN', 'IF', 'TF')
              AND {_SYSTEM_SCHEMAS_FILTER}
            ORDER BY routine_type, s.name, o.name
        """)  # nosec B608 — _SYSTEM_SCHEMAS_FILTER is a module constant, not user input

    def collect_triggers(self) -> list[dict]:
        return self._query(f"""
            SELECT
                tr.name AS trigger_name,
                t.name AS table_name,
                s.name AS schema_name,
                CASE WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsInsteadOfTrigger') = 1
                     THEN 'INSTEAD_OF'
                     ELSE 'AFTER' END AS timing,
                CASE
                    WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsInsertTrigger') = 1 THEN 'INSERT'
                    WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsUpdateTrigger') = 1 THEN 'UPDATE'
                    WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsDeleteTrigger') = 1 THEN 'DELETE'
                    ELSE 'OTHER'
                END AS event_type
            FROM sys.triggers tr
            INNER JOIN sys.tables t ON tr.parent_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE tr.is_ms_shipped = 0
              AND {_SYSTEM_SCHEMAS_FILTER}
            ORDER BY s.name, t.name, tr.name
        """)  # nosec B608 — _SYSTEM_SCHEMAS_FILTER is a module constant, not user input

    # -------------------------------------------------------------------
    # Query patterns (DMV-based)
    # -------------------------------------------------------------------

    def collect_query_patterns(self, min_executions: int = 10, limit: int = 1000) -> list[dict]:
        """Collect query stats from ``sys.dm_exec_query_stats``.

        Per-execution averages derive from totals divided by ``execution_count``.
        Times are converted from microseconds to milliseconds.
        Note: ``sys.dm_exec_query_stats`` reflects the plan cache only — long
        idle queries that aged out are not visible.

        Filtering note: ``sys.dm_exec_sql_text`` returns NULL for ``dbid`` on
        ad-hoc queries (only stored-procedure-bound statements get a real
        dbid). Filtering by ``dbid = DB_ID()`` would drop all user ad-hoc
        workload, so we don't filter on dbid here. RDS-internal noise
        (rds_configuration, rds_is_db_writable, etc.) is filtered via a
        ``NOT LIKE`` allow-list on the query text.
        """
        # CONVERT(NVARCHAR(MAX), ...) keeps the query text on a single line for
        # tab-separated parsing; CHAR(13)/CHAR(10) are stripped explicitly to
        # avoid embedded line breaks.
        raw = self._query(f"""
            SELECT TOP {limit}
                CONVERT(VARCHAR(40), qs.query_hash, 1) AS query_id,
                REPLACE(REPLACE(
                    SUBSTRING(st.text,
                        (qs.statement_start_offset/2)+1,
                        ((CASE qs.statement_end_offset
                            WHEN -1 THEN DATALENGTH(st.text)
                            ELSE qs.statement_end_offset
                            END - qs.statement_start_offset)/2) + 1),
                    CHAR(13), ' '), CHAR(10), ' ') AS query_text,
                qs.execution_count,
                qs.total_logical_reads,
                qs.total_physical_reads,
                qs.total_worker_time,
                qs.total_elapsed_time,
                qs.total_rows,
                qs.min_elapsed_time,
                qs.max_elapsed_time,
                CONVERT(VARCHAR(30), qs.creation_time, 121) AS creation_time,
                CONVERT(VARCHAR(30), qs.last_execution_time, 121) AS last_execution_time
            FROM sys.dm_exec_query_stats qs
            CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
            WHERE qs.execution_count >= {min_executions}
              AND (st.dbid = DB_ID() OR st.dbid IS NULL OR st.dbid = 0)
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
            ORDER BY qs.total_elapsed_time DESC
        """)  # nosec B608 — min_executions and limit are internal constants

        patterns = []
        for r in raw:
            query_text = str(r.get("query_text") or "").strip()
            calls = int(r.get("execution_count") or 1)

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
                    "query_id": str(r.get("query_id") or _hash(query_text)),
                    "query_text": query_text,
                    "query_type": _extract_query_type(query_text),
                    "execution_count": calls,
                    "frequency_per_hour": calls / 24,
                    "calls_per_second": calls / (24 * 3600),
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
                    # SQL Server-specific (semantically reusable for Oracle later)
                    "avg_logical_reads": round(avg_logical, 2),
                    "avg_physical_reads": round(avg_physical, 2),
                    "avg_cpu_time_ms": round(avg_cpu_ms, 3),
                    "cache_hit_ratio_pct": (
                        round(cache_hit_ratio, 2) if cache_hit_ratio is not None else None
                    ),
                    "first_seen": str(r.get("creation_time") or "") or None,
                    "last_seen": str(r.get("last_execution_time") or "") or None,
                }
            )
        return patterns

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

    # -------------------------------------------------------------------
    # Sample data
    # -------------------------------------------------------------------

    def collect_sample_data(self, table_name: str, limit: int = 10) -> list[dict]:
        schema, table = self._split_qualified(table_name)
        # SQL Server uses TOP N rather than LIMIT N.
        sql = f"SELECT TOP {limit} * FROM [{schema}].[{table}]"  # nosec B608 — schema/table from sys catalog, not user input
        return self._query(sql)


# -----------------------------------------------------------------------
# Module-level helpers (mirrors the duplicated pattern in mysql_tools and
# postgres_tools — to be extracted to a shared module in a follow-up PR).
# -----------------------------------------------------------------------


def _extract_query_type(sql: str) -> str:
    s = sql.strip().upper()
    for keyword in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"):
        if s.startswith(keyword):
            return keyword
    return "OTHER"


def _extract_tables(sql: str) -> list[str]:
    """Extract table names from FROM and JOIN clauses (best-effort regex).

    Skips references to system schemas (``sys``, ``INFORMATION_SCHEMA``) and
    bare references to system pseudo-tables.
    """
    tables = set()
    for match in re.finditer(
        r"\b(?:FROM|JOIN)\s+(?:\[?(\w+)\]?\.)?\[?(\w+)\]?", sql, re.IGNORECASE
    ):
        schema, table = match.group(1), match.group(2)
        if not table:
            continue
        if schema and schema.lower() in ("sys", "information_schema"):
            continue
        if table.lower() in ("sys", "information_schema"):
            continue
        tables.add(f"{schema}.{table}" if schema else table)
    return sorted(tables)


def _extract_filter_columns(sql: str) -> list[str] | None:
    """Best-effort extraction of column names appearing in WHERE clauses."""
    where_match = re.search(
        r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|$)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if not where_match:
        return None
    where_clause = where_match.group(1)
    cols = re.findall(r"\[?(\w+)\]?\s*(?:=|<|>|<=|>=|<>|!=|LIKE|IN)", where_clause, re.IGNORECASE)
    return sorted(set(cols)) if cols else None


def _extract_sort_columns(sql: str) -> list[str] | None:
    order_match = re.search(
        r"\bORDER\s+BY\b(.*?)(?:\bLIMIT\b|\bOFFSET\b|$)", sql, re.IGNORECASE | re.DOTALL
    )
    if not order_match:
        return None
    cols = re.findall(r"\[?(\w+)\]?", order_match.group(1))
    return sorted({c for c in cols if c.upper() not in ("ASC", "DESC")}) if cols else None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _normalize_index_type(raw: object) -> str:
    """Map SQL Server index type names to the contract's IndexType enum.

    SQL Server's sys.indexes.type_desc returns values like ``CLUSTERED``,
    ``NONCLUSTERED``, ``XML``, ``SPATIAL``, ``CLUSTERED COLUMNSTORE``, etc.
    The contract enum is engine-neutral (btree/hash/gist/gin/fulltext/spatial/other).
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


def _has_text_search(query_text: str) -> bool:
    lower = query_text.lower()
    if "like '%" in lower or "like n'%" in lower:
        return True
    if " contains(" in lower or " freetext(" in lower:  # SQL Server full-text search
        return True
    return False


def _text_search_type(query_text: str) -> str | None:
    lower = query_text.lower()
    if " contains(" in lower or " freetext(" in lower:
        return "fulltext"
    if "like '%" in lower or "like n'%" in lower:
        return "like_wildcard"
    return None


def _has_time_range_filter(query_text: str) -> bool:
    lower = query_text.lower()
    if re.search(r"\b(getdate|sysdatetime|current_timestamp)\b", lower):
        return True
    if re.search(r"\bbetween\b.*\b(date|datetime|datetime2|date_)", lower):
        return True
    if re.search(r"(date|datetime|datetime2|created|updated|_at|_date)\s*[<>]=?", lower):
        return True
    return False
