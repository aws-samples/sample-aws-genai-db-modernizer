"""
Offline Collection Parser

Reads the JSON output from collect-mysql.sql and transforms it into the
same raw data structures that MySQLRemoteCollector produces, so the
existing _build_tables, _build_queries, etc. work unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Mapping
from typing import TYPE_CHECKING

import boto3

from src.tools.database.collection_text import extract_json_object
from src.tools.sql.query_type import statement_query_type
from src.tools.validation.ingestion_limits import check_collection_counts, check_file_size

if TYPE_CHECKING:
    from src.tools.sql.facade import SqlReading, TableReferences
    from src.tools.sql.shadow import TableShadow

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


def parse_offline_collection(data: dict, shadow: TableShadow | None = None) -> dict:
    """
    Transform the flat offline JSON into the nested structure expected
    by mysql_collector's builder functions.

    Every query's table references and its ``has_joins``/``join_count``/
    ``has_aggregations``/``has_subqueries`` facts come from the sqlglot facade
    (:func:`src.tools.sql.facade.read_sql`, #553, #796), read in the
    collection's source-engine dialect; the regex scans are the facade's own
    fallback. ``shadow``, when given, also runs the regex scan on every query
    and records where it disagrees with the facade (#557). It only observes:
    the returned structure is the same with or without it.

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

    raw_queries, dropped_other_db = _drop_other_database_queries(data.get("queries") or [], db_name)
    other_db_note = {"queries_dropped_other_database": dropped_other_db} if dropped_other_db else {}

    return {
        **other_db_note,
        "metadata": metadata,
        "tables": tables,
        "views": data.get("views", []),
        "procedures": data.get("procedures", []),
        "triggers": data.get("triggers", []),
        "queries": _transform_queries(
            raw_queries,
            db_name,
            known_table_names,
            table_schemas=table_schemas,
            shadow=shadow,
            source_engine=detect_source_engine(metadata if isinstance(metadata, dict) else None)[0],
        ),
        "global_stats": global_stats,
        "io_stats": data.get("io_stats", {}),
        "wait_events": data.get("wait_events", []),
        "os_stats": data.get("os_stats", {}),
    }


def _drop_other_database_queries(raw: list[dict], db_name: str) -> tuple[list[dict], int]:
    """Drop the statements a row's ``database_name`` attributes to another database.

    SQL Server's query DMVs are server-wide (#382); its collection script records
    the database each statement belongs to. A row with no ``database_name`` (older
    collections, the other engines' scripts, an unresolved statement) is kept.
    Returns the kept rows and how many were dropped (also logged).
    """
    target = db_name.casefold()
    kept = [
        r for r in raw if not r.get("database_name") or str(r["database_name"]).casefold() == target
    ]
    dropped = len(raw) - len(kept)
    if dropped:
        logger.warning(
            "offline_parser: dropped %d statement(s) belonging to other databases "
            "than the collected %r",
            dropped,
            db_name,
        )
    return kept, dropped


def _merge_query_variants(raw: list[dict], source_engine: str) -> list[dict]:
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

    Every merged row gets ``query_tags`` and ``query_tags_dropped``: the leading
    comment tags of all its variants' texts, in row order, read in the
    ``source_engine`` dialect (:class:`src.tools.sql.query_tags.QueryTags`, #1180),
    so the tags of a variant whose text is not the representative are kept too.
    """
    import hashlib

    from src.tools.sql.query_tags import QueryTags

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
        tags = QueryTags.of(
            (str(v.get("query_text") or "") for v in variants), source_engine
        ).fields()
        if len(variants) == 1:
            row = dict(variants[0])
            row["digest"] = key
            row.update(tags)
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

        merged.update(tags)
        merged_rows.append(merged)

    return merged_rows


# Engines whose table names compare case-insensitively (#742): SQL Server's
# default collation, PostgreSQL and Oracle unquoted names (folded to one case),
# DB2. MySQL is not one: on Linux, RDS and Aurora its table names are
# case-sensitive (``lower_case_table_names=0``).
_CASE_INSENSITIVE_ENGINES = frozenset({"sqlserver", "postgresql", "postgres", "oracle", "db2"})
# Of those, the ones where a quoted identifier keeps its case (``"Topics"`` is
# not ``topics``), so a quoted name only matches exactly. SQL Server's
# brackets and quotes don't change how its collation compares names.
_QUOTED_KEEPS_CASE_ENGINES = frozenset({"postgresql", "postgres", "oracle", "db2"})


def _case_folded_names(
    known_table_names: set[str], source_engine: str
) -> Mapping[str, tuple[str, ...]] | None:
    """Lower-cased name -> the collected names that fold to it, for an engine
    that compares table names case-insensitively; None (exact match only) for
    any other engine."""
    if (source_engine or "").strip().lower() not in _CASE_INSENSITIVE_ENGINES:
        return None
    folded: dict[str, list[str]] = {}
    for name in sorted(known_table_names):
        folded.setdefault(name.lower(), []).append(name)
    return {key: tuple(names) for key, names in folded.items()}


def _known_table_name(
    table: str,
    known_table_names: set[str],
    case_folded: Mapping[str, tuple[str, ...]] | None,
    exact_names: frozenset[str] = frozenset(),
) -> str | None:
    """The collected name ``table`` refers to, or None.

    An exact match always wins. With ``case_folded`` (an engine that compares
    names case-insensitively) a name matching exactly one collected table in
    another case is that table; one matching two (``EventLog`` and
    ``eventlog``) is no single table, so it stays unresolved. A name in
    ``exact_names`` (written quoted where quotes keep case) only matches exactly.
    """
    if table in known_table_names:
        return table
    if case_folded is None or table in exact_names:
        return None
    matches = case_folded.get(table.lower(), ())
    return matches[0] if len(matches) == 1 else None


def _resolve_qualified_table(
    qualifiers: list[str],
    table: str,
    db_name: str,
    known_table_names: set[str],
    table_schemas: Mapping[str, set[str]],
    case_folded: Mapping[str, tuple[str, ...]] | None = None,
    exact_names: frozenset[str] = frozenset(),
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

    Table names match exactly, or case-insensitively where the engine compares
    them that way (``case_folded``, see ``_known_table_name``, #742); the id
    uses the collection's own spelling.
    """
    written = ".".join([*qualifiers, table])
    known = _known_table_name(table, known_table_names, case_folded, exact_names)
    if known is None:
        return written
    table = known
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


def _resolve_table_ids(
    refs: list[tuple[list[str], str]],
    db_name: str,
    known_table_names: set[str],
    table_schemas: Mapping[str, set[str]],
    case_folded: Mapping[str, tuple[str, ...]] | None = None,
    exact_names: frozenset[str] = frozenset(),
) -> list[str]:
    """Resolve ``(qualifiers, table)`` references to collection table ids.

    System catalogs (``sys``, ``information_schema``) are skipped, a qualified
    reference goes through ``_resolve_qualified_table`` (#653), a bare name the
    collection knows becomes ``<database>.<table>``, and anything else is kept as
    written. A qualified and a bare reference to one table resolve to the same id,
    so duplicates collapse in first-seen order; no reference at all is
    ``["unknown"]``. ``case_folded`` (from ``_case_folded_names``) lets a name
    match a collected table in another case (#742), except a name in
    ``exact_names`` (``_quoted_names``).
    """
    tables_accessed: list[str] = []
    for qualifiers, table in refs:
        # Skip system catalogs / RDS-internal references
        if qualifiers and qualifiers[-1].lower() in ("sys", "information_schema"):
            continue
        if table.lower() in ("sys", "information_schema"):
            continue
        if qualifiers:
            tables_accessed.append(
                _resolve_qualified_table(
                    qualifiers,
                    table,
                    db_name,
                    known_table_names,
                    table_schemas,
                    case_folded,
                    exact_names,
                )
            )
        # Prefix with db_name if the bare-name table is known
        elif (
            known := _known_table_name(table, known_table_names, case_folded, exact_names)
        ) is not None:
            tables_accessed.append(f"{db_name}.{known}")
        else:
            tables_accessed.append(table)
    tables_accessed = list(dict.fromkeys(tables_accessed))
    return tables_accessed or ["unknown"]


def _quoted_names(refs: TableReferences, source_engine: str) -> frozenset[str]:
    """The names written quoted in one query, where a quoted name keeps its
    case (PostgreSQL, Oracle, DB2); empty for any other engine."""
    if (source_engine or "").strip().lower() not in _QUOTED_KEEPS_CASE_ENGINES:
        return frozenset()
    return frozenset(ref.name for ref in refs.tables if ref.quoted)


def _facade_table_ids(
    query_id: str,
    query_text: str,
    source_engine: str,
    db_name: str,
    known_table_names: set[str],
    table_schemas: Mapping[str, set[str]],
    case_folded: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[list[str], TableReferences, SqlReading]:
    """One query's table ids and the facade's reading from the sqlglot facade
    (#553, #796), from one parse.

    The facade picks the dialect from ``source_engine`` and falls back to the
    regex scans itself when sqlglot can't read the statement. It never raises by
    contract; if it does anyway, the regex answers are used, labelled
    ``fallback_reason="facade_error"``, and a warning is logged, so a parser
    bug can't fail a collection. Returns the resolved ids, the facade's table
    references and its whole reading (the facts the pattern stores, #934).
    """
    from src.tools.sql import facade

    try:
        reading = facade.read_sql(query_text, source_engine)
    except Exception:  # noqa: BLE001 -- the regex answer keeps the collection going
        logger.warning(
            "offline_parser: sql facade failed on query %s, using the regex scans",
            query_id,
            exc_info=True,
        )
        reading = facade.regex_reading(query_text, source_engine, "facade_error")
    refs = reading.tables
    ids = _resolve_table_ids(
        [(ref.qualifiers, ref.name) for ref in refs.tables],
        db_name,
        known_table_names,
        table_schemas,
        case_folded,
        _quoted_names(refs, source_engine),
    )
    return ids, refs, reading


_POSTGRES_ENGINES = frozenset({"postgresql", "postgres"})


def is_unlisted_sequence_read(
    query_text: str, source_engine: str, refs: TableReferences, known_table_names: set[str]
) -> bool:
    """Whether a PostgreSQL query reads a sequence's own state, not a schema table (#783).

    ``SELECT last_value FROM public.badges_id_seq`` names the sequence as if it
    were a table, but a sequence is not a schema table: when the relation it
    reads is not one the collection holds, the query has no source table, so
    its ``tables_accessed`` is ``["unknown"]`` and
    :func:`src.agents.referee.utility_statements.is_utility_statement` counts it
    as utility. A real table that happens to be named ``invoice_seq`` is in the
    schema, so it stays a table, however an unquoted reference spells its case. Only a PostgreSQL source has sequence
    relations; MySQL has none, and SQL Server and Oracle read sequences with
    ``NEXT VALUE FOR`` / ``seq.NEXTVAL`` instead.
    """
    from src.agents.referee.utility_statements import is_sequence_state_read

    if source_engine.lower() not in _POSTGRES_ENGINES or not refs.tables:
        return False
    if not is_sequence_state_read(query_text):
        return False
    folded = {name.lower() for name in known_table_names}

    def in_schema(name: str) -> bool:
        # PostgreSQL folds an unquoted name to lower case, so ``INVOICE_SEQ`` is
        # the schema's ``invoice_seq``; a quoted ``"INVOICE_SEQ"`` keeps its case.
        # The facade does not say which it read, so the query text does.
        if f'"{name}"' in query_text:
            return name in known_table_names
        return name.lower() in folded

    return not any(in_schema(ref.name) for ref in refs.tables)


def _shadow_table_refs(
    shadow: TableShadow,
    query_id: str,
    query_text: str,
    facade_ids: list[str],
    refs: TableReferences,
    source_engine: str,
    db_name: str,
    known_table_names: set[str],
    table_schemas: Mapping[str, set[str]],
    case_folded: Mapping[str, tuple[str, ...]] | None = None,
) -> None:
    """Record the regex scan's table ids next to the facade's for one query.

    The regex references go through the same resolver as the facade's, so the
    comparison is id for id. The cross-check must never break a collection:
    any error here is logged and the query is skipped.
    """
    from src.tools.sql.regex_tables import regex_table_refs

    shadow.source_engine = source_engine
    try:
        regex_ids = _resolve_table_ids(
            regex_table_refs(query_text, source_engine),
            db_name,
            known_table_names,
            table_schemas,
            case_folded,
            _quoted_names(refs, source_engine),
        )
        shadow.record(query_id, query_text, regex_ids, list(facade_ids), refs)
    except Exception:  # noqa: BLE001 -- the cross-check only observes
        logger.warning("offline_parser: sql shadow skipped query %s", query_id, exc_info=True)


def _parsed_query_facts(
    query_id: str,
    query_text: str,
    source_engine: str,
    db_name: str,
    known_table_names: set[str],
    table_schemas: Mapping[str, set[str]],
    case_folded: Mapping[str, tuple[str, ...]] | None,
    shadow: TableShadow | None = None,
) -> tuple[dict, TableReferences]:
    """The facts this parser derives from one statement's text, and the facade's
    table references (for the fallback count).

    The one place a query's ``tables_accessed``, ``query_type``, join, aggregation
    and subquery facts and its stored reading come from, for a raw collection
    (:func:`_transform_queries`) and for a collector output whose facts are read
    again (:func:`rederive_query_facts`).
    """
    from src.tools.sql.stored_facts import stored_reading

    # Table references come from the sqlglot facade (src/tools/sql/facade.py),
    # with the regex scan as its fallback, resolved to collection table ids.
    tables_accessed, refs, reading = _facade_table_ids(
        query_id,
        query_text,
        source_engine,
        db_name,
        known_table_names,
        table_schemas,
        case_folded,
    )
    facts = reading.facts

    if shadow is not None:
        _shadow_table_refs(
            shadow,
            query_id,
            query_text,
            tables_accessed,
            refs,
            source_engine,
            db_name,
            known_table_names,
            table_schemas,
            case_folded,
        )

    # After the cross-check, which compares the facade's own reading with the
    # regex scan: a sequence is not a table either of them should know about.
    if is_unlisted_sequence_read(query_text, source_engine, refs, known_table_names):
        tables_accessed = ["unknown"]

    return (
        {
            # The first verb after the leading comments, in the source dialect; a bulk
            # load (COPY ... FROM / LOAD DATA) is an INSERT (#777, #1178).
            "query_type": statement_query_type(query_text, source_engine),
            "tables_accessed": tables_accessed,
            "has_joins": facts.has_joins,
            "join_count": facts.join_count,
            "has_aggregations": facts.has_aggregation,
            "has_subqueries": facts.has_subquery,
            "parsed_facts": stored_reading(query_text, source_engine, reading),
        },
        refs,
    )


# Collector-output fields this parser never fills from a raw collection: a value a
# caller supplied there is a fact we did not derive, so it is dropped.
_UNDERIVED_QUERY_FIELDS = (
    "filter_columns",
    "sort_columns",
    "has_text_search",
    "text_search_type",
    "has_time_range_filter",
)


def rederive_query_facts(collector_output: dict, source_engine: str) -> dict:
    """A copy of a collector output whose per-query facts are read again from each
    statement's ``query_text`` by this parser, in the ``source_engine`` dialect.

    For a caller that hands in a collector output instead of a raw collection: the
    facts routing depends on (the tables a statement touches, its type, joins,
    aggregations and subqueries, its leading-comment tags) are this parser's, never
    the caller's. Execution statistics and the schema are kept as given. A table
    reference resolves to the collector output's own table id for that table name
    (or the name as written, when no single table has it); a field this parser
    never fills from a raw collection (``_UNDERIVED_QUERY_FIELDS``) is cleared.
    The input is not changed.
    """
    import copy

    from src.tools.sql.query_tags import QueryTags

    out = copy.deepcopy(collector_output)
    metadata = (out.get("metadata") or {}).get("source_database") or {}
    db_name = str(metadata.get("database_name") or "")
    tables = [
        t for t in (out.get("database_schema") or {}).get("tables") or [] if isinstance(t, dict)
    ]
    known: set[str] = set()
    schemas: dict[str, set[str]] = {}
    ids_by_name: dict[str, list[str]] = {}
    for t in tables:
        name = str(t.get("table_name") or "")
        if not name:
            continue
        known.add(name)
        if t.get("schema_name"):
            schemas.setdefault(name, set()).add(str(t["schema_name"]))
        ids_by_name.setdefault(name, []).append(str(t.get("table_id") or name))
    table_ids = {tid for ids in ids_by_name.values() for tid in ids}
    case_folded = _case_folded_names(known, source_engine)

    def table_id(resolved: str) -> str:
        if resolved in table_ids:
            return resolved
        prefix = f"{db_name}."
        if resolved.startswith(prefix):
            ids = ids_by_name.get(resolved[len(prefix) :], [])
            if len(ids) == 1:
                return ids[0]
        return resolved

    rows: list[dict] = []
    for pattern in (out.get("queries") or {}).get("query_patterns") or []:
        if not isinstance(pattern, dict):
            continue
        query_text = str(pattern.get("query_text") or "")
        facts, _ = _parsed_query_facts(
            str(pattern.get("query_id") or ""),
            query_text,
            source_engine,
            db_name,
            known,
            schemas,
            case_folded,
        )
        facts["tables_accessed"] = list(
            dict.fromkeys(table_id(t) for t in facts["tables_accessed"])
        )
        # The collector output's query type, mapped as a raw row's is (REPLACE is an INSERT).
        qtype = facts["query_type"]
        facts["query_type"] = "INSERT" if qtype == "REPLACE" else (qtype or "SELECT")
        row = {**pattern, **facts, **QueryTags.of([query_text], source_engine).fields()}
        for field in _UNDERIVED_QUERY_FIELDS:
            row[field] = None
        rows.append(row)
    queries = out.setdefault("queries", {})
    queries["query_patterns"] = rows
    return out


def _transform_queries(
    raw: list[dict],
    db_name: str,
    known_table_names: set[str],
    table_schemas: Mapping[str, set[str]] | None = None,
    shadow: TableShadow | None = None,
    source_engine: str = "",
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

    ``tables_accessed`` comes from the sqlglot facade, read in the
    ``source_engine`` dialect (see ``_facade_table_ids``, #553). ``shadow``,
    when given, also gets the regex scan's table ids for every query (see
    ``_shadow_table_refs``); the facade's answer is the one returned either way.
    A PostgreSQL sequence-state read of a relation the schema does not hold
    (``SELECT last_value FROM public.badges_id_seq``) has no source table, so
    its ``tables_accessed`` is ``["unknown"]`` (``is_unlisted_sequence_read``, #783).
    """
    import hashlib

    patterns = []
    fallbacks: Counter[str] = Counter()
    case_folded = _case_folded_names(known_table_names, source_engine)
    for r in _merge_query_variants(raw, source_engine):
        query_text = str(r.get("query_text") or "")
        exec_count = r.get("execution_count") or 1
        total_rows_sent = r.get("total_rows_sent") or 0
        total_rows_examined = r.get("total_rows_examined") or 0
        total_rows_affected = r.get("total_rows_affected") or 0
        total_time_ms = float(r.get("total_time_ms") or 0)

        digest = r.get("digest") or hashlib.sha256(query_text.encode()).hexdigest()[:16]

        facts, refs = _parsed_query_facts(
            str(digest),
            query_text,
            source_engine,
            db_name,
            known_table_names,
            table_schemas or {},
            case_folded,
            shadow,
        )
        if refs.fallback_reason:
            fallbacks[refs.fallback_reason] += 1

        patterns.append(
            {
                "query_id": digest,
                "query_text": query_text,
                "query_type": facts["query_type"],
                # Every merged row's leading comment tags (#1180).
                "query_tags": r["query_tags"],
                "query_tags_dropped": r["query_tags_dropped"],
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
                "tables_accessed": facts["tables_accessed"],
                "has_joins": facts["has_joins"],
                "join_count": facts["join_count"],
                "scan_efficiency_pct": min(
                    round(total_rows_sent / max(total_rows_examined, 1) * 100, 2), 100.0
                ),
                "has_aggregations": facts["has_aggregations"],
                "has_subqueries": facts["has_subqueries"],
                "parsed_facts": facts["parsed_facts"],
                "errors": r.get("sum_errors") or 0,
                "warnings": r.get("sum_warnings") or 0,
                "first_seen": r.get("first_seen"),
                "last_seen": r.get("last_seen"),
            }
        )
    if fallbacks:
        logger.info(
            "offline_parser: table references: %d/%d queries used the regex fallback (%s)",
            sum(fallbacks.values()),
            len(patterns),
            dict(sorted(fallbacks.items())),
        )
    return patterns


# Default port per engine, for an offline collection's CollectorInput (mode="offline"
# never actually connects, so this is cosmetic/display only).
_ENGINE_PORTS: dict[str, int] = {
    "mysql": 3306,
    "mariadb": 3306,
    "postgresql": 5432,
    "sqlserver": 1433,
    "oracle": 1521,
    "db2": 50000,
}


# Spellings of an engine id that collection scripts write, mapped to the id the
# collector input contract accepts (``DatabaseEngine``): ``"postgres"`` used to be
# returned as is and then refused by ``CollectorInput`` (#1177).
_ENGINE_ALIASES: dict[str, str] = {"postgres": "postgresql"}


def recognised_source_engine(metadata: dict | None) -> tuple[str, int] | None:
    """``(engine, port)`` when an offline collection's metadata names its engine, else None.

    ``metadata.engine`` is checked first -- the SQL Server collector script sets it
    explicitly (#381: it used to be ignored, so every SQL Server offline collection was
    mislabelled ``mysql``). Then the server version string (``version``, or
    ``version_full``/``product_version`` when the SQL Server script wrote those instead,
    #381): PostgreSQL, SQL Server, Oracle, DB2 and MariaDB banners are recognised
    (``scripts/collect-mysql.sql`` writes only ``VERSION()``, so a MariaDB server's
    collection says ``10.11.6-MariaDB`` and nothing else, #1177). A plain MySQL version
    number (``8.0.36``) is not recognised: it names no engine.
    """
    meta = metadata or {}
    engine = str(meta.get("engine") or "").strip().lower()
    engine = _ENGINE_ALIASES.get(engine, engine)
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
    if "db2" in version:
        return "db2", 50000
    if "mariadb" in version:
        return "mariadb", 3306
    return None


def detect_source_engine(metadata: dict | None) -> tuple[str, int]:
    """Return ``(engine, port)`` for an offline collection from its metadata.

    :func:`recognised_source_engine`, else MySQL, the original (pre-#381) default for
    every offline collection.

    This is the one source-engine detector for every ingest path (#1177): the
    library ingest, the scripts and the collector handler all call it, and
    ``tests/unit/tools/database/test_one_source_engine_detector.py`` fails on a
    second, inline guess anywhere in ``src/`` or ``scripts/``.
    """
    return recognised_source_engine(metadata) or ("mysql", 3306)


def normalize_engine(engine: str | None) -> str:
    """An engine id as the collector input contract spells it (``"postgres"`` reads as
    ``"postgresql"``), lower-cased; ``""`` for none."""
    key = str(engine or "").strip().lower()
    return _ENGINE_ALIASES.get(key, key)


def default_port(engine: str) -> int | None:
    """The default port of a source engine id (``"postgres"`` reads as
    ``"postgresql"``), or ``None`` for an engine this module does not know."""
    return _ENGINE_PORTS.get(normalize_engine(engine))


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
