"""The Engineering Report: the Markdown deliverable for the migration team."""

from __future__ import annotations

import re
from typing import Any

from src.shared.design_text import (
    design_text_item,
    split_carried_write_detail,
    split_close_call_description,
    split_severe_risk_strategy,
    unsupported_pattern_detail,
)
from src.shared.engine_names import canonical_engine_id, display_engine
from src.shared.executive_brief import executive_brief_text
from src.shared.ranking import PIN_RULE_PREDATES_NOTE, report_predates_pin_rule
from src.shared.report_facts import CACHE_LAYER, query_group_at
from src.shared.target_databases import engine_scope_statement
from src.shared.unsupported_pattern import (
    unsupported_pattern_ids,
    unsupported_pattern_label,
    unsupported_pattern_text,
)
from src.shared.workload_fit_labels import (
    CACHE_FIT_DEFINITION,
    WORKLOAD_FIT_DEFINITION,
    WORKLOAD_FIT_LABEL,
)

from . import escaping
from .architecture import (
    KEPT_IN_SYNC_NOTE,
    KEPT_IN_SYNC_SOURCES_NOTE,
    MAP_SPLIT_NOTE,
    SHARED_TABLES_LABEL,
    SHARED_TABLES_NOTE,
    _architecture_engines,
    _mapping_split,
)
from .cache_layer import _cache_layer_md, cache_layer_text
from .close_calls import close_calls_md
from .derived_copy_sync import derived_copy_sync_md
from .engine_choices import _reroutes_md, _tied_queries_md, auto_decided_md
from .engine_roles import _RELATIONAL_ENGINES, SEARCH_READ_MODEL
from .facts import report_facts
from .formatting import (
    _completed_designs,
    _cost_baseline_text,
    _fmt_usd,
    _retired_in_use_lines,
    access_pattern_scope,
    commit_footer,
    cost_baseline_known,
    cost_caveat_text,
    cost_scope_text,
    cost_sizing_text,
    fallback_withheld_reason,
    fmt_num,
    label_summary_counts,
    plural_noun,
    plural_verb,
    price_list_text,
)
from .query_patterns import query_patterns_md
from .query_traces import query_traces_md
from .relational_gaps import heterogeneous_wave
from .risks import (
    NO_OPEN_RISKS_CAVEAT,
    _repeats,
    _risk_engine_and_body,
    _severity_counts_text,
    filtered_risks,
    resolved_risk_label,
    resolved_risks,
)
from .roadmap import _roadmap_md
from .sources_of_truth import sources_of_truth_md
from .table_owners import (
    OWNER_RULE,
    also_read_by_cell,
    cached_by_cell,
    owner_cell,
    ownership_facts,
    ownership_summary_text,
    target_cell,
)
from .unreviewed import unreviewed_moves_md
from .unservable import (
    cost_exclusion_text,
    headline_query_total,
    no_engine_clause,
    placed_share_note,
    unservable_md,
)

# Stated once, after the summary: the retired decision report's closing words (#987).
NO_MODEL_DECIDES_NOTE = (
    "Engine and query assignments are produced deterministically. No language model "
    "decides which engine a table or query goes to. The executive summary is written over "
    "already-computed results and cannot change a recommendation."
)
# How the overall risk level is derived (#248; synthesis_report.overall_risk_level).
RISK_RULE_NOTE = (
    "Overall risk is the highest severity among the open risks; risks the assignment "
    "resolved do not count."
)
WITHHELD_UNTRUSTED_NOTE = (
    "The generated narrative was withheld because it referenced schema work this run "
    "did not perform; the figures here are unaffected."
)


def summary_trusted(report: dict[str, Any]) -> bool:
    """Whether the generated summary may be shown: some schema design ran."""
    return any(r.get("schema_design_available") for r in (report.get("ranking") or []))


def _withheld_note(report: dict[str, Any], trust: bool, summary: Any) -> str | None:
    """Why the summary shown is not the generated one, worded as the retired decision
    report worded it (#987): withheld because no schema design ran, or replaced by the
    deterministic fallback for the reason synthesis recorded. A run that never generated
    a narrative (``--llm-mode none``: the summary is the deterministic one) withheld
    nothing, so it gets no note."""
    if not summary:
        return None
    if not trust:
        generated = report.get("summary")
        if generated and generated != report.get("summary_deterministic"):
            return WITHHELD_UNTRUSTED_NOTE
        return None
    if report.get("summary_source") == "deterministic_fallback":
        return (
            f"The generated narrative was withheld because it "
            f"{fallback_withheld_reason(report)}; this summary is built from the "
            "effective assignment."
        )
    return None


def summary_paragraphs(
    report: dict[str, Any], trust: bool | None = None
) -> tuple[list[str], str | None]:
    """The executive summary as the deliverables show it: its paragraphs and the note on why a
    generated one was withheld (None when nothing was). ``/modernize``'s printed summary
    (#1307) reads the same function, so the two cannot differ."""
    trust = summary_trusted(report) if trust is None else trust
    brief = brief_summary(report)
    if brief is not None:
        # #1403: the deterministic summary is the facts' recommendation, risk and
        # decision, so it is shown whether or not a schema design ran; only a generated
        # one with no schema design behind it is withheld, for the same text.
        # A deterministic one is worded again from the facts the report holds now, so a
        # risk a report-only rule backfilled (#1148) is in its level too.
        generated = report.get("summary_source") == "llm"
        summary = (report.get("summary") if trust and generated else None) or brief
        note = WITHHELD_UNTRUSTED_NOTE if generated and not trust else None
        if report.get("summary_source") == "deterministic_fallback":
            note = _withheld_note(report, True, summary)
        return _paragraphs(summary), note
    summary = report.get("summary") if trust else report.get("summary_deterministic")
    summary = summary or report.get("summary_deterministic")
    if summary and summary == report.get("summary_deterministic"):
        summary = label_summary_counts(summary, report)
    return _paragraphs(summary), _withheld_note(report, trust, summary)


def _paragraphs(summary: Any) -> list[str]:
    return [c.strip() for c in str(summary or "").strip().split("\n") if c.strip()]


def brief_summary(report: dict[str, Any]) -> str | None:
    """The deterministic executive summary (#1403), worded from the stored
    ``facts.executive_brief``; None for a report written before it."""
    facts = report.get("facts")
    if not isinstance(facts, dict):
        return None
    return executive_brief_text(facts, str(report.get("database_name") or ""))


def _summary_md(report: dict[str, Any], trust: bool) -> list[str]:
    """Engineering Report "Executive summary" section (ported from the retired
    multi-page PDF report, #1020): the summary the run wrote, why a generated one was
    withheld, and that no language model decides an assignment."""
    paragraphs, withheld = summary_paragraphs(report, trust)
    out = ["## Executive summary", ""]
    if paragraphs:
        for chunk in paragraphs:
            out += [escaping.md_text(chunk), ""]
    else:
        out += ["No executive summary was generated for this run.", ""]
    if withheld:
        out += [f"*{escaping.md_text(withheld)}*", ""]
    out += [f"*{escaping.md_text(NO_MODEL_DECIDES_NOTE)}*", ""]
    return out


def _cost_by_engine_md(report: dict[str, Any]) -> list[str]:
    """The Cost section's per-engine rows and total, all stored figures (#1020)."""
    engines = _architecture_engines(report)
    if not engines:
        return []
    facts = report_facts(report)
    out = ["| Engine | Role | Est. monthly | Sized for |", "|---|---|---|---|"]
    for e in engines:
        # #1195: what the figure was sized for, as the facts layer states it.
        sized = cost_sizing_text(e.get("cost_sizing")) or "-"
        out.append(
            f"| {escaping.md_cell(display_engine(e['engine']))} "
            f"| {escaping.md_cell(e['role'])} | {escaping.md_cell(_fmt_usd(e.get('cost')))} "
            f"| {escaping.md_cell(sized)} |"
        )
    out += [
        f"| Total | | {escaping.md_cell(_fmt_usd(facts.get('engine_cost_total_usd')))} | |",
        "",
    ]
    return out


def _risk_posture_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report "Risk posture" section: the overall level, the open and
    resolved counts and how the level is derived, worded as the retired decision
    report worded it (ported from the retired multi-page PDF report, #1020)."""
    facts = report_facts(report)
    risk = report.get("risk_assessment") or {}
    open_count = int(facts.get("open_risk_count") or 0)
    resolved_count = int(facts.get("resolved_risk_count") or 0)
    out = [
        "## Risk posture",
        "",
        f"- Overall risk: {escaping.md_text(risk.get('overall_risk_level', 'not assessed'))}",
    ]
    if filtered_risks(report):
        sev = _severity_counts_text(facts.get("open_risks_by_severity") or {})
        out.append(f"- Open risks: {open_count} ({escaping.md_text(sev)})")
        if resolved_count:
            out.append(
                f"- Resolved by the assignment: {resolved_count}{_resolved_by_severity(facts)}"
            )
        out += ["", f"*{escaping.md_text(RISK_RULE_NOTE)}*", ""]
    else:
        out.append(
            f"- No open migration risks; {resolved_count} resolved by the assignment"
            f"{_resolved_by_severity(facts)}."
            if resolved_count
            else "- No risks were identified in this analysis."
        )
        out += ["", f"*{escaping.md_text(NO_OPEN_RISKS_CAVEAT)}*", ""]
    return out


def _resolved_by_severity(facts: dict[str, Any]) -> str:
    """ " (2 high, 4 medium)" from ``facts.resolved_risks_by_severity``, or ""."""
    text = _severity_counts_text(facts.get("resolved_risks_by_severity") or {})
    return f" ({escaping.md_text(text)})" if text else ""


def _mitigation_strategies_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report "Mitigation strategies" section (#1020).

    A CRITICAL or HIGH risk's line repeats the mitigation its Risk register entry
    prints; here it names the risk and refers to it (#1220). The executive report's
    Next steps print the line whole: it has no register."""
    risk = report.get("risk_assessment") or {}
    strategies = [s for s in (risk.get("mitigation_strategies") or []) if s]
    if not strategies:
        return []
    in_register = {
        str(r.get("risk_id")): str(r.get("mitigation") or "").strip().rstrip(".")
        for r in filtered_risks(report)
    }
    lines: list[str] = []
    for s in strategies:
        split = split_severe_risk_strategy(s)
        if split and in_register.get(split[0]) and split[2].rstrip(".") == in_register[split[0]]:
            lines.append(
                f"- {escaping.md_text(split[1])}: its mitigation is under its Risk register entry."
            )
        else:
            lines.append(f"- {escaping.md_text(s)}")
    return ["## Mitigation strategies", "", *lines, ""]


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

    body = escaping.md_text(unsupported_pattern_text(u))
    # Inside the closing parenthesis the count reads "and 2 more", never a nested "(+2 more)".
    listed = id_bits.replace(f" (+{len(ids) - 3} more)", f" and {len(ids) - 3} more")
    which = f"{'query' if len(ids) == 1 else 'queries'} {listed}" if id_bits else ""

    if str(u.get("pattern_type") or "").strip().strip("*"):
        head_bits = [f"**{escaping.md_text(unsupported_pattern_label(u))}**"]
        if id_bits:
            head_bits.append(f"({listed})")
        head = " ".join(head_bits)
    elif which and body:
        # #1170: with no pattern type, the generic label under "Unsupported
        # patterns" says nothing; #1410: the description leads and the entry
        # names its queries after it, so no id opens the sentence.
        stop = body[-1] if body.endswith((".", "!", "?")) else ""
        return f"{body[: len(body) - len(stop)]} ({which}){stop}"
    elif which:
        head = which[:1].upper() + which[1:]
    else:
        head = ""

    if head and body:
        return f"{head}: {body}"
    return head or body or "(no detail provided)"


def _migration_note_md(mn: dict[str, Any], said: _SaidOnce | None = None) -> str:
    """One readable Markdown bullet for a schema-design ``migration_notes`` entry.

    ``migration_notes`` is a list of dicts (``object_name``, ``object_type``,
    ``source_table`` optional, ``application_logic_required``) -- the same
    four field names on every engine contract, so there is no shape to
    reconcile here the way ``unsupported_patterns`` needs. The report used to
    interpolate the whole list with ``escaping.md_text(notes)``, which prints
    the Python list/dict repr verbatim (e.g.
    ``[{'object_name': 'x', 'object_type': 'trigger', ...}]``); this renders
    one bullet per note instead, tolerant of any of the fields being absent.

    ``said`` (#1574) is the report's ledger of long sentences: a sentence an earlier
    note stated (a model often words two triggers' logic the same) points at that note.
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
    if said is not None and body:
        where = f"in the {escaping.md_text(object_name)} migration note" if object_name else "above"
        body = said.line(body, where)

    if head and body:
        return f"{head}: {body}"
    return head or body or "(no detail provided)"


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
    counts = report_facts(report).get("query_group_counts") or {}
    rows = [r for r in counts.get("relational_rows") or [] if isinstance(r, dict)]
    if not rows:
        return out
    out += ["## Queries on Aurora by table", ""]
    by_engine: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_engine.setdefault(str(r.get("engine")), []).append(r)
    for eng, engine_rows in by_engine.items():
        total_queries = int((counts.get("relational_query_counts") or {}).get(eng) or 0)
        out += [
            f"### {escaping.md_text(display_engine(eng))} "
            f"({total_queries} {plural_noun(total_queries, 'query', 'queries')})",
            "",
            "| Table | Queries | Calls/s | Reason |",
            "|---|---|---|---|",
        ]
        for r in engine_rows:
            g = query_group_at(report, r.get("group_index"))
            reasons = sorted(
                {
                    text
                    for ap in g.get("access_patterns") or []
                    if isinstance(ap, dict)
                    and ap.get("engine") == eng
                    and (text := _reason_text_for(g, ap))
                }
            )
            reason_text = _table_reason_cell(reasons)
            out.append(
                f"| {escaping.md_cell(str(g.get('group_name', '?')))} | {r.get('query_count', 0)} "
                f"| {escaping.md_cell(fmt_num(r.get('calls_per_second', 0)))} "
                f"| {escaping.md_cell(reason_text or '-')} |"
            )
        out.append("")
    return out


# How many distinct reasons one "Queries on Aurora by table" row names; the rest
# are counted. Each query's full reason is in its trace.
_TABLE_REASONS_SHOWN = 3


# A reality-check consolidation's reason. Its scores are each query's own, which the
# appendix lists per query, and its why is under "Migration trade-offs", so a row names
# the move alone (#1220).
_CONSOLIDATED_REASON = "reality check: consolidated from "


def _table_reason_cell(reasons: list[str]) -> str:
    """One table's reasons, each stated once and whole (#951).

    A reason is "<why this engine> | <why not the others>"; the cell names the
    first part only, so the same "highest fit for Aurora MySQL" never repeats
    with a different tail. Reasons that differ only in their per-query detail
    in parentheses ("... (scores tie with OpenSearch; ...)", "... (no unique
    value: DocumentDB scores 79.0, ...)") are named once, without it: each
    query's own detail is in its trace. A reality-check consolidation is named
    without its scores even when only one variant reads (#1220). Nothing is cut
    mid-sentence: after the first few reasons, the rest are counted.
    """
    by_rule: dict[str, list[str]] = {}
    for reason in reasons:
        head = reason.split(" | ", 1)[0].strip()
        if head:
            rule = head.split(" (", 1)[0].strip()
            variants = by_rule.setdefault(rule, [])
            if head not in variants:
                variants.append(head)
    whole = [
        variants[0] if len(variants) == 1 and not rule.startswith(_CONSOLIDATED_REASON) else rule
        for rule, variants in by_rule.items()
    ]
    shown = whole[:_TABLE_REASONS_SHOWN]
    more = len(whole) - len(shown)
    return "; ".join(shown) + (f" (+{more} more)" if more else "")


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
        # #1410: columns whose note reads the same share one bullet that lists them.
        by_note: dict[str, list[str]] = {}
        for n in defaults:
            by_note.setdefault(str(n.get("recommendation") or ""), []).append(
                f"`{escaping.md_code(n.get('source_object') or '?')}`"
            )
        for note, columns in by_note.items():
            text = escaping.md_text(note.strip())
            if len(columns) == 1 or not text:
                out.append(f"- {', '.join(columns)}" + (f": {text}" if text else ""))
            else:
                stop = "" if text.endswith((".", "!", "?")) else "."
                out.append(f"- {text}{stop} Columns: {', '.join(columns)}.")
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
    # A model writes the same note on each index or table it applies to; the first
    # trade-off keeps the sentence and a later one points back to it (#1563).
    said = _SaidOnce()
    if current:
        by_engine: dict[str, list] = {}
        for t in current:
            by_engine.setdefault(t.get("engine") or "(general)", []).append(t)
        for eng, items in by_engine.items():
            # #572: eng is a raw engine id, or "(general)"/"reality-check", neither a
            # known engine; display_engine's title-case fallback would read oddly for
            # them. #1170: "reality-check" is a source id, so it reads "Reality check".
            heading = {"(general)": "(general)", "reality-check": "Reality check"}.get(
                eng, display_engine(eng)
            )
            out += [f"### {escaping.md_text(heading)}", ""]
            for t in items:
                desc = str(t.get("description", "")).strip()
                impact = str(t.get("impact", "")).strip()
                aff = list(t.get("source_tables") or [])
                line = f"- **{escaping.md_text(desc)}**"
                if impact and impact != desc:  # reality-check items repeat their title
                    names = ", ".join(f"`{escaping.md_code(s)}`" for s in aff[:3])
                    where = f"in the trade-off for {names}" if names else "above"
                    line += ": " + said.line(escaping.md_text(impact), where)
                out.append(line)
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


def _target_engines_md(report: dict[str, Any]) -> list[str]:
    """Engineering Report "Target engines" section: the table, the role notes and
    the fit definition.

    Each engine's Scope cell and the "N tables migrate" total are the facts layer's
    figures (``_architecture_engines``, ``facts.tables_migrate``), in the wording
    the retired decision report used (#987).
    """
    target_engines = _architecture_engines(report)
    out: list[str] = []
    if target_engines:
        facts = report_facts(report)
        migrated = int(facts.get("tables_migrate") or 0)
        # Why each engine is in the target, from the workload routed to it (#152)
        out += [
            "## Target engines",
            "",
            "| Engine | Role | Workload | Scope | Why |",
            "|---|---|---|---|---|",
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
                f"| {escaping.md_cell(share)} | {escaping.md_cell(e['scope'])} "
                f"| {escaping.md_cell(e['rationale'] or '-')} |"
            )
        out.append(
            f"| Total | | {fmt_num(facts.get('workload_total_percent') or 0, 0)}% "
            f"| {migrated} {plural_noun(migrated, 'table')} "
            f"{plural_verb(migrated, 'migrates', 'migrate')} | |"
        )
        out.append("")
        # #1020: the executive report's "Queries moved" tile, the same stored fact.
        moving = int(facts.get("moving_query_count") or 0)
        n_in = headline_query_total(facts)
        # #1329: the queries with no engine in the scope, named in the same headline.
        no_engine = no_engine_clause(facts)
        out += [
            f"Queries moved: {moving} of {n_in} {plural_noun(n_in, 'query', 'queries')} "
            f"{plural_verb(moving, 'moves', 'move')} to a new engine (a migration target or "
            "a search read model)"
            + (f"; {no_engine}" if no_engine else "")
            + "; the rest stay on the engine that runs them today.",
            "",
        ]
        if placed_share_note(facts):
            out += [placed_share_note(facts), ""]
        # #1146: the same in-scope queries, as the query patterns they make.
        out += query_patterns_md(report)
        notes = _engine_role_notes_md(
            target_engines,
            migrated,
            sources_pointer=bool((facts.get("table_sources") or {}).get("written_table_count")),
            wave_one=heterogeneous_wave(report),
        )
        if notes:
            out += [" ".join(notes), ""]
        # First appearance of a fit figure in this report (inside "Why", e.g. "30%
        # mean fit"): the plain-language definition every deliverable states once,
        # shared with the executive report and the UI (bug: fit score presented as a
        # confidence percentage). Stated here, not under "Migration
        # map" below -- that section only renders once schema design has produced
        # table_mappings, so a report rendered with no schema design (e.g. every
        # ``--llm-mode none`` run) would otherwise never state the definition at
        # all, even though "Target engines" above already shows the figure.
        out.append(f"*{escaping.md_text(WORKLOAD_FIT_DEFINITION)}*")
        out.append("")
        if report_predates_pin_rule(report):
            # #1389: the stored figures cannot be recounted here (ADR-032).
            out += [f"*{escaping.md_text(PIN_RULE_PREDATES_NOTE)}*", ""]
        # #1291: a listed cache layer's figure is a cache fit of the reads it fronts,
        # not a workload fit; say so once, under the same table.
        if any(e.get("role") == CACHE_LAYER for e in target_engines):
            out.append(f"*{escaping.md_text(CACHE_FIT_DEFINITION)}*")
            out.append("")
    return out


def _fit_cell(fit: Any) -> str:
    return "-" if fit is None else fmt_num(fit)


def _map_gaps_md(basis: dict[str, Any], migrated: int) -> str:
    """#1169: the moving tables the schema design has no target table for yet, and the
    rows it maps to a migration target that no query routed there reads."""
    out = ""
    missing = [str(t) for t in basis.get("migrating_tables_not_mapped_names") or []]
    n = int(basis.get("migrating_tables_not_mapped") or 0)
    if n:
        out += (
            f" The schema design has no target table yet for {n} of the {migrated} "
            f"{plural_noun(migrated, 'table')} that "
            f"{plural_verb(migrated, 'migrates', 'migrate')}: {_code_list(missing)}."
        )
    for eng, tables in (basis.get("mapped_tables_not_served") or {}).items():
        name = escaping.md_text(display_engine(eng))
        out += (
            f" Rows the schema design maps to {name} that no query routed to {name} reads "
            f"stay on the source engine: {_code_list([str(t) for t in tables])}."
        )
    return out


def _code_list(tables: list[str], cap: int = 20) -> str:
    """Table names as code spans: the first ``cap``, then how many more."""
    shown = ", ".join(f"`{escaping.md_code(t)}`" for t in tables[:cap])
    return shown + (f" (+{len(tables) - cap} more)" if len(tables) > cap else "")


def _engine_role_notes_md(
    engines: list[dict[str, Any]],
    migrated: int,
    sources_pointer: bool = False,
    wave_one: dict[str, Any] | None = None,
) -> list[str]:
    """What the retained, search and migration-target roles mean for this plan, and
    what "incl. shared" counts; one sentence each, only for roles the plan has. The
    cache layer has its own section.

    ``wave_one`` is a cross-engine wave 1's facts row (#1120): its engine receives
    every source table, and the later waves move some of them on, so the
    migration-target sentence says that instead of "the N tables assigned to" every
    target."""
    notes: list[str] = []
    retained = [e["engine"] for e in engines if e["role"] == "Retained"]
    if retained:
        names = ", ".join(escaping.md_text(display_engine(x)) for x in retained)
        notes.append(
            f"**{names}** is retained as the relational core (source-compatible, no migration)."
        )
    search = [e["engine"] for e in engines if e["role"] == SEARCH_READ_MODEL]
    if search:
        names = ", ".join(escaping.md_text(display_engine(x)) for x in search)
        notes.append(
            f"**{names}** is a search read model: it serves its queries from data synced "
            "from the engines that own those tables, holds no system-of-record data, and is "
            "built after its owners."
        )
    targets = [e["engine"] for e in engines if e["role"] == "Migration target"]
    first = [str(e) for e in (wave_one or {}).get("engines") or []][:1]
    if targets and first and first[0] in targets:
        later = ", ".join(escaping.md_text(display_engine(x)) for x in targets if x != first[0])
        moved = int((wave_one or {}).get("moving_tables") or 0)
        notes.append(
            f"Wave 1 moves {'the' if moved == 1 else 'all'} {moved} source "
            f"{plural_noun(moved, 'table')} to {escaping.md_text(display_engine(first[0]))}"
            + (f"; later waves move some of them on to {later}." if later else ".")
        )
    elif targets:
        names = ", ".join(escaping.md_text(display_engine(x)) for x in targets)
        notes.append(
            f"The migration moves the {migrated} {plural_noun(migrated, 'table')} assigned "
            f"to {names}."
        )
    if any(SHARED_TABLES_LABEL in str(e["scope"]) for e in engines):
        notes.append(escaping.md_text(SHARED_TABLES_NOTE))
    if _defines_kept_in_sync(engines):
        notes.append(escaping.md_text(KEPT_IN_SYNC_NOTE))
        if sources_pointer:
            notes.append(escaping.md_text(KEPT_IN_SYNC_SOURCES_NOTE))
    return notes


def _defines_kept_in_sync(engines: list[dict[str, Any]]) -> bool:
    """Whether "Target engines" defines served, migrate and kept in sync
    (``KEPT_IN_SYNC_NOTE``): some migration target serves tables it does not move."""
    return any(e.get("kept_in_sync") or e.get("moves_to") for e in engines)


# #1410: the Migration map refers to the definitions "Target engines" gave above.
MAP_SPLIT_DEFINED_ABOVE = (
    "Migrate and kept in sync are as Target engines above defines them; Migrate counts the "
    "same tables as its Total."
)


def _affects_line(risk: dict[str, Any]) -> str | None:
    """A risk's "Affects" sub-bullet: its first eight tables, then how many more."""
    aff = [a for a in risk.get("affected_tables") or [] if a]
    if not aff:
        return None
    shown = ", ".join(f"`{escaping.md_code(a)}`" for a in aff[:8])
    more = f" (+{len(aff) - 8} more)" if len(aff) > 8 else ""
    return f"  - Affects: {shown}{more}"


def _unsupported_entries(design: dict[str, Any]) -> list[Any]:
    """A design's ``unsupported_patterns`` as "Target schemas by engine" lists them: a
    bare string is one entry, not one character per entry; any other shape is none."""
    unsupported = design.get("unsupported_patterns") or []
    if isinstance(unsupported, str):
        return [unsupported]
    return unsupported if isinstance(unsupported, list) else []


# Where this report lists a risk's details in full (#1410), by rule: the section, and
# whether it comes before ("above") or after ("below") the risk register.
_DETAILS_SECTION = {
    "design.trade_offs": ("Migration trade-offs", "below"),
    "cache.unsupported": ("Target schemas by engine", "above"),
}


def _texts_listed_elsewhere(report: dict[str, Any], rule: Any, eng: str) -> set[str]:
    """The detail texts this report lists in full in another section for a risk of
    ``rule`` on ``eng`` (#1410), written as synthesis writes a risk's details
    (``src.shared.design_text``): each trade-off title under "Migration trade-offs";
    each unsupported pattern of a completed cache design under "Target schemas by
    engine" (a bare-string entry is printed as it is, so it matches no detail)."""
    engine = canonical_engine_id(eng)
    if rule == "design.trade_offs":
        return {
            design_text_item(str(t.get("description") or ""))
            for t in report.get("trade_offs") or []
            if isinstance(t, dict) and canonical_engine_id(str(t.get("engine") or "")) == engine
        }
    if rule == "cache.unsupported":
        return {
            unsupported_pattern_detail(u)
            for e, design in _completed_designs(report).items()
            if canonical_engine_id(e) == engine
            for u in _unsupported_entries(design)
            if isinstance(u, dict)
        }
    return set()


def _carried_write_lines(details: list[str]) -> list[str]:
    """The carried writes of a ``design.carried_writes`` risk (#1454), the statements that
    share a target-native approach listed under it once: ``Statements: ... Approach: ...``.
    A statement with an approach of its own keeps its line, in the same order."""
    parsed = [(d, split_carried_write_detail(d)) for d in details]
    leads: dict[str, list[str]] = {}
    for _, split in parsed:
        if split:
            leads.setdefault(split[1], []).append(split[0])
    out: list[str] = []
    done: set[str] = set()
    for detail, split in parsed:
        if split is None or len(leads[split[1]]) == 1:
            out.append(f"  - {escaping.md_text(detail)}")
        elif split[1] not in done:
            done.add(split[1])
            names = "; ".join(escaping.md_text(lead) for lead in leads[split[1]])
            out.append(f"  - Statements: {names}. Approach: {escaping.md_text(split[1])}")
    return out


def _risk_details_md(report: dict[str, Any], risk: dict[str, Any], eng: str) -> list[str]:
    """A risk's ``details`` sub-bullets (#950): each text in full, except those this
    report already lists in their own section (#1410), matched by text. Those are named
    by a pointer to that section, unless the risk's own text already names it and none
    is left here."""
    details = [d for d in risk.get("details") or [] if isinstance(d, str) and d.strip()]
    elsewhere = _texts_listed_elsewhere(report, risk.get("rule"), eng)
    kept = [d for d in details if d not in elsewhere]
    out = _carried_write_lines(kept) if risk.get("rule") == "design.carried_writes" else None
    if out is None:
        out = [f"  - {escaping.md_text(d)}" for d in kept]
    if len(kept) < len(details):
        where, side = _DETAILS_SECTION[str(risk.get("rule"))]
        heading = where if where == "Migration trade-offs" else f"{where} ({display_engine(eng)})"
        if kept:
            out.append(f"  - The others are listed under {heading} {side}.")
        elif where not in str(risk.get("description") or ""):
            out.append(f"  - Each one is listed under {heading} {side}.")
    return out


# A sentence this long is stated once in the report's risk sections (#1454); the same
# length the report guard (``tests/report_concision.py``) holds every sample to.
_ONCE_MIN_WORDS = 15
_SENTENCE_END = re.compile(r"((?<=[.!?])[:;]?\s+)")


class _SaidOnce:
    """Risk text that states each long sentence once (#1454).

    Analysis risks carry a catalog explanation ("Aurora serves single-table CRUD natively
    ...") that the register prints for the queries still flagged and "Resolved by the
    assignment" prints again for the queries the assignment moved. The first line to
    print a sentence keeps it; a later line says where it is stated. Sentences are cut
    where the guard cuts them, so a sentence is replaced only when it reads word for word
    as one printed before."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def line(self, text: str, where: str) -> str:
        """``text`` with each long sentence stated before replaced by a pointer to
        ``where`` (the first line's own label: ``under RISK-001`` or ``above``)."""
        parts = _SENTENCE_END.split(text)
        for i in range(0, len(parts), 2):
            key = " ".join(parts[i].replace("**", "").split()).strip(" -*:,.;")
            if len(key.split()) < _ONCE_MIN_WORDS:
                continue
            if key in self._seen:
                end = parts[i][len(parts[i].rstrip(" .;:,")) :]
                parts[i] = f"Stated {self._seen[key]}{end or '.'}"
            else:
                self._seen[key] = where
        return "".join(parts)


def _close_call_body(body: str, risk: dict[str, Any], listed: bool) -> str:
    """A close-call risk's text without its engine pairs when the Close calls section
    (``listed``) shows them, one table row per pair (#1220); any other text as it is."""
    split = split_close_call_description(body) if listed else None
    if risk.get("rule") != "routing.close_calls" or split is None:
        return body
    lead, _, tail = split
    return f"{lead}, listed by engine pair under Close calls. {tail}"


def _risk_register_md(report: dict[str, Any], said: _SaidOnce | None = None) -> list[str]:
    """Engineering Report "Risk register" and "Resolved by the assignment" sections.

    ``said`` is the report's ledger of long sentences, shared with the migration notes
    (#1574): a mitigation built from a note's logic points at the note."""
    facts = report_facts(report)
    out: list[str] = []
    said = said if said is not None else _SaidOnce()
    close_calls = bool(close_calls_md(report))
    risks = filtered_risks(report)
    if risks:
        by_eng: dict[str, list] = {}
        for r in risks:
            eng, _ = _risk_engine_and_body(r.get("description"))
            by_eng.setdefault(eng, []).append(r)
        out += [f"## Risk register ({facts.get('open_risk_count', 0)})", ""]
        # #1410: a mitigation another risk above already states in full is named by it.
        mitigated_by: dict[str, str] = {}
        for eng, items in by_eng.items():
            # #572: eng is the raw [engine] tag _risk_engine_and_body parsed off the
            # description (or "(general)") -- display_engine leaves "(general)" as is
            # (not a known engine id, falls back to its own title-cased fallback,
            # which already reads fine unchanged).
            heading = eng if eng == "(general)" else display_engine(eng)
            out += [f"### {escaping.md_text(heading)}", ""]
            for r in items:
                _, body = _risk_engine_and_body(r.get("description"))
                body = _close_call_body(body, r, close_calls)
                sev = r.get("severity", "-")
                rtype = str(r.get("risk_type", "")).replace("_", " ").lower()
                rid = r.get("risk_id", "-")
                where = f"under {escaping.md_text(rid)}"
                out.append(
                    f"- **{escaping.md_text(rid)}** \u00b7 {escaping.md_text(sev)} \u00b7 "
                    + said.line(f"{escaping.md_text(rtype)} \u00b7 {escaping.md_text(body)}", where)
                )
                mitigation = str(r.get("mitigation") or "")
                if mitigation and not _repeats(mitigation, body):
                    if mitigation in mitigated_by:
                        out.append(f"  - Mitigation: as for {mitigated_by[mitigation]}.")
                    else:
                        mitigated_by[mitigation] = escaping.md_text(rid)
                        out.append(
                            "  - Mitigation: " + said.line(escaping.md_text(mitigation), where)
                        )
                affects = _affects_line(r)
                if affects:
                    out.append(affects)
                # #1527: a design conflict's derived copies, their sync and its cost.
                out += derived_copy_sync_md(report, r.get("risk_id"))
                # #950: the design texts the risk covers, in full (the executive
                # report states only their count), each one once in this report.
                out += _risk_details_md(report, r, eng)
            out.append("")

    resolved = resolved_risks(report)
    if resolved:
        # Analysis risks the effective assignment resolved (#221): listed so none
        # disappears without a record.
        out += [f"## Resolved by the assignment ({facts.get('resolved_risk_count', 0)})", ""]
        # #1410: risks resolved for the same reason are listed together, and the
        # reason is stated once, under the first of them.
        by_reason: dict[str, list[dict[str, Any]]] = {}
        for r in resolved:
            by_reason.setdefault(str(r.get("reason") or ""), []).append(r)
        for reason, members in by_reason.items():
            for i, r in enumerate(members):
                _, body = _risk_engine_and_body(r.get("description"))
                where = resolved_risk_label(r)
                out.append(
                    "- "
                    + said.line(
                        f"{escaping.md_text(r.get('severity', '-'))} \u00b7 "
                        f"{escaping.md_text(where)} \u00b7 {escaping.md_text(body)}",
                        "above",
                    )
                )
                if reason and i == 0:
                    out.append(f"  - Resolved because {escaping.md_text(reason)}.")
                elif reason:
                    out.append("  - Resolved for the same reason as the risk above.")
                # #950: a resolved risk keeps naming its tables, as an open one does.
                affects = _affects_line(r)
                if affects:
                    out.append(affects)
        out.append("")
    return out


def _query_groups_heading(facts: dict[str, Any]) -> str:
    """``## Query groups (33 of 51)``: the groups listed, of all of them (#1410)."""
    counts = facts.get("query_group_counts") or {}
    listed = int(counts.get("listed_group_count") or 0)
    total = int(counts.get("group_count") or 0)
    return (
        f"## Query groups ({listed} of {total})"
        if total > listed
        else f"## Query groups ({listed})"
    )


def _relational_only_groups_text(facts: dict[str, Any]) -> str:
    """The sentence naming the query groups only relational engines serve (#1410)."""
    counts = facts.get("query_group_counts") or {}
    n = int(counts.get("relational_only_group_count") or 0)
    if not n:
        return ""
    names = ", ".join(display_engine(str(e)) for e in counts.get("relational_only_engines") or [])
    run = "runs" if n == 1 else "run"
    on = f" {run} only on {names}" if names else f" {run} only on a relational engine"
    if not counts.get("listed_group_count"):
        lead = "The only group" if n == 1 else f"All {n} groups"
    else:
        lead = f"The other {n} {plural_noun(n, 'group')}"
    return (
        f"{lead}{on}; Queries on Aurora by table lists "
        f"{'its' if n == 1 else 'their'} queries per table."
    )


def _ungrouped_queries_text(facts: dict[str, Any]) -> str:
    """The sentences naming each owner engine's queries no group serves (#226)."""
    counts = facts.get("query_group_counts") or {}
    out = []
    for row in counts.get("ungrouped_queries") or []:
        if not isinstance(row, dict) or not row.get("query_count"):
            continue
        n = int(row["query_count"])
        name = display_engine(str(row.get("engine")))
        queries = f"{n} {name} {plural_noun(n, 'query', 'queries')}"
        if row.get("schema_design_available"):
            out.append(
                f"No group serves {queries}: no access pattern in {name}'s schema design "
                f"lists {'it' if n == 1 else 'them'}."
            )
        else:
            out.append(
                f"No group yet for {queries}: {name}'s schema design has not run, and its "
                "access patterns make the groups."
            )
    return " ".join(out)


def render_engineering_report_md(
    report: dict[str, Any],
    prov: dict[str, str] | None = None,
    *,
    trust_generated_summary: bool | None = None,
) -> str:
    """Build-team-facing document: the executive summary, migration map, per-engine
    target schemas, query groups, risks and traces. Markdown with mermaid fences,
    which render in the tooling engineers open it in (VS Code, GitHub, GitLab).

    ``trust_generated_summary`` decides whether a generated summary is shown;
    ``None`` decides it from the report (``summary_trusted``).
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
        "# Database Modernization: Engineering Report",
        "",
        f"Source database: `{escaping.md_code(db)}`. This is the build companion to the "  # nosemgrep: string-concat-in-list -- intentional multi-line string
        "executive report (`executive-report.pdf`): "
        "the source-to-target mapping, the per-engine target schemas, and the query "
        "groups.",
        "",
    ]

    facts = report_facts(report)
    if facts.get("engine_scope"):
        # #1328: the customer's engine scope, stated once near the top; each name is
        # escaped, since a loaded report.json may hold any string there.
        out += [engine_scope_statement(facts["engine_scope"], escaping.md_text) + ".", ""]
    trust = summary_trusted(report) if trust_generated_summary is None else trust_generated_summary
    out += _summary_md(report, trust)
    out += _target_engines_md(report)

    tco = report.get("tco_analysis") or {}
    if tco:
        current_cost_text, savings_text = _cost_baseline_text(tco)
        # #949 (#1028): with no source baseline the current-cost and savings lines are
        # left out, not "source cost not provided" / "not available".
        known = cost_baseline_known(tco)
        out += ["## Cost", "", escaping.md_text(cost_scope_text(facts)), ""]
        if known:
            out.append(f"- Current monthly (source): {escaping.md_text(current_cost_text)}")
        out.append(
            f"- Projected monthly (target): "
            f"{escaping.md_text(_fmt_usd(tco.get('projected_monthly_cost')))}"
        )
        if known:
            out.append(f"- Savings: {escaping.md_text(savings_text)}")
        if cost_exclusion_text(facts):
            out.append(f"- {cost_exclusion_text(facts)}")
        prices_text = price_list_text(facts)
        if prices_text:
            out.append(f"- List prices: {escaping.md_text(prices_text)}")
        # #1076: an engine the source runs today that the plan retires, stated once.
        for statement, reason, _name in _retired_in_use_lines(facts):
            line = f"Retired by this plan: {statement}."
            if reason:
                line += f" {reason}."
            line += " Not in this projection (see Migration trade-offs below for why)."
            out.append(f"- {escaping.md_text(line)}")
        # #1197: the read I/O basis a figure rests on (report facts, ADR-032).
        for note in facts.get("cost_basis_notes") or []:
            out.append(f"- Cost basis: {escaping.md_text(note)}")
        out.append("")
        out += _cost_by_engine_md(report)
        # #1195: the TCO's assumption lines, as the facts state them for every deliverable.
        assumptions = facts.get("cost_assumptions") or []
        if assumptions:
            out += [f"Assumptions: {escaping.md_text('; '.join(assumptions))}.", ""]
        # #1474: the one caveat where the costs are stated.
        out += [escaping.md_text(cost_caveat_text(facts)), ""]

    mappings = [m for m in (report.get("table_mappings") or []) if isinstance(m, dict)]
    mapped = int((facts.get("table_basis") or {}).get("mapped_tables") or 0)
    ownership = ownership_facts(report)
    if mappings and ownership:
        # #212: one line per table, its owner (the engine that takes its writes, never a
        # cache), the engines that also read it and its cache, from the facts layer.
        written = (facts.get("table_sources") or {}).get("written_table_count")
        # #1410: "Target engines" above already defined migrate and kept in sync, and
        # pointed at Sources of truth and replicas; the map refers to it, once.
        defined_above = _defines_kept_in_sync(_architecture_engines(report))
        sources_pointer = (
            " Sources of truth and replicas below lists each written table's replica reads."
            if written and not defined_above
            else ""
        )
        split = _mapping_split(report, mappings)
        basis = facts.get("table_basis") or {}
        split_note = (
            f" {MAP_SPLIT_DEFINED_ABOVE if defined_above else MAP_SPLIT_NOTE}"
            if (basis.get("mapped_tables_by_move") or {}).get("kept_in_sync")
            else ""
        )
        gaps = _map_gaps_md(basis, int(facts.get("tables_migrate") or 0))
        out += [
            f"## Migration map ({mapped} {plural_noun(mapped, 'table')}{split})",
            "",
            f"{escaping.md_text(OWNER_RULE)}{split_note}{gaps}{sources_pointer}",
            "",
            escaping.md_text(ownership_summary_text(ownership)),
            "",
            "| Source table | Owner | Also read by | Cached by | Target | Pattern "
            f"| {WORKLOAD_FIT_LABEL} |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in ownership.get("table_lines") or []:
            # Code-span cells go through md_cell too, as in the schema table below (#466).
            source = escaping.md_cell(f"`{escaping.md_code(row.get('table', '?'))}`")
            table, design = target_cell(row)
            target = escaping.md_cell(
                f"`{escaping.md_code(table)}`" + (f" ({design} design)" if design else "")
            )
            out.append(
                f"| {source} "
                f"| {escaping.md_cell(owner_cell(row))} "
                f"| {escaping.md_cell(also_read_by_cell(row))} "
                f"| {escaping.md_cell(cached_by_cell(row))} "
                f"| {target} "
                f"| {escaping.md_cell(row.get('aggregate_pattern') or '-')} "
                f"| {escaping.md_cell(_fit_cell(row.get('confidence_score')))} |"
            )
        out.append("")

    out += unreviewed_moves_md(report)
    out += _tied_queries_md(report)
    out += auto_decided_md(report)
    out += unservable_md(report)
    out += close_calls_md(report)
    out += _reroutes_md(report)
    out += _cache_layer_md(report)
    out += _roadmap_md(report)
    out += sources_of_truth_md(report)

    # #1454, #1574: one ledger of long sentences for the migration notes and the risk
    # register, so a sentence is stated once in the report.
    said = _SaidOnce()
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
            unsupported = _unsupported_entries(dz)
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
                    f"- {_migration_note_md(mn, said) if isinstance(mn, dict) else escaping.md_text(mn)}"
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

    # #478: a group served only by relational engine(s) is shown per table by the
    # "Queries on Aurora by table" subsection below, not here (359 extra rows on the
    # discourse sample); a group a relational engine shares with another engine stays.
    # #1410: the rows are the facts' listed groups, the same list the heading counts.
    groups = [
        g
        for i in (facts.get("query_group_counts") or {}).get("listed_group_indexes") or []
        if (g := query_group_at(report, i))
    ]
    # #226: the queries no group serves are stated here too, even with no listed group.
    ungrouped = _ungrouped_queries_text(facts)
    if groups or ungrouped:
        # "Query groups", as the deck summary calls them: the assignment's
        # co-dependency groups (tables that must move together) are a different,
        # usually much smaller count the deck also shows (#258).
        co_dep = (report.get("assignment_summary") or {}).get("co_dependency_groups")
        out += [_query_groups_heading(facts), ""]
        intro = ["Queries grouped by the access patterns that serve them."]
        # #478, #1410: the groups excluded just above (pure-Aurora, their queries
        # listed per table under "Queries on Aurora by table") are named once, with
        # the facts' count, so the heading's two counts add up.
        aurora = _relational_only_groups_text(facts)
        if aurora:
            intro.append(aurora)
        if ungrouped:
            intro.append(ungrouped)
        if any(g.get("cache_layer") for g in groups):
            intro.append(
                "A cache layer group lists the reads it fronts as cached reads; "
                "their owner's group serves them."
            )
        if isinstance(co_dep, int):
            intro.append(
                f"These are not the assignment's {co_dep} co-dependency "
                f"{plural_noun(co_dep, 'group')} (tables that must move together)."
            )
        out += [" ".join(intro), ""]
        if groups:
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
            queries = len(sqs) if isinstance(sqs, list) else escaping.md_cell(sqs or "-")
            if g.get("cache_layer") and isinstance(sqs, list):
                # #226: the reads it fronts, which their owner's group serves.
                queries = f"{len(sqs)} cached {plural_noun(len(sqs), 'read')}"
            out.append(
                f"| {escaping.md_cell(g.get('group_name', '?'))} | {escaping.md_cell(engines)} "
                f"| {len(aps) if isinstance(aps, list) else escaping.md_cell(aps or '-')} "
                f"| {queries} "
                f"| {escaping.md_cell(fmt_num(g.get('total_design_rps', '-')))} |"
            )
        if groups:
            out.append("")

    out += _aurora_queries_by_table_md(report)

    out += _risk_posture_md(report)
    out += _risk_register_md(report, said)
    out += _mitigation_strategies_md(report)

    out += _trade_offs_md(report, facts)
    out += query_traces_md(report)

    out += [
        "---",
        "",
        "Assignments are deterministic. The complete machine-readable assessment is the "  # nosemgrep: string-concat-in-list -- intentional multi-line string
        "Assessment Data (JSON) artifact.",
        "",
    ]
    # #1166: the code commit of the render, the file's last line.
    out += commit_footer(prov)
    return "\n".join(out)
