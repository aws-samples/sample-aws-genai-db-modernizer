"""The target architecture section: per-engine scope, table counts and the SVG diagram."""

from __future__ import annotations

import re
from typing import Any

from src.shared.engine_names import display_engine

from . import escaping
from .cache_layer import cache_layer_counts
from .engine_roles import _ROLE_STROKE, SEARCH_READ_MODEL, _engine_role
from .formatting import _engine_costs, _fmt_usd, fmt_num, plural_noun
from .roadmap import resolve_migration_waves

# Scope-cell suffix for source tables an engine's design serves although their
# recommended engine is another one, and the footnote that defines it (#257).
SHARED_TABLES_LABEL = "incl. shared"
SHARED_TABLES_NOTE = (
    "\u201cincl. shared\u201d counts the source tables the engine\u2019s schema design "
    "serves, including tables also served by another engine."
)


def _design_source_tables(tables: Any) -> int:
    """Distinct source tables an engine's schema design reads from."""
    if not isinstance(tables, list):
        return 0
    return len(
        {
            str(src)
            for t in tables
            if isinstance(t, dict)
            for src in (t.get("source_tables") or [])
            if isinstance(t.get("source_tables"), list)
        }
    )


def _rationale_text(value: Any) -> str:
    """A rationale as text; legacy reports may carry a list of reasons or nothing."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v)
    return ""


def _architecture_engines(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The full target architecture, one entry per engine, ordered by workload.

    Derived from ``ranking`` (every engine that carries workload) joined with
    ``tco_analysis.cost_breakdown``, ``recommended_architecture.databases`` (source
    table counts for migration targets) and ``schema_designs`` (design status and
    cache-object counts). This is the picture synthesis actually computed;
    ``recommended_architecture.databases`` alone is a lossy projection of it that
    neither reconciles to the cost total nor matches the executive summary.
    """
    ranking = [r for r in (report.get("ranking") or []) if isinstance(r, dict)]
    arch = report.get("recommended_architecture") or {}
    dbs = [d for d in (arch.get("databases") or []) if isinstance(d, dict)]
    recommended = {d.get("service") for d in dbs}
    src_tables = {d.get("service"): d.get("table_count") for d in dbs}
    schema_designs = report.get("schema_designs") or {}
    costs = _engine_costs(report)
    # #524: per-engine fallback for the branch below, read from the same
    # per-wave ``table_count`` the tile and the roadmap already use.
    #
    # #568 review (Important 2): a wave with more than one engine (the
    # merged DynamoDB/DocumentDB wave) has one ``table_count`` for the union
    # of both engines' served tables -- crediting it to every engine in
    # ``engines`` (the pre-review behaviour) double counted (discourse:
    # DynamoDB and DocumentDB both showed the union's 200, not their real
    # 118/82). ``table_counts_by_engine``/``owned_counts_by_engine`` (set
    # only on such a wave) give the real per-engine split; a single-engine
    # wave still just uses its own ``table_count``/``table_owners`` directly.
    # A multi-engine wave written before this field existed is skipped
    # rather than guessed at -- contributing nothing undercounts, but never
    # double counts, and every other total here already falls back past a
    # wave fallback that reads 0.
    wave_served: dict[str, int] = {}
    wave_owned: dict[str, int] = {}
    for w in resolve_migration_waves(report):
        if not (w.get("moves_from") and w.get("homogeneity") is None):
            continue
        engines_here = w.get("engines") or []
        per_engine_served = w.get("table_counts_by_engine")
        per_engine_owned = w.get("owned_counts_by_engine")
        if per_engine_served and per_engine_owned:
            for e in engines_here:
                wave_served[e] = wave_served.get(e, 0) + int(per_engine_served.get(e) or 0)
                wave_owned[e] = wave_owned.get(e, 0) + int(per_engine_owned.get(e) or 0)
        elif len(engines_here) == 1:
            e = engines_here[0]
            served_n = int(w.get("table_count") or 0)
            owned_n = served_n - len(w.get("table_owners") or [])
            wave_served[e] = wave_served.get(e, 0) + served_n
            wave_owned[e] = wave_owned.get(e, 0) + max(owned_n, 0)

    rows: list[dict[str, Any]] = []
    for r in ranking:
        eng = r.get("target")
        if not eng:
            continue
        role = _engine_role(eng, recommended, schema_designs, r.get("workload_percent"))
        design_tables = (schema_designs.get(eng) or {}).get("tables")
        objs = len(design_tables) if isinstance(design_tables, list) else None
        migrates = 0
        if role == "Migration target":
            n = src_tables.get(eng)
            has_design = (schema_designs.get(eng) or {}).get("status") == "completed"
            wave_owned_n: int | None = None
            if not n and not has_design:
                # No schema design has mapped source tables to this engine yet
                # (table_mappings/table_count stay 0): read the same wave figure
                # the tile and the roadmap already show, instead of a bare 0.
                wave_n = wave_served.get(eng)
                n = wave_n if isinstance(wave_n, int) and wave_n > 0 else n
                # #568 review (Important 1): the wave figure above is the
                # SERVED count (every table this engine's queries touch,
                # #562) -- shown in the Scope cell as-is. "Tables migrate"
                # must count only the tables that actually move, i.e. the
                # subset this engine durably owns; a served-but-not-owned
                # table stays on its real owner, kept in sync, and never
                # migrates anywhere.
                wave_owned_n = wave_owned.get(eng)
            migrates = (
                wave_owned_n
                if isinstance(wave_owned_n, int)
                else (n if isinstance(n, int) else (objs or 0))
            )
            served = _design_source_tables(design_tables)
            scope = (
                # Source tables mapped to the engine -- named as such, because the
                # summary also counts the schema design's *target* tables (#219).
                # Queries, not tables, are assigned, so the design can also serve
                # tables mapped to another engine; say so when it does (#257).
                f"{n} source {plural_noun(n, 'table')}"
                + (
                    f" ({served} {SHARED_TABLES_LABEL})"
                    if isinstance(n, int) and served > n
                    else ""
                )
                if n is not None
                else (f"{objs} {plural_noun(objs, 'target object')}" if objs else "\u2014")
            )
        elif role == SEARCH_READ_MODEL:
            # Indexes data synced from the owners; nothing migrates to it
            scope = (
                f"{objs} {plural_noun(objs, 'index', 'indexes')}, synced from owners"
                if objs
                else "synced from owners"
            )
        elif role == "Cache layer":
            scope = f"{objs} key {plural_noun(objs, 'design')}" if objs else "cache"
            cached = cache_layer_counts(report, eng)
            if cached and not objs:
                scope = "cache-aside"
        elif role == "Retained":
            scope = "source schema retained"
        elif role == "Evaluated":
            scope = "no queries assigned"
        else:
            scope = "\u2014"
        cached = cache_layer_counts(report, eng) if role == "Cache layer" else None
        rows.append(
            {
                "engine": eng,
                "role": role,
                # The cache layer owns no query: no workload share, its cached reads
                # and their share of calls instead (#296)
                "workload": None if cached else r.get("workload_percent"),
                "cached_queries": cached[0] if cached else None,
                "cached_call_share": cached[1] if cached else None,
                "scope": scope,
                # Mapped source tables that move, for the "tables migrate" totals;
                # the scope text is not parsed because it can carry two numbers.
                "migrates": migrates,
                "cost": costs.get(eng),
                # Synthesis's per-engine rationale, built from the routed workload
                # (#152); the ranking entry carries it for engines `databases` omits
                "rationale": _rationale_text(
                    next((d.get("rationale") for d in dbs if d.get("service") == eng), None)
                    or r.get("rationale")
                ),
            }
        )
    rows.sort(
        key=lambda x: (x["workload"] if isinstance(x["workload"], (int, float)) else -1),
        reverse=True,
    )
    return rows


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


def _mapping_split(report: dict[str, Any], mappings: list[dict[str, Any]]) -> str:
    """ ": 21 migrate, 1 to the cache layer" for the migration map heading (#258).

    The Decision Report and the deck count only the tables mapped to migration
    targets as "tables migrate"; a table mapped to the cache layer needs no data
    migration. Empty when every mapped table migrates.
    """
    roles = {e["engine"]: e["role"] for e in _architecture_engines(report)}
    counts: dict[str, int] = {}
    for m in mappings:
        phrase = _ROLE_PHRASE.get(roles.get(m.get("recommended_database"), ""), "elsewhere")
        counts[phrase] = counts.get(phrase, 0) + 1
    if set(counts) <= {"migrate"}:
        return ""
    order = [*_ROLE_PHRASE.values(), "elsewhere"]
    return ": " + ", ".join(f"{counts[p]} {p}" for p in order if counts.get(p))


# "Analyzed 311 source tables and 1654 query patterns across 5 target
# database(s)." -- ``build_summary``'s own fixed, deterministic opening
# sentence (never the LLM-rewritten ``summary``, only ``summary_deterministic``,
# whose format synthesis itself controls). Same precedent as
# ``formatting._RESOLVED_RISKS``/``_PER_ENGINE_APS``: read a count back out of this
# one deterministic string rather than re-derive an approximation of it.
_ANALYZED_SOURCE_TABLES = re.compile(r"\bAnalyzed (\d+) source tables?\b")


def _n_tables_mapped(report: dict[str, Any]) -> tuple[int, bool]:
    """(table count, from a completed schema design) for "N tables mapped to a
    named target" (#530).

    ``table_mappings`` gives the precise, per-table figure once a schema
    design has run. Without one every entry is empty, so this read 0 while
    the "Tables migrate" tile and roadmap, read from the same assignment,
    already named real tables — the deck's own basis for the figure
    contradicted the figure it was supposed to support.

    Without a completed design, prefers ``source_table_count``
    (``SynthesisOutputContract``, #530 review): the collected schema's own
    tables-only total (no views), the same figure ``build_summary``'s
    "Analyzed N source tables ..." opening sentence already states, now a
    structured field. A report synthesized before this field existed has
    none, so this falls back to parsing that sentence out of
    ``summary_deterministic`` (``_ANALYZED_SOURCE_TABLES``), and only then to
    the migration roadmap's own per-wave table lists
    (``resolve_migration_waves``). Those lists mix in views -- wave 1
    carries "every collected table and view" (#321) -- so their union can
    overcount against the tables-only total by exactly the view count
    (#530 round 2: discourse's ``badge_posts`` view inflated it from 311 to
    312); it is a last resort only, for a report old enough to carry
    neither ``source_table_count`` nor a parseable summary.

    The second element says whether the figure came from a completed
    design, so a caller can avoid claiming a per-table confidence score
    that does not exist without one.
    """
    mappings = [m for m in (report.get("table_mappings") or []) if isinstance(m, dict)]
    if mappings:
        return len(mappings), True
    count = report.get("source_table_count")
    if isinstance(count, int):
        return count, False
    m = _ANALYZED_SOURCE_TABLES.search(str(report.get("summary_deterministic") or ""))
    if m:
        return int(m.group(1)), False
    tables: set[str] = set()
    for w in resolve_migration_waves(report):
        tables.update(str(t) for t in (w.get("tables") or []) if t)
    return len(tables), False
