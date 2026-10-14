"""
Offline Collection Parser

Reads the JSON output from collect-mysql.sql and transforms it into the
same raw data structures that MySQLRemoteCollector produces, so the
existing _build_tables, _build_queries, etc. work unchanged.
"""

import json
import logging
import re
from collections.abc import Mapping

import boto3

from src.tools.database.collection_text import extract_json_object
from src.tools.validation.ingestion_limits import check_collection_counts, check_file_size

logger = logging.getLogger(__name__)


def fetch_offline_json(bucket: str, key: str, region: str = "us-east-1") -> dict:
    """Download the offline collection JSON from S3.

    Checks the object's size via ``head_object`` (#142) and rejects it before
    downloading the body if it exceeds the configured ingestion limit.
    """
    s3 = boto3.client("s3", region_name=region)
    head = s3.head_object(Bucket=bucket, Key=key)
    check_file_size(head["ContentLength"], source=f"s3://{bucket}/{key}")
    resp = s3.get_object(Bucket=bucket, Key=key)
    content = extract_json_object(
        resp["Body"].read().decode("utf-8"), source=f"s3://{bucket}/{key}"
    )

    # Oracle 19c JSON_OBJECT doesn't escape control chars in string values.
    # Strip them (except newlines which are line separators in the JSON).
    content = "".join(c if ord(c) >= 32 or c == "\n" else " " for c in content)

    # Remove sentinel objects used for trailing-comma handling in PL/SQL scripts.
    # The regex handles three real-world cases produced by DBMS_OUTPUT.PUT_LINE:
    #   [item, item, {"_sentinel":true}]               inline emission
    #   [\nitem,\n{"_sentinel": true}\n]               multi-line emission
    #   [\n{"_sentinel": true}\n]                      empty-array case (no comma)
    # A lookahead `(?=\s*\])` restricts matches to sentinels that immediately
    # precede an array-close bracket, so a nested object with a legitimate
    # `_sentinel:true` field elsewhere in the JSON is preserved.
    # Previous two literal `.replace()` calls handled only the first case,
    # which caused parse failures on real Oracle customer data.
    content = re.sub(
        r'(?:,\s*)?\{\s*"_sentinel"\s*:\s*true\s*\}(?=\s*\])',
        "",
        content,
    )

    result: dict = json.loads(content)
    return result


def parse_offline_collection(data: dict) -> dict:
    """
    Transform the flat offline JSON into the nested structure expected
    by mysql_collector's builder functions.

    Returns dict with keys: metadata, tables (list of table dicts with
    nested columns/indexes/fks/pk), views, procedures, triggers,
    queries, global_stats.

    Raises:
        IngestionLimitError: the collection's table or query count exceeds the
            configured ingestion limit (#142). Checked here, before the more
            expensive nested column/index/FK/PK structures are built below.
    """
    check_collection_counts(
        table_count=len(data.get("tables") or []),
        query_count=len(data.get("queries") or []),
        source="offline collection",
    )

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
                "predicate": i.get("predicate") or None,
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
    table_schemas: dict[str, set[str]] = {}
    tables = []
    for t in data.get("tables") or []:
        tbl_name = t["table_name"]
        table_id = t.get("table_id") or f"{db_name}.{tbl_name}"
        known_table_names.add(tbl_name)
        if t.get("schema_name"):
            table_schemas.setdefault(tbl_name, set()).add(str(t["schema_name"]))
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
        "queries": _transform_queries(
            data.get("queries") or [], db_name, known_table_names, table_schemas=table_schemas
        ),
        "global_stats": global_stats,
        "io_stats": data.get("io_stats", {}),
        "wait_events": data.get("wait_events", []),
        "os_stats": data.get("os_stats", {}),
    }


def _merge_query_variants(raw: list[dict]) -> list[dict]:
    """Merge rows that share a ``digest`` into one row per query shape.

    This is the safety net for every offline collection format (#386): SQL Server's
    ``sys.dm_exec_query_stats`` keeps one row per cached *statement*, so a
    non-parameterized query gets one row per literal value, all sharing the
    same ``query_hash``/``digest``. The collection scripts should already
    aggregate by digest, but offline collections may predate that fix (or
    come from an engine that doesn't group), so we merge here defensively
    before building the patterns list. It only merges rows that already
    share a digest: Oracle's ``SQL_ID`` differs per literal, so Oracle
    literal variants are not merged here (tracked separately).

    Summable counters (execution_count, rows sent/examined/affected, total
    time, lock time, scan/index counters, errors/warnings) are summed across
    variants; min/max fields take the min/max across variants; averages are
    recomputed from the merged totals; ``first_seen``/``last_seen`` take the
    min/max across variants; one representative ``query_text`` is kept (the
    variant with the highest ``total_time_ms``). Pattern order is
    deterministic: one row per digest, in order of first appearance.
    """
    import hashlib

    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in raw:
        query_text = str(r.get("query_text") or "")
        key = str(r.get("digest") or hashlib.sha256(query_text.encode()).hexdigest()[:16])
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(r)

    merged_variant_count = sum(len(variants) - 1 for variants in groups.values())
    if merged_variant_count:
        logger.info(
            "offline_parser: merged %d literal-variant row(s) sharing a digest "
            "into %d query shape(s)",
            merged_variant_count,
            len(groups),
        )

    int_sum_fields = (
        "execution_count",
        "total_rows_sent",
        "total_rows_examined",
        "total_rows_affected",
        "full_table_scans",
        "range_scans",
        "no_index_used",
        "no_good_index_used",
        "sum_errors",
        "sum_warnings",
    )
    float_sum_fields = ("total_time_ms", "lock_time_ms")

    merged_rows = []
    for key in order:
        variants = groups[key]
        if len(variants) == 1:
            row = dict(variants[0])
            row["digest"] = key
            merged_rows.append(row)
            continue

        # Representative text/sample comes from the costliest variant.
        representative = max(variants, key=lambda v: float(v.get("total_time_ms") or 0))
        merged = dict(representative)
        merged["digest"] = key

        for field in int_sum_fields:
            if any(field in v for v in variants):
                merged[field] = int(sum(float(v.get(field) or 0) for v in variants))
        for field in float_sum_fields:
            if any(field in v for v in variants):
                merged[field] = sum(float(v.get(field) or 0) for v in variants)

        min_times = [float(v["min_time_ms"]) for v in variants if v.get("min_time_ms") is not None]
        max_times = [float(v["max_time_ms"]) for v in variants if v.get("max_time_ms") is not None]
        if min_times:
            merged["min_time_ms"] = min(min_times)
        if max_times:
            merged["max_time_ms"] = max(max_times)

        exec_count = merged.get("execution_count") or 1
        merged["avg_time_ms"] = float(merged.get("total_time_ms") or 0) / exec_count

        first_seens = [v["first_seen"] for v in variants if v.get("first_seen")]
        last_seens = [v["last_seen"] for v in variants if v.get("last_seen")]
        if first_seens:
            merged["first_seen"] = min(first_seens)
        if last_seens:
            merged["last_seen"] = max(last_seens)

        merged_rows.append(merged)

    return merged_rows


def _resolve_qualified_table(
    qualifiers: list[str],
    table: str,
    db_name: str,
    known_table_names: set[str],
    table_schemas: Mapping[str, set[str]],
) -> str:
    """The table id for a qualified reference ``[database.]schema.table`` (#653).

    ``_build_tables`` keys every offline table as ``<database>.<table>``, whatever
    schema the collection reports for it, so ``[Audit].[EventLog]`` has to become
    ``trailgear.EventLog`` to match its own table recommendation. The qualifier must
    name that table's own schema (compared case-insensitively, as SQL Server and
    unquoted PostgreSQL/Oracle names do) or the database itself (MySQL's
    ``db.table``); a table the collection reports no schema for accepts any
    qualifier. A T-SQL three-part name ``database.schema.table`` resolves the same
    way by its last two parts when the first names this database.

    Anything else is kept as written: an unknown table, a table in a different
    schema or database, ``alias.column`` after ``FROM``, and a table name the
    collection holds in more than one schema (``Audit.EventLog`` and
    ``Archive.EventLog`` both get the id ``<database>.EventLog``, #382, so no
    single id is the right one).

    Table names still match case-sensitively, the same as bare names do.
    """
    written = ".".join([*qualifiers, table])
    if table not in known_table_names:
        return written
    if len(qualifiers) == 2:
        if qualifiers[0].lower() != db_name.lower():
            return written
        qualifiers = qualifiers[1:]
    qualifier = qualifiers[0].lower()
    schemas = {s.lower() for s in table_schemas.get(table, ())}
    if len(schemas) > 1:
        logger.info(
            "offline_parser: %s kept as written: table %s is collected in %d schemas",
            written,
            table,
            len(schemas),
        )
        return written
    if not schemas or qualifier in schemas or qualifier == db_name.lower():
        return f"{db_name}.{table}"
    return written


def _transform_queries(
    raw: list[dict],
    db_name: str,
    known_table_names: set[str],
    table_schemas: Mapping[str, set[str]] | None = None,
) -> list[dict]:
    """Transform raw MySQL performance_schema rows into the format _build_queries expects.

    Maps field names from the SQL collection script output to the same
    keys that MySQLRemoteCollector.collect_query_patterns() produces.

    ``table_schemas`` maps a collected table's bare name to the schema(s) the
    collection reports for it, so a schema-qualified reference resolves to the
    table's own id (see ``_resolve_qualified_table``, #653).

    Rows that share a ``digest`` are merged first (see
    ``_merge_query_variants``), as a safety net against collections where
    one query shape produced multiple rows (e.g. SQL Server literal
    variants, #386).
    """
    import hashlib
    import re

    patterns = []
    for r in _merge_query_variants(raw):
        query_text = str(r.get("query_text") or "")
        exec_count = r.get("execution_count") or 1
        total_rows_sent = r.get("total_rows_sent") or 0
        total_rows_examined = r.get("total_rows_examined") or 0
        total_rows_affected = r.get("total_rows_affected") or 0
        total_time_ms = float(r.get("total_time_ms") or 0)

        # Extract tables from query text and resolve to table_id format.
        # Handles three quote styles + optional schema-qualified names:
        #   FROM table         (MySQL bare)
        #   FROM `table`       (MySQL backtick)
        #   FROM "table"       (PostgreSQL/Oracle double quote)
        #   FROM [table]       (SQL Server bracket)
        #   FROM schema.table, FROM [schema].[table], FROM `schema`.`table`
        #   FROM database.schema.table (T-SQL three-part name)
        #   REFRESH MATERIALIZED VIEW [CONCURRENTLY] <name> (PostgreSQL)
        # The named groups capture up to two qualifiers and the table.
        table_re = re.compile(
            r"(?:FROM|JOIN|INTO|UPDATE|REFRESH\s+MATERIALIZED\s+VIEW(?:\s+CONCURRENTLY)?)\s+"
            r'(?:[`"\[]?(?P<qual1>\w+)[`"\]]?\s*\.\s*)?'
            r'(?:[`"\[]?(?P<qual2>\w+)[`"\]]?\s*\.\s*)?'
            r'[`"\[]?(?P<table>\w+)[`"\]]?',
            re.I,
        )
        # Upsert clauses use UPDATE for a column assignment, not a table reference.
        scanned_query = re.sub(
            r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b|\bDO\s+UPDATE\b",
            " ",
            query_text,
            flags=re.I,
        )
        tables_accessed: list[str] = []
        for m_raw in table_re.finditer(scanned_query):
            # Up to two qualifiers: schema.table, or T-SQL's database.schema.table.
            qualifiers = [q for q in (m_raw.group("qual1"), m_raw.group("qual2")) if q]
            table = m_raw.group("table")
            if not table:
                continue
            # Skip system catalogs / RDS-internal references
            if qualifiers and qualifiers[-1].lower() in ("sys", "information_schema"):
                continue
            if table.lower() in ("sys", "information_schema"):
                continue
            if qualifiers:
                tables_accessed.append(
                    _resolve_qualified_table(
                        qualifiers, table, db_name, known_table_names, table_schemas or {}
                    )
                )
            # Prefix with db_name if the bare-name table is known
            elif table in known_table_names:
                tables_accessed.append(f"{db_name}.{table}")
            else:
                tables_accessed.append(table)
        # A qualified and a bare reference to one table resolve to the same id.
        tables_accessed = list(dict.fromkeys(tables_accessed))
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


# Default port per engine, for an offline collection's CollectorInput (mode="offline"
# never actually connects, so this is cosmetic/display only).
_ENGINE_PORTS: dict[str, int] = {
    "mysql": 3306,
    "mariadb": 3306,
    "postgresql": 5432,
    "postgres": 5432,
    "sqlserver": 1433,
    "oracle": 1521,
    "db2": 50000,
}


def detect_source_engine(metadata: dict | None) -> tuple[str, int]:
    """Return ``(engine, port)`` for an offline collection from its metadata.

    ``metadata.engine`` is checked first -- the SQL Server collector script sets it
    explicitly (#381: it used to be ignored, so every SQL Server offline collection was
    mislabelled ``mysql``). Falling back to the server version string (``version``, or
    ``version_full``/``product_version`` when the SQL Server script wrote those instead,
    #381) when there is no explicit engine, or it is not one this function recognizes.
    Anything that matches neither is treated as MySQL, the original (pre-#381) default
    for every offline collection.
    """
    meta = metadata or {}
    engine = str(meta.get("engine") or "").strip().lower()
    if engine in _ENGINE_PORTS:
        return engine, _ENGINE_PORTS[engine]

    version = str(
        meta.get("version") or meta.get("version_full") or meta.get("product_version") or ""
    ).lower()
    if "postgres" in version:
        return "postgresql", 5432
    if "sql server" in version:
        return "sqlserver", 1433
    if "oracle" in version:
        return "oracle", 1521
    return "mysql", 3306


def offline_version_label(metadata: dict | None) -> str:
    """Short, human-readable source version string for an offline collection (#381).

    ``metadata.product_version`` (a short build number, e.g. ``"15.0.2000.5"`` for
    SQL Server) is preferred over ``metadata.version``/``version_full`` (the full
    ``@@VERSION``/version-banner string some collector scripts write instead,
    which embeds the edition, OS and processor info across several lines and
    tabs -- never fit for a user-visible sentence like wave 1's rationale).

    A SQL Server ``@@VERSION`` banner (``"Microsoft SQL Server 2019 (RTM) -
    15.0.2000.5 (X64) ..."``) is run through ``sqlserver_collector.
    _parse_sqlserver_version`` to pull out just the build number, or failing that
    the marketing year (#381 review round 2): the banner's first line still starts
    with "Microsoft", not "SQL Server", so wave 1's rationale (which only dedupes a
    version string that already starts with the source engine's own display name)
    would otherwise render the engine name twice -- "SQL Server Microsoft SQL
    Server 2019 (RTM) - 15.0.2000.5". Every other (non-SQL-Server) banner falls
    back to the first line only, as before. ``"unknown"`` when there is neither
    ``product_version`` nor ``version``/``version_full``.
    """
    meta = metadata or {}
    product_version = meta.get("product_version")
    if product_version:
        return str(product_version).strip()
    full = meta.get("version") or meta.get("version_full")
    if full:
        full_str = str(full).strip()
        if "sql server" in full_str.lower():
            from src.agents.collector.sqlserver_collector import _parse_sqlserver_version

            return _parse_sqlserver_version(full_str)
        first_line = full_str.splitlines()[0].strip()
        return first_line or "unknown"
    return "unknown"
