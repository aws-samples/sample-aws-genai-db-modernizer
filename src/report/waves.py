"""Resolve a report's migration waves (#225), with the legacy fallback for a
report synthesized before ``migration_waves`` existed."""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine, display_source_database
from src.shared.migration_wave_engines import CACHE_ENGINES as _WAVE_CACHE_ENGINES
from src.shared.migration_wave_engines import DOCUMENT_ENGINES as _WAVE_DOCUMENT_ENGINES
from src.shared.migration_wave_engines import KV_ENGINES as _WAVE_KV_ENGINES
from src.shared.migration_wave_engines import NAMED_ENGINES as _WAVE_NAMED_ENGINES
from src.shared.migration_wave_engines import RELATIONAL_ENGINES as _WAVE_RELATIONAL_ENGINES
from src.shared.migration_wave_engines import SEARCH_ENGINES as _WAVE_SEARCH_ENGINES
from src.shared.migration_wave_engines import cache_front_description

from .formatting import fmt_num, plural_noun

# The wave-specific engine buckets come from src/shared/migration_wave_engines.py
# (#225), the same ones src.agents.referee.migration_waves
# uses. The cache bucket is the same shared set engine_roles uses for role labels;
# the relational bucket is narrower than RELATIONAL_ENGINES in the facts layer (which
# also recognizes generic aurora for whole-report role classification):
# src/report never imports src/agents (clean layering), so both modules import
# the shared bucket set instead of each hand-mirroring the other's copy.

# A raw source-engine key (collector's source_database_engine) a legacy report's
# retained Aurora engine implies, best-effort: report.json has no source_engine
# field of its own, only the Aurora target it was mapped to.
_RELATIONAL_TO_SOURCE_ENGINE = {"aurora_mysql": "mysql", "aurora_postgresql": "postgresql"}


def resolve_migration_waves(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The incremental migration roadmap (#225): ``report['migration_waves']``
    when synthesis wrote one, else derived the same way from what report.json
    still has (``ranking``, ``cache_overlay``, ``table_mappings``).

    Every deliverable (deck, decision report, engineering report, UI) calls
    this one function, so a report synthesized before this field existed still
    shows the same roadmap shape as a new one — just with less table detail,
    since the legacy path has no assignment-level table groups.
    """
    waves = report.get("migration_waves")
    if waves:
        return [w for w in waves if isinstance(w, dict)]
    return _legacy_migration_waves(report)


def _legacy_migration_waves(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-derive the roadmap from a pre-#225 ``report.json`` with no stored
    ``migration_waves`` at all.

    #562 scoped a live wave's tables by the queries its engine actually
    serves (``_served_tables``) instead of the table-level ``primary_engine``
    plurality (``_owned_tables``) -- query_assignments carries the data for
    that. This fallback has no such luck: a legacy ``report.json`` kept only
    ``table_mappings`` (``source_table`` -> one ``recommended_database``,
    itself a table-level summary with no query behind it) and ``ranking``'s
    aggregate counts, never ``query_assignments``. There is no served set to
    recompute from here -- ``tables_for`` stays table-level because the data
    the real fix needs simply is not there, not because a wave's scope is
    legitimately table-level (#568 review, Suggestion 3: it is not -- #562
    is the one rule). A report new enough to carry ``migration_waves`` never
    reaches this function at all.
    """
    ranking = [r for r in (report.get("ranking") or []) if isinstance(r, dict)]
    by_engine = {str(r.get("target")): r for r in ranking if r.get("target")}
    mappings = [m for m in (report.get("table_mappings") or []) if isinstance(m, dict)]

    def tables_for(engine: str) -> list[str]:
        return sorted(
            {
                str(m.get("source_table"))
                for m in mappings
                if m.get("recommended_database") == engine and m.get("source_table")
            }
        )

    retained_engine = next(
        (
            e
            for e in sorted(_WAVE_RELATIONAL_ENGINES)
            if e in by_engine
            and (
                by_engine[e].get("assigned_queries")
                or tables_for(e)
                or by_engine[e].get("workload_percent")
            )
        ),
        None,
    )
    # Best-effort raw source-engine key, for `fronts`/`moves_from` (#225):
    # report.json has no source_engine field, only the Aurora target it mapped to.
    source_engine = _RELATIONAL_TO_SOURCE_ENGINE.get(retained_engine or "")
    # #321: later waves move from the retained Aurora engine (wave 1 has
    # already reached it), falling back to the legacy source only when there
    # is no retained engine to resolve (no Aurora wave was possible either).
    moves_from_engine = retained_engine or source_engine
    # The cache fronts Aurora once wave 1 exists; otherwise it still fronts
    # the legacy source database directly, same as before #321.
    front_engine = retained_engine or source_engine
    front_label = (
        display_engine(retained_engine)
        if retained_engine
        else (
            display_source_database(source_engine)
            if source_engine
            else "the current source database"
        )
    )

    waves: list[dict[str, Any]] = []

    # Wave 1 (#321): the whole source database to the retained Aurora engine
    # first, schema carried over 1:1. A legacy report's retained_engine is
    # only ever resolved from `_WAVE_RELATIONAL_ENGINES`, i.e. always a
    # source-compatible mapping -- the legacy fallback never has enough data
    # to detect a heterogeneous source, so this wave is always "homogeneous"
    # when present at all.
    if retained_engine:
        all_tables = sorted({str(m.get("source_table")) for m in mappings if m.get("source_table")})
        n_retained = int(by_engine.get(retained_engine, {}).get("assigned_queries") or 0)
        share_retained = round(
            float(by_engine.get(retained_engine, {}).get("workload_percent") or 0.0), 1
        )
        waves.append(
            {
                "title": f"Move to {display_engine(retained_engine)}",
                "engines": [retained_engine],
                "moves_from": [source_engine] if source_engine else [],
                "serves_from": [],
                "fronts": None,
                "tables": all_tables,
                "table_count": len(all_tables),
                "table_groups": None,
                "homogeneity": "homogeneous",
                "query_count": n_retained,
                "workload_share_percent": share_retained,
                "share_basis": "queries",
                "rationale": (
                    f"The whole source database moves to {display_engine(retained_engine)} "
                    "first, schema carried over 1:1 (every collected table and view): a "
                    "homogeneous migration (same engine family). "
                    f"{n_retained} {plural_noun(n_retained, 'query', 'queries')} "
                    f"({fmt_num(share_retained, 1)}% of query patterns) are still assigned to "
                    f"{display_engine(retained_engine)} once every later wave in this roadmap "
                    "has moved its own share away; every later wave's table count is a subset "
                    "of this wave's tables, never an addition to them."
                ),
                "gate": (
                    "Schema and data parity validated against the source database before "
                    "cutover; decommission the legacy source once replication lag is zero."
                ),
            }
        )

    overlay = report.get("cache_overlay") or {}
    cache_engine = overlay.get("engine")
    n_cached = int(overlay.get("query_count") or 0)
    share_basis = "calls"
    share = round(float(overlay.get("call_share_percent") or 0.0), 1)
    if not (cache_engine in _WAVE_CACHE_ENGINES and n_cached):
        # Older shape: cache stats embedded on the ranking entry itself
        # (``role: "cache_layer"``, #296) with no top-level ``cache_overlay``.
        cache_rank = next(
            (
                r
                for e, r in by_engine.items()
                if e in _WAVE_CACHE_ENGINES and r.get("cache_overlay_queries")
            ),
            None,
        )
        if cache_rank:
            cache_engine = cache_rank.get("target")
            n_cached = int(cache_rank.get("cache_overlay_queries") or 0)
            share = round(float(cache_rank.get("cache_call_share_percent") or 0.0), 1)
        else:
            # Even older shape (pre-#296): the cache engine listed directly in
            # ``ranking`` with its own workload share, no cache stats anywhere.
            cache_engine = next((e for e in sorted(_WAVE_CACHE_ENGINES) if e in by_engine), None)
            cache_rank_entry = by_engine.get(cache_engine) if cache_engine else None
            n_cached = int((cache_rank_entry or {}).get("assigned_queries") or 0)
            share = round(float((cache_rank_entry or {}).get("workload_percent") or 0.0), 1)
            share_basis = "queries"
    if cache_engine in _WAVE_CACHE_ENGINES and (n_cached or share):
        waves.append(
            {
                "title": f"Cache hot reads with {display_engine(cache_engine)}",
                "engines": [cache_engine],
                "moves_from": [],
                "serves_from": [],
                "fronts": front_engine,
                "tables": [],
                "table_count": 0,
                "table_groups": None,
                "query_count": n_cached,
                "workload_share_percent": share,
                "share_basis": share_basis,
                "rationale": (
                    f"{n_cached} hot {plural_noun(n_cached, 'read')} "
                    f"({fmt_num(share, 1)}% of {'calls' if share_basis == 'calls' else 'query patterns'}), "
                    "cache-aside in front of "
                    f"{cache_front_description(retained_engine, overlay.get('owners')) if retained_engine else front_label}"
                    ": no data migration, fully reversible."
                ),
                "gate": "Cache hit rate and invalidation verified against the source database.",
            }
        )

    def migration_wave(engine: str, title: str) -> dict[str, Any] | None:
        tables = tables_for(engine)
        n = int(by_engine.get(engine, {}).get("assigned_queries") or 0)
        share = round(float(by_engine.get(engine, {}).get("workload_percent") or 0.0), 1)
        # The oldest report shape tracks workload_percent only, no assigned_queries
        # at all (always 0) -- a nonzero share is still "something to move".
        if not n and not tables and not share:
            return None
        return {
            "title": title,
            "engines": [engine],
            # #321: moves from the retained Aurora engine once wave 1 has reached
            # it, else the legacy source (no wave 1 was possible).
            "moves_from": [moves_from_engine] if moves_from_engine else [],
            "serves_from": [],
            "tables": tables,
            "table_count": len(tables),
            "table_groups": None,
            "query_count": n,
            "workload_share_percent": share,
            "share_basis": "queries",
            "rationale": (
                f"{n} {plural_noun(n, 'query', 'queries')} ({fmt_num(share, 1)}% of query "
                f"patterns) to {display_engine(engine)}."
            ),
            "gate": "Query parity confirmed before the next wave.",
        }

    for engine in sorted(_WAVE_KV_ENGINES & set(by_engine)):
        wave = migration_wave(
            engine, f"Move key-value and point-lookup queries to {display_engine(engine)}"
        )
        if wave:
            waves.append(wave)

    for engine in sorted(_WAVE_DOCUMENT_ENGINES & set(by_engine)):
        wave = migration_wave(engine, f"Move document-shaped data to {display_engine(engine)}")
        if wave:
            waves.append(wave)

    others = sorted(
        (e for e in by_engine if e not in _WAVE_NAMED_ENGINES),
        key=lambda e: (-float(by_engine[e].get("workload_percent") or 0.0), e),
    )
    for engine in others:
        wave = migration_wave(engine, f"Move the remaining workload to {display_engine(engine)}")
        if wave:
            waves.append(wave)

    for engine in sorted(_WAVE_SEARCH_ENGINES & set(by_engine)):
        n = int(by_engine.get(engine, {}).get("assigned_queries") or 0)
        tables = tables_for(engine)
        share = round(float(by_engine.get(engine, {}).get("workload_percent") or 0.0), 1)
        if not n and not tables and not share:
            continue
        fallback_owner = retained_engine or source_engine
        lead_in = (
            f"{n} search/analytics {plural_noun(n, 'query', 'queries')} "
            f"({fmt_num(share, 1)}% of query patterns) build a read model in "
            f"{display_engine(engine)}"
        )
        if tables:
            rationale = (
                f"{lead_in}, synced from the engines that own its tables: "
                f"{display_engine(engine)} never becomes the system of record, and recovery is "
                "always by re-indexing."
            )
            serves_from: list[str] = []
        else:
            owner_label = (
                display_engine(fallback_owner) if fallback_owner else "the source database"
            )
            rationale = (
                f"{lead_in}. The indexed tables could not be resolved from the table mappings, "
                f"so {owner_label} is treated as the owner of record until they can be."
            )
            serves_from = [fallback_owner] if fallback_owner else []
        waves.append(
            {
                "title": f"Sync search and analytics read models to {display_engine(engine)}",
                "engines": [engine],
                "moves_from": [],
                "serves_from": serves_from,
                "tables": tables,
                "table_count": len(tables),
                "table_groups": None,
                "query_count": n,
                "workload_share_percent": share,
                "share_basis": "queries",
                "rationale": rationale,
                "gate": "Sync lag inside SLA and every served table still has a durable owner.",
            }
        )

    for i, wave in enumerate(waves, start=1):
        wave["wave"] = i
    return waves
