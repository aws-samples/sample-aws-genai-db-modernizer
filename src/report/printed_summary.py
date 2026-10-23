"""The text /modernize quotes at the approval gate and at completion (#1307, ADR-032).

Both blocks are printed by ``scripts/print_summary.py``; the command quotes them verbatim
instead of composing them, so a figure is the one the deliverables state. They mirror what the
UI shows, at the UI's depth, and point to the artifacts for the detail:

- the gate block is the assignment page's hero: the recommended architecture, the engine
  table (the query flow), the engine consolidations, the architectural patterns and the
  pending engine choices;
- the final summary is the results page's top: the executive summary, the headline figures,
  one line per engine, the open risks by severity with the top ones, and where to look next.

Every figure comes from the facts layer (``report_facts`` for the final summary,
``gate_block_facts`` for the gate); this module only formats, never cuts prose the
deliverables show in full, and leaves a line out when its figure is missing, as the reports do.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from src.shared.engine_names import display_engine
from src.shared.engines_in_use import retired_in_use_statement
from src.shared.gate_facts import gate_block_facts
from src.shared.risk_severity import severity_of, severity_rank

from . import escaping
from .cache_layer import cache_change_notes, cache_layer_text
from .deliverables import FOLDER_ENTRIES, FolderEntry
from .engineering_report import summary_paragraphs
from .executive_report import _names_pattern, table_names, without_table_names
from .facts import report_facts
from .formatting import (
    _cost_baseline_text,
    _fmt_usd,
    cost_baseline_known,
    fmt_num,
    plural_noun,
)
from .query_patterns import query_pattern_count_text
from .risks import (
    NO_OPEN_RISKS_CAVEAT,
    _repeats,
    _risk_engine_and_body,
    _severity_counts_text,
    filtered_risks,
)

# The final summary lists every CRITICAL and HIGH risk, and tops the list up with the next most
# severe ones to at least this many; the results page and the engineering report list them all.
MIN_LISTED_RISKS = 5

_CODE_SPAN = re.compile(r"`[^`\n]*`")


def prose(text: Any) -> str:
    """Escape untrusted text for flowing Markdown, leaving the inside of a code span as written.

    ``md_text`` backslash-escapes ``( ) _ *`` and the like, which a code span shows literally;
    the span's own backticks already keep its content inert.
    """
    value = str(text)
    out: list[str] = []
    last = 0
    for m in _CODE_SPAN.finditer(value):
        out.append(escaping.md_text(value[last : m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(escaping.md_text(value[last:]))
    return "".join(out)


def _percent(value: Any) -> str:
    return f"{fmt_num(value, 1)}%" if isinstance(value, (int, float)) else "-"


def _names(engines: Sequence[str]) -> str:
    return ", ".join(display_engine(e) for e in engines)


# --------------------------------------------------------------------------- gate


def render_gate_block(
    gate: Mapping[str, Any],
    *,
    database_name: str,
    job_id: str,
    summary_url: str | None = None,
) -> str:
    """The approval-gate block, as the assignment page presents it.

    ``summary_url`` (the job's summary page, given unless the run is chat only) ends the
    block, so quoting the block quotes the pointer the user follows before approving."""
    facts = gate_block_facts(gate)
    lines = [f"## Assignment for approval: {prose(database_name)} (job {job_id})", ""]
    surviving = [display_engine(e) for e in facts["surviving"]]
    if surviving:
        head = (
            surviving[0]
            if len(surviving) == 1
            else ", ".join(surviving[:-1]) + f" and {surviving[-1]}"
        )
        lines += [f"Recommended architecture: {head}", ""]
    narrative = str(gate.get("executive_summary") or "").strip()
    if narrative:
        lines += [prose(narrative), ""]
    lines += [
        "| Engine | Queries before | Queries final | Share of final | Outcome |",
        "|---|---:|---:|---:|---|",
    ]
    for row in facts["rows"]:
        outcome = "dropped" if row["dropped"] else "kept"
        lines.append(
            f"| {display_engine(row['engine'])} | {row['before']} | {row['after']} | "
            f"{_percent(row['share_percent'])} | {outcome} |"
        )
    lines += [
        "",
        f"Total: {facts['total_after']} {plural_noun(facts['total_after'], 'query', 'queries')}.",
        "",
    ]
    consolidations = [c for c in gate.get("consolidations") or [] if isinstance(c, Mapping)]
    if facts["unchanged"]:
        lines += ["Reality check changed nothing: the assignment stays as routed.", ""]
    if consolidations:
        lines.append("Consolidations:")
        for c in consolidations:
            reason = str(c.get("reason") or "").strip()
            lines.append(
                f"- {display_engine(str(c.get('from')))} to {display_engine(str(c.get('to')))}: "
                f"{c.get('queries')} {plural_noun(c.get('queries'), 'query', 'queries')}"
                + (f". {prose(reason)}" if reason else "")
            )
        lines.append("")
    kept = [s for s in gate.get("sweep_kept") or [] if isinstance(s, Mapping)]
    if kept:
        lines.append("Small engines kept:")
        for s in kept:
            lines.append(f"- {display_engine(str(s.get('engine')))}: {prose(s.get('reason'))}")
        lines.append("")
    candidates = facts["cache_candidates"]
    if candidates:
        name = display_engine(facts["cache_engine"]) if facts["cache_engine"] else "The cache layer"
        # #1252: at the gate these are candidates; the schema design confirms the ones it covers.
        count = candidates["read_count"]
        lines += [
            f"Cache layer: {name} has {count} cache {plural_noun(count, 'candidate')} "
            f"({_percent(candidates['call_share_percent'])} of calls) and owns no queries. "
            "Schema design then confirms which ones it covers.",
            "",
        ]
    patterns = [p for p in gate.get("architectural_patterns") or [] if isinstance(p, Mapping)]
    if patterns:
        lines.append("Architectural patterns:")
        for p in patterns:
            lines.append(f"- {prose(p.get('name'))}")
            if p.get("when"):
                lines.append(f"  - When: {prose(p['when'])}")
            if p.get("example"):
                lines.append(f"  - {prose(p['example'])}")
        lines.append("")
    pending = [p for p in gate.get("pending_choices") or [] if isinstance(p, Mapping)]
    if pending:
        lines.append("Engine choices to settle before schema design:")
        for p in pending:
            count = p.get("queries")
            lines.append(
                f"- `{escaping.md_code(p.get('table'))}` ({count} {plural_noun(count, 'query', 'queries')}"
                f"): {prose(p.get('why_tied'))}"
            )
            for e in p.get("engines") or []:
                if isinstance(e, Mapping):
                    lines.append(
                        f"  - {display_engine(str(e.get('engine')))}, score {e.get('score')}: "
                        f"{prose(e.get('trade_off'))}"
                    )
            if p.get("decide_for_me"):
                lines.append(
                    f"  - Decide for me picks {display_engine(str(p['decide_for_me']))}: "
                    f"{prose(p.get('decide_for_me_reason'))}"
                )
        lines.append("")
    elif facts["surviving"]:
        designed = [str(e) for e in gate.get("schema_design_engines") or []]
        if designed:
            lines += [f"Schema design runs next for: {_names(designed)}.", ""]
    if summary_url:
        lines.append(f"- Summary page (the query flow and every query's engine): {summary_url}")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- final


def ordered_risks(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The open risks, most severe first (CRITICAL, HIGH, MEDIUM, LOW, then unrated), each
    severity in the report's own order."""
    return sorted(filtered_risks(dict(report)), key=lambda r: -severity_rank(severity_of(r)))


def listed_risks(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The risks the summary lists: every CRITICAL and HIGH one, topped up to
    ``MIN_LISTED_RISKS`` with the next most severe."""
    risks = ordered_risks(report)
    serious = [r for r in risks if severity_of(r) in ("CRITICAL", "HIGH")]
    return risks[: max(len(serious), MIN_LISTED_RISKS)]


def _engine_lines(report: Mapping[str, Any], facts: Mapping[str, Any], names: Any) -> list[str]:
    lines = []
    for e in facts.get("engines") or []:
        if not isinstance(e, Mapping):
            continue
        name = display_engine(str(e.get("engine")))
        if e.get("cached_queries") is not None:
            # The cache layer owns no queries: its figure is the reads it fronts, never a share.
            scope = cache_layer_text(
                int(e["cached_queries"]), float(e.get("cached_call_share_percent") or 0)
            )
        else:
            scope = f"{_percent(e.get('workload_percent'))} of queries"
        detail = f"{e.get('role')}, {scope}, {_fmt_usd(e.get('monthly_cost_usd'))}/mo"
        why = without_table_names(e.get("rationale"), names)
        lines.append(f"- {name} ({detail})" + (f": {prose(why)}" if why else ""))
    return lines


def _risk_lines(report: Mapping[str, Any], facts: Mapping[str, Any]) -> list[str]:
    risk = report.get("risk_assessment") or {}
    level = str(risk.get("overall_risk_level") or "").strip()
    risks = ordered_risks(report)
    resolved = facts.get("resolved_risk_count") or 0
    resolved_text = _severity_counts_text(facts.get("resolved_risks_by_severity") or {})
    lines: list[str] = []
    if level:
        lines.append(f"Overall risk: {prose(level)}")
    if not risks:
        lines.append(f"Open risks: none. {NO_OPEN_RISKS_CAVEAT}")
    else:
        counts = _severity_counts_text(facts.get("open_risks_by_severity") or {})
        total = facts.get("open_risk_count", 0)
        lines.append(f"Open risks: {total}" + (f" ({counts})" if counts else ""))
    if resolved:
        lines.append(
            f"Resolved by the assignment: {resolved}"
            + (f" ({resolved_text})" if resolved_text else "")
        )
    shown = listed_risks(report)
    if shown:
        lines += ["", "Most severe first:"]
    for r in shown:
        engine, body = _risk_engine_and_body(r.get("description"))
        label = engine if engine == "(general)" else display_engine(engine)
        rid = f" ({prose(r['risk_id'])})" if r.get("risk_id") else ""
        lines.append(f"- {prose(severity_of(r))}, {prose(label)}{rid}: {prose(body)}")
        mitigation = r.get("mitigation")
        if mitigation and not _repeats(mitigation, body):
            lines.append(f"  - Mitigation: {prose(' '.join(str(mitigation).split()))}")
    if shown and facts.get("open_risk_count", 0) > len(shown):
        lines += ["", "The results page and the engineering report list every open risk."]
    return lines


def render_final_summary(
    report: Mapping[str, Any],
    *,
    database_name: str,
    job_id: str,
    deliverables_dir: str,
    deliverables: Sequence[FolderEntry],
    summary_url: str | None = None,
    results_url: str | None = None,
) -> str:
    """The completion summary, as the results page opens: the executive summary, the headline
    figures, the engines, the risks and where to look next."""
    facts = report_facts(dict(report))
    tco = report.get("tco_analysis") or {}
    names = _names_pattern(table_names(dict(report)))
    lines = [f"## Assessment: {prose(database_name)} (job {job_id})", ""]

    lines += ["### Executive summary", ""]
    paragraphs, withheld = summary_paragraphs(dict(report))
    if paragraphs:
        for chunk in paragraphs:
            lines += [prose(chunk), ""]
    else:
        lines += ["No executive summary was generated for this run.", ""]
    if withheld:
        lines += [f"*{prose(withheld)}*", ""]

    lines += ["### At a glance", ""]
    queries = query_pattern_count_text(dict(report))
    if queries:
        lines.append(f"- Workload: {queries}")
    engines = [e for e in facts.get("engines") or [] if isinstance(e, Mapping)]
    labelled = [
        display_engine(str(e.get("engine")))
        + (" (cache layer)" if e.get("cached_queries") is not None else "")
        for e in engines
    ]
    lines.append(f"- Target engines: {', '.join(labelled) if labelled else 'none'}")
    lines.append(f"- Projected monthly cost: {_fmt_usd(facts.get('projected_monthly_cost_usd'))}")
    if cost_baseline_known(tco):
        current, savings = _cost_baseline_text(tco)
        lines += [f"- Current monthly cost (source): {current}", f"- Savings: {savings}"]
    for row in facts.get("retired_in_use_engines") or []:
        if isinstance(row, Mapping) and row.get("engine"):
            lines.append(f"- Retired by this plan: {retired_in_use_statement(row)}")
    waves = facts.get("migration_wave_count", 0)
    lines.append(f"- Migration: {waves} {plural_noun(waves, 'wave')}")
    lines += [f"- Cache layer note: {prose(n)}" for n in cache_change_notes(dict(report))]

    lines += ["", "### Engines", ""] + _engine_lines(report, facts, names)
    lines += ["", "### Risks", ""] + _risk_lines(report, facts)

    lines += ["", "### Where to look next", ""]
    if deliverables:
        lines += [
            f"- `{deliverables_dir}{e.filename}`: {e.title}. For: {e.audience}."
            for e in deliverables
        ]
        lines.append(f"- `{deliverables_dir}index.md`: lists each file and the assignment version")
    else:
        lines.append("- No deliverable is rendered yet.")
    missing = [e for e in FOLDER_ENTRIES if e not in deliverables]
    if deliverables and missing:
        lines.append("- Not rendered this time: " + ", ".join(f"`{e.filename}`" for e in missing))
    if results_url:
        lines.append(f"- Results page (the interactive report): {results_url}")
    if summary_url:
        lines.append(f"- Summary page (the query flow and the assignment): {summary_url}")
    return "\n".join(lines).rstrip() + "\n"
