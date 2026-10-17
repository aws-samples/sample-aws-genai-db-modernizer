"""The target architecture section: per-engine scope and table counts."""

from __future__ import annotations

from typing import Any

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
        return f"{n} source {plural_noun(n, 'table')}" + (
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
            "cost": e.get("monthly_cost_usd"),
            "rationale": e.get("rationale") or "",
        }
        for e in report_facts(report).get("engines") or []
    ]


_ROLE_PHRASE = {
    "Migration target": "migrate",
    "Cache layer": "to the cache layer",
    "Retained": "stay on the source engine",
    SEARCH_READ_MODEL: "to the search read model",
}


def _mapping_split(report: dict[str, Any], mappings: list[dict[str, Any]] | None = None) -> str:
    """ ": 21 migrate, 1 to the cache layer" for the migration map heading (#258).

    The engine table's total and the executive report count only the tables mapped to
    migration targets as "tables migrate"; a table mapped to the cache layer needs no data
    migration. Empty when every mapped table migrates. The per-role counts are
    the facts layer's ``table_basis.mapped_tables_by_role`` (#615); ``mappings``
    is accepted for older callers and not read.
    """
    by_role = (report_facts(report).get("table_basis") or {}).get("mapped_tables_by_role") or {}
    if set(by_role) <= {"Migration target"}:
        return ""
    order = [*_ROLE_PHRASE, "Other"]
    return ": " + ", ".join(
        f"{by_role[r]} {_ROLE_PHRASE.get(r, 'elsewhere')}" for r in order if by_role.get(r)
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
