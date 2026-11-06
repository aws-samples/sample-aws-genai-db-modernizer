# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Offline Collection Parser

Reads the JSON output from collect-mysql.sql and transforms it into the
same raw data structures that MySQLRemoteCollector produces, so the
existing _build_tables, _build_queries, etc. work unchanged.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections import Counter
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import boto3

from src.agents.collector.routine_rows import merge_trigger_rows
from src.shared.rate_window import (
    RateSample,
    ServerWindow,
    rate_sample,
    server_window,
    statement_rate_fields,
)
from src.shared.sample_rows import without_sample_rows
from src.shared.sql_spans import span_dialect, span_settings_of, with_span_settings
from src.tools.database.index_keys import add_index_key, expression_key_fields
from src.tools.database.offline_collection_check import (
    read_collection_text,
    validate_offline_collection,
    warn_absent_sections,
    without_sentinels,
)
from src.tools.database.routine_links import routine_id
from src.tools.sql.query_type import statement_query_type
from src.tools.sql.redact import (
    raw_collection_settings,
    redact_collector_output,
    redact_definition,
    redact_literals,
)
from src.tools.sql.view_lineage import apply_view_lineage
from src.tools.validation.ingestion_limits import check_collection_counts

if TYPE_CHECKING:
    from src.tools.sql.facade import SqlReading, TableReferences
    from src.tools.sql.shadow import TableShadow

logger = logging.getLogger(__name__)


class DatabaseNameMismatchError(ValueError):
    """The name a run gives the database is ambiguous for its collection (#1572).

    Raised only when the collection states no database name and its statements
    belong to several databases, none of them the given name: routing never
    guesses which database such a run is about."""


def _quoted_list(names: list[str]) -> str:
    return ", ".join(f"'{n}'" for n in names)


def collected_database_name(
    metadata: object, rows: list[dict], database_name: str | None = None
) -> str | None:
    """The database ``rows`` (a raw collection's statements) and its tables belong
    to, the qualifier of every table id (``<database>.<table>``, #1572); None
    when neither the collection nor ``database_name`` names one.

    The collection's own ``metadata.database_name`` always wins: ``database_name``
    (the name the run was given: ``--db``, the collection file name, ``assess()``'s
    ``database_name``) only names the run, so a different name routes the same. A
    run named after another database the statements belong to (SQL Server's
    statistics are server-wide, #382) logs a warning; that database's statements
    are dropped either way. With no stated name, ``database_name`` names the
    database, unless the statements say otherwise: one database is that database;
    several, none of them ``database_name``, are ambiguous.

    Raises:
        DatabaseNameMismatchError: no stated name, and the statements belong to
            several databases, none of them ``database_name``.
    """
    stated = metadata.get("database_name") if isinstance(metadata, dict) else None
    stated = str(stated) if stated not in (None, "") else None
    if database_name is None or (
        stated is not None and database_name.casefold() == stated.casefold()
    ):
        return stated
    by_fold: dict[str, str] = {}
    for row in rows:
        name = row.get("database_name") if isinstance(row, dict) else None
        if name:
            by_fold.setdefault(str(name).casefold(), str(name))
    if stated is not None:
        if database_name.casefold() in by_fold:
            logger.warning(
                "offline_parser: the run is named %r, another database the collection's "
                "statements belong to; the collection is of %r, which keys its tables, "
                "and %r's statements are dropped",
                database_name,
                stated,
                by_fold[database_name.casefold()],
            )
        return stated
    if not by_fold or database_name.casefold() in by_fold:
        return database_name
    others = sorted(by_fold.values(), key=str.casefold)
    if len(others) == 1:
        return others[0]
    raise DatabaseNameMismatchError(
        f"The collection states no database name, its statements belong to the "
        f"databases {_quoted_list(others)}, and this run names it '{database_name}', "
        "none of them. Name the run after the database the collection was taken from."
    )


def fetch_offline_json(bucket: str, key: str, region: str = "us-east-1") -> dict:
    """Download the offline collection JSON from S3, whatever its size (#384); its
    table and query counts are checked when it is parsed (#142)."""
    s3 = boto3.client("s3", region_name=region)
    resp = s3.get_object(Bucket=bucket, Key=key)
    # One reader for every path (#131): the object found in the text, Oracle 19c's
    # unescaped control characters made spaces (newlines kept), the trailing
    # ``{"_sentinel": true}`` the PL/SQL script ends each list with dropped, and a
    # decode error placed at its line and column of the file.
    return read_collection_text(resp["Body"].read().decode("utf-8"), source=f"s3://{bucket}/{key}")


def parse_offline_collection(
    data: dict,
    shadow: TableShadow | None = None,
    *,
    database_name: str | None = None,
    source: str = "passed in",
) -> dict:
    """:func:`_parse_offline_collection`, with every statement and definition read with
    the source settings the collection reports (``metadata.settings``: MySQL's
    ``sql_mode``, PostgreSQL's ``standard_conforming_strings``, #1482): a string is read,
    redacted and grouped the way the source reads it. A collection that reports none
    is read with each engine's defaults.

    A trailing Oracle ``{"_sentinel": true}`` entry is dropped from each list, then
    ``data`` passes the input check
    (:func:`~src.tools.database.offline_collection_check.validate_offline_collection`,
    #131), before anything is transformed; ``source`` names the file in its messages.
    The common sections the collection lacks read as empty and are listed in the
    result's ``absent_sections``.

    Raises:
        OfflineCollectionError: ``data`` is not an offline collection, or has
            sections or entries the parser cannot read (every one listed).
    """
    # The Oracle script's end-of-list sentinels never count as entries (#131): dropped
    # here too for a collection given in memory (AWS Transform ingest, ``assess()``).
    data = without_sentinels(data)
    absent = validate_offline_collection(data, source=source)
    warn_absent_sections(data, absent, source=source)
    parsed = _parse_with_source_settings(data, shadow, database_name=database_name)
    if absent:
        parsed["absent_sections"] = list(absent)
    return parsed


def _parse_with_source_settings(
    data: dict, shadow: TableShadow | None, *, database_name: str | None
) -> dict:
    metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    engine = detect_source_engine(metadata if isinstance(metadata, dict) else None)[0]
    with with_span_settings(raw_collection_settings(engine, metadata)):
        return _parse_offline_collection(data, shadow, database_name=database_name)


def _fk_position(row: dict) -> tuple[bool, int]:
    """Sort key for a foreign key row: its position in the constraint, rows without one last."""
    position = row.get("ordinal_position")
    if position is None:
        position = row.get("constraint_column_id")
    if position is None:
        return (True, 0)
    try:
        return (False, int(position))
    except (TypeError, ValueError):
        return (True, 0)


def _parse_offline_collection(
    data: dict, shadow: TableShadow | None = None, *, database_name: str | None = None
) -> dict:
    """
    Transform the flat offline JSON into the nested structure expected
    by mysql_collector's builder functions.

    ``database_name`` is the name the run gives the database (#1572). It never
    replaces the collection's own name: table references are qualified with the
    database the collection holds (:func:`collected_database_name`), recorded as
    ``collected_database_name`` for the builder to key the tables the same way.

    Every query's table references and its ``has_joins``/``join_count``/
    ``has_aggregations``/``has_subqueries`` facts come from the sqlglot facade
    (:func:`src.tools.sql.facade.read_sql`, #553, #796), read in the
    collection's source-engine dialect; the regex scans are the facade's own
    fallback. ``shadow``, when given, also runs the regex scan on every query
    and records where it disagrees with the facade (#557). It only observes:
    the returned structure is the same with or without it.

    Returns dict with keys: metadata, tables (list of table dicts with
    nested columns/indexes/fks/pk), views, procedures, triggers,
    queries, global_stats, extensions.

    Raises:
        IngestionLimitError: the collection's table or query count exceeds the
            configured ingestion limit (#142). Checked here, before the more
            expensive nested column/index/FK/PK structures are built below.
        DatabaseNameMismatchError: ``database_name`` is ambiguous for the
            collection (see :func:`collected_database_name`).
    """
    check_collection_counts(
        table_count=len(data.get("tables") or []),
        query_count=len(data.get("queries") or []),
        source="offline collection",
    )
    # Room for every statement's facts, so a large collection reads each text once (#384).
    from src.tools.sql.query_facts import reserve_fact_caches

    reserve_fact_caches(len(data.get("queries") or []))

    metadata = data.get("metadata", {})
    if isinstance(metadata, str):
        metadata = json.loads(metadata)  # an object: the input check read it (#131)

    # Group columns, indexes, FKs, PKs by table_name
    columns_by_table: dict[str, list] = {}
    for c in data.get("columns") or []:
        columns_by_table.setdefault(c["table_name"], []).append(c)

    source_engine = detect_source_engine(metadata if isinstance(metadata, dict) else None)[0]
    indexes_by_table: dict[str, dict[str, dict]] = {}
    # An index's column order is the key's position (``seq_in_index``; SQL Server's
    # ``key_ordinal``), not the row order: MySQL's JSON_ARRAYAGG drops the subquery's
    # ORDER BY (#1927). The sort is stable and moves a row whole, so ``columns`` and
    # ``prefix_lengths`` stay aligned. Rows without a position keep their order, so old
    # collections load as before; SQL Server writes 0 for an included column, which
    # follows the key columns.
    for i in sorted(data.get("indexes") or [], key=_index_key_position):
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
                "prefix_lengths": [],
                **expression_key_fields(i),
            }
        # An expression key names no column (#295): the index still counts, and is
        # flagged rather than narrowed to its plain keys, whatever the engine.
        add_index_key(indexes_by_table[tbl][idx_name], i, source_engine)
    for by_name in indexes_by_table.values():
        for index in by_name.values():
            if not any(n is not None for n in index["prefix_lengths"]):
                index["prefix_lengths"] = None

    fks_by_table: dict[str, dict[str, dict]] = {}
    # A foreign key's column order is its position in the constraint (``ordinal_position``;
    # SQL Server's ``constraint_column_id``), not the row order: MySQL's JSON_ARRAYAGG
    # drops the subquery's ORDER BY (#1868). The sort is stable and moves a row as a whole,
    # so ``columns`` and ``referenced_columns`` stay aligned, and rows without a position
    # keep their order.
    for fk in sorted(data.get("foreign_keys") or [], key=_fk_position):
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
    # The key's order is its ``ordinal_position``, not the row order: MySQL's
    # JSON_ARRAYAGG drops the subquery's ORDER BY (#1865). The sort is stable, so rows
    # without a position keep their order.
    for pk in sorted(
        data.get("primary_keys") or [],
        key=lambda r: (r.get("ordinal_position") is None, r.get("ordinal_position") or 0),
    ):
        pks_by_table.setdefault(pk["table_name"], []).append(pk["column_name"])

    # Assemble tables with nested children. The one qualifier of every table id,
    # here and in the builder (#1572).
    collected = collected_database_name(metadata, data.get("queries") or [], database_name)
    db_name = collected or "unknown"
    source_engine = detect_source_engine(metadata if isinstance(metadata, dict) else None)[0]
    known_table_names: set[str] = set()
    table_schemas: dict[str, set[str]] = {}
    tables = []
    # #387: no row data is kept. A collection written before #387 (or edited by hand)
    # may carry sample rows on its tables; they are dropped here and counted.
    raw_tables, sample_rows_dropped = without_sample_rows(data.get("tables") or [])
    for t in raw_tables:
        tbl_name = t["table_name"]
        qualifier = offline_table_qualifier(source_engine, t.get("schema_name"), db_name)
        table_id = t.get("table_id") or f"{qualifier}.{tbl_name}"
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
            }
        )

    # Parse global stats into the format collect_global_stats() returns
    raw_stats = data.get("global_stats") or {}
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

    raw_queries, dropped_other_db = _drop_other_database_queries(
        _with_routines(data.get("queries") or [], data), db_name
    )
    other_db_note = {"queries_dropped_other_database": dropped_other_db} if dropped_other_db else {}
    sample_rows_note: dict[str, int] = {}
    if sample_rows_dropped:
        logger.warning(
            "offline_parser: dropped %d sample row(s) from the collection's tables; "
            "no output of the parser holds them (#387)",
            sample_rows_dropped,
        )
        sample_rows_note = {"sample_rows_dropped": sample_rows_dropped}

    routines = _routine_rows(data, metadata, source_engine)
    return {
        **other_db_note,
        **sample_rows_note,
        "metadata": metadata,
        # #384: the statement rows the collection script wrote, which its cap counts.
        "script_query_rows": len(data.get("queries") or []),
        "collected_database_name": collected,
        "tables": tables,
        # #1625: string literals in stored definitions are redacted too.
        "views": _redacted_definitions(data.get("views", []), source_engine),
        "procedures": routines["procedures"],
        # #1651: one row per trigger, with every event (PostgreSQL writes one per event).
        "triggers": routines["triggers"],
        # #1652: MySQL scheduled events, how routine bodies and execution counts were read.
        "events": routines["events"],
        "routine_capture": routines["routine_capture"],
        "routine_stats_status": routines["routine_stats_status"],
        "queries": _transform_queries(
            raw_queries,
            db_name,
            known_table_names,
            table_schemas=table_schemas,
            shadow=shadow,
            source_engine=source_engine,
            # #1893: the window a statement with no usable observed window falls back on.
            server_window=_collection_server_window(data, source_engine),
            # #1908: the script's two snapshots of the counters, when it took them.
            sample=_collection_rate_sample(data, source_engine),
            first_counts=_collection_first_counts(data),
        ),
        "global_stats": global_stats,
        "io_stats": data.get("io_stats", {}),
        "wait_events": data.get("wait_events", []),
        "os_stats": data.get("os_stats", {}),
        # #1078: the PostgreSQL script's installed extensions; None when the collection
        # has no such section (written before #1078, or another engine's).
        "extensions": data.get("extensions"),
        # #1834: the PostgreSQL script's enum types and labels; None when not collected.
        "enum_types": data.get("enum_types"),
    }


def _index_key_position(row: dict) -> tuple[bool, int]:
    """Sort key for an index row: its position in the index, rows without one last.

    ``seq_in_index`` is MySQL's (from 1), PostgreSQL's (``array_position`` on ``indkey``,
    from 0) and Oracle's. SQL Server's ``key_ordinal`` is 0 for an included column, which
    follows the key columns.
    """
    included = False
    position = row.get("seq_in_index")
    if position is None:
        position = row.get("key_ordinal")
        included = True
    try:
        number = int(position)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return (True, 0)
    return (included and number <= 0, number)


def _routine_rows(data: Mapping, metadata: object, source_engine: str) -> dict[str, Any]:
    """The collection's procedure, trigger and event rows as the builders read them
    (#1652), every definition and comment redacted (#1625):

    - one row per trigger with every event (#1651);
    - Oracle bodies joined from their ``routine_sources`` lines (``ALL_SOURCE``);
    - the execution counts the engine keeps (``routine_stats``) on their routines;
    - each row's ``body_status`` as the collection gives it, ``not_supported`` when its
      CREATE cannot be made (PostgreSQL and MySQL), with its reason, and whether a MySQL
      rebuild is exact (``body_exact``).

    The CREATE statements themselves are never read through here: an ingest
    writes them to the job's routine sources from the raw file
    (:func:`src.agents.collector.routine_sources.iter_collection_sources`).
    """
    from src.agents.collector.routine_rows import (
        attach_routine_sources,
        attach_routine_stats,
        routine_stats_status,
    )
    from src.agents.collector.routine_sources import (
        apply_body_statuses,
        iter_routine_sources,
        routine_capture,
    )

    raw_procedures = data.get("procedures", [])
    raw_triggers = _merged_triggers(data.get("triggers", []))
    if not isinstance(raw_procedures, list) or not isinstance(raw_triggers, list):
        # Not a list: left for the collection check to report, as before.
        return {
            "procedures": _redacted_definitions(raw_procedures, source_engine),
            "triggers": _redacted_definitions(raw_triggers, source_engine),
            "events": [],
            "routine_capture": None,
            "routine_stats_status": None,
        }
    procedures = [dict(p) if isinstance(p, dict) else p for p in raw_procedures]
    triggers = [dict(t) if isinstance(t, dict) else t for t in raw_triggers]
    events = [dict(e) for e in data.get("events") or [] if isinstance(e, dict)]
    attach_routine_sources(procedures, triggers, data.get("routine_sources"))
    attach_routine_stats(procedures, triggers, data.get("routine_stats"))
    from src.agents.collector.routine_sources import RoutineSourcesBuild

    notes = RoutineSourcesBuild()
    for _ in iter_routine_sources(
        source_engine,
        procedures=procedures,
        triggers=triggers,
        events=events,
        parameters=data.get("routine_parameters"),
        notes=notes,
    ):
        pass  # only each routine's status and exactness are kept here, never its CREATE
    apply_body_statuses(
        notes, source_engine, procedures=procedures, triggers=triggers, events=events
    )
    capture = routine_capture(
        metadata.get("routine_capture") if isinstance(metadata, dict) else None,
        listed=len(procedures) + len(triggers) + len(events),
        parameters=data.get("routine_parameters"),
    )
    return {
        "procedures": _redacted_comments(
            _redacted_definitions(procedures, source_engine), "routine_comment", source_engine
        ),
        "triggers": _redacted_definitions(triggers, source_engine),
        "events": _redacted_comments(
            _redacted_definitions(events, source_engine), "event_comment", source_engine
        ),
        "routine_capture": capture,
        "routine_stats_status": routine_stats_status(data.get("routine_stats_status")),
    }


def _redacted_comments(items: object, key: str, source_engine: str) -> object:
    """Rows with their ``key`` comment redacted as any comment is (#1625): a value can
    hide in a routine's or an event's comment."""
    from src.tools.sql.redact import redact_comment

    if not isinstance(items, list):
        return items
    return [
        (
            {**item, key: redact_comment(item[key], source_engine)}
            if isinstance(item, dict) and isinstance(item.get(key), str)
            else item
        )
        for item in items
    ]


def _merged_triggers(rows: object) -> object:
    """The trigger rows merged to one per trigger (:func:`merge_trigger_rows`, #1651);
    a section that is not a list is left for the collection check to report."""
    return merge_trigger_rows(rows) if isinstance(rows, list) else rows


def _redacted_definitions(items: object, source_engine: str) -> object:
    """View, procedure or trigger rows with each ``definition``'s string literals
    redacted (:func:`src.tools.sql.redact.redact_definition`, #1625)."""
    if not isinstance(items, list):
        return items
    return [
        (
            {**item, "definition": redact_definition(item["definition"], source_engine)}
            if isinstance(item, dict) and isinstance(item.get("definition"), str)
            else item
        )
        for item in items
    ]


def _with_routines(raw: list[dict], data: Mapping) -> list[dict]:
    """``raw`` with each row's ``parent_routine_ids`` (#1891): the routines the
    collection's ``query_routines`` section links to its ``digest`` (SQL Server's
    ``sys.dm_exec_sql_text(sql_handle).objectid``), sorted; ``[]`` for a row it links to
    none. A collection with no such section (written before #1891, or another engine's)
    records none, so its rows are returned unchanged and the field stays None."""
    links = data.get("query_routines")
    if not isinstance(links, list):
        return raw
    by_digest: dict[str, set[str]] = {}
    for link in links:
        if not isinstance(link, Mapping) or not link.get("digest"):
            continue
        rid = routine_id(link.get("routine_schema"), link.get("routine_name"))
        if rid:
            by_digest.setdefault(str(link["digest"]), set()).add(rid)
    return [
        (
            {**r, "parent_routine_ids": sorted(by_digest.get(str(r.get("digest") or ""), ()))}
            if isinstance(r, dict)
            else r
        )
        for r in raw
    ]


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
    before building the patterns list. Rows merge when they share a digest.
    For Oracle they also merge when their texts share a literal shape
    (:func:`src.tools.sql.normalize.literal_shape`, #412): ``SQL_ID`` hashes the
    exact text, so a collection made without ``FORCE_MATCHING_SIGNATURE`` has a
    digest per literal value, and two signatures can differ only in the length
    of an ``IN`` list. A merged Oracle row takes the digest of its costliest
    variant. Statements that differ in structure never merge.

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
    The tags are read from each text with its literals redacted
    (:func:`src.tools.sql.redact.redact_literals`, #1625); everything else here reads
    the texts as collected: the ``digest`` of a row with none is the SHA-256 of its
    text as collected, so redaction never changes a ``query_id``, and Oracle's shapes
    and cut-off texts are compared as collected.
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

    if span_dialect(source_engine) == "oracle":
        order = _merge_oracle_shapes(groups, order)

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
            (redact_literals(str(v.get("query_text") or ""), source_engine) for v in variants),
            source_engine,
        ).fields()
        nested = _nested(variants)
        if len(variants) == 1:
            row = dict(variants[0])
            row["digest"] = key
            row["nested"] = nested
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
        # #1893: PostgreSQL 17's per-entry window, the oldest variant's (as first_seen).
        ages = [
            float(v["stats_since_age_seconds"])
            for v in variants
            if isinstance(v.get("stats_since_age_seconds"), int | float)
        ]
        if ages:
            merged["stats_since_age_seconds"] = max(ages)
        # #1908: the first-snapshot count of the variants that were in the first
        # snapshot; a variant only in the second added all its calls in the interval.
        starts = [
            float(v["sample_first_count"])
            for v in variants
            if isinstance(v.get("sample_first_count"), int | float)
            and not isinstance(v.get("sample_first_count"), bool)
        ]
        merged["sample_first_count"] = int(sum(starts)) if starts else None

        # A shape ran in every routine any of its variants ran in (#1891).
        if any("parent_routine_ids" in v for v in variants):
            merged["parent_routine_ids"] = sorted(
                {str(rid) for v in variants for rid in v.get("parent_routine_ids") or []}
            )
        merged["nested"] = nested
        merged.update(tags)
        merged_rows.append(merged)

    return merged_rows


def _nested(variants: list[dict]) -> bool | None:
    """PostgreSQL's ``toplevel`` read backwards (#1652): True when any variant ran inside
    a function or procedure (``pg_stat_statements.track = all`` counts those apart),
    False when every reported one ran at top level, None when the collection does not
    report it (an older script, another engine, PostgreSQL before 14)."""
    seen = [v.get("toplevel") for v in variants if v.get("toplevel") is not None]
    if not seen:
        return None
    return any(t is False or str(t).strip().lower() in ("false", "f") for t in seen)


# The collection script cuts Oracle statement text at this many characters
# (``DBMS_LOB.SUBSTR(SQL_FULLTEXT, 2000, 1)`` in scripts/collect-oracle.sql).
ORACLE_TEXT_CAP = 2000
# A truncated shape shorter than this is too little to call two statements the same.
_MIN_TRUNCATED_PREFIX = 100
_PARTIAL_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*$")


def _stable_prefix(shape: str) -> str:
    """A truncated shape without its last, possibly cut, word, or, when the cut falls inside
    an ``IN (...)`` list, the text before that list (#1541); ``\0`` (matches nothing) when
    too little is left to compare."""
    from src.tools.sql.normalize import open_in_list_start

    in_list = open_in_list_start(shape, "oracle")
    if in_list is not None:
        prefix = shape[: in_list + 1]
    else:
        prefix = _PARTIAL_WORD_RE.sub("", shape.rstrip())
    return prefix if len(prefix) >= _MIN_TRUNCATED_PREFIX else "\0"


def _merge_oracle_shapes(groups: dict[str, list[dict]], order: list[str]) -> list[str]:
    """Fold digest groups whose texts share a literal shape into one group (in place).

    The surviving key is the digest of the group's costliest variant (the first one on a
    tie), so a merged row is named like the sample text it keeps. Returns the new order,
    by first appearance. A group whose rows differ in shape (one digest, several shapes)
    is left whole: a shared digest is already the collection's own statement of identity.
    """
    from src.tools.sql.normalize import literal_shape

    by_shape: dict[str, list[str]] = {}
    truncated: set[str] = set()
    for key in order:
        shapes = {literal_shape(str(v.get("query_text") or ""), "oracle") for v in groups[key]}
        if len(shapes) != 1:
            by_shape.setdefault(f"digest:{key}", []).append(key)
            continue
        (shape,) = shapes
        by_shape.setdefault(shape, []).append(key)
        if all(len(str(v.get("query_text") or "")) >= ORACLE_TEXT_CAP for v in groups[key]):
            truncated.add(shape)

    # Old collections cut every statement at the cap, so two literal variants of a long
    # statement are cut at different points and their masked texts differ at the end (#1518).
    # Merge truncated shapes that agree up to the shorter cut, shortest first.
    anchors: list[str] = []
    for shape in sorted(truncated, key=len):
        anchor = next((a for a in anchors if shape.startswith(_stable_prefix(a))), None)
        if anchor is None:
            anchors.append(shape)
            continue
        by_shape[anchor].extend(by_shape.pop(shape))
    # Keep the row order of the variants and of the merged shapes.
    order_of = {key: i for i, key in enumerate(order)}
    by_shape = {
        shape: sorted(keys, key=order_of.__getitem__)
        for shape, keys in sorted(by_shape.items(), key=lambda kv: min(order_of[k] for k in kv[1]))
    }

    new_groups: dict[str, list[dict]] = {}
    new_order: list[str] = []
    for keys in by_shape.values():
        variants = [v for key in keys for v in groups[key]]
        name = keys[0]
        if len(keys) > 1:
            name = max(
                keys, key=lambda k: max(float(v.get("total_time_ms") or 0) for v in groups[k])
            )
        new_groups[name] = variants
        new_order.append(name)
    groups.clear()
    groups.update(new_groups)
    # ``by_shape`` is in order of first appearance of each shape's first digest.
    return new_order


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


POSTGRES_DEFAULT_SCHEMA = "public"


def _is_postgres(source_engine: str | None) -> bool:
    return normalize_engine(source_engine) == "postgresql"


def offline_table_qualifier(source_engine: str | None, schema_name: object, db_name: str) -> str:
    """The qualifier of an offline table's id: ``<qualifier>.<table>`` (#1903).

    A PostgreSQL table is named after its own schema (``public.topics``), as the live
    collector names it: ``collect-postgresql.sql`` reports each table's
    ``schema_name``, and a table without one (a collection written by hand) is in
    ``public``, where PostgreSQL's default ``search_path`` finds a bare name. Every
    other engine keeps the database (MySQL's database is its schema; SQL Server's and
    Oracle's ids stay ``<database>.<table>``, #382).
    """
    if _is_postgres(source_engine):
        return str(schema_name or "").strip() or POSTGRES_DEFAULT_SCHEMA
    return db_name


def _bare_table_id(
    table: str, db_name: str, table_schemas: Mapping[str, set[str]], source_engine: str = ""
) -> str | None:
    """The id of collected table ``table`` for a reference that names no schema.

    ``<database>.<table>`` outside PostgreSQL. A PostgreSQL table in one schema is
    that schema's; a name collected in several is ``public``'s when ``public`` is one
    of them (the default ``search_path``), else ambiguous (None) (#1903).
    """
    if not _is_postgres(source_engine):
        return f"{db_name}.{table}"
    schemas = sorted({s for s in table_schemas.get(table, ()) if s})
    if len(schemas) <= 1:
        own = schemas[0] if schemas else None
        return f"{offline_table_qualifier(source_engine, own, db_name)}.{table}"
    if POSTGRES_DEFAULT_SCHEMA in schemas:
        return f"{POSTGRES_DEFAULT_SCHEMA}.{table}"
    return None


def _resolve_qualified_table(
    qualifiers: list[str],
    table: str,
    db_name: str,
    known_table_names: set[str],
    table_schemas: Mapping[str, set[str]],
    case_folded: Mapping[str, tuple[str, ...]] | None = None,
    exact_names: frozenset[str] = frozenset(),
    source_engine: str = "",
) -> str:
    """The table id for a qualified reference ``[database.]schema.table`` (#653).

    ``_build_tables`` keys every offline table outside PostgreSQL as
    ``<database>.<table>``, whatever schema the collection reports for it, so
    ``[Audit].[EventLog]`` has to become ``trailgear.EventLog`` to match its own
    table recommendation. The qualifier must name that table's own schema (compared case-insensitively, as SQL Server and
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

    A PostgreSQL table's id is ``<schema>.<table>`` (#1903), so there the qualifier
    picks the table among its schemas: ``audit.events`` is the ``audit`` table even
    when ``public`` has one of that name too.
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
    if _is_postgres(source_engine):
        spelled = {s.lower(): s for s in table_schemas.get(table, ()) if s}
        if qualifier in spelled:
            return f"{spelled[qualifier]}.{table}"
        if not spelled or qualifier == db_name.lower():
            return _bare_table_id(table, db_name, table_schemas, source_engine) or written
        return written
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
    source_engine: str = "",
) -> list[str]:
    """Resolve ``(qualifiers, table)`` references to collection table ids.

    A system catalog table (``information_schema.columns``, ``sys.objects``) is a
    table the statement reads, so it is named as written (#483): routing and the
    waves already treat it as an engine object
    (``table_resolution.is_engine_system_object``). A qualified
    reference goes through ``_resolve_qualified_table`` (#653), a bare name the
    collection knows becomes its table id (``_bare_table_id``: ``<database>.<table>``,
    or ``<schema>.<table>`` for PostgreSQL, #1903), and anything else is kept as
    written. A qualified and a bare reference to one table resolve to the same id,
    so duplicates collapse in first-seen order; no reference at all is
    ``["unknown"]``. ``case_folded`` (from ``_case_folded_names``) lets a name
    match a collected table in another case (#742), except a name in
    ``exact_names`` (``_quoted_names``).
    """
    tables_accessed: list[str] = []
    for qualifiers, table in refs:
        # A bare ``sys`` or ``information_schema`` is a schema name read as a table
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
                    source_engine,
                )
            )
        # A bare name the collection knows is that table's id
        elif (
            known := _known_table_name(table, known_table_names, case_folded, exact_names)
        ) is not None:
            tables_accessed.append(
                _bare_table_id(known, db_name, table_schemas, source_engine) or table
            )
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
        source_engine,
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
            source_engine,
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
    Every query text, tag and definition is redacted first
    (:func:`src.tools.sql.redact.redact_collector_output`, #1625), so the facts are
    read from the text that is kept, and every text is read with the collection's source
    settings (``metadata.source_database.settings``, #1482). The input is not changed.
    """
    with with_span_settings(span_settings_of(collector_output)):
        return _rederive_query_facts(collector_output, source_engine)


def _rederive_query_facts(collector_output: dict, source_engine: str) -> dict:
    from src.tools.sql.query_tags import QueryTags

    out = copy.deepcopy(dict(redact_collector_output(collector_output)))
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
        # The parser qualifies a known table with the database, or, for PostgreSQL,
        # with its schema (#1903); an output saved before then, or another builder's,
        # may key the same table otherwise (``discourse.topics``, ``public.topics``).
        if resolved in table_ids:
            return resolved
        qualifier, _, name = resolved.rpartition(".")
        ids = ids_by_name.get(name, [])
        own = (
            {db_name.lower(), POSTGRES_DEFAULT_SCHEMA}
            if _is_postgres(source_engine)
            else {db_name.lower()}
        )
        own |= {s.lower() for s in schemas.get(name, ())}
        if len(ids) == 1 and qualifier.lower() in own:
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
    # #1875: a statement on a view is scored on the view's base tables, as in a collection.
    for row in rows:
        for key in ("views_read", "view_resolved_text", "view_text_form", "view_text_fallback"):
            row[key] = None
    resolved: dict = apply_view_lineage(out, source_engine)
    return resolved


def _collection_server_window(data: dict, source_engine: str) -> ServerWindow | None:
    """The window an offline collection's statement counters cover (#1893).

    PostgreSQL: only ``global_stats.stats_reset_age_seconds``, the time since
    ``pg_stat_statements`` was reset (PostgreSQL 14+); its counters survive a restart, so
    the uptime is never their window. Every other engine: its uptime,
    ``os_stats.db_uptime_seconds`` (SQL Server, Oracle), ``global_stats.uptime`` (MySQL,
    MariaDB) or, from an older collection, ``os_stats.db_uptime_hours``.
    """
    stats = data.get("global_stats") or {}
    if source_engine == "postgresql":
        return server_window(stats_reset_seconds=stats.get("stats_reset_age_seconds"))
    os_stats = data.get("os_stats") or {}
    return server_window(
        uptime_seconds=os_stats.get("db_uptime_seconds") or stats.get("uptime"),
        uptime_hours=os_stats.get("db_uptime_hours"),
    )


def _collection_rate_sample(data: dict, source_engine: str) -> RateSample | None:
    """The collection script's two snapshots of the counters (#1908): its
    ``metadata.rate_sample`` (``seconds``, the interval asked for, and
    ``elapsed_seconds``, the time between the two reads by the database's clock), checked
    against the server window the script read with the second snapshot. None for a
    collection made before #1908, which took one snapshot."""
    metadata = data.get("metadata")
    meta = metadata.get("rate_sample") if isinstance(metadata, Mapping) else None
    if not isinstance(meta, Mapping):
        return None
    return rate_sample(
        meta.get("seconds"),
        meta.get("elapsed_seconds"),
        _collection_server_window(data, source_engine),
    )


def _collection_first_counts(data: dict) -> Mapping[str, object]:
    """The MySQL script's first-read counts (#1908), ``metadata.rate_sample.first_counts``:
    each digest's count keyed as its statement row's ``sample_key``. Empty when the
    collection has none (every other script writes ``sample_first_count`` on each row)."""
    metadata = data.get("metadata")
    meta = metadata.get("rate_sample") if isinstance(metadata, Mapping) else None
    counts = meta.get("first_counts") if isinstance(meta, Mapping) else None
    return counts if isinstance(counts, Mapping) else {}


def _transform_queries(
    raw: list[dict],
    db_name: str,
    known_table_names: set[str],
    table_schemas: Mapping[str, set[str]] | None = None,
    shadow: TableShadow | None = None,
    source_engine: str = "",
    server_window: ServerWindow | None = None,
    sample: RateSample | None = None,
    first_counts: Mapping[str, object] | None = None,
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

    Each row's ``frequency_per_hour`` and ``calls_per_second`` divide its execution
    count by the window it covers, recorded as ``rate_window_seconds`` and
    ``rate_window_basis`` (:mod:`src.shared.rate_window`, #1893): the row's own
    ``first_seen`` to ``last_seen`` (PostgreSQL 17: its ``stats_since_age_seconds``),
    else ``server_window`` (:func:`_collection_server_window`), else 24 hours. With
    ``sample`` (the script's two snapshots, #1908), a statement's rate is the change from
    its first-snapshot count (``sample_first_count``, summed over the entries the script
    found in both snapshots; absent when none was) to its ``execution_count``, over the
    measured interval, with ``rate_sample`` recording why when it kept the single
    snapshot's rate. A row with a ``sample_key`` (the MySQL script) takes its
    ``sample_first_count`` from ``first_counts`` (:func:`_collection_first_counts`).

    Each ``query_text`` is stored, and parsed, with its literal values redacted in the
    ``source_engine`` dialect (:func:`src.tools.sql.redact.redact_literals`, #1625),
    after the rows are merged; the ``query_id`` is the merged row's ``digest``.
    """
    patterns = []
    fallbacks: Counter[str] = Counter()
    case_folded = _case_folded_names(known_table_names, source_engine)
    if first_counts:
        raw = [
            (
                {**r, "sample_first_count": first_counts.get(str(r["sample_key"]))}
                if r.get("sample_first_count") is None and r.get("sample_key") is not None
                else r
            )
            for r in raw
        ]
    for r in _merge_query_variants(raw, source_engine):
        query_text = redact_literals(str(r.get("query_text") or ""), source_engine)
        exec_count = r.get("execution_count") or 1
        total_rows_sent = r.get("total_rows_sent") or 0
        total_rows_examined = r.get("total_rows_examined") or 0
        total_rows_affected = r.get("total_rows_affected") or 0
        total_time_ms = float(r.get("total_time_ms") or 0)

        # Every merged row has its digest: the collection's, else the SHA-256 of its
        # text as collected (``_merge_query_variants``, the one place the id rule holds).
        digest = r["digest"]

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
                # #1652: PostgreSQL's toplevel read backwards; None when not reported.
                "nested": r.get("nested"),
                "execution_count": exec_count,
                **statement_rate_fields(
                    exec_count,
                    r.get("first_seen"),
                    r.get("last_seen"),
                    server_window,
                    observed_seconds=r.get("stats_since_age_seconds"),
                    sample=sample,
                    first_count=r.get("sample_first_count"),
                ),
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
                # The routines the statement ran in (#1891); None when not collected.
                "parent_routine_ids": r.get("parent_routine_ids"),
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
