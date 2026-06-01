"""
Offline Collection Parser

Reads the JSON output from collect-mysql.sql and transforms it into the
same raw data structures that MySQLRemoteCollector produces, so the
existing _build_tables, _build_queries, etc. work unchanged.
"""

import json
import logging

import boto3

logger = logging.getLogger(__name__)


def fetch_offline_json(bucket: str, key: str, region: str = "us-east-1") -> dict:
    """Download the offline collection JSON from S3."""
    s3 = boto3.client("s3", region_name=region)
    resp = s3.get_object(Bucket=bucket, Key=key)
    content = resp["Body"].read().decode("utf-8").strip()

    # MySQL collection script may include a column header line before the JSON
    # when run without the -N flag (e.g. "collection_output\n{...}")
    if not content.startswith("{") and "\n" in content:
        content = content[content.index("\n") + 1 :]

    result: dict = json.loads(content)
    return result


def parse_offline_collection(data: dict) -> dict:
    """
    Transform the flat offline JSON into the nested structure expected
    by mysql_collector's builder functions.

    Returns dict with keys: metadata, tables (list of table dicts with
    nested columns/indexes/fks/pk), views, procedures, triggers,
    queries, global_stats.
    """
    metadata = data.get("metadata", {})
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    # Group columns, indexes, FKs, PKs by table_name
    columns_by_table: dict[str, list] = {}
    for c in data.get("columns", []):
        columns_by_table.setdefault(c["table_name"], []).append(c)

    indexes_by_table: dict[str, dict[str, dict]] = {}
    for i in data.get("indexes", []):
        tbl = i["table_name"]
        idx_name = i["index_name"]
        if tbl not in indexes_by_table:
            indexes_by_table[tbl] = {}
        if idx_name not in indexes_by_table[tbl]:
            indexes_by_table[tbl][idx_name] = {
                "index_name": idx_name,
                "columns": [],
                "is_unique": not i["non_unique"],
                "is_primary": idx_name == "PRIMARY",
                "index_type": str(i.get("index_type") or "btree").lower(),
            }
        indexes_by_table[tbl][idx_name]["columns"].append(i["column_name"])

    fks_by_table: dict[str, dict[str, dict]] = {}
    for fk in data.get("foreign_keys", []):
        tbl = fk["table_name"]
        name = fk["constraint_name"]
        if tbl not in fks_by_table:
            fks_by_table[tbl] = {}
        if name not in fks_by_table[tbl]:
            fks_by_table[tbl][name] = {
                "constraint_name": name,
                "columns": [],
                "referenced_table": fk["referenced_table_name"],
                "referenced_columns": [],
                "on_delete": fk.get("on_delete"),
                "on_update": fk.get("on_update"),
            }
        fks_by_table[tbl][name]["columns"].append(fk["column_name"])
        fks_by_table[tbl][name]["referenced_columns"].append(fk["referenced_column_name"])

    pks_by_table: dict[str, list[str]] = {}
    for pk in data.get("primary_keys", []):
        pks_by_table.setdefault(pk["table_name"], []).append(pk["column_name"])

    # Assemble tables with nested children
    db_name = metadata.get("database_name", "unknown") if isinstance(metadata, dict) else "unknown"
    known_table_names: set[str] = set()
    tables = []
    for t in data.get("tables", []):
        tbl_name = t["table_name"]
        table_id = t.get("table_id") or f"{db_name}.{tbl_name}"
        known_table_names.add(tbl_name)
        tables.append(
            {
                **t,
                "table_id": table_id,
                "columns": columns_by_table.get(tbl_name, []),
                "indexes": list(indexes_by_table.get(tbl_name, {}).values()),
                "foreign_keys": list(fks_by_table.get(tbl_name, {}).values()),
                "primary_key": pks_by_table.get(tbl_name, []),
                "sample_data": None,
            }
        )

    # Parse global stats into the format collect_global_stats() returns
    raw_stats = data.get("global_stats", {})
    read_req = int(raw_stats.get("innodb_buffer_pool_read_requests", 0))
    reads = int(raw_stats.get("innodb_buffer_pool_reads", 0))
    global_stats = {
        "cache_hit_ratio_pct": round((read_req - reads) / max(read_req, 1) * 100, 2),
        "buffer_pool_hits": read_req - reads,
        "buffer_pool_reads_from_disk": reads,
        "buffer_pool_read_requests": read_req,
        "tmp_disk_tables": int(raw_stats.get("created_tmp_disk_tables", 0)),
        "tmp_tables": int(raw_stats.get("created_tmp_tables", 0)),
    }

    return {
        "metadata": metadata,
        "tables": tables,
        "views": data.get("views", []),
        "procedures": data.get("procedures", []),
        "triggers": data.get("triggers", []),
        "queries": _transform_queries(data.get("queries", []), db_name, known_table_names),
        "global_stats": global_stats,
    }


def _transform_queries(raw: list[dict], db_name: str, known_table_names: set[str]) -> list[dict]:
    """Transform raw MySQL performance_schema rows into the format _build_queries expects.

    Maps field names from the SQL collection script output to the same
    keys that MySQLRemoteCollector.collect_query_patterns() produces.
    """
    import hashlib
    import re

    patterns = []
    for r in raw:
        query_text = str(r.get("query_text") or "")
        exec_count = r.get("execution_count") or 1
        total_rows_sent = r.get("total_rows_sent") or 0
        total_rows_examined = r.get("total_rows_examined") or 0
        total_rows_affected = r.get("total_rows_affected") or 0
        total_time_ms = float(r.get("total_time_ms") or 0)

        # Extract tables from query text and resolve to table_id format
        # Handles: FROM table, FROM `table` (MySQL), FROM "table" (PostgreSQL)
        table_re = re.compile(r'(?:FROM|JOIN|INTO|UPDATE)\s+[`"]?(\w+)[`"]?', re.I)
        raw_tables = list(dict.fromkeys(table_re.findall(query_text)))
        # Prefix with db_name if the table is known
        tables_accessed = []
        for t in raw_tables:
            if t in known_table_names:
                tables_accessed.append(f"{db_name}.{t}")
            else:
                tables_accessed.append(t)
        if not tables_accessed:
            tables_accessed = ["unknown"]

        # Extract query type
        type_re = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE|REPLACE)\b", re.I)
        m = type_re.match(query_text)
        query_type = m.group(1).upper() if m else "OTHER"

        digest = r.get("digest") or hashlib.sha256(query_text.encode()).hexdigest()[:16]

        patterns.append(
            {
                "query_id": digest,
                "query_text": query_text,
                "query_type": query_type,
                "execution_count": exec_count,
                "frequency_per_hour": exec_count / 24,
                "calls_per_second": exec_count / (24 * 3600),
                "execution_time_ms_avg": float(r.get("avg_time_ms") or 0),
                "execution_time_ms_min": float(r.get("min_time_ms") or 0),
                "execution_time_ms_max": float(r.get("max_time_ms") or 0),
                "execution_time_ms_p50": float(r.get("avg_time_ms") or 0),
                "total_time_ms": total_time_ms,
                "rows_returned_avg": total_rows_sent / exec_count,
                "rows_examined_avg": total_rows_examined / exec_count,
                "rows_affected_avg": total_rows_affected / exec_count,
                "full_table_scans": r.get("full_table_scans") or 0,
                "range_scans": r.get("range_scans") or 0,
                "queries_without_index": r.get("no_index_used") or 0,
                "queries_with_bad_index": r.get("no_good_index_used") or 0,
                "lock_time_ms": float(r.get("lock_time_ms") or 0),
                "lock_time_pct": round(
                    float(r.get("lock_time_ms") or 0) / max(total_time_ms, 0.001) * 100, 2
                ),
                "tables_accessed": tables_accessed,
                "has_joins": " join " in query_text.lower(),
                "join_count": len(re.findall(r"\bjoin\b", query_text, re.I)),
                "scan_efficiency_pct": min(
                    round(total_rows_sent / max(total_rows_examined, 1) * 100, 2), 100.0
                ),
                "has_aggregations": bool(
                    re.search(r"\b(count|sum|avg|min|max|group\s+by)\b", query_text, re.I)
                ),
                "has_subqueries": query_text.lower().count("select") > 1,
                "errors": r.get("sum_errors") or 0,
                "warnings": r.get("sum_warnings") or 0,
                "first_seen": r.get("first_seen"),
                "last_seen": r.get("last_seen"),
            }
        )
    return patterns
