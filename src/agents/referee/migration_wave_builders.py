"""Migration waves: one builder per wave kind (Aurora move, cache, key-value, document,
other target, search read model), and the merge of the key-value and document waves."""

from __future__ import annotations

from typing import Any

from src.agents.referee.aurora_choice import describe_features
from src.agents.referee.migration_wave_tables import (
    _dedupe_table_owners,
    _durable_owner,
    _dynamodb_table_groups,
    _plural,
    _query_noun,
    _served_tables,
    _short_version,
    _sync_pattern,
    _table_ids,
    _table_owners_for_served,
)
from src.shared.engine_names import (
    SOURCE_ENGINE_DISPLAY_NAMES,
    display_engine,
    heterogeneous_source_clause,
)
from src.shared.migration_wave_engines import (
    CACHE_ENGINES,
    RELATIONAL_ENGINES,
    cache_front_description,
)

# A source engine value that is already an Aurora engine: wave 1 has nothing
# to move (#321). The collector contract has no such source value today (its
# own comment: "Aurora is a target database, not a source") -- this guards a
# hand-built or future caller rather than a case the real pipeline hits.
_ALREADY_AURORA = RELATIONAL_ENGINES | {"aurora"}


# A source that is the same engine family in substance, worded without
# implying it is literally the same product (#324 review): MariaDB is a
# fork, so its wave 1 rationale calls it "MySQL-compatible" rather than
# claiming the stronger "same engine family" MySQL/PostgreSQL get.
_MYSQL_COMPATIBLE_SOURCES = {"mariadb"}


def _aurora_wave(
    by_engine: dict[str, dict[str, Any]],
    retained_engine: str | None,
    source_engine: str,
    known: set[str] | None,
    table_assignments: list[dict[str, Any]],
    source_version: Any,
    mapped_tables: set[str] | None = None,
    cross_engine: bool = False,
    aurora_engine_choice: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Wave 1 (#321): the whole source database moves to Aurora first.

    ``None`` when there is no source engine to report at all (nothing to say,
    #321: never invent a wave from no data) or the source is already an
    Aurora engine (nothing to move). Otherwise always present — unlike the
    pre-#321 "retained" wave, this wave does not depend on any engine having
    a share of the workload: it is the schema-carryover step itself, not a
    leftover accounting of what nothing else claimed.

    ``homogeneity`` is ``"homogeneous"`` when the source maps to a
    source-compatible Aurora engine (MySQL/MariaDB -> Aurora MySQL,
    PostgreSQL -> Aurora PostgreSQL); ``"cross_engine"`` (#381, ``cross_engine=True``,
    the caller's own signal: the source has no Aurora dialect of its own, but the
    assignment resolver still picked one of the two competing Aurora engines, so
    ``retained_engine`` names it) when the source has no Aurora dialect but a choice
    was made anyway -- the title and rationale state this is a schema-and-dialect
    conversion, not a 1:1 carry-over, and the gate calls out validating the
    translation, but the engine is still named and still given a share, same as the
    homogeneous case; and ``"heterogeneous"`` otherwise, in which case ``engines`` is
    empty (this assessment did not pick an Aurora target at all, so none is named),
    the title says what the source moves *off* instead of *to*, and the rationale
    drops the share and "later waves move off Aurora" sentences entirely -- there is
    no Aurora here for either to be true of (#324 review).

    ``aurora_engine_choice`` (#381) is the resolver's own decision trace
    (``Assignment.aurora_engine_choice``, as a dict: ``totals``, ``margin``,
    ``deciding_features``, ``reason``) for a ``cross_engine`` wave's rationale, naming
    both engines' totals and whatever tipped the choice. ``None`` (homogeneous, no
    Aurora at all, or a legacy assignment with no such field) drops that sentence.

    The rationale states only what the collector actually reports for
    homogeneity: engine and version. The collector never reports feature or
    extension compatibility, so the wording says that plainly instead of
    implying a check that was never run (#324 review finding 8).

    ``cutover_query_count`` (homogeneous only) is every in-scope query across
    every wave with a query-pattern share (i.e. every wave but the cache,
    which is a share of calls): the whole workload runs on Aurora the moment
    this wave finishes, before any later wave has moved its own share away.
    Set by the caller (``build_migration_waves``) once every wave exists.

    ``mapped_tables`` (#380 review) is the caller's set of tables some
    engine's schema design actually mapped (``table_mappings``' own
    ``source_table`` set, already resolved against the collector's real
    schema). It never narrows ``tables`` -- wave 1 still carries every
    collected base table and view, because that is what "the whole source
    database moves 1:1" actually means, and every other deliverable
    (``roadmap.py``, ``executive_report.py``) keeps this wave's ``table_count``
    as the whole collected schema. It is used only to add one explanatory
    sentence to the rationale, reconciling this wave's full count against the
    migration map's smaller "N tables mapped to a target engine" total (the
    #380 complaint was the two counts disagreeing with no explanation, not
    that wave 1 counted too much): "``M`` of these are mapped to a target
    engine in a later wave; the other ``K`` stay on ``<engine>`` unchanged."
    Omitted (``None``) when the caller has nothing mapped at all (no schema
    design ran for any engine, e.g. ``--llm-mode none``) -- there is nothing
    to reconcile against yet.
    """
    source_engine = (source_engine or "").strip().lower()
    if not source_engine or source_engine in _ALREADY_AURORA:
        return None

    version = _short_version(source_version)
    source_name = SOURCE_ENGINE_DISPLAY_NAMES.get(source_engine, display_engine(source_engine))
    # The collector's version string sometimes already leads with the engine
    # name (a full server banner, e.g. "PostgreSQL 16.10"): drop the
    # duplicate rather than show "PostgreSQL PostgreSQL 16.10".
    if version and version.lower().startswith(source_name.lower()):
        version_number = version[len(source_name) :].strip()
        source_desc = version if version_number else source_name
    else:
        version_number = version or ""
        source_desc = source_name + (f" {version}" if version else "")

    # #380 review: wave 1 carries the whole collected schema -- every base
    # table and view the collector saw, minus only engine-internal noise
    # (a sequence, a stats-extension object: ``is_engine_system_object``),
    # never narrowed to what some later wave happens to map. A real
    # application table with no schema-design mapping (a framework table
    # like ``ar_internal_metadata``, or simply one no query touches) is
    # still part of "the whole database" and stays.
    tables = sorted(known) if known is not None else _table_ids(table_assignments)

    if retained_engine and not cross_engine:
        target_name = display_engine(retained_engine)
        family = (
            "MySQL-compatible"
            if source_engine in _MYSQL_COMPATIBLE_SOURCES
            else "same engine family"
        )
        owner_entry = by_engine.get(retained_engine) or {}
        n = int(owner_entry.get("assigned_queries") or 0)
        share = round(float(owner_entry.get("workload_percent") or 0.0), 1)
        rationale = (
            f"The whole source database ({source_desc}) moves 1:1 to {target_name}: {family}. "
            "The collector reports engine and version only; feature compatibility is not "
            f"assessed here. {n} {_query_noun(n)} ({share:.1f}%) remain on {target_name} once "
            f"later waves have moved their share; later waves move subsets of these "
            f"{len(tables)} tables and views out of {target_name}."
        )
        if mapped_tables is not None:
            # #380 review: reconciles this wave's full table-and-view count
            # against the migration map's smaller "mapped" total, so the two
            # numbers disagreeing (322 vs 280 in the field) reads as explained,
            # not contradictory.
            mapped_count = len(set(tables) & mapped_tables)
            unmapped_count = len(tables) - mapped_count
            rationale += (
                f" {mapped_count} of these tables and views are mapped to a target engine "
                f"in a later wave; the other {unmapped_count} stay on {target_name} "
                "unchanged."
            )
        gate = (
            "Schema and data parity validated against the source database before cutover; "
            "decommission the legacy source once replication lag is zero."
        )
        title = f"Move to {target_name}"
        engines: list[str] = [retained_engine]
        homogeneity = "homogeneous"
    elif retained_engine and cross_engine:
        # #381: the source has no Aurora dialect of its own (SQL Server, Oracle, DB2),
        # but the assignment resolver still picked one of the two competing Aurora
        # engines by scoring every query against both -- named and given a share here,
        # the same as the homogeneous case, but stated plainly as a dialect-and-schema
        # conversion rather than a 1:1 carry-over, so this wave never reads as "no data
        # model changes".
        target_name = display_engine(retained_engine)
        owner_entry = by_engine.get(retained_engine) or {}
        n = int(owner_entry.get("assigned_queries") or 0)
        share = round(float(owner_entry.get("workload_percent") or 0.0), 1)
        decision = ""
        if aurora_engine_choice:
            totals = aurora_engine_choice.get("totals") or {}
            if totals:
                decision = (
                    " Aurora MySQL scored "
                    f"{float(totals.get('aurora_mysql', 0.0)):.1f} and Aurora PostgreSQL "
                    f"{float(totals.get('aurora_postgresql', 0.0)):.1f} in total adjusted "
                    "query fit"
                )
            features = aurora_engine_choice.get("deciding_features") or []
            if features:
                decision += f"; tipped by {describe_features(features)}"
            if decision:
                decision += "."
        rationale = (
            f"{heterogeneous_source_clause(source_engine, version_number)}, "
            # #381 review: never "earned the move" -- that reads as a decisive win even
            # when the actual decision was a tie-break or an unneeded default (an exact
            # tie, or a close call with no deciding feature). "Selected" is accurate
            # whichever of those the trace in ``decision`` (if present) turns out to be.
            f"and the assignment resolver selected {target_name} to serve it.{decision} "
            # #1120: one story with the wave's not-yet-optimized note: the dialect and
            # the types are converted, the data model stays as it is.
            "This is a cross-engine move: the SQL dialect and the data types are "
            "converted and the data model stays as it is, so it is not a 1:1 carry-over. "
            f"{n} {_query_noun(n)} ({share:.1f}%) remain on {target_name} once later waves "
            f"have moved their share; later waves move subsets of these {len(tables)} "
            f"tables and views out of {target_name}."
        )
        if mapped_tables is not None:
            mapped_count = len(set(tables) & mapped_tables)
            unmapped_count = len(tables) - mapped_count
            rationale += (
                f" {mapped_count} of these tables and views are mapped to a target engine "
                f"in a later wave; the other {unmapped_count} stay on {target_name} "
                "unchanged."
            )
        gate = (
            f"Cross-engine move: validate the schema and query translation to {target_name} "
            "(SQL dialect and data types converted, with the current data model) before "
            "cutover."
        )
        title = f"Move off {source_name} to {target_name} (cross-engine)"
        engines = [retained_engine]
        homogeneity = "cross_engine"
    else:
        rationale = (
            f"The source database ({source_desc}) has no source-compatible Aurora engine in "
            "this assessment: a heterogeneous move (schema and query translation, not a 1:1 "
            "carry-over)."
        )
        gate = (
            "Risk: no Aurora-compatible target engine was identified for this source database; "
            "pick and validate one (schema and query translation, not a 1:1 carry-over) before "
            "this wave starts."
        )
        title = f"Move off {source_name} (heterogeneous)"
        engines = []
        n = 0
        share = 0.0
        homogeneity = "heterogeneous"

    return {
        "title": title,
        "engines": engines,
        "moves_from": [source_engine],
        "serves_from": [],
        "fronts": None,
        "tables": tables,
        "table_count": len(tables),
        "table_groups": None,
        "homogeneity": homogeneity,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": rationale,
        "gate": gate,
    }


def _cache_wave(cache_overlay: dict[str, Any] | None, front_engine: str) -> dict[str, Any] | None:
    """Wave 2 (#321, was wave 1): the cache now fronts Aurora, not the legacy
    source database -- wave 1 has already carried the schema over by the time
    this wave runs. ``front_engine`` is ``retained_engine`` normally, falling
    back to the raw source engine when there is no retained engine to front
    instead -- a heterogeneous source (wave 1 still ran, it just names no
    Aurora engine) or no source engine at all (wave 1 did not run): the
    cache still needs *something* to front either way.
    """
    if not cache_overlay or cache_overlay.get("engine") not in CACHE_ENGINES:
        return None
    n = int(cache_overlay.get("query_count") or 0)
    if not n:
        return None
    engine = cache_overlay["engine"]
    share = round(float(cache_overlay.get("call_share_percent") or 0.0), 1)
    read = _plural(n, "read")
    fronts_aurora = front_engine in RELATIONAL_ENGINES
    if fronts_aurora:
        # Review of #375: say both stages when the cache's final owner (the
        # engine the cached queries actually end up on, after every wave)
        # differs from the relational engine wave 2 fronts it with here --
        # the same shared helper every other deliverable's cache-fronting
        # sentence calls, so none of them can disagree about it.
        front = cache_front_description(front_engine, cache_overlay.get("owners"))
        authority = display_engine(front_engine)
    else:
        source_name = SOURCE_ENGINE_DISPLAY_NAMES.get(front_engine)
        front = (
            f"the current source database ({source_name})"
            if source_name
            else "the current source database"
        )
        authority = "the source database"
    return {
        "title": f"Cache hot reads with {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [],
        "serves_from": [],
        "fronts": front_engine or None,
        "tables": [],
        "table_count": 0,
        "table_groups": None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "calls",
        "rationale": (
            f"{n} hot {read} ({share:.1f}% of calls), cache-aside in front of {front}: no "
            "data migration, fully reversible. It takes the read pressure off before the "
            "data migrations that follow."
        ),
        "gate": f"Cache hit rate and invalidation verified against {authority}.",
    }


def _kv_wave(
    engine: str,
    by_engine: dict[str, dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    query_assignments: list[dict[str, Any]],
    co_dependency_groups: list[list[str]],
    retained_engine: str | None,
    source_engine: str,
) -> dict[str, Any] | None:
    rows = _served_tables(engine, table_assignments)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    groups = _dynamodb_table_groups(engine, rows, co_dependency_groups, query_assignments)
    n_groups = len(groups) or len(rows)
    query = _query_noun(n)
    group = _plural(n_groups, "group")
    moves_from_engine = retained_engine or source_engine
    return {
        "title": f"Move key-value and point-lookup queries to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [moves_from_engine] if moves_from_engine else [],
        "serves_from": [],
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": groups or None,
        "table_owners": _table_owners_for_served(rows, engine, retained_engine),
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} key-value and point-lookup {query} ({share:.1f}% of query patterns) across "
            # #951: no "where possible": the grouping is deterministic, each
            # co-dependency group's tables form one group (_dynamodb_table_groups).
            f"{n_groups} table {group}, where co-dependent tables move together: the "
            f"pattern {display_engine(engine)} fits best, and the smallest-blast-radius data "
            "migration available once the cache wave has absorbed the read pressure."
        ),
        "gate": "Dual-write/backfill validated and query parity confirmed per table group.",
    }


def _other_target_wave(
    engine: str,
    by_engine: dict[str, dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    retained_engine: str | None,
    source_engine: str,
) -> dict[str, Any] | None:
    rows = _served_tables(engine, table_assignments)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    query = _query_noun(n)
    moves_from_engine = retained_engine or source_engine
    return {
        "title": f"Move the remaining workload to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [moves_from_engine] if moves_from_engine else [],
        "serves_from": [],
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": None,
        "table_owners": _table_owners_for_served(rows, engine, retained_engine),
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} {query} ({share:.1f}% of query patterns) to {display_engine(engine)}, the "
            "migration target the earlier waves do not already cover."
        ),
        "gate": "Query parity confirmed before the next wave.",
    }


def _document_wave(
    engine: str,
    by_engine: dict[str, dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    retained_engine: str | None,
    source_engine: str,
) -> dict[str, Any] | None:
    rows = _served_tables(engine, table_assignments)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    query = _query_noun(n)
    moves_from_engine = retained_engine or source_engine
    return {
        "title": f"Move document-shaped data to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [moves_from_engine] if moves_from_engine else [],
        "serves_from": [],
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": None,
        "table_owners": _table_owners_for_served(rows, engine, retained_engine),
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} document-shaped {query} ({share:.1f}% of query patterns) to "
            f"{display_engine(engine)}, kept as an owner engine for its nested, variable-shape "
            "tables."
        ),
        "gate": "Document shape conformance and query coverage validated before the next wave.",
    }


def _search_wave(
    engine: str,
    by_engine: dict[str, dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    retained_engine: str | None,
    source_engine: str,
) -> dict[str, Any] | None:
    rows = _served_tables(engine, table_assignments)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    query = _query_noun(n)
    fallback_owner = retained_engine or (source_engine or None)

    if not rows:
        # #225: queries exist, but the SQL gave no resolvable source
        # table (every ``source_tables`` entry was pseudo/unknown or dropped as
        # parser noise). Say so explicitly instead of silently showing no owner.
        owner_label = display_engine(fallback_owner) if fallback_owner else "the source database"
        return {
            "title": f"Sync search and analytics read models to {display_engine(engine)}",
            "engines": [engine],
            "moves_from": [],
            "serves_from": [fallback_owner] if fallback_owner else [],
            "tables": [],
            "table_count": 0,
            "table_groups": None,
            "table_owners": [],
            "query_count": n,
            "workload_share_percent": share,
            "share_basis": "queries",
            "rationale": (
                f"{n} search/analytics {query} ({share:.1f}% of query patterns) build a read "
                f"model in {display_engine(engine)}. The indexed tables could not be resolved "
                "from the SQL, so until the OpenSearch index/mapping definitions are audited "
                f"directly, {owner_label} is treated as the owner of record."
            ),
            "gate": (
                "Resolve the indexed tables from the OpenSearch index/mapping definitions (not "
                "just the SQL) and confirm each has a durable owner before the next wave."
            ),
        }

    table_owners = [
        {"table": table_id, "owner": owner, "sync": _sync_pattern(owner)}
        for table_id, owner in sorted(
            (str(t["table_id"]), _durable_owner(t, retained_engine))
            for t in rows
            if t.get("table_id")
        )
    ]
    owners = sorted({o["owner"] for o in table_owners})
    owner_names = ", ".join(display_engine(o) for o in owners) or "the engines that own its tables"
    return {
        "title": f"Sync search and analytics read models to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [],
        "serves_from": owners,
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": None,
        "table_owners": table_owners,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} search/analytics {query} ({share:.1f}% of query patterns) build a read model "
            f"in {display_engine(engine)}, kept in sync (zero-ETL, OpenSearch Ingestion or CDC "
            f"per table) from {owner_names}, which keep durable ownership: every table it "
            f"serves still has an owner. {display_engine(engine)} never becomes the system of "
            "record, and recovery is always by re-indexing, never a data migration."
        ),
        "gate": (
            "Sync lag and re-index time inside SLA, and every served table still has a "
            "durable owner, before the next wave."
        ),
    }


def _merge_kv_and_document_waves(dynamo: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """One wave for DynamoDB and DocumentDB when both are routed (#321 review
    finding 10): the maintainer's own plan is "cache + DynamoDB or cache +
    DocumentDB", not a separate wave for each. Each engine's rationale and
    gate text stay distinct, labeled, inside the shared wave, rather than
    blended into one sentence that would blur which gate blocks which
    engine.

    ``_dynamo_label``/``_dynamo_gate``/``_dynamo_owned``/``_dynamo_synced``/
    ``_doc_label``/``_doc_gate``/``_doc_owned``/``_doc_synced`` are transient
    bookkeeping ``build_migration_waves`` uses to splice each engine's own
    cross-wave dual-read note (#562: now computed for both, not DynamoDB's
    alone) into its own gate text specifically, keyed to its own OWNED
    table set for the forward clause (#568 review, Critical 1 -- never the
    served set) and its own ``table_owners`` entries for the reverse clause
    (#568 review round 2, Important 2 -- the deduped, combined public
    ``table_owners`` no longer says which engine each entry is for), rather
    than blending them or attaching both to the end of one engine's gate;
    they are stripped before the waves are returned, never part of the
    public shape.
    """
    dynamo_name = display_engine(dynamo["engines"][0])
    doc_name = display_engine(doc["engines"][0])
    dynamo_engine = dynamo["engines"][0]
    doc_engine = doc["engines"][0]
    tables = sorted(set(dynamo["tables"]) | set(doc["tables"]))
    moves_from = sorted(set(dynamo.get("moves_from") or []) | set(doc.get("moves_from") or []))
    dynamo_owners = dynamo.get("table_owners") or []
    doc_owners = doc.get("table_owners") or []
    # #568 review, Suggestion 4: a table neither engine durably owns (both
    # merely serve it) would otherwise appear twice, identically -- dedupe
    # the fact itself rather than keep two copies with no way to tell them
    # apart.
    table_owners = _dedupe_table_owners([*dynamo_owners, *doc_owners])
    dynamo_owned = sorted(set(dynamo["tables"]) - {o["table"] for o in dynamo_owners})
    doc_owned = sorted(set(doc["tables"]) - {o["table"] for o in doc_owners})
    return {
        "title": f"Move key-value and document-shaped data to {dynamo_name} and {doc_name}",
        "engines": [dynamo_engine, doc_engine],
        "moves_from": moves_from,
        "serves_from": [],
        "tables": tables,
        "table_count": len(tables),
        "table_groups": dynamo.get("table_groups"),
        "table_owners": table_owners,
        # #568 review, Important 2: table_count above is the union of both
        # engines' served tables and cannot be split back into each
        # engine's own share -- a consumer that credited it to every engine
        # (the pre-#568-review behaviour) double counted. These two give
        # the real per-engine served and owned counts instead.
        "table_counts_by_engine": {
            dynamo_engine: dynamo["table_count"],
            doc_engine: doc["table_count"],
        },
        "owned_counts_by_engine": {
            dynamo_engine: len(dynamo_owned),
            doc_engine: len(doc_owned),
        },
        "query_count": dynamo["query_count"] + doc["query_count"],
        "workload_share_percent": round(
            dynamo["workload_share_percent"] + doc["workload_share_percent"], 1
        ),
        "share_basis": "queries",
        "rationale": f"{dynamo['rationale']} {doc['rationale']}",
        "gate": f"{dynamo_name}: {dynamo['gate']} {doc_name}: {doc['gate']}",
        "_dynamo_label": dynamo_name,
        "_dynamo_gate": dynamo["gate"],
        "_dynamo_owned": dynamo_owned,
        "_dynamo_synced": dynamo_owners,
        "_doc_label": doc_name,
        "_doc_gate": doc["gate"],
        "_doc_owned": doc_owned,
        "_doc_synced": doc_owners,
    }
