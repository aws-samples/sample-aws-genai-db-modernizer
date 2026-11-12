# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The target architecture section: per-engine scope and table counts."""

from __future__ import annotations

from typing import Any

from src.shared.cache_engines import CACHE_ENGINES
from src.shared.engine_names import display_engine
from src.shared.migration_wave_engines import (
    cache_front_description,
    cache_front_is_staged,
    cache_front_reference,
)
from src.shared.report_facts import (
    MAP_CACHE_LAYER,
    MAP_KEPT_IN_SYNC,
    MAP_MIGRATE,
    MAP_OTHER,
    MAP_SEARCH_READ_MODEL,
    MAP_SPLIT_ORDER,
    MAP_STAYS,
    wave_retained_engine,
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
# Migration map heading and the notes that define them. "Kept current by CDC" is the wave
# plan's rule (#581): an engine outside the wave owns the table.
MIGRATE_LABEL = "migrate"
KEPT_IN_SYNC_LABEL = "kept current by CDC"
KEPT_IN_SYNC_NOTE = (
    "In the Scope column, \u201cserved\u201d is the source tables a migration target\u2019s queries "
    "read. The tables it owns migrate, and the Total counts only those. A table another "
    "engine of the same wave owns moves to that engine. A table an engine outside the "
    "wave owns stays there and is kept current by CDC from it, as in the wave plan."
)
KEPT_IN_SYNC_SOURCES_NOTE = (
    "These counts are about reads; Sources of truth and replicas below lists where "
    "each written table's writes land and which engines hold copies of it."
)


def counted_once_note(engines: list[dict[str, Any]]) -> str:
    """ "A table kept current by CDC that DynamoDB and DocumentDB both read is counted
    once, in the DynamoDB row." (#2162), from each row's ``kept_in_sync_counted_under``;
    empty when no row states one."""
    pairs = [
        (str(o), str(e["engine"]))
        for e in engines
        for o, k in (e.get("kept_in_sync_counted_under") or {}).items()
        if k
    ]
    return " ".join(
        f"A table {KEPT_IN_SYNC_LABEL} that {display_engine(o)} and {display_engine(e)} both "
        f"read is counted once, in the {display_engine(o)} row."
        for o, e in pairs
    )


def counted_under_text(n: int, engine: str) -> str:
    """ "5 kept current by CDC, also read by DynamoDB (counted in its row)" (#2162)."""
    return f"{n} {KEPT_IN_SYNC_LABEL}, also read by {display_engine(engine)} (counted in its row)"


MAP_SPLIT_NOTE = (
    "Migrate counts the mapped tables a migration wave moves, which the Target engines "
    "Total also counts; kept current by CDC counts the ones a migration wave reads while an "
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
        # wave, and how many stay with an owner outside the wave, kept current by CDC.
        # #2162: a kept-current table an earlier engine of the wave also reads is in that
        # engine's count; this one says so, so the rows add up to the wave's figure.
        kept = e.get("kept_in_sync_tables") or 0
        moves_to = e.get("moves_to_tables") or {}
        under = e.get("kept_in_sync_counted_under") or {}
        parts = [f"{e.get('tables_migrate', 0)} {MIGRATE_LABEL}"]
        parts += [f"{k} move to {display_engine(o)}" for o, k in moves_to.items() if k]
        parts += [f"{kept} {KEPT_IN_SYNC_LABEL}"] if kept else []
        parts += [counted_under_text(k, o) for o, k in under.items() if k]
        splits = bool(kept) or any(moves_to.values()) or any(under.values())
        split = f" served: {', '.join(parts)}" if splits else ""
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


def _wave_plan_rationale(e: dict[str, Any], report: dict[str, Any]) -> str:
    """An engine's "Why" text as a report that prints the wave plan shows it (#2171).

    The stored cache-layer rationale states a staged front in full ("cache-aside in front
    of Aurora PostgreSQL in wave 2, then DynamoDB once wave 3 moves these reads"), for
    the chat summary and the summary model, which print no wave plan. The wave 2
    rationale states it too, so here a staged front (``cache_front_is_staged``, decided
    from the wave plan's retained engine and the overlay's owners) becomes a reference
    to the wave plan. Any other text is returned as stored."""
    text = str(e.get("rationale") or "")
    if e.get("role") != "Cache layer":
        return text
    waves = report.get("migration_waves") or []
    if not any(set(w.get("engines") or []) & CACHE_ENGINES for w in waves):
        return text
    retained = wave_retained_engine(waves)
    owners = (report.get("cache_overlay") or {}).get("owners")
    if not cache_front_is_staged(retained, owners):
        return text
    staged = f"in front of {cache_front_description(retained, owners)}"
    return text.replace(staged, f"in front of {cache_front_reference(retained, owners)}", 1)


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
            "kept_in_sync_counted_under": e.get("kept_in_sync_counted_under"),
            "cost": e.get("monthly_cost_usd"),
            "cost_sizing": e.get("cost_sizing"),
            "rationale": _wave_plan_rationale(e, report),
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
    """ ": 15 migrate, 3 kept current by CDC, 1 to the cache layer" for the migration map
    heading (#258, #1169).

    The counts are the facts layer's ``table_basis.mapped_tables_by_move``: a table
    migrates when its migrating wave moves it, so "N migrate" counts on the same
    basis as the Target engines Total's "N tables migrate". Empty when every mapped
    table migrates. ``mappings`` is accepted for older callers and not read.
    """
    by_move = (report_facts(report).get("table_basis") or {}).get("mapped_tables_by_move") or {}
    if set(by_move) <= {MAP_MIGRATE}:
        return ""
    # The tables that stay, stay on wave 1's engine: name it, so "stay" is never read as
    # the source database next to wave 1's "the other N stay on Aurora" (#1642).
    retained = wave_retained_engine(report.get("migration_waves"))
    phrase = dict(_MAP_PHRASE)
    if retained:
        phrase[MAP_STAYS] = f"stay on {display_engine(retained)}"
    return ": " + ", ".join(f"{by_move[k]} {phrase[k]}" for k in MAP_SPLIT_ORDER if by_move.get(k))


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
