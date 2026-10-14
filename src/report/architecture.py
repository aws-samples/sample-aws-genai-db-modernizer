"""The target architecture section: per-engine scope, table counts and the SVG diagram."""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine

from . import escaping
from .engine_roles import _ROLE_STROKE, SEARCH_READ_MODEL
from .facts import report_facts
from .formatting import _fmt_usd, fmt_num, plural_noun

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


def architecture_svg(report: dict[str, Any]) -> str:
    """Inline SVG of the full target architecture.

    Source on the left, every engine that carries workload on the right, colored
    by role (retained / migration target / cache) with the cache layer dashed.
    Driven by ``_architecture_engines`` so the diagram, the recommendation table
    and the cost total all reflect the same 5-engine picture rather than the
    3-engine ``recommended_architecture.databases`` projection.

    Light theme, self-contained, no external fetch, renders offline.
    """
    src = report.get("database_name", "source")
    engines = _architecture_engines(report)

    def esc(s: Any) -> str:
        return escaping.svg_text(s)

    box_w, box_h, gap = 230, 54, 20
    left_x, right_x = 24, 380
    n = max(len(engines), 1)
    body_top = 20
    stack_h = n * (box_h + gap) - gap
    height = max(body_top + stack_h + 20, 140)
    width = right_x + box_w + 24
    src_y = body_top + stack_h // 2 - box_h // 2

    def box(x, y, w, h, title, sub, stroke, dashed=False):
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        ty = y + (h / 2 - 5 if sub else h / 2 + 4)
        title_t = (
            f'<text x="{x + w / 2}" y="{ty}" text-anchor="middle" font-size="14" '
            f'font-weight="600" fill="#111">{esc(title)}</text>'
        )
        sub_t = (
            f'<text x="{x + w / 2}" y="{y + h / 2 + 13}" text-anchor="middle" '
            f'font-size="10.5" fill="#555">{esc(sub)}</text>'
            if sub
            else ""
        )
        return (
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" fill="#fff" '
            f'stroke="{stroke}" stroke-width="2"{dash}/>{title_t}{sub_t}'
        )

    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '  # nosemgrep: string-concat-in-list -- intentional multi-line string
        f'viewBox="0 0 {width} {height}" font-family="-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">',
        '<defs><marker id="arr" markerWidth="9" markerHeight="9" refX="7" refY="3" '  # nosemgrep: string-concat-in-list -- intentional multi-line string
        'orient="auto"><path d="M0,0 L7,3 L0,6 Z" fill="#146EB4"/></marker></defs>',
        f'<rect width="{width}" height="{height}" fill="#fff"/>',
        box(left_x, src_y, box_w, box_h, str(src), "source database", "#232F3E"),
    ]
    for i, e in enumerate(engines):
        y = body_top + i * (box_h + gap)
        role = e["role"]
        stroke = _ROLE_STROKE.get(role, "#6B7280")
        dashed = role == "Cache layer"
        sub_bits = [role]
        if e.get("cached_queries") is not None:
            # Room for role + cache counts only; the cost is in the table below
            sub_bits.append(
                f"{e['cached_queries']} {plural_noun(e['cached_queries'], 'read')} \u00b7 "
                f"{fmt_num(e['cached_call_share'], 1)}% of calls"
            )
        else:
            if isinstance(e.get("workload"), (int, float)):
                sub_bits.append(f"{fmt_num(e['workload'], 1)}%")
            if e.get("cost") is not None:
                sub_bits.append(f"{_fmt_usd(e['cost'])}/mo")
        sub = "  \u00b7  ".join(sub_bits)
        p.append(
            box(right_x, y, box_w, box_h, display_engine(e["engine"]), sub, stroke, dashed=dashed)
        )
        dash_line = ' stroke-dasharray="5 4"' if dashed else ""
        marker = "" if dashed else ' marker-end="url(#arr)"'
        p.append(
            f'<line x1="{left_x + box_w}" y1="{src_y + box_h / 2}" x2="{right_x}" '
            f'y2="{y + box_h / 2}" stroke="{stroke}" stroke-width="1.5"{dash_line}{marker}/>'
        )
    p.append("</svg>")
    return "".join(p)


_ROLE_PHRASE = {
    "Migration target": "migrate",
    "Cache layer": "to the cache layer",
    "Retained": "stay on the source engine",
    SEARCH_READ_MODEL: "to the search read model",
}


def _mapping_split(report: dict[str, Any], mappings: list[dict[str, Any]] | None = None) -> str:
    """ ": 21 migrate, 1 to the cache layer" for the migration map heading (#258).

    The Decision Report and the deck count only the tables mapped to migration
    targets as "tables migrate"; a table mapped to the cache layer needs no data
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
