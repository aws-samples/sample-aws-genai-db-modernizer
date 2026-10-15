"""The Engineering Report: the Markdown deliverable for the migration team."""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine
from src.shared.unsupported_pattern import (
    unsupported_pattern_ids,
    unsupported_pattern_label,
    unsupported_pattern_text,
)
from src.shared.workload_fit_labels import WORKLOAD_FIT_DEFINITION, WORKLOAD_FIT_LABEL

from . import escaping
from .architecture import _architecture_engines, _mapping_split
from .cache_layer import _cache_layer_md, cache_layer_text
from .close_calls import close_calls_md
from .engine_choices import _reroutes_md, _tied_queries_md
from .engine_roles import _RELATIONAL_ENGINES
from .facts import report_facts
from .formatting import (
    _completed_designs,
    _cost_baseline_text,
    _eliminated_engine_costs_text,
    _fmt_usd,
    access_pattern_scope,
    clip,
    fmt_num,
    plural_noun,
)
from .query_traces import query_traces_md
from .risks import (
    _repeats,
    _risk_engine_and_body,
    filtered_risks,
    resolved_risk_label,
    resolved_risks,
)
from .roadmap import _roadmap_md
from .unservable import unservable_md


def _mermaid_er(engine: str, design: dict, max_nodes: int = 15) -> str | None:
    """A small mermaid flowchart of source tables -> target tables for one engine.

    Returns None when the design has more target tables than ``max_nodes`` — a
    diagram past that point is an unreadable wall, and the target-table table
    already lists them completely.
    """
    tables = [t for t in (design.get("tables") or []) if isinstance(t, dict)]
    if not tables or len(tables) > max_nodes:
        return None
    lines = ["```mermaid", "flowchart LR"]
    seen_src: dict[str, str] = {}
    sid = 0
    for i, t in enumerate(tables):
        tgt = t.get("table_name", f"t{i}")
        tnode = f"T{i}"
        lines.append(f'    {tnode}["{escaping.mermaid_label(tgt)}"]')
        for s in t.get("source_tables") or []:
            if s not in seen_src:
                seen_src[s] = f"S{sid}"
                lines.append(f'    {seen_src[s]}[("{escaping.mermaid_label(s)}")]')
                sid += 1
            lines.append(f"    {seen_src[s]} --> {tnode}")
    lines.append("```")
    return "\n".join(lines)


def _unsupported_pattern_md(u: dict[str, Any]) -> str:
    """One readable Markdown line for a schema-design ``unsupported_patterns`` entry.

    The four engine contracts (``*_model_output.py``) disagree on field names
    -- see ``src.shared.unsupported_pattern`` for the full picture, and for
    why that module (not this one) owns reading them: the same field-reading
    is also needed by ``synthesis_report.build_risk_assessment`` (#210), in a
    package this one must not depend on. Printing ``str(u)`` for whichever
    shape showed up produced a raw Python dict/list repr in the engineering
    report (#204); this reads the shared helpers' output into one sentence
    instead, escaped as Markdown flowing text via ``escaping.md_text``/
    ``escaping.md_code``. ``source_query`` (opensearch's original SQL text)
    is never shown here -- see ``unsupported_pattern_text``.

    Query ids are long hashes; showing every one of them wrecked readability,
    so only the first three are shown with a "+N more" count, each truncated
    to an 8-character prefix (enough to recognise, not to collide visibly).
    """
    ids = unsupported_pattern_ids(u)
    id_bits = ", ".join(f"`{escaping.md_code(i[:8])}`" for i in ids[:3])
    if len(ids) > 3:
        id_bits += f" (+{len(ids) - 3} more)"

    label = unsupported_pattern_label(u)
    head_bits = [f"**{escaping.md_text(label)}**"]
    if id_bits:
        head_bits.append(f"({id_bits})")
    head = " ".join(head_bits)

    body = escaping.md_text(unsupported_pattern_text(u))

    if head and body:
        return f"{head} \u2014 {body}"
    return head or body or "(no detail provided)"


def _migration_note_md(mn: dict[str, Any]) -> str:
    """One readable Markdown bullet for a schema-design ``migration_notes`` entry.

    ``migration_notes`` is a list of dicts (``object_name``, ``object_type``,
    ``source_table`` optional, ``application_logic_required``) -- the same
    four field names on every engine contract, so there is no shape to
    reconcile here the way ``unsupported_patterns`` needs. The report used to
    interpolate the whole list with ``escaping.md_text(notes)``, which prints
    the Python list/dict repr verbatim (e.g.
    ``[{'object_name': 'x', 'object_type': 'trigger', ...}]``); this renders
    one bullet per note instead, tolerant of any of the fields being absent.
    """
    object_type = str(mn.get("object_type") or "").strip()
    object_name = str(mn.get("object_name") or "").strip()
    logic = str(mn.get("application_logic_required") or "").strip()

    head_bits = []
    if object_type:
        head_bits.append(f"**{escaping.md_text(object_type)}**")
    if object_name:
        head_bits.append(escaping.md_text(object_name))
    head = " ".join(head_bits)
    body = escaping.md_text(logic)

    if head and body:
        return f"{head} \u2014 {body}"
    return head or body or "(no detail provided)"


# #478: a relational engine's query_groups entry for a
# query with no real source table is labelled one of these two ways, never
# a generic "unknown" -- a real utility/session/DDL statement
# (synthesis_report.UTILITY_GROUP_LABEL, already routed to the relational
# engine by the assignment: SHOW/SET/EXPLAIN, catalog introspection) or a
# query the collector could not resolve a table for at all
# (synthesis_report.UNRESOLVED_TABLE_GROUP_LABEL). Duplicated here as exact
# literals rather than imported -- src/report has no existing dependency on
# src/agents.referee and this file already duplicates engine-literal
# constants rather than take one on (see ``engine_roles._RELATIONAL_ENGINES``).
_UTILITY_GROUP_LABEL = "Utility and session statements"
_UNRESOLVED_TABLE_GROUP_LABEL = "Table not identified by the collector"


def _reason_text_for(group: dict[str, Any], access_pattern: dict[str, Any]) -> str:
    """An access pattern's reason text (synthesis_report.reason_text_for,
    duplicated here for the same reason the two labels above are: #478 round
    5 review).

    A relational engine's entry (``build_query_groups``'s relational branch)
    carries ``reason_index`` into its group's own ``reasons`` list instead of
    a copy of the string -- 146 distinct reasons backed 1281 access patterns
    on the discourse sample, so copying one onto every entry was pure
    duplication. Every other engine's entry still carries the string
    directly as ``description``.
    """
    reason_index = access_pattern.get("reason_index")
    if isinstance(reason_index, int):
        reasons = group.get("reasons") or []
        if 0 <= reason_index < len(reasons):
            # A ``None`` entry resolves the same way the JS ``reasonTextFor``
            # already does: never a literal "None".
            value = reasons[reason_index]
            return str(value) if value else ""
        return ""
    return str(access_pattern.get("description") or "")


def _aurora_queries_by_table_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report subsection: "Queries on Aurora by table" (#478).

    A query the assignment routed to a relational engine is a ``query_groups``
    entry like any other (build_query_groups's relational branch, while the
    engine's own schema design has not run yet; once it has, #157's real,
    script-derived access patterns take over the same group through the
    module's main loop instead), but it reads oddly folded into the generic
    "## Query groups" table above when a group is shared with, say, the cache
    layer serving reads on the same source table: that table's row would
    merge the two engines' access-pattern/source-query counts into one
    figure. This subsection is scoped to one Aurora engine's own
    access_patterns within each group instead, so its numbers are never
    someone else's.
    """
    out: list[str] = []
    groups = [g for g in (report.get("query_groups") or []) if isinstance(g, dict)]
    all_patterns = [
        ap for g in groups for ap in (g.get("access_patterns") or []) if isinstance(ap, dict)
    ]
    engines_present: list[str] = sorted(
        {str(ap["engine"]) for ap in all_patterns if ap.get("engine") in _RELATIONAL_ENGINES}
    )
    if not engines_present:
        return out

    out += ["## Queries on Aurora by table", ""]
    for eng in engines_present:
        rows: list[tuple[str, int, float, list[str]]] = []
        for g in groups:
            aps = [
                ap
                for ap in (g.get("access_patterns") or [])
                if isinstance(ap, dict) and ap.get("engine") == eng
            ]
            if not aps:
                continue
            total_rps = sum(ap.get("design_rps") or 0 for ap in aps)
            query_ids: set[str] = set()
            for ap in aps:
                query_ids.update(ap.get("query_ids") or [])
            reasons = sorted({text for ap in aps if (text := _reason_text_for(g, ap))})
            rows.append((str(g.get("group_name", "?")), len(query_ids), total_rps, reasons))
        if not rows:
            continue
        # Real table rows first, by calls/s descending; the two blame labels
        # always last (#478: neither is a busiest access pattern, just a
        # bucket for what the collector/utility traffic is).
        rows.sort(
            key=lambda r: (
                r[0] in (_UTILITY_GROUP_LABEL, _UNRESOLVED_TABLE_GROUP_LABEL),
                -r[2],
            )
        )
        total_queries = sum(r[1] for r in rows)
        out += [
            f"### {escaping.md_text(display_engine(eng))} "
            f"({total_queries} {plural_noun(total_queries, 'query', 'queries')})",
            "",
            "| Table | Queries | Calls/s | Reason |",
            "|---|---|---|---|",
        ]
        for table, n_queries, total_rps, reasons in rows:
            reason_text = clip("; ".join(reasons), 200)
            out.append(
                f"| {escaping.md_cell(table)} | {n_queries} "
                f"| {escaping.md_cell(fmt_num(total_rps))} "
                f"| {escaping.md_cell(reason_text or '-')} |"
            )
        out.append("")
    return out


def _app_layer_notes_md(design: dict[str, Any]) -> list[str]:
    """An Aurora design's app-layer notes, column defaults listed on their own (#156).

    ``column_default`` notes are the deterministic ones the schema merge writes
    for each source DEFAULT the target DDL dropped or turned into an identity
    column; the rest are time-zone and model-written notes.
    """
    notes = design.get("app_layer_notes") or []
    if not isinstance(notes, list):
        return []
    notes = [n for n in notes if isinstance(n, dict)]
    defaults = [n for n in notes if n.get("feature") == "column_default"]
    others = [n for n in notes if n.get("feature") != "column_default"]
    out: list[str] = []
    if others:
        out += [f"**Application-layer notes ({len(others)}):**", ""]
        for n in others:
            feature = str(n.get("feature") or "note").replace("_", " ")
            out.append(
                f"- {escaping.md_text(feature.capitalize())} on "
                f"`{escaping.md_code(n.get('source_object') or '?')}`: "
                f"{escaping.md_text(n.get('recommendation') or '')}"
            )
        out.append("")
    if defaults:
        out += [
            f"**Column defaults changed or dropped ({len(defaults)}):** set these by hand "
            "where the application relies on them.",
            "",
        ]
        out += [
            f"- `{escaping.md_code(n.get('source_object') or '?')}`: "
            f"{escaping.md_text(n.get('recommendation') or '')}"
            for n in defaults
        ]
        out.append("")
    return out


def _trade_offs_md(report: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    """Engineering Report "Migration trade-offs" section.

    #546: a reality-check line a later assignment edit reversed (the facts
    layer's ``reality_check_trade_offs[].reversed``: an edit put every query
    it moved back, #861) is not listed as a current trade-off -- it would
    still say what the check moved, and for a removed engine that it "leaves
    the architecture" and what removing it saves. It is named once under
    "Reversed by a later assignment edit" instead, so the audit trail stays
    without claiming the removal. A line an edit undid only in part is listed
    as the facts layer restates it (``description_now``), for what stayed
    moved.
    """
    all_trade_offs = report.get("trade_offs") or []
    reversed_at = {
        t["index"]: t for t in facts.get("reality_check_trade_offs") or [] if t.get("reversed")
    }
    restated = {
        t["index"]: t["description_now"]
        for t in facts.get("reality_check_trade_offs") or []
        if t.get("description_now") and not t.get("reversed")
    }
    current = [
        {**t, "description": restated[i], "impact": restated[i]} if i in restated else t
        for i, t in enumerate(all_trade_offs)
        if isinstance(t, dict) and i not in reversed_at
    ]
    if not current and not reversed_at:
        return []
    out = [f"## Migration trade-offs ({facts.get('trade_off_count', 0)})", ""]
    if current:
        by_engine: dict[str, list] = {}
        for t in current:
            by_engine.setdefault(t.get("engine") or "(general)", []).append(t)
        for eng, items in by_engine.items():
            # #572: eng is a raw engine id (or "(general)"/"reality-check", neither a
            # known engine -- display_engine's title-case fallback would read oddly
            # for the hyphenated "reality-check", so both are left unchanged).
            heading = eng if eng in ("(general)", "reality-check") else display_engine(eng)
            out += [f"### {escaping.md_text(heading)}", ""]
            for t in items:
                desc = str(t.get("description", "")).strip()
                impact = str(t.get("impact", "")).strip()
                line = f"- **{escaping.md_text(desc)}**"
                if impact and impact != desc:  # reality-check items repeat their title
                    line += f" \u2014 {escaping.md_text(impact)}"
                out.append(line)
                aff = list(t.get("source_tables") or [])
                if aff:
                    shown = ", ".join(f"`{escaping.md_code(s)}`" for s in aff[:8])
                    more = f" (+{len(aff) - 8} more)" if len(aff) > 8 else ""
                    out.append(f"  - Affects: {shown}{more}")
            out.append("")
    if reversed_at:
        out += ["### Reversed by a later assignment edit", ""]
        for t in reversed_at.values():
            src = display_engine(str(t.get("from_engine") or ""))
            dst = t.get("to_engine")
            moved = f" to {display_engine(str(dst))}" if dst else ""
            out.append(
                f"- The reality check moved {escaping.md_text(src)}'s queries{escaping.md_text(moved)}; "
                f"an assignment edit moved queries back, so {escaping.md_text(src)} stays in "
                "the architecture and this consolidation no longer applies."
            )
        out.append("")
    return out


def render_engineering_report_md(report: dict[str, Any], prov: dict[str, str] | None = None) -> str:
    """Build-team-facing document: migration map, per-engine target schemas,
    query groups. Markdown with mermaid fences, which render in the tooling
    engineers open it in (VS Code, GitHub, GitLab).
    """
    db = report.get("database_name", "?")
    out: list[str] = []
    if prov:
        # Front-matter values are quoted so a value containing ``:`` or ``#`` cannot
        # be misread as YAML structure, and the double quotes inside are escaped so
        # the value cannot close its own quoting. ``db`` here is customer-derived.
        out += ["---"]
        out += [f'{k}: "{escaping.md_yaml_value(v)}"' for k, v in prov.items() if v]
        out += ["---", ""]
    out += [
        "# Database Modernization \u2014 Engineering Report",
        "",
        f"Source database: `{escaping.md_code(db)}`. This is the build companion to the "  # nosemgrep: string-concat-in-list -- intentional multi-line string
        "Decision Report: "
        "the source-to-target mapping, the per-engine target schemas, and the query "
        "groups.",
        "",
    ]

    target_engines = _architecture_engines(report)
    facts = report_facts(report)
    if target_engines:
        # Why each engine is in the target, from the workload routed to it (#152)
        out += [
            "## Target engines",
            "",
            "| Engine | Role | Workload | Why |",
            "|---|---|---|---|",
        ]
        for e in target_engines:
            cached = e.get("cached_queries")
            share = (
                cache_layer_text(int(cached), float(e.get("cached_call_share") or 0))
                if cached is not None
                else (
                    f"{fmt_num(e['workload'], 1)}%"
                    if isinstance(e.get("workload"), (int, float))
                    else "-"
                )
            )
            out.append(
                f"| {escaping.md_cell(display_engine(e['engine']))} | {escaping.md_cell(e['role'])} "
                f"| {escaping.md_cell(share)} | {escaping.md_cell(e['rationale'] or '-')} |"
            )
        out.append("")
        # First appearance of a fit figure in this report (inside "Why", e.g. "30%
        # mean fit"): the plain-language definition every deliverable states once,
        # shared with the decision report, the deck and the UI (bug: fit score
        # presented as a confidence percentage). Stated here, not under "Migration
        # map" below -- that section only renders once schema design has produced
        # table_mappings, so a report rendered with no schema design (e.g. every
        # ``--llm-mode none`` run) would otherwise never state the definition at
        # all, even though "Target engines" above already shows the figure.
        out.append(f"*{escaping.md_text(WORKLOAD_FIT_DEFINITION)}*")
        out.append("")

    tco = report.get("tco_analysis") or {}
    if tco:
        current_cost_text, savings_text = _cost_baseline_text(tco)
        out += [
            "## Cost",
            "",
            f"- Current monthly (source): {escaping.md_text(current_cost_text)}",
            f"- Projected monthly (target): "
            f"{escaping.md_text(_fmt_usd(tco.get('projected_monthly_cost')))}",
            f"- Savings: {escaping.md_text(savings_text)}",
        ]
        eliminated_cost_text = _eliminated_engine_costs_text(tco)
        if eliminated_cost_text:
            out.append(
                f"- Already removed from this comparison: "
                f"{escaping.md_text(eliminated_cost_text)}, "
                "retired before this projection (see Migration trade-offs below for why)."
            )
        out.append("")

    mappings = [m for m in (report.get("table_mappings") or []) if isinstance(m, dict)]
    mapped = int((facts.get("table_basis") or {}).get("mapped_tables") or 0)
    if mappings:
        out += [
            f"## Migration map ({mapped} {plural_noun(mapped, 'table')}"
            f"{_mapping_split(report, mappings)})",
            "",
            f"| Source table | Target engine | Target | Pattern | {WORKLOAD_FIT_LABEL} |",
            "|---|---|---|---|---|",
        ]
        for m in mappings:
            # Code-span cells go through md_cell too, as in the schema table below (#466).
            source = escaping.md_cell(f"`{escaping.md_code(m.get('source_table', '?'))}`")
            target = escaping.md_cell(f"`{escaping.md_code(m.get('target_table', '-'))}`")
            out.append(
                f"| {source} "
                f"| {escaping.md_cell(display_engine(m.get('recommended_database', '?')))} "
                f"| {target} "
                f"| {escaping.md_cell(m.get('aggregate_pattern', '-'))} "
                f"| {escaping.md_cell(fmt_num(m.get('confidence_score', '-')))} |"
            )
        out.append("")

    out += _tied_queries_md(report)
    out += unservable_md(report)
    out += close_calls_md(report)
    out += _reroutes_md(report)
    out += _cache_layer_md(report)
    out += _roadmap_md(report)

    designs = _completed_designs(report)
    if designs:
        out += ["## Target schemas by engine", ""]
        ap_scope = access_pattern_scope(report)
        for eng, dz in designs.items():
            tables = [t for t in (dz.get("tables") or []) if isinstance(t, dict)]
            n_in, n_out = ap_scope.get(eng, (0, 0))
            # #478/#157: a relational engine's query_groups entries are
            # queries, not access patterns, only while its own schema design
            # has not run yet (built from the assignment as a routing record,
            # with no access_patterns of its own) -- access_pattern_count is
            # the signal either way, since it is 0 until #157's merge has
            # derived real ones. access_pattern_scope counts the group
            # entries the same way regardless (unique engine/pattern_id pairs
            # from query_groups), so this just names the unit differently
            # instead of reaching for a second, separately-computed count
            # that could disagree.
            if eng in _RELATIONAL_ENGINES and not dz.get("access_pattern_count"):
                n_queries = n_in + n_out
                unit_text = f"{n_queries} {plural_noun(n_queries, 'query', 'queries')}"
                eng_heading = escaping.md_text(display_engine(eng))
            else:
                n_aps = dz.get("access_pattern_count", 0)
                # The deck and the summary count in-scope patterns only; say how
                # this total splits so the two numbers reconcile (#255).
                split = (
                    f": {n_aps - n_out} in scope, {n_out} out of scope"
                    if n_out and isinstance(n_aps, int) and n_aps >= n_out
                    else ""
                )
                unit_text = f"{n_aps} access {plural_noun(n_aps, 'pattern')}{split}"
                # #572: display_engine for every engine, not just the relational
                # ones the branch above already covers -- a non-relational engine
                # (dynamodb, elasticache, ...) was still showing its raw id here.
                eng_heading = escaping.md_text(display_engine(eng))
            out += [
                f"### {eng_heading} ({len(tables)} target "
                f"{plural_noun(len(tables), 'object')}, "
                f"{unit_text})",
                "",
            ]
            if tables:
                # engine-specific columns surface when present
                has_ttl = any("ttl_seconds" in t for t in tables)
                has_shards = any("shards" in t for t in tables)
                cols = ["Target", "Pattern", "Source tables", "GSIs"]
                if has_ttl:
                    cols.append("TTL(s)")
                if has_shards:
                    cols += ["Shards", "Replicas", "Fields"]
                out += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
                for t in tables:
                    # Code-span cells go through md_cell too: a ``|`` splits a GFM
                    # row even inside a code span, which breaks the span and leaves
                    # the rest of the name as live markup (#457).
                    row = [
                        escaping.md_cell(f"`{escaping.md_code(t.get('table_name', '?'))}`"),
                        escaping.md_cell(t.get("aggregate_pattern", "-")),
                        escaping.md_cell(
                            ", ".join(
                                f"`{escaping.md_code(s)}`" for s in (t.get("source_tables") or [])
                            )
                            or "-"
                        ),
                        escaping.md_cell(fmt_num(t.get("gsi_count", "-"))),
                    ]
                    if has_ttl:
                        row.append(escaping.md_cell(fmt_num(t.get("ttl_seconds", "-"))))
                    if has_shards:
                        row += [
                            escaping.md_cell(fmt_num(t.get("shards", "-"))),
                            escaping.md_cell(fmt_num(t.get("replicas", "-"))),
                            escaping.md_cell(fmt_num(t.get("field_count", "-"))),
                        ]
                    out.append("| " + " | ".join(row) + " |")
                out.append("")
            unsupported = dz.get("unsupported_patterns") or []
            if isinstance(unsupported, str):
                # Same malformed-shape guard as migration_notes below: a bare
                # string must be one entry, not one character per entry.
                unsupported = [unsupported]
            elif not isinstance(unsupported, list):
                unsupported = []
            if unsupported:
                out += [f"**Unsupported patterns ({len(unsupported)}):**", ""]
                out += [
                    f"- {_unsupported_pattern_md(u) if isinstance(u, dict) else escaping.md_text(u)}"
                    for u in unsupported
                ]
                out.append("")
            notes = dz.get("migration_notes") or []
            if isinstance(notes, str):
                # Contractually a list of dicts; a bare string is a malformed
                # shape that `for mn in notes` would otherwise iterate one
                # character at a time, rather than treat as one note.
                notes = [notes]
            elif not isinstance(notes, list):
                notes = []
            if notes:
                out += ["**Migration notes:**", ""]
                out += [
                    f"- {_migration_note_md(mn) if isinstance(mn, dict) else escaping.md_text(mn)}"
                    for mn in notes
                ]
                out.append("")
            out += _app_layer_notes_md(dz)
            er = _mermaid_er(eng, dz)
            if er:
                out += [er, ""]
            elif tables:
                out += [
                    f"_ER diagram omitted ({len(tables)} target "
                    f"{plural_noun(len(tables), 'object')}); see the table above._",
                    "",
                ]

    all_groups = [g for g in (report.get("query_groups") or []) if isinstance(g, dict)]
    # #478: a group served only by relational engine(s) is
    # already shown in full, per table, by the "Queries on Aurora by table"
    # subsection below -- listing it again here just duplicated it (and, on
    # a large relational workload, bloated this file: 359 extra rows on the
    # discourse sample). A group a relational engine shares with another
    # engine (e.g. the cache layer reading the same source table) stays,
    # since its non-relational side belongs here.
    groups = [
        g for g in all_groups if not set(g.get("engines") or []).issubset(_RELATIONAL_ENGINES)
    ]
    if groups:
        # "Query groups", as the deck summary calls them: the assignment's
        # co-dependency groups (tables that must move together) are a different,
        # usually much smaller count the deck also shows (#258).
        co_dep = (report.get("assignment_summary") or {}).get("co_dependency_groups")
        # #478: the groups excluded just above (pure-Aurora,
        # already shown in full by "Queries on Aurora by table") would
        # otherwise look like missing data to a reader who only sees this
        # heading's count -- point at where they actually are instead.
        omitted_aurora = len(all_groups) - len(groups)
        aurora_pointer = (
            f" (plus {omitted_aurora} Aurora {plural_noun(omitted_aurora, 'group')} below)"
            if omitted_aurora > 0
            else ""
        )
        out += [f"## Query groups ({len(groups)}){aurora_pointer}", ""]
        if isinstance(co_dep, int):
            out += [
                "Queries grouped by the access patterns that serve them. These are not the "
                f"assignment's {co_dep} co-dependency {plural_noun(co_dep, 'group')} "
                "(tables that must move together).",
                "",
            ]
        out += [
            "| Group | Engines | Access patterns | Source queries | Design RPS |",
            "|---|---|---|---|---|",
        ]
        for g in groups:
            engines = (
                ", ".join(display_engine(e) for e in g.get("engines") or [])
                if isinstance(g.get("engines"), list)
                else str(g.get("engines", "-"))
            )
            aps = g.get("access_patterns")
            sqs = g.get("source_queries")
            out.append(
                f"| {escaping.md_cell(g.get('group_name', '?'))} | {escaping.md_cell(engines)} "
                f"| {len(aps) if isinstance(aps, list) else escaping.md_cell(aps or '-')} "
                f"| {len(sqs) if isinstance(sqs, list) else escaping.md_cell(sqs or '-')} "
                f"| {escaping.md_cell(fmt_num(g.get('total_design_rps', '-')))} |"
            )
        out.append("")

    out += _aurora_queries_by_table_md(report)

    risks = filtered_risks(report)
    if risks:
        by_eng: dict[str, list] = {}
        for r in risks:
            eng, _ = _risk_engine_and_body(r.get("description"))
            by_eng.setdefault(eng, []).append(r)
        out += [f"## Risk register ({facts.get('open_risk_count', 0)})", ""]
        for eng, items in by_eng.items():
            # #572: eng is the raw [engine] tag _risk_engine_and_body parsed off the
            # description (or "(general)") -- display_engine leaves "(general)" as is
            # (not a known engine id, falls back to its own title-cased fallback,
            # which already reads fine unchanged).
            heading = eng if eng == "(general)" else display_engine(eng)
            out += [f"### {escaping.md_text(heading)}", ""]
            for r in items:
                _, body = _risk_engine_and_body(r.get("description"))
                sev = r.get("severity", "-")
                rtype = str(r.get("risk_type", "")).replace("_", " ").lower()
                rid = r.get("risk_id", "-")
                out.append(
                    f"- **{escaping.md_text(rid)}** \u00b7 {escaping.md_text(sev)} \u00b7 "
                    f"{escaping.md_text(rtype)} \u2014 {escaping.md_text(body)}"
                )
                if r.get("mitigation") and not _repeats(r.get("mitigation"), body):
                    out.append(f"  - Mitigation: {escaping.md_text(r.get('mitigation'))}")
                aff = list(r.get("affected_tables") or [])
                if aff:
                    shown = ", ".join(f"`{escaping.md_code(a)}`" for a in aff[:8])
                    more = f" (+{len(aff) - 8} more)" if len(aff) > 8 else ""
                    out.append(f"  - Affects: {shown}{more}")
            out.append("")

    resolved = resolved_risks(report)
    if resolved:
        # Analysis risks the effective assignment resolved (#221): listed so none
        # disappears without a record.
        out += [f"## Resolved by the assignment ({facts.get('resolved_risk_count', 0)})", ""]
        for r in resolved:
            _, body = _risk_engine_and_body(r.get("description"))
            where = resolved_risk_label(r)
            out.append(
                f"- {escaping.md_text(r.get('severity', '-'))} \u00b7 "
                f"{escaping.md_text(where)} \u2014 {escaping.md_text(body)}"
            )
            if r.get("reason"):
                out.append(f"  - Resolved because {escaping.md_text(r['reason'])}.")
        out.append("")

    out += _trade_offs_md(report, facts)
    out += query_traces_md(report)

    out += [
        "---",
        "",
        "Assignments are deterministic. The complete machine-readable assessment is the "  # nosemgrep: string-concat-in-list -- intentional multi-line string
        "Assessment Data (JSON) artifact.",
        "",
    ]
    return "\n".join(out)
