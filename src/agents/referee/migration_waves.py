"""Migration waves (#225): the incremental roadmap, written once by synthesis.

The target architecture synthesis recommends can be fully decomposed, but a
customer needs an incremental path to get there, and the waves are how every
deliverable presents it. The order is one documented, deterministic rule — a
model may explain a wave, it never decides the sequence:

1. **Cache.** ElastiCache cache-aside in front of the current source database
   (MySQL/PostgreSQL), fronting the hot reads from the cache overlay
   (:mod:`.cache_overlay`). No data migration (``moves_from`` is always
   empty; the fronted engine is recorded in ``fronts``), fully reversible.
   Skipped when no cache overlay is recommended. ElastiCache owns no query
   and is never a system of record.
2. **Key-value and point lookups -> DynamoDB.** Table group by table group,
   respecting co-dependency groups (queries sharing a significant JOIN,
   ``assignment.co_dependency_groups``): a group that touches a DynamoDB table
   moves as one group, so co-dependent tables never split across groups where
   that is possible. A table a co-dependency group could not keep whole (an
   Aurora-routed query elsewhere still reads it) says so in this wave's gate,
   naming the dual-read requirement and the wave the group finishes in.
3. **Any other direct migration target** this rule does not otherwise name
   (forward compatible with an engine added later), ordered by workload share.
4. **Search / analytics read models -> OpenSearch.** OpenSearch never owns a
   write or the sole copy of a table: every table it serves keeps a durable
   owner, synced by zero-ETL, OpenSearch Ingestion or CDC, and its recovery
   path is always to re-index — never a data migration. A table whose owner
   is itself OpenSearch (an upstream data error, #317) is reassigned to the
   next real owner in its ``engines`` list, or the retained engine as a last
   resort. When a query's indexed tables cannot be resolved from the SQL at
   all, the wave says so explicitly and falls back to the retained/source
   engine as the owner of record rather than silently showing no owner.
5. **Document-shaped data -> DocumentDB.** DocumentDB stays an owner engine.
6. **Retained.** Whatever stays on the source-compatible relational engine
   (Aurora MySQL/PostgreSQL): a homogeneous migration, schema carried over
   1:1 (snapshot or replication). Always last. Also carries every collected
   table or view with no observed query, so the wave table counts add up to
   the collected schema.

Every wave but the cache and the search/analytics read model moves data away
from the current source database, never from an end-state engine it has not
reached yet: ``moves_from`` is always the source engine (``source_engine``,
e.g. ``"mysql"``), rendered as "the source MySQL database"
(:func:`src.shared.engine_names.display_source_database`).

A wave is scoped to tables and views the collector actually saw
(``known_tables``, synthesis's own set): a parser artifact (a CTE alias, a
keyword, a system catalog name) never reaches a wave or a deliverable.

A wave with nothing to move (no table, no query, no cached read) is skipped;
the remaining waves are numbered consecutively from 1.
"""

from __future__ import annotations

from typing import Any

from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.shared.engine_names import display_engine
from src.shared.migration_wave_engines import (
    CACHE_ENGINES,
    DOCUMENT_ENGINES,
    KV_ENGINES,
    NAMED_ENGINES,
    RELATIONAL_ENGINES,
    SEARCH_ENGINES,
)

# Pseudo "tables" a parser or the dialect itself introduces (MySQL's dummy
# ``DUAL`` target, a column a parser mistook for a table). They are not source
# tables, so a wave never claims to move them. Public: synthesis reuses this to
# record ``unresolved_names`` (#225 review finding 5) without re-deriving it.
PSEUDO_TABLES = frozenset({"DUAL", "unknown"})


def _plural(n: int, noun: str) -> str:
    return noun if n == 1 else f"{noun}s"


def _table_ids(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({str(t["table_id"]) for t in rows if t.get("table_id")})


def _durable_owner(table: dict[str, Any], retained_engine: str | None) -> str:
    """The engine that durably owns ``table`` — never a read-model engine.

    ``primary_engine`` is used unless it is a read-model (search) engine, in
    which case the first non-search engine in ``engines`` takes over, and
    failing that the source-compatible retained engine (review finding 1):
    OpenSearch is a read model only, so every table it serves must resolve to
    a real owner, never itself.
    """
    primary = table.get("primary_engine")
    engines = table.get("engines") or ([primary] if primary else [])
    if primary and primary not in SEARCH_ENGINES:
        return str(primary)
    for engine in engines:
        if engine and engine not in SEARCH_ENGINES:
            return str(engine)
    return retained_engine or "unresolved"


def _owned_tables(
    engine: str, table_assignments: list[dict[str, Any]], retained_engine: str | None
) -> list[dict[str, Any]]:
    """Tables ``engine`` durably owns (the corrected ``primary_engine``, finding 1)."""
    return [t for t in table_assignments if _durable_owner(t, retained_engine) == engine]


def _served_tables(engine: str, table_assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tables ``engine`` serves, as primary or secondary (the multi-engine rule, AGENTS.md)."""
    return [
        t for t in table_assignments if engine in (t.get("engines") or [t.get("primary_engine")])
    ]


def _sync_pattern(owner_engine: str) -> str:
    """Deterministic sync pattern OpenSearch uses to stay current with ``owner_engine``."""
    if owner_engine in RELATIONAL_ENGINES:
        return "zero-ETL"
    if owner_engine in KV_ENGINES:
        return "OpenSearch Ingestion"
    return "CDC"


def _dynamodb_table_groups(
    engine: str,
    rows: list[dict[str, Any]],
    co_dependency_groups: list[list[str]],
    query_assignments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """DynamoDB's tables, table group by table group, respecting co-dependency groups.

    A co-dependency group (queries sharing a significant JOIN) that touches at
    least one DynamoDB-assigned query against one of DynamoDB's tables becomes
    one group (``kind: "co_dependency"``), named by its tables, with the count
    of *distinct DynamoDB-assigned query IDs* that touch it (review finding 8
    — not ``TableAssignment.query_count``, which double-counts a multi-table
    query and counts every engine's queries, not just DynamoDB's). Every
    DynamoDB table that is not part of any co-dependency group forms one final
    group of independent tables (``kind: "independent"``, finding 7).
    Deterministic: groups are visited in the order ``co_dependency_groups``
    lists them, tables within a group are sorted.
    """
    dynamo_tables = {str(t["table_id"]) for t in rows if t.get("table_id")}
    if not dynamo_tables:
        return []
    by_qid = {qa.get("query_id"): qa for qa in query_assignments}
    dynamo_qids = {
        qid
        for qid, qa in by_qid.items()
        if qa.get("assigned_engine") == engine
        and set(qa.get("source_tables") or []) & dynamo_tables
    }

    groups: list[dict[str, Any]] = []
    grouped_tables: set[str] = set()
    for group in co_dependency_groups:
        qids = set(group) & dynamo_qids
        if not qids:
            continue
        tables: set[str] = set()
        for qid in qids:
            tables |= set(by_qid[qid].get("source_tables") or []) & dynamo_tables
        if not tables:
            continue
        grouped_tables |= tables
        groups.append({"tables": sorted(tables), "query_count": len(qids), "kind": "co_dependency"})

    remaining = dynamo_tables - grouped_tables
    if remaining:
        remaining_qids = {
            qid for qid in dynamo_qids if set(by_qid[qid].get("source_tables") or []) & remaining
        }
        groups.append(
            {"tables": sorted(remaining), "query_count": len(remaining_qids), "kind": "independent"}
        )
    return groups


def _cross_wave_note(
    dynamo_tables: set[str],
    query_assignments: list[dict[str, Any]],
    dynamo_engine: str,
    engine_wave_number: dict[str, int],
) -> str:
    """Gate addendum when a DynamoDB table is still read by another wave's queries.

    Detects a co-dependency the DynamoDB wave could not keep whole (review
    finding 7): a table it owns that a query assigned to a *different* engine
    still reads. Returns the empty string when every DynamoDB table is read
    only by DynamoDB-assigned queries.
    """
    tables_by_engine: dict[str, set[str]] = {}
    for qa in query_assignments:
        other = qa.get("assigned_engine")
        if not other or other == dynamo_engine:
            continue
        hit = set(qa.get("source_tables") or []) & dynamo_tables
        if hit:
            tables_by_engine.setdefault(other, set()).update(hit)
    if not tables_by_engine:
        return ""

    all_tables: set[str] = set()
    clauses: list[str] = []
    for other in sorted(tables_by_engine, key=lambda e: (-len(tables_by_engine[e]), e)):
        tables = tables_by_engine[other]
        all_tables |= tables
        wave_no = engine_wave_number.get(other)
        wave_text = f"wave {wave_no}" if wave_no else "a later wave"
        clauses.append(f"{display_engine(other)} queries until {wave_text}")
    n = len(all_tables)
    tables_list = ", ".join(sorted(all_tables))
    return (
        f" {n} {_plural(n, 'table')} ({tables_list}) stay dual-read by "
        + "; and by ".join(clauses)
        + "; keep them in sync via CDC until then."
    )


def _cache_overlap_note(cache_overlay: dict[str, Any], engine_wave_number: dict[str, int]) -> str:
    """Says which wave(s) the cache's owner engines move in (review finding 11).

    The cache's share is of calls, the owner shares are of query patterns; a
    reader seeing both needs to know they measure different things and that
    the cached reads already belong to a later wave's queries.
    """
    owners = cache_overlay.get("owners") or {}
    clauses = []
    for engine in sorted(owners, key=lambda e: (-owners[e], e)):
        wave_no = engine_wave_number.get(engine)
        if wave_no:
            clauses.append(f"wave {wave_no} ({display_engine(engine)})")
    if not clauses:
        return ""
    return (
        " These reads belong to queries that move in "
        + " and ".join(clauses)
        + "; this share overlaps the owner shares, which alone sum to 100%."
    )


def _cache_wave(cache_overlay: dict[str, Any] | None, source_engine: str) -> dict[str, Any] | None:
    if not cache_overlay or cache_overlay.get("engine") not in CACHE_ENGINES:
        return None
    n = int(cache_overlay.get("query_count") or 0)
    if not n:
        return None
    engine = cache_overlay["engine"]
    share = round(float(cache_overlay.get("call_share_percent") or 0.0), 1)
    read = _plural(n, "read")
    return {
        "title": f"Cache hot reads with {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [],
        "serves_from": [],
        "fronts": source_engine or None,
        "tables": [],
        "table_count": 0,
        "table_groups": None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "calls",
        "rationale": (
            f"{n} hot {read} ({share:.1f}% of calls), cache-aside in front of the current "
            "source database (MySQL/PostgreSQL): no data migration, fully reversible. It "
            "takes the read pressure off the source database first, de-risking the data "
            "migrations that follow."
        ),
        "gate": (
            "Cache hit rate and invalidation verified against the source database; the source "
            "stays authoritative throughout this wave."
        ),
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
    rows = _owned_tables(engine, table_assignments, retained_engine)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    groups = _dynamodb_table_groups(engine, rows, co_dependency_groups, query_assignments)
    n_groups = len(groups) or len(rows)
    query = _plural(n, "query").replace("querys", "queries")
    group = _plural(n_groups, "group")
    return {
        "title": f"Move key-value and point-lookup queries to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [source_engine] if source_engine else [],
        "serves_from": [],
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": groups or None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} key-value and point-lookup {query} ({share:.1f}% of the workload) across "
            f"{n_groups} table {group}, respecting co-dependent tables where possible: the "
            f"pattern {display_engine(engine)} fits best, and the smallest-blast-radius data "
            "migration available once Wave 1's cache has absorbed the read pressure."
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
    rows = _owned_tables(engine, table_assignments, retained_engine)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    query = _plural(n, "query").replace("querys", "queries")
    return {
        "title": f"Move the remaining workload to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [source_engine] if source_engine else [],
        "serves_from": [],
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} {query} ({share:.1f}% of the workload) to {display_engine(engine)}, the "
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
    rows = _owned_tables(engine, table_assignments, retained_engine)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    query = _plural(n, "query").replace("querys", "queries")
    return {
        "title": f"Move document-shaped data to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [source_engine] if source_engine else [],
        "serves_from": [],
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} document-shaped {query} ({share:.1f}% of the workload) to "
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
    query = _plural(n, "query").replace("querys", "queries")
    fallback_owner = retained_engine or (source_engine or None)

    if not rows:
        # Review finding 2: queries exist, but the SQL gave no resolvable source
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
                f"{n} search/analytics {query} ({share:.1f}% of the workload) build a read "
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
            f"{n} search/analytics {query} ({share:.1f}% of the workload) build a read model "
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


def _retained_wave(
    engine: str,
    by_engine: dict[str, dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    retained_engine: str | None,
    uncovered: list[str],
) -> dict[str, Any] | None:
    rows = _owned_tables(engine, table_assignments, retained_engine)
    n = int((by_engine.get(engine) or {}).get("assigned_queries") or 0)
    if not n and not rows and not uncovered:
        return None
    share = round(float((by_engine.get(engine) or {}).get("workload_percent") or 0.0), 1)
    query = _plural(n, "query").replace("querys", "queries")
    tables = sorted(set(_table_ids(rows)) | set(uncovered))
    extra = (
        f" plus {len(uncovered)} collected {_plural(len(uncovered), 'table')} with no observed "
        "query, carried over 1:1, no observed queries"
        if uncovered
        else ""
    )
    return {
        "title": f"Keep the rest on {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [],
        "serves_from": [],
        "tables": tables,
        "table_count": len(tables),
        "table_groups": None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} {query} ({share:.1f}% of the workload) stay on {display_engine(engine)}: a "
            "homogeneous migration, schema carried over 1:1 (snapshot or replication) — it is "
            "already source-compatible, so it is the lowest-risk engine to finish the roadmap "
            f"on{extra}."
        ),
        "gate": (
            "End state: every earlier wave's gate has passed; decommission the legacy source "
            "engine."
        ),
    }


def build_migration_waves(
    *,
    ranking: list[dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    query_assignments: list[dict[str, Any]],
    co_dependency_groups: list[list[str]],
    cache_overlay: dict[str, Any] | None,
    source_engine: str,
    known_tables: set[str] | list[str] | None = None,
) -> list[dict[str, Any]] | None:
    """The incremental migration roadmap for this report, or ``None`` with no assignment.

    Pure function of the synthesis data already loaded for ``ranking``,
    ``table_mappings``/``cache_overlay`` and the assignment artifact
    (``table_assignments``, ``query_assignments``, ``co_dependency_groups``):
    no model call, so the same inputs always produce the same waves. See the
    module docstring for the sequencing rule.

    ``known_tables``, when given, is synthesis's own set of tables and views
    the collector actually saw (review finding 5): a ``table_assignments`` row
    whose ``table_id`` is not in it (a parser artifact — a CTE alias, a
    keyword, a system catalog name; tracked separately, #316) never reaches a
    wave. The retained wave then also carries every table in ``known_tables``
    that has no row at all here (no observed query) — review finding 6 — so
    wave table counts add up to the collected schema. ``None`` skips both: no
    filtering, no unreferenced-table accounting (back-compat for a caller that
    cannot supply the collected schema).
    """
    if not ranking:
        return None
    by_engine = {str(r["target"]): r for r in ranking if r.get("target")}
    source_engine = (source_engine or "").lower()
    retained_engine = SOURCE_ENGINE_TO_AURORA.get(source_engine)

    known: set[str] | None = None if known_tables is None else {str(t) for t in known_tables}

    def _in_scope(table_id: Any) -> bool:
        if not table_id or table_id in PSEUDO_TABLES:
            return False
        return known is None or table_id in known

    table_assignments = [t for t in table_assignments if _in_scope(t.get("table_id"))]

    waves: list[dict[str, Any]] = []

    cache = _cache_wave(cache_overlay, source_engine)
    if cache:
        waves.append(cache)

    for engine in sorted(KV_ENGINES & set(by_engine)):
        wave = _kv_wave(
            engine,
            by_engine,
            table_assignments,
            query_assignments,
            co_dependency_groups,
            retained_engine,
            source_engine,
        )
        if wave:
            waves.append(wave)

    others = sorted(
        (e for e in by_engine if e not in NAMED_ENGINES),
        key=lambda e: (-float(by_engine[e].get("workload_percent") or 0.0), e),
    )
    for engine in others:
        wave = _other_target_wave(
            engine, by_engine, table_assignments, retained_engine, source_engine
        )
        if wave:
            waves.append(wave)

    for engine in sorted(SEARCH_ENGINES & set(by_engine)):
        wave = _search_wave(engine, by_engine, table_assignments, retained_engine, source_engine)
        if wave:
            waves.append(wave)

    for engine in sorted(DOCUMENT_ENGINES & set(by_engine)):
        wave = _document_wave(engine, by_engine, table_assignments, retained_engine, source_engine)
        if wave:
            waves.append(wave)

    uncovered: list[str] = []
    if retained_engine and known is not None:
        referenced = {str(t["table_id"]) for t in table_assignments if t.get("table_id")}
        uncovered = sorted(known - referenced - PSEUDO_TABLES)

    if retained_engine and (retained_engine in by_engine or uncovered):
        wave = _retained_wave(
            retained_engine, by_engine, table_assignments, retained_engine, uncovered
        )
        if wave:
            waves.append(wave)

    if not waves:
        return None

    for i, wave in enumerate(waves, start=1):
        wave["wave"] = i

    engine_wave_number: dict[str, int] = {}
    for w in waves:
        for e in w["engines"]:
            engine_wave_number.setdefault(e, w["wave"])

    for w in waves:
        if w["engines"] and w["engines"][0] in KV_ENGINES and w.get("table_groups"):
            note = _cross_wave_note(
                set(w["tables"]), query_assignments, w["engines"][0], engine_wave_number
            )
            if note:
                w["gate"] = w["gate"] + note

    if cache_overlay:
        cache_w = next(
            (w for w in waves if w["engines"] and w["engines"][0] in CACHE_ENGINES), None
        )
        if cache_w:
            note = _cache_overlap_note(cache_overlay, engine_wave_number)
            if note:
                cache_w["rationale"] = cache_w["rationale"] + note

    return waves
