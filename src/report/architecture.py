"""The target architecture section: per-engine scope and table counts."""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine
from src.shared.report_facts import (
    MAP_CACHE_LAYER,
    MAP_KEPT_IN_SYNC,
    MAP_MIGRATE,
    MAP_OTHER,
    MAP_SEARCH_READ_MODEL,
    MAP_SPLIT_ORDER,
    MAP_STAYS,
)

from .engine_roles import SEARCH_READ_MODEL
from .facts import report_facts
from .formatting import plural_noun

# Scope-cell suffix for source tables an engine's design serves although their
# recommended engine is another one, and the footnote that defines it (#257).
SHARED_TABLES_LABEL = "incl. shared"
SHARED_TABLES_NOTE = (
    "\u201cincl. shared\u201d counts the source tables the engine\u2019s schema design "
    "serves, including tables also served by another engine."
)


# #1169: what a migration target's served tables split into, in the Scope cell, the
# Migration map heading and the notes that define them. "Kept in sync" is the wave
# plan's rule (#581): an engine outside the wave owns the table.
MIGRATE_LABEL = "migrate"
KEPT_IN_SYNC_LABEL = "kept in sync"
KEPT_IN_SYNC_NOTE = (
    "Served counts the source tables a migration target\u2019s queries read. The tables "
    "it owns migrate, and the Total counts only those. A table another engine of the "
    "same wave owns moves to that engine. A table an engine outside the wave owns stays "
    "there and is kept in sync from it, as in the wave plan."
)
KEPT_IN_SYNC_SOURCES_NOTE = (
    "These counts are about reads; Sources of truth and replicas below lists where "
    "each written table's writes land and which engines hold copies of it."
)
MAP_SPLIT_NOTE = (
    "Migrate counts the mapped tables a migration wave moves, which the Target engines "
    "Total also counts; kept in sync counts the ones a migration wave reads while an "
    "engine outside that wave owns them, as in the wave plan."
)


def _scope_text(e: dict[str, Any], report: dict[str, Any]) -> str:
    """The Scope cell for one engine, formatted from its facts (#615)."""
    role = e["role"]
    objs = e.get("target_objects")
    if role == "Migration target":
        n = e.get("engine_source_tables")
        shared = e.get("shared_source_tables")
        if n is None:
            return f"{objs} {plural_noun(objs, 'target object')}" if objs else "\u2014"
        # Source tables mapped to the engine -- named as such, because the
        # summary also counts the schema design's *target* tables (#219).
        # Queries, not tables, are assigned, so the design can also serve
        # tables mapped to another engine; say so when it does (#257).
        # #1169: when it does not own all of them, the cell says how many migrate
        # (the figure the Total counts), how many move to another engine of the same
        # wave, and how many stay with an owner outside the wave, kept in sync.
        kept = e.get("kept_in_sync_tables") or 0
        moves_to = e.get("moves_to_tables") or {}
        parts = [f"{e.get('tables_migrate', 0)} {MIGRATE_LABEL}"]
        parts += [f"{k} move to {display_engine(o)}" for o, k in moves_to.items() if k]
        parts += [f"{kept} {KEPT_IN_SYNC_LABEL}"] if kept else []
        split = f" served: {', '.join(parts)}" if kept or any(moves_to.values()) else ""
        return f"{n} source {plural_noun(n, 'table')}{split}" + (
            f" ({shared} {SHARED_TABLES_LABEL})" if shared else ""
        )
    if role == SEARCH_READ_MODEL:
        # Indexes data synced from the owners; nothing migrates to it
        return (
            f"{objs} {plural_noun(objs, 'index', 'indexes')}, synced from owners"
            if objs
            else "synced from owners"
        )
    if role == "Cache layer":
        if objs:
            return f"{objs} key {plural_noun(objs, 'design')}"
        return "cache-aside" if e.get("cached_queries") is not None else "cache"
    if role == "Retained":
        return "source schema retained"
    if role == "Evaluated":
        return "no queries assigned"
    return "\u2014"


def _architecture_engines(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The full target architecture, one row per engine, ordered by workload.

    Every figure comes from the facts layer (``report_facts(report)["engines"]``,
    #615); this only formats the Scope cell and keeps the row keys the
    renderers already use (``workload``, ``cached_call_share``, ``migrates``,
    ``cost``).
    """
    return [
        {
            "engine": e["engine"],
            "role": e["role"],
            "workload": e.get("workload_percent"),
            "cached_queries": e.get("cached_queries"),
            "cached_call_share": e.get("cached_call_share_percent"),
            "scope": _scope_text(e, report),
            "migrates": e.get("tables_migrate", 0),
            "kept_in_sync": e.get("kept_in_sync_tables"),
            "moves_to": e.get("moves_to_tables"),
            "cost": e.get("monthly_cost_usd"),
            "rationale": e.get("rationale") or "",
        }
        for e in report_facts(report).get("engines") or []
    ]


_MAP_PHRASE = {
    MAP_MIGRATE: MIGRATE_LABEL,
    MAP_KEPT_IN_SYNC: KEPT_IN_SYNC_LABEL,
    MAP_CACHE_LAYER: "to the cache layer",
    MAP_SEARCH_READ_MODEL: "to the search read model",
    MAP_STAYS: "stay on the source engine",
    MAP_OTHER: "elsewhere",
}


def _mapping_split(report: dict[str, Any], mappings: list[dict[str, Any]] | None = None) -> str:
    """ ": 15 migrate, 3 kept in sync, 1 to the cache layer" for the migration map
    heading (#258, #1169).

    The counts are the facts layer's ``table_basis.mapped_tables_by_move``: a table
    migrates when its migrating wave moves it, so "N migrate" counts on the same
    basis as the Target engines Total's "N tables migrate". Empty when every mapped
    table migrates. ``mappings`` is accepted for older callers and not read.
    """
    by_move = (report_facts(report).get("table_basis") or {}).get("mapped_tables_by_move") or {}
    if set(by_move) <= {MAP_MIGRATE}:
        return ""
    return ": " + ", ".join(
        f"{by_move[k]} {_MAP_PHRASE[k]}" for k in MAP_SPLIT_ORDER if by_move.get(k)
    )


def _n_tables_mapped(report: dict[str, Any]) -> tuple[int, bool]:
    """(table count, backed by a complete schema design) for "N tables mapped
    to a named target" (#530), read from the facts layer's ``table_basis``.

    #533: ``True`` only when *every* migration target has a completed
    design; a partial design no longer passes ``len(table_mappings)`` off as
    the complete, fit-scored count while the "Tables migrate" tile adds the
    wave fallback of the engines still without one.
    """
    basis = report_facts(report).get("table_basis") or {}
    return int(basis.get("basis_table_count") or 0), basis.get("design_coverage") == "complete"
