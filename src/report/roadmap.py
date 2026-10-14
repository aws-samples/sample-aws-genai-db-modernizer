"""Migration waves: resolve them from the report and render the roadmap section."""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine, display_source_database
from src.shared.report_facts import wave_facts

from . import escaping
from .engine_roles import _RELATIONAL_ENGINES
from .facts import report_facts
from .facts import wave_facts_by_number as _facts_by_wave
from .formatting import _completed_designs, fmt_num, plural_noun
from .waves import (  # noqa: F401 -- re-exported for renderers
    _RELATIONAL_TO_SOURCE_ENGINE,
    _legacy_migration_waves,
    resolve_migration_waves,
)


def _moves_from_label(engine: str) -> str:
    """Label for a wave's ``moves_from``/``fronts`` engine (#321).

    ``display_source_database`` phrases a raw legacy source key as "the
    source MySQL database" -- correct for wave 1, which really does move
    away from the legacy source. Every later wave's ``moves_from``/
    ``fronts`` instead names the *retained Aurora engine* once wave 1 has
    reached it; wrapping that in the same "the source ... database" phrasing
    would read as "the source Aurora MySQL database", calling Aurora the
    original source. A relational engine is shown by its own display name
    instead; anything else keeps the legacy phrasing.
    """
    if engine in _RELATIONAL_ENGINES:
        return display_engine(engine)
    return display_source_database(engine)


def _views_phrase(wf: dict[str, Any]) -> str:
    """ " (311 tables, 1 view)" for wave 1 when it carries views (#578), else ""."""
    views = wf.get("views")
    if not views:
        return ""
    tables = wf.get("tables_only")
    if not isinstance(tables, int):
        return ""
    return f" ({tables} {plural_noun(tables, 'table')}, {views} {plural_noun(views, 'view')})"


def _wave_headline_stats(w: dict[str, Any], wf: dict[str, Any] | None = None) -> tuple[str, str]:
    """(count-and-share phrase, table-count phrase) for one wave's headline.

    Every table figure comes from the wave's facts (``wf``, #615): the
    served count, and for a migrating wave the tables that move --
    ``owned_counts_by_engine`` summed for the merged DynamoDB/DocumentDB
    wave, so the headline and the "Tables migrate" tile read one figure
    (#581).

    #321 review (finding 2): wave 1 (Aurora, homogeneous -- it has a
    ``cutover_query_count``) states its share as the end state explicitly
    ("all N queries at cutover; M (X%) remain on <engine> at the end")
    rather than just "M queries (X%)", which reads as if only M queries ever
    touch Aurora, when the whole workload does at cutover.

    #321 review (finding 4): wave 1's table count is tables *and* views
    (wave 1 carries the whole collected schema, views included); #578: it
    says how many of each, so it reconciles with "Analyzed N source tables"
    (tables only).

    #568 review round 2, Suggestion 2: a wave with tables kept in sync from
    an owner outside it states both halves -- "N served, M move".
    """
    if wf is None:
        wf = wave_facts(w)
    n = int(w.get("query_count") or 0)
    basis = "of calls" if w.get("share_basis") == "calls" else "of query patterns"
    share = fmt_num(w.get("workload_share_percent", 0), 1)
    tables = int(wf.get("served_tables") or 0)
    cutover = w.get("cutover_query_count")
    engines = w.get("engines") or []
    if cutover is not None and engines:
        target_name = display_engine(engines[0])
        count_phrase = (
            f"all {cutover} {plural_noun(cutover, 'query', 'queries')} at cutover; "
            f"{n} ({share}%) remain on {target_name} at the end"
        )
        table_phrase = (
            f", {tables} source {plural_noun(tables, 'table')} and views{_views_phrase(wf)}"
            if tables
            else ""
        )
    else:
        count_phrase = f"{n} {plural_noun(n, 'query', 'queries')} ({share}% {basis})"
        moving = wf.get("moving_tables")
        if tables and wf.get("kept_in_sync_tables") and isinstance(moving, int):
            table_phrase = f", {tables} served, {moving} move"
        else:
            table_phrase = f", {tables} source {plural_noun(tables, 'table')}" if tables else ""
    return count_phrase, table_phrase


# #321 review (finding 9): the "one suggested path" wording, with one real
# em dash, never an ASCII "--", kept identical everywhere it appears so the
# deliverables read as one voice. ``{pointer}`` names the direct, single-step
# alternative already shown in the same deliverable.
_SUGGESTED_PATH_NOTE = (
    "One suggested adoption path, not the only one — to modernize in one step "
    "instead, adopt {pointer} directly."
)


def _roadmap_html(report: dict[str, Any]) -> list[str]:
    """Decision Report "Migration roadmap" section (#225): one short card per wave.

    #321: the roadmap is one suggested adoption path, not the only one -- the
    section says so, and points at the direct, single-step alternative (the
    target architecture already shown above).
    """
    esc = escaping.html_text
    waves = resolve_migration_waves(report)
    if not waves:
        return []
    pointer = "the target architecture in Recommended architecture above"
    out = [
        "<h2 class=section-title>Migration roadmap</h2>",
        f"<p class=note>{_SUGGESTED_PATH_NOTE.format(pointer=pointer)}</p>",
        "<div class=card><div class=card-b>",
    ]
    facts = _facts_by_wave(report)
    for w in waves:
        names = ", ".join(esc(display_engine(e)) for e in w.get("engines") or [])
        count_phrase, table_bit = _wave_headline_stats(w, facts.get(int(w.get("wave") or 0)))
        # `names` is already escaped; escaping it again (via `esc(title or names)`) would
        # double-escape it when the title is missing (#225).
        title_html = esc(w["title"]) if w.get("title") else names
        wave_no = esc(str(w.get("wave") or ""))
        out.append(
            f'<p data-wave="{escaping.html_attr(str(w.get("wave") or ""))}">'
            f"<b>Wave {wave_no}: {title_html}</b> — "
            f"<span data-figure=wave-stats>{esc(count_phrase)}{esc(table_bit)}</span>. "
            f"{esc(w.get('rationale') or '')}</p>"
        )
        if w.get("gate"):
            out.append(f"<p class=note>Gate: {esc(w['gate'])}</p>")
    out.append("</div></div>")
    return out


# Readable labels for a TableGroup's "kind" (#225): the raw contract value is
# snake_case for machine consumers, never shown verbatim to a reader.
_GROUP_KIND_LABELS = {"co_dependency": "co-dependency", "independent": "independent"}


def _owner_list_md(owners: list[dict[str, Any]], cap: int = 15) -> str:
    shown = ", ".join(
        f"`{escaping.md_code(o['table'])}` -> {escaping.md_text(display_engine(o['owner']))} "
        f"({escaping.md_text(o.get('sync', ''))})"
        for o in owners[:cap]
    )
    return shown + (f" (+{len(owners) - cap} more)" if len(owners) > cap else "")


def _owner_lines_md(wf: dict[str, Any]) -> list[str]:
    """The kept-in-sync line (owners outside the wave) and, for the merged
    wave, the tables one wave engine serves from the other (#581)."""
    out: list[str] = []
    kept = wf.get("kept_in_sync") or []
    if kept:
        n = int(wf.get("kept_in_sync_tables") or 0)
        # #568 review, Suggestion 4: "N table owners" read oddly for a
        # list of tables this wave does NOT own -- it names the real
        # owner elsewhere this wave keeps a synced copy of, so say that.
        out.append(
            f"- {n} {plural_noun(n, 'table')} kept in sync from their owner: "
            f"{_owner_list_md(kept)}"
        )
    shared = wf.get("shared_in_wave") or []
    if shared:
        out.append(
            "- Served by one engine of this wave from its owner in the same wave (moves "
            f"with its owner): {_owner_list_md(shared)}"
        )
    return out


def _roadmap_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report "Migration roadmap" section (#225): tables and queries per wave.

    #321: one suggested adoption path, not the only one -- points at the
    direct, single-step alternative. #321 review (finding 5): the pointer is
    conditional on "Target schemas by engine" actually being written below
    (only when a schema design completed, same condition
    ``render_engineering_report_md`` itself uses) -- it falls back to "the
    Target engines table above", which is always present whenever a wave is.
    """
    waves = resolve_migration_waves(report)
    if not waves:
        return []
    pointer = (
        'the target schemas in "Target schemas by engine" below'
        if _completed_designs(report)
        else "the target architecture in the Target engines table above"
    )
    n_waves = int(report_facts(report).get("migration_wave_count") or 0)
    out = [
        f"## Migration roadmap ({n_waves} {plural_noun(n_waves, 'wave')})",
        "",
        f"*{_SUGGESTED_PATH_NOTE.format(pointer=pointer)}*",
        "",
    ]
    unresolved = report.get("unresolved_names") or {}
    if unresolved.get("count"):
        out.append(
            f"*{unresolved['count']} table "
            f"{plural_noun(unresolved['count'], 'name')} in the assignment did not resolve "
            "to a table or view in the collected schema (a CTE alias, a system catalog, a "
            "sequence, a keyword, or a column the SQL parser mistook for a table) and "
            f"{plural_noun(unresolved['count'], 'is', 'are')} not shown in any wave.*"
        )
        out.append("")
    facts = _facts_by_wave(report)
    for w in waves:
        names = ", ".join(display_engine(e) for e in w.get("engines") or [])
        wf = facts.get(int(w.get("wave") or 0)) or wave_facts(w)
        count_phrase, _ = _wave_headline_stats(w, wf)
        out += [
            f"### Wave {w.get('wave')}: {escaping.md_text(w.get('title') or names)}",
            "",
            f"- Engines: {escaping.md_text(names)}",
            f"- {escaping.md_text(count_phrase)}",
        ]
        tables = w.get("tables") or []
        if tables:
            served = int(wf.get("served_tables") or 0)
            # #321 review (finding 4): wave 1 carries the whole collected
            # schema, views included, so its count says "tables and views"
            # (#578: and how many of each).
            table_noun = (
                f"tables and views{_views_phrase(wf)}"
                if w.get("cutover_query_count") is not None
                else f"{plural_noun(served, 'table')}"
            )
            shown = ", ".join(f"`{escaping.md_code(t)}`" for t in tables[:20])
            more = f" (+{len(tables) - 20} more)" if len(tables) > 20 else ""
            out.append(f"- {served} source {table_noun}: {shown}{more}")
        groups = w.get("table_groups") or []
        if groups:
            out.append(f"- {len(groups)} table {plural_noun(len(groups), 'group')}:")
            for g in groups:
                g_table_ids = g.get("tables") or []
                g_tables = ", ".join(f"`{escaping.md_code(t)}`" for t in g_table_ids[:10])
                more = f" (+{len(g_table_ids) - 10} more)" if len(g_table_ids) > 10 else ""
                g_n = g.get("query_count", 0)
                kind_raw = _GROUP_KIND_LABELS.get(g.get("kind") or "", g.get("kind") or "")
                kind = f", {escaping.md_text(kind_raw)}" if kind_raw else ""
                out.append(
                    f"  - {g_n} {plural_noun(g_n, 'query', 'queries')}{kind}: {g_tables}{more}"
                )
        if w.get("moves_from"):
            out.append(
                "- Moves from: "
                + ", ".join(escaping.md_text(_moves_from_label(e)) for e in w["moves_from"])
            )
        if w.get("fronts"):
            out.append(f"- Fronts: {escaping.md_text(_moves_from_label(w['fronts']))}")
        if w.get("serves_from"):
            out.append(
                "- Synced from (durable owner): "
                + ", ".join(escaping.md_text(display_engine(e)) for e in w["serves_from"])
            )
        out += _owner_lines_md(wf)
        out.append(f"- Why: {escaping.md_text(w.get('rationale') or '-')}")
        out.append(f"- Gate before the next wave: {escaping.md_text(w.get('gate') or '-')}")
        out.append("")
    return out
