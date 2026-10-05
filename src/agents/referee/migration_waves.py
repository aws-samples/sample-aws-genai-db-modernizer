"""Migration waves (#225): the incremental roadmap, written once by synthesis.

The target architecture synthesis recommends can be fully decomposed, but a
customer needs an incremental path to get there, and the waves are how every
deliverable presents it. The order is one documented, deterministic rule — a
model may explain a wave, it never decides the sequence:

1. **Cache.** ElastiCache cache-aside in front of the current source-compatible
   engine, fronting the hot reads from the cache overlay (:mod:`.cache_overlay`).
   No data migration, fully reversible. Skipped when no cache overlay is
   recommended. ElastiCache owns no query and is never a system of record.
2. **Key-value and point lookups -> DynamoDB.** Table group by table group,
   respecting co-dependency groups (queries sharing a significant JOIN,
   ``assignment.co_dependency_groups``): a group that touches a DynamoDB table
   moves as one group, so co-dependent tables never split across groups.
3. **Any other direct migration target** this rule does not otherwise name
   (forward compatible with an engine added later), ordered by workload share.
4. **Search / analytics read models -> OpenSearch.** OpenSearch never owns a
   write or the sole copy of a table: every table it serves keeps a durable
   owner (the engine named by ``moves_from``/``serves_from``), synced by
   zero-ETL, OpenSearch Ingestion or CDC, and its recovery path is always to
   re-index — never a data migration.
5. **Document-shaped data -> DocumentDB.** DocumentDB stays an owner engine.
6. **Retained.** Whatever stays on the source-compatible relational engine
   (Aurora MySQL/PostgreSQL), carried over 1:1 with no migration. Always last.

A wave with nothing to move (no table, no query, no cached read) is skipped;
the remaining waves are numbered consecutively from 1.
"""

from __future__ import annotations

from typing import Any

from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.shared.engine_names import display_engine

# Engines that can only ever be a cache layer, never a system of record (#296).
CACHE_ENGINES = frozenset({"elasticache"})
# Key-value / point-lookup engine, Wave 2.
KV_ENGINES = frozenset({"dynamodb"})
# Read-model engines: they index data synced from an owner, never own it (#303).
SEARCH_ENGINES = frozenset({"opensearch"})
# Document-shaped data, kept as an owner engine.
DOCUMENT_ENGINES = frozenset({"documentdb"})
# The source-compatible relational core, retained and carried over 1:1.
RELATIONAL_ENGINES = frozenset({"aurora_mysql", "aurora_postgresql"})

_NAMED_ENGINES = CACHE_ENGINES | KV_ENGINES | SEARCH_ENGINES | DOCUMENT_ENGINES | RELATIONAL_ENGINES

# Pseudo "tables" a parser or the dialect itself introduces (MySQL's dummy
# ``DUAL`` target, a column a parser mistook for a table). They are not source
# tables, so a wave never claims to move them.
_PSEUDO_TABLES = frozenset({"DUAL", "unknown"})


def _owned_tables(engine: str, table_assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tables ``engine`` is the primary (system-of-record) engine for."""
    return [
        t
        for t in table_assignments
        if t.get("primary_engine") == engine and t.get("table_id") not in _PSEUDO_TABLES
    ]


def _served_tables(engine: str, table_assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tables ``engine`` serves, as primary or secondary (the multi-engine rule, AGENTS.md)."""
    return [
        t
        for t in table_assignments
        if engine in (t.get("engines") or [t.get("primary_engine")])
        and t.get("table_id") not in _PSEUDO_TABLES
    ]


def _table_ids(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({str(t["table_id"]) for t in rows if t.get("table_id")})


def _plural(n: int, noun: str) -> str:
    return noun if n == 1 else f"{noun}s"


def _dynamodb_table_groups(
    rows: list[dict[str, Any]],
    co_dependency_groups: list[list[str]],
    query_assignments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """DynamoDB's tables, table group by table group, respecting co-dependency groups.

    A co-dependency group (queries sharing a significant JOIN) that touches at
    least one of DynamoDB's tables becomes one group, named by its tables, with
    the sum of each table's query count (``TableAssignment.query_count``, the
    same basis every group below uses, so counts are comparable). Every
    DynamoDB table that is not part of any co-dependency group forms one final
    group of independent tables. Deterministic: groups are visited in the
    order ``co_dependency_groups`` lists them, tables within a group are sorted.
    """
    table_query_count = {
        str(t["table_id"]): int(t.get("query_count") or 0) for t in rows if t.get("table_id")
    }
    dynamo_tables = set(table_query_count)
    if not dynamo_tables:
        return []
    by_qid = {qa.get("query_id"): qa for qa in query_assignments}
    groups: list[dict[str, Any]] = []
    grouped: set[str] = set()
    for group in co_dependency_groups:
        tables: set[str] = set()
        for qid in group:
            qa = by_qid.get(qid)
            if not qa:
                continue
            tables |= {t for t in (qa.get("source_tables") or []) if t in dynamo_tables}
        if tables:
            grouped |= tables
            groups.append(
                {
                    "tables": sorted(tables),
                    "query_count": sum(table_query_count[t] for t in tables),
                }
            )
    remaining = sorted(dynamo_tables - grouped)
    if remaining:
        groups.append(
            {
                "tables": remaining,
                "query_count": sum(table_query_count[t] for t in remaining),
            }
        )
    return groups


def _cache_wave(
    cache_overlay: dict[str, Any] | None, retained_engine: str | None
) -> dict[str, Any] | None:
    if not cache_overlay or cache_overlay.get("engine") not in CACHE_ENGINES:
        return None
    n = int(cache_overlay.get("query_count") or 0)
    if not n:
        return None
    engine = cache_overlay["engine"]
    share = round(float(cache_overlay.get("call_share_percent") or 0.0), 1)
    owners = sorted(cache_overlay.get("owners") or {})
    owner_names = ", ".join(display_engine(o) for o in owners) or "its owner engine"
    read = _plural(n, "read")
    return {
        "title": f"Cache hot reads with {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [retained_engine] if retained_engine else [],
        "serves_from": [],
        "tables": [],
        "table_count": 0,
        "table_groups": None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "calls",
        "rationale": (
            f"{n} hot {read} ({share:.1f}% of calls), cache-aside in front of {owner_names}: "
            "no data migration, fully reversible. It takes the read pressure off the source "
            "database first, de-risking the data migrations that follow."
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
) -> dict[str, Any] | None:
    rows = _owned_tables(engine, table_assignments)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    groups = _dynamodb_table_groups(rows, co_dependency_groups, query_assignments)
    n_groups = len(groups) or len(rows)
    query = _plural(n, "query").replace("querys", "queries")
    group = _plural(n_groups, "group")
    return {
        "title": f"Move key-value and point-lookup queries to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [retained_engine] if retained_engine else [],
        "serves_from": [],
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": groups or None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} key-value and point-lookup {query} ({share:.1f}% of the workload) across "
            f"{n_groups} table {group}, respecting co-dependent tables: the pattern "
            f"{display_engine(engine)} fits best, and the smallest-blast-radius data migration "
            "available once Wave 1's cache has absorbed the read pressure."
        ),
        "gate": "Dual-write/backfill validated and query parity confirmed per table group.",
    }


def _other_target_wave(
    engine: str,
    by_engine: dict[str, dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    retained_engine: str | None,
) -> dict[str, Any] | None:
    rows = _owned_tables(engine, table_assignments)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    query = _plural(n, "query").replace("querys", "queries")
    return {
        "title": f"Move the remaining workload to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [retained_engine] if retained_engine else [],
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
) -> dict[str, Any] | None:
    rows = _owned_tables(engine, table_assignments)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    query = _plural(n, "query").replace("querys", "queries")
    return {
        "title": f"Move document-shaped data to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [retained_engine] if retained_engine else [],
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
) -> dict[str, Any] | None:
    rows = _served_tables(engine, table_assignments)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    owners = sorted(
        {
            str(t.get("primary_engine"))
            for t in rows
            if t.get("primary_engine") and t["primary_engine"] != engine
        }
    )
    owner_names = ", ".join(display_engine(o) for o in owners) or "the engines that own its tables"
    query = _plural(n, "query").replace("querys", "queries")
    return {
        "title": f"Sync search and analytics read models to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [],
        "serves_from": owners,
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} search/analytics {query} ({share:.1f}% of the workload) build a read model "
            f"in {display_engine(engine)}, kept in sync (zero-ETL, OpenSearch Ingestion or CDC "
            f"per table) from {owner_names}, which keep durable ownership: "
            f"{display_engine(engine)} never becomes the system of record, and recovery is "
            "always by re-indexing, never a data migration."
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
) -> dict[str, Any] | None:
    rows = _owned_tables(engine, table_assignments)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    query = _plural(n, "query").replace("querys", "queries")
    return {
        "title": f"Keep the rest on {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [],
        "serves_from": [],
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} {query} ({share:.1f}% of the workload) stay on {display_engine(engine)}, "
            "carried over 1:1 with no migration: it is already source-compatible, so it is the "
            "lowest-risk engine to finish the roadmap on."
        ),
        "gate": "End state: every earlier wave's gate has passed; decommission the legacy source engine.",
    }


def build_migration_waves(
    *,
    ranking: list[dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    query_assignments: list[dict[str, Any]],
    co_dependency_groups: list[list[str]],
    cache_overlay: dict[str, Any] | None,
    source_engine: str,
) -> list[dict[str, Any]] | None:
    """The incremental migration roadmap for this report, or ``None`` with no assignment.

    Pure function of the synthesis data already loaded for ``ranking``,
    ``table_mappings``/``cache_overlay`` and the assignment artifact
    (``table_assignments``, ``query_assignments``, ``co_dependency_groups``):
    no model call, so the same inputs always produce the same waves. See the
    module docstring for the sequencing rule.
    """
    if not ranking:
        return None
    by_engine = {str(r["target"]): r for r in ranking if r.get("target")}
    retained_engine = SOURCE_ENGINE_TO_AURORA.get((source_engine or "").lower())

    waves: list[dict[str, Any]] = []

    cache = _cache_wave(cache_overlay, retained_engine)
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
        )
        if wave:
            waves.append(wave)

    others = sorted(
        (e for e in by_engine if e not in _NAMED_ENGINES),
        key=lambda e: (-float(by_engine[e].get("workload_percent") or 0.0), e),
    )
    for engine in others:
        wave = _other_target_wave(engine, by_engine, table_assignments, retained_engine)
        if wave:
            waves.append(wave)

    for engine in sorted(SEARCH_ENGINES & set(by_engine)):
        wave = _search_wave(engine, by_engine, table_assignments)
        if wave:
            waves.append(wave)

    for engine in sorted(DOCUMENT_ENGINES & set(by_engine)):
        wave = _document_wave(engine, by_engine, table_assignments, retained_engine)
        if wave:
            waves.append(wave)

    if retained_engine and retained_engine in by_engine:
        wave = _retained_wave(retained_engine, by_engine, table_assignments)
        if wave:
            waves.append(wave)

    if not waves:
        return None
    for i, wave in enumerate(waves, start=1):
        wave["wave"] = i
    return waves
