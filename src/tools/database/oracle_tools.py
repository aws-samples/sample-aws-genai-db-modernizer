"""
Oracle Collector Tools — SSM-based.

All queries execute on a remote automation instance via SSM Run Command.
Uses V$SQLSTATS for query patterns (no AWR/Diagnostic Pack dependency).
Uses ALL_* catalog views filtered by OWNER for schema extraction.

Oracle uppercases unquoted identifiers — all output from this module is
lowercased for downstream consistency.
"""

import hashlib
import logging
import re

from src.tools.aws.ssm_executor import SSMExecutor

logger = logging.getLogger(__name__)

# Oracle internal schemas to exclude from collection.
_SYSTEM_OWNERS = (
    "'SYS'",
    "'SYSTEM'",
    "'XDB'",
    "'CTXSYS'",
    "'MDSYS'",
    "'OUTLN'",
    "'DBSNMP'",
    "'AUDSYS'",
    "'GSMADMIN_INTERNAL'",
    "'ORDSYS'",
    "'WMSYS'",
    "'APPQOSSYS'",
    "'DBSFWUSER'",
    "'OJVMSYS'",
    "'GSMUSER'",
    "'DVSYS'",
    "'LBACSYS'",
    "'OLAPSYS'",
    "'ORDDATA'",
    "'SI_INFORMTN_SCHEMA'",
    "'EXFSYS'",
    "'ANONYMOUS'",
    "'RDSADMIN'",
    "'RDSSEC'",
)
_SYSTEM_OWNERS_FILTER = f"OWNER NOT IN ({', '.join(_SYSTEM_OWNERS)})"


class OracleRemoteCollector:
    """Collects Oracle metadata via SSM Run Command."""

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
        self.database = database  # SERVICE_NAME (PDB name for CDB architecture)
        self.secret_arn = secret_arn
        self.region = region

    def _query(self, sql: str) -> list[dict]:
        # Oracle sqlplus requires ; to execute a statement
        if not sql.rstrip().endswith(";"):
            sql = sql.rstrip() + ";"
        return self.ssm.run_sql_json(
            engine="oracle",
            host=self.host,
            port=self.port,
            database=self.database,
            secret_arn=self.secret_arn,
            sql=sql,
            region=self.region,
        )

    def _query_raw(self, sql: str) -> str:
        if not sql.rstrip().endswith(";"):
            sql = sql.rstrip() + ";"
        return self.ssm.run_sql(
            engine="oracle",
            host=self.host,
            port=self.port,
            database=self.database,
            secret_arn=self.secret_arn,
            sql=sql,
            region=self.region,
        )

    @staticmethod
    def _split_qualified(table_name: str) -> tuple[str, str]:
        """Split ``"owner.table"`` into ``(owner, table)``."""
        if "." in table_name:
            owner, table = table_name.split(".", 1)
            return owner.upper(), table.upper()
        return "", table_name.upper()

    # -------------------------------------------------------------------
    # Metadata
    # -------------------------------------------------------------------

    def get_version(self) -> str:
        rows = self._query("SELECT BANNER AS version FROM V$VERSION WHERE ROWNUM = 1")
        return str(rows[0]["version"]).strip() if rows else "unknown"

    def get_database_size_gb(self) -> float:
        rows = self._query(
            "SELECT ROUND(SUM(bytes)/1024/1024/1024, 3) AS size_gb FROM DBA_SEGMENTS"
        )
        return float(rows[0]["size_gb"] or 0) if rows else 0

    def get_current_schema(self) -> str:
        """Return the effective schema (OWNER) to filter on."""
        rows = self._query("SELECT USER AS s FROM DUAL")
        return str(rows[0]["s"]).upper() if rows else ""

    # -------------------------------------------------------------------
    # Schema
    # -------------------------------------------------------------------

    def collect_tables(self, owner: str | None = None) -> list[dict]:
        owner = owner or self.get_current_schema()
        rows = self._query(f"""
            SELECT
                t.OWNER AS schema_name,
                t.TABLE_NAME AS table_name,
                t.NUM_ROWS AS row_count,
                ROUND(s.bytes/1024/1024, 2) AS data_size_mb
            FROM ALL_TABLES t
            LEFT JOIN (
                SELECT OWNER, SEGMENT_NAME, SUM(BYTES) AS bytes
                FROM DBA_SEGMENTS
                WHERE SEGMENT_TYPE = 'TABLE'
                GROUP BY OWNER, SEGMENT_NAME
            ) s ON s.OWNER = t.OWNER AND s.SEGMENT_NAME = t.TABLE_NAME
            WHERE t.OWNER = '{owner}'
            ORDER BY t.TABLE_NAME
        """)  # nosec B608 — owner from sys catalog
        # Lowercase all identifiers for downstream consistency
        for r in rows:
            r["schema_name"] = str(r.get("schema_name") or "").lower()
            r["table_name"] = str(r.get("table_name") or "").lower()
        return rows

    def collect_columns(self, table_name: str, owner: str | None = None) -> list[dict]:
        own, tbl = self._split_qualified(table_name)
        own = own or (owner or self.get_current_schema())
        rows = self._query(f"""
            SELECT
                COLUMN_NAME AS column_name,
                COLUMN_ID AS ordinal_position,
                DATA_TYPE AS data_type,
                DATA_LENGTH AS max_length,
                CHAR_USED AS char_used,
                NULLABLE AS is_nullable,
                DATA_DEFAULT AS column_default,
                DATA_PRECISION AS data_precision,
                DATA_SCALE AS data_scale,
                IDENTITY_COLUMN AS is_identity
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = '{own}' AND TABLE_NAME = '{tbl}'
            ORDER BY COLUMN_ID
        """)  # nosec B608 — owner/table from sys catalog
        for r in rows:
            r["column_name"] = str(r.get("column_name") or "").lower()
            r["data_type"] = str(r.get("data_type") or "").lower()
        return rows

    def collect_indexes(self, table_name: str, owner: str | None = None) -> list[dict]:
        own, tbl = self._split_qualified(table_name)
        own = own or (owner or self.get_current_schema())
        raw = self._query(f"""
            SELECT
                i.INDEX_NAME AS index_name,
                ic.COLUMN_NAME AS column_name,
                ic.COLUMN_POSITION AS key_ordinal,
                i.UNIQUENESS AS uniqueness,
                i.INDEX_TYPE AS index_type,
                CASE WHEN c.CONSTRAINT_TYPE = 'P' THEN 'YES' ELSE 'NO' END AS is_primary
            FROM ALL_INDEXES i
            JOIN ALL_IND_COLUMNS ic
                ON i.OWNER = ic.INDEX_OWNER AND i.INDEX_NAME = ic.INDEX_NAME
            LEFT JOIN ALL_CONSTRAINTS c
                ON c.OWNER = i.OWNER AND c.INDEX_NAME = i.INDEX_NAME
                AND c.CONSTRAINT_TYPE = 'P'
            WHERE i.TABLE_OWNER = '{own}' AND i.TABLE_NAME = '{tbl}'
            ORDER BY i.INDEX_NAME, ic.COLUMN_POSITION
        """)  # nosec B608 — owner/table from sys catalog

        indexes: dict[str, dict] = {}
        for r in raw:
            name = str(r.get("index_name") or "").lower()
            if not name:
                continue
            if name not in indexes:
                indexes[name] = {
                    "index_name": name,
                    "columns": [],
                    "is_unique": str(r.get("uniqueness") or "").upper() == "UNIQUE",
                    "is_primary": str(r.get("is_primary") or "").upper() == "YES",
                    "index_type": _normalize_index_type(r.get("index_type")),
                }
            indexes[name]["columns"].append(str(r.get("column_name") or "").lower())
        return list(indexes.values())

    def collect_foreign_keys(self, table_name: str, owner: str | None = None) -> list[dict]:
        own, tbl = self._split_qualified(table_name)
        own = own or (owner or self.get_current_schema())
        raw = self._query(f"""
            SELECT
                a.CONSTRAINT_NAME AS constraint_name,
                ac.COLUMN_NAME AS column_name,
                ac.POSITION AS ordinal,
                rc.TABLE_NAME AS referenced_table_name,
                rcc.COLUMN_NAME AS referenced_column_name,
                a.DELETE_RULE AS on_delete
            FROM ALL_CONSTRAINTS a
            JOIN ALL_CONS_COLUMNS ac
                ON a.OWNER = ac.OWNER AND a.CONSTRAINT_NAME = ac.CONSTRAINT_NAME
            JOIN ALL_CONSTRAINTS rc
                ON a.R_OWNER = rc.OWNER AND a.R_CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            JOIN ALL_CONS_COLUMNS rcc
                ON rc.OWNER = rcc.OWNER AND rc.CONSTRAINT_NAME = rcc.CONSTRAINT_NAME
                AND ac.POSITION = rcc.POSITION
            WHERE a.CONSTRAINT_TYPE = 'R'
              AND a.OWNER = '{own}' AND a.TABLE_NAME = '{tbl}'
            ORDER BY a.CONSTRAINT_NAME, ac.POSITION
        """)  # nosec B608 — owner/table from sys catalog

        fks: dict[str, dict] = {}
        for r in raw:
            name = str(r.get("constraint_name") or "").lower()
            if name not in fks:
                fks[name] = {
                    "constraint_name": name,
                    "columns": [],
                    "referenced_table": str(r.get("referenced_table_name") or "").lower(),
                    "referenced_columns": [],
                    "on_delete": _normalize_fk_action(r.get("on_delete")),
                    "on_update": "NO ACTION",  # Oracle doesn't support ON UPDATE
                }
            fks[name]["columns"].append(str(r.get("column_name") or "").lower())
            fks[name]["referenced_columns"].append(
                str(r.get("referenced_column_name") or "").lower()
            )
        return list(fks.values())

    def collect_primary_key(self, table_name: str, owner: str | None = None) -> list[str]:
        own, tbl = self._split_qualified(table_name)
        own = own or (owner or self.get_current_schema())
        rows = self._query(f"""
            SELECT cc.COLUMN_NAME AS column_name
            FROM ALL_CONSTRAINTS c
            JOIN ALL_CONS_COLUMNS cc
                ON c.OWNER = cc.OWNER AND c.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
            WHERE c.CONSTRAINT_TYPE = 'P'
              AND c.OWNER = '{own}' AND c.TABLE_NAME = '{tbl}'
            ORDER BY cc.POSITION
        """)  # nosec B608 — owner/table from sys catalog
        return [str(r["column_name"]).lower() for r in rows]

    # -------------------------------------------------------------------
    # Views, procedures, triggers
    # -------------------------------------------------------------------

    def collect_views(self, owner: str | None = None) -> list[dict]:
        owner = owner or self.get_current_schema()
        rows = self._query(f"""
            SELECT
                VIEW_NAME AS view_name,
                OWNER AS schema_name,
                TEXT_LENGTH AS text_length
            FROM ALL_VIEWS
            WHERE OWNER = '{owner}'
            ORDER BY VIEW_NAME
        """)  # nosec B608 — owner from sys catalog
        for r in rows:
            r["view_name"] = str(r.get("view_name") or "").lower()
            r["schema_name"] = str(r.get("schema_name") or "").lower()
        return rows

    def collect_procedures(self, owner: str | None = None) -> list[dict]:
        owner = owner or self.get_current_schema()
        rows = self._query(f"""
            SELECT
                OBJECT_NAME AS routine_name,
                OWNER AS schema_name,
                OBJECT_TYPE AS routine_type
            FROM ALL_OBJECTS
            WHERE OWNER = '{owner}'
              AND OBJECT_TYPE IN ('PROCEDURE', 'FUNCTION', 'PACKAGE')
            ORDER BY OBJECT_TYPE, OBJECT_NAME
        """)  # nosec B608 — owner from sys catalog
        for r in rows:
            r["routine_name"] = str(r.get("routine_name") or "").lower()
            r["schema_name"] = str(r.get("schema_name") or "").lower()
            r["routine_type"] = str(r.get("routine_type") or "").upper()
            r["language"] = "PL/SQL"
        return rows

    def collect_triggers(self, owner: str | None = None) -> list[dict]:
        owner = owner or self.get_current_schema()
        rows = self._query(f"""
            SELECT
                TRIGGER_NAME AS trigger_name,
                TABLE_NAME AS table_name,
                TABLE_OWNER AS schema_name,
                TRIGGER_TYPE AS timing,
                TRIGGERING_EVENT AS event_type
            FROM ALL_TRIGGERS
            WHERE OWNER = '{owner}'
            ORDER BY TABLE_NAME, TRIGGER_NAME
        """)  # nosec B608 — owner from sys catalog
        for r in rows:
            r["trigger_name"] = str(r.get("trigger_name") or "").lower()
            r["table_name"] = str(r.get("table_name") or "").lower()
            r["schema_name"] = str(r.get("schema_name") or "").lower()
            # Normalize timing: "BEFORE EACH ROW" → "BEFORE"
            timing = str(r.get("timing") or "")
            if "BEFORE" in timing.upper():
                r["timing"] = "BEFORE"
            elif "AFTER" in timing.upper() or "COMPOUND" in timing.upper():
                r["timing"] = "AFTER"
            elif "INSTEAD OF" in timing.upper():
                r["timing"] = "INSTEAD_OF"
        return rows

    # -------------------------------------------------------------------
    # Query patterns (V$SQLSTATS-based)
    # -------------------------------------------------------------------

    def collect_query_patterns(
        self, min_executions: int = 10, limit: int = 1000, owner: str | None = None
    ) -> list[dict]:
        """Collect query stats from V$SQL.

        No AWR/Diagnostic Pack dependency. Only totals available — no min/max
        per-execution, so percentile estimation is not possible (left as None).
        CLOB sql_fulltext is truncated to 4000 chars via DBMS_LOB.SUBSTR.

        Note: V$SQLSTATS lacks PARSING_SCHEMA_NAME, so we query V$SQL which
        has the same metrics plus schema filtering capability.
        """
        owner = owner or self.get_current_schema()
        raw = self._query(f"""
            SELECT *
            FROM (
                SELECT
                    SQL_ID AS query_id,
                    REPLACE(REPLACE(
                        DBMS_LOB.SUBSTR(SQL_FULLTEXT, 4000, 1),
                        CHR(13), ' '), CHR(10), ' ') AS query_text,
                    EXECUTIONS AS execution_count,
                    ELAPSED_TIME AS total_elapsed_us,
                    CPU_TIME AS total_cpu_us,
                    BUFFER_GETS AS total_logical_reads,
                    DISK_READS AS total_physical_reads,
                    ROWS_PROCESSED AS total_rows,
                    FIRST_LOAD_TIME AS first_seen,
                    TO_CHAR(LAST_ACTIVE_TIME, 'YYYY-MM-DD HH24:MI:SS') AS last_seen
                FROM V$SQL
                WHERE EXECUTIONS >= {min_executions}
                  AND PARSING_SCHEMA_NAME = '{owner}'
                ORDER BY ELAPSED_TIME DESC
            )
            WHERE ROWNUM <= {limit}
        """)  # nosec B608 — min_executions/limit are internal; owner from catalog

        patterns = []
        for r in raw:
            query_text = str(r.get("query_text") or "").strip()
            calls = int(r.get("execution_count") or 1)

            total_elapsed_us = float(r.get("total_elapsed_us") or 0)
            total_cpu_us = float(r.get("total_cpu_us") or 0)
            total_rows = int(r.get("total_rows") or 0)
            total_logical = int(r.get("total_logical_reads") or 0)
            total_physical = int(r.get("total_physical_reads") or 0)

            avg_ms = total_elapsed_us / 1000.0 / max(calls, 1)
            avg_cpu_ms = total_cpu_us / 1000.0 / max(calls, 1)
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
                    "execution_time_ms_min": None,  # V$SQLSTATS has no per-exec min
                    "execution_time_ms_max": None,  # V$SQLSTATS has no per-exec max
                    "execution_time_ms_p50": None,  # Not estimable without min/max
                    "execution_time_ms_p95": None,
                    "execution_time_ms_p99": None,
                    "total_time_ms": round(total_elapsed_us / 1000.0, 3),
                    "rows_returned_avg": round(avg_rows, 2),
                    "rows_returned_p95": None,
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
                    "avg_logical_reads": round(avg_logical, 2),
                    "avg_physical_reads": round(avg_physical, 2),
                    "avg_cpu_time_ms": round(avg_cpu_ms, 3),
                    "cache_hit_ratio_pct": (
                        round(cache_hit_ratio, 2) if cache_hit_ratio is not None else None
                    ),
                    "first_seen": str(r.get("first_seen") or "").replace("/", " ").strip() or None,
                    "last_seen": str(r.get("last_seen") or "").strip() or None,
                }
            )
        return patterns

    # -------------------------------------------------------------------
    # Global stats
    # -------------------------------------------------------------------

    def collect_global_stats(self) -> dict:
        rows = self._query("""
            SELECT
                (SELECT COUNT(*) FROM V$SESSION WHERE TYPE = 'USER') AS active_connections,
                (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'db block gets') AS db_block_gets,
                (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'consistent gets') AS consistent_gets,
                (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'physical reads') AS physical_reads,
                (SELECT VALUE FROM V$SYSSTAT WHERE NAME = 'user commits') AS user_commits
            FROM DUAL
        """)
        if not rows:
            return {}
        r = rows[0]
        block_gets = float(r.get("db_block_gets") or 0)
        consistent = float(r.get("consistent_gets") or 0)
        physical = float(r.get("physical_reads") or 0)
        total_logical = block_gets + consistent
        ratio = ((total_logical - physical) / total_logical * 100) if total_logical > 0 else 0.0
        return {
            "cache_hit_ratio_pct": round(ratio, 2),
            "active_connections": int(r.get("active_connections") or 0),
            "total_transactions": int(r.get("user_commits") or 0),
        }

    # -------------------------------------------------------------------
    # Sample data
    # -------------------------------------------------------------------

    def collect_sample_data(
        self, table_name: str, limit: int = 10, owner: str | None = None
    ) -> list[dict]:
        own, tbl = self._split_qualified(table_name)
        own = own or (owner or self.get_current_schema())
        sql = f'SELECT * FROM "{own}"."{tbl}" WHERE ROWNUM <= {limit}'  # nosec B608
        return self._query(sql)


# -----------------------------------------------------------------------
# Module-level helpers
# -----------------------------------------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _normalize_index_type(raw: object) -> str:
    val = str(raw or "").upper()
    if "NORMAL" in val and "FUNCTION" not in val:
        return "btree"
    if "FUNCTION" in val or "DOMAIN" in val:
        return "functional"
    if "BITMAP" in val:
        return "bitmap"
    if "LOB" in val:
        return "other"
    if "NORMAL" in val:
        return "btree"
    return "btree"


def _normalize_fk_action(raw: object) -> str:
    val = str(raw or "").upper().replace(" ", "_")
    mapping = {
        "CASCADE": "CASCADE",
        "SET_NULL": "SET NULL",
        "SET_DEFAULT": "SET DEFAULT",
        "NO_ACTION": "NO ACTION",
    }
    return mapping.get(val, "NO ACTION")


def _extract_query_type(sql: str) -> str:
    first = sql.strip().split()[0].upper() if sql.strip() else ""
    return first if first in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE") else "OTHER"


def _extract_tables(sql: str) -> list[str]:
    """Extract table names from FROM/JOIN clauses."""
    tables: list[str] = []
    # Match FROM table, JOIN table (handles schema.table and "quoted")
    for m in re.finditer(r"(?:from|join)\s+\"?(\w+)\"?(?:\.\"?(\w+)\"?)?", sql, re.I):
        if m.group(2):
            tables.append(f"{m.group(1).lower()}.{m.group(2).lower()}")
        else:
            tables.append(m.group(1).lower())
    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for t in tables:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _extract_filter_columns(sql: str) -> list[str] | None:
    # Match columns in comparisons (col = val, col > val, col != val, etc.)
    matches = re.findall(r"\b(\w+)\s*(?:[=<>!]+|(?:NOT\s+)?IN|(?:NOT\s+)?LIKE|IS)", sql, re.I)
    # Filter out SQL keywords that regex might capture
    keywords = {"where", "and", "or", "not", "between", "case", "when", "then", "else", "end"}
    result = list(dict.fromkeys(m.lower() for m in matches if m.lower() not in keywords))
    return result or None


def _extract_sort_columns(sql: str) -> list[str] | None:
    m = re.search(r"order\s+by\s+(.+?)(?:;|\)|$)", sql, re.I)
    if not m:
        return None
    cols = [c.strip().split()[0].lower() for c in m.group(1).split(",")]
    return [c for c in cols if c and c not in ("asc", "desc")] or None


def _has_text_search(query_text: str) -> bool:
    lower = query_text.lower()
    return bool(
        re.search(r"contains\s*\(", lower) or re.search(r"like\s+'%", lower) or "ctx_" in lower
    )


def _text_search_type(query_text: str) -> str | None:
    lower = query_text.lower()
    if re.search(r"contains\s*\(", lower) or "ctx_" in lower:
        return "oracle_text"
    if re.search(r"like\s+'%", lower):
        return "like_wildcard"
    return None


def _has_time_range_filter(query_text: str) -> bool:
    lower = query_text.lower()
    return bool(
        re.search(r"(sysdate|systimestamp|current_date|current_timestamp)", lower)
        or re.search(r"between\s+.*?(date|timestamp|to_date)", lower)
        or re.search(r"interval\s+'", lower)
    )
