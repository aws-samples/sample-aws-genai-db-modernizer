"""The detailed engineering report: the full routing detail, for auditing a result (#1364).

The engineering report tells the build team's story; this one is the record behind it,
"debug mode on": per table its owner line and every query that touches it with its
routed score on each analysed engine; per query its scores by dimension, every Move
of the ledger with its reason, tie and close-call detail and the redesign's key
candidates; per write group and co-dependency group the group score, the winner's
margin and the members that fit poorly, with the target-native guidance.

Everything comes from ``src.shared.report_facts.routing_detail_facts`` (read from the
job's saved routing records, ``src.shared.routing_detail``); this module only formats
it (ADR-032) and escapes every collected or generated string (``escaping``).
"""

from __future__ import annotations

from typing import Any

from src.shared.engine_names import display_engine
from src.shared.move_reasons import rule_label
from src.shared.report_facts import CLOSE_CALL_MARGIN
from src.shared.target_databases import engine_scope_statement

from . import escaping
from .formatting import commit_footer, fmt_num, plural_noun

FILENAME = "engineering-report-detailed.md"

ABSENT = "not recorded"


def _engine(engine: Any) -> str:
    return escaping.md_cell(display_engine(str(engine))) if engine else "—"


def _engine_text(engine: Any) -> str:
    return escaping.md_text(display_engine(str(engine))) if engine else "no engine"


def _score(value: Any) -> str:
    return fmt_num(value, 1) if isinstance(value, (int, float)) else "—"


def _cell(cell: dict[str, Any] | None) -> str:
    """A routed score, the gate that kept the engine out, or a dash when unrecorded."""
    cell = cell or {}
    if cell.get("gate"):
        return f"gated: {escaping.md_cell(cell.get('gate_text') or cell['gate'])}"
    return _score(cell.get("score"))


def _code(value: Any) -> str:
    return f"`{escaping.md_code(value)}`"


def _cell_code(value: Any) -> str:
    """An id in a code span inside a table cell: a pipe is escaped for the table."""
    return f"`{escaping.md_cell(escaping.md_code(value))}`"


def _absences(detail: dict[str, Any]) -> list[str]:
    """One line per routing record the job's saved artifacts do not hold."""
    out = []
    if not detail.get("effective_version"):
        out.append(
            "No assignment is saved next to this report, so there is no per-query or "
            "per-table routing detail to show."
        )
        return out
    if not detail.get("has_ledger"):
        out.append(
            "No move ledger: this job's assignments were saved before each routing step "
            "was recorded, so no query has a move history, scores per engine or a runner-up."
        )
    if not detail.get("has_access_record"):
        out.append(
            "No access-pattern record for this assignment version: classes, pattern scores, "
            "anti-pattern findings and key candidates are not shown."
        )
    if not detail.get("has_analysis"):
        out.append(
            "No engine analysis is saved with this job: the complexity and performance "
            "dimensions are not shown."
        )
    if not detail.get("has_carried_writes"):
        out.append(
            "This report was synthesized before the poorly fitting write-group members were "
            "recorded: a member is marked when its own routed score is below the floor, "
            "with no target-native guidance."
        )
    return out


def _how_to_read(detail: dict[str, Any]) -> list[str]:
    floor = detail.get("poor_fit_floor")
    lines = [
        "## How to read the scores",
        "",
        "- **Routed score**: the score the routing step compared for the query on each "
        "engine (0 to 100), from the latest step of its move ledger that recorded a "
        "candidate ladder; `gated: <reason>` means that engine could not take the query, "
        "for that reason.",
        "- **Pattern**: the score of the query's access-pattern class on the engine, as "
        "written; **after redesign** is the class score once the keys the redesign assumes "
        "exist, which routing uses when it is higher.",
        "- **Complexity** and **performance**: each of the query's tables as the engine's "
        "analysis rated it.",
        "- **Cost**: the query's own cost on each engine is part of the routed score; it is "
        "not saved on its own, so it is not shown separately.",
        "- **Penalty**: the points routing took off for the query's intrinsic anti-pattern "
        "findings on the engine: the biggest one applies, not their sum; a fixable finding "
        "costs nothing, since a redesign removes it.",
    ]
    if isinstance(floor, (int, float)):
        lines.append(
            f"- **Fits poorly**: a write-group member its engine serves poorly, as the risk "
            f"register counts them: its own score on its table's writer is below "
            f"{fmt_num(floor, 1)}, the floor the write-intent catalog sets. A co-dependency "
            "group marks the same statements."
        )
    return lines + [""]


def _tables_md(detail: dict[str, Any], engines: list[str]) -> list[str]:
    tables = detail.get("tables") or []
    out = [f"## Tables the queries touch ({fmt_num(detail.get('table_count') or 0)})", ""]
    if not tables:
        return out + ["No table is recorded in this assignment.", ""]
    if not detail.get("has_owner_lines"):
        out += [
            "This report maps no table to a target (no schema design ran), so it states no "
            "table owner; the queries and their scores follow.",
            "",
        ]
    header = "| Query | Engine | Class | " + " | ".join(_engine(e) for e in engines) + " |"
    rule = "|---|---|---|" + "---|" * len(engines)
    for t in tables:
        n = t.get("query_count") or 0
        out += [f"### {_code(t['table'])}", ""]
        if detail.get("has_owner_lines"):
            owner = f"Owner: {_engine_text(t.get('owner'))}"
            if t.get("owner_reason"):
                owner += f". {escaping.md_text(t['owner_reason'])}"
            else:
                owner += "."
            readers = ", ".join(_engine_text(e) for e in t.get("also_read_by") or []) or "none"
            cached = _engine_text(t.get("cached_by")) if t.get("cached_by") else "none"
            out += [owner, "", f"Also read by: {readers}. Cached by: {cached}.", ""]
        out += [
            f"{fmt_num(n)} {plural_noun(n, 'query', 'queries')} touch it:",
            "",
            header,
            rule,
        ]
        for q in t.get("queries") or []:
            cells = {c.get("engine"): c for c in q.get("scores") or []}
            out.append(
                f"| {_cell_code(q['query_id'])} | {_engine(q.get('engine'))} | "
                f"{_cell_code(q['access_class']) if q.get('access_class') else ABSENT} | "
                + " | ".join(_cell(cells.get(e)) for e in engines)
                + " |"
            )
        out.append("")
    return out


def _dimension_table(q: dict[str, Any]) -> list[str]:
    rows = q.get("dimensions") or []
    if not rows:
        return []
    out = [
        "| Engine | Routed score | Pattern | After redesign | Penalty | Complexity / performance by table |",
        "|---|---|---|---|---|---|",
    ]
    for d in rows:
        tables = "; ".join(
            f"{_cell_code(t['table'])} {_score(t.get('complexity'))} / {_score(t.get('performance'))}"
            for t in d.get("tables") or []
        )
        findings = [_cell_code(f) for f in d.get("intrinsic_findings") or [] if f]
        penalty = _score(d.get("intrinsic_penalty")) if findings else "0"
        if findings:
            penalty += f" ({', '.join(findings)})"
        fixable = [_cell_code(f) for f in d.get("fixable_findings") or [] if f]
        if fixable:
            penalty += f"; fixable: {', '.join(fixable)}"
        pattern = _score(d.get("pattern_score"))
        if d.get("parity_anchor"):
            pattern += " (parity anchor)"
        out.append(
            f"| {_engine(d['engine'])} | {_cell(d.get('routed'))} | {pattern} | "
            f"{_score(d.get('redesigned_pattern_score'))} | {penalty} | {tables or '—'} |"
        )
    return out + [""]


def _query_md(q: dict[str, Any]) -> list[str]:
    out = [f"### Query {_code(q['query_id'])}", ""]
    if q.get("excerpt"):
        out += [_code(q["excerpt"]), ""]
    tables = ", ".join(_code(t) for t in q.get("tables") or []) or "none"
    engine = f"- Engine: {_engine_text(q.get('engine'))}"
    if q.get("final_score") is not None:
        engine += f", routed score {_score(q['final_score'])}"
    if q.get("pinned"):
        engine += f", pinned ({escaping.md_text(q['pinned'])})"
    if not q.get("in_scope", True):
        engine += ", out of scope"
    out += [f"- Tables: {tables}", engine]
    if q.get("access_class"):
        line = f"- Access-pattern class: {_code(q['access_class'])}"
        if q.get("access_reason"):
            line += f" ({escaping.md_text(q['access_reason'])})"
        out.append(line)
    if q.get("redesign_class"):
        line = f"- After redesign: {_code(q['redesign_class'])}"
        if q.get("redesign_reason"):
            line += f" ({escaping.md_text(q['redesign_reason'])})"
        if q.get("assumed_keys"):
            line += ", assuming the keys " + ", ".join(_code(k) for k in q["assumed_keys"])
        out.append(line)
    if q.get("summary"):
        out.append(f"- Why: {escaping.md_text(q['summary'])}")
    if q.get("tied_engines"):
        out.append("- Open tie between " + ", ".join(_engine_text(e) for e in q["tied_engines"]))
    runner = q.get("runner_up")
    if runner and runner not in (q.get("summary") or ""):
        out.append(f"- Runner-up: {escaping.md_text(runner)}")
    if q.get("close_call"):
        out.append(
            f"- Close call: the runner-up is less than {fmt_num(CLOSE_CALL_MARGIN, 1)} points behind"
        )
    if q.get("settled_tie"):
        out.append("- Settled tie: the runner-up scored the same, and a routing rule decided")
    for o in q.get("outscored_by") or []:
        out.append(f"- Scored higher: {escaping.md_text(o)}")
    out.append("")
    out += _dimension_table(q)
    steps = q.get("steps") or []
    if steps:
        out += ["Move history:", ""]
        for s in steps:
            moved = f"{_engine_text(s.get('from_engine'))} to " if s.get("from_engine") else ""
            out.append(
                f"{s.get('step')}. v{s.get('version')} move {s.get('seq')}, "
                f"{escaping.md_text(s.get('rule') or '')} ({escaping.md_text(s.get('actor') or '')}): "
                f"{moved}{_engine_text(s.get('to_engine'))}. {escaping.md_text(s.get('explanation') or '')}"
            )
        out.append("")
    keys = q.get("key_candidates") or []
    if keys:
        out += ["Key candidates:", ""]
        out += [
            f"- {_code(k['column'])}: "
            f"{escaping.md_text(k.get('explanation') or k.get('outcome') or '')}"
            for k in keys
        ]
        out.append("")
    return out


def _queries_md(detail: dict[str, Any]) -> list[str]:
    queries = detail.get("queries") or []
    out = [f"## Queries ({fmt_num(detail.get('query_count') or 0)})", ""]
    if not queries:
        return out + ["No query is recorded in this assignment.", ""]
    moved = detail.get("moved_query_count") or 0
    close = detail.get("close_call_count") or 0
    settled = detail.get("settled_tie_count") or 0
    tied = detail.get("tied_query_count") or 0
    out += [
        f"{fmt_num(moved)} {plural_noun(moved, 'query', 'queries')} changed engine after the "
        f"first assignment, {fmt_num(close)} {plural_noun(close, 'is a close call', 'are close calls')}, "
        f"{fmt_num(settled)} {plural_noun(settled, 'is an exact tie', 'are exact ties')} a routing "
        f"rule settled and {fmt_num(tied)} {plural_noun(tied, 'is', 'are')} still tied. Queries "
        "are in query id order.",
        "",
    ]
    for q in queries:
        out += _query_md(q)
    return out


def _group_md(g: dict[str, Any]) -> list[str]:
    title = _code(g["name"]) if g.get("kind") == "write" else escaping.md_text(g["name"])
    n = g.get("member_count") or 0
    engines = ", ".join(_engine_text(e) for e in g.get("engines") or []) or "no engine"
    out = [
        f"### {'Write group ' if g.get('kind') == 'write' else ''}{title}",
        "",
        f"- Members: {fmt_num(n)} {plural_noun(n, 'statement')}, on {engines}",
    ]
    if g.get("placed_version"):
        placed = (
            f"- Placed by v{g['placed_version']} move {g.get('placed_seq')}, "
            f"{escaping.md_text(rule_label(str(g.get('placed_rule') or '')))}"
        )
        unit = g.get("placement_member_count")
        if g.get("placement_group_key") and unit:
            placed += (
                f", as one unit of {fmt_num(unit)} {plural_noun(unit, 'statement')} "
                f"(placement {_code(g['placement_group_key'])})"
            )
        out.append(placed)
        scores = ", ".join(
            f"{_engine_text(e)} {_score(s)}" for e, s in (g.get("group_scores") or {}).items()
        )
        out.append(f"- Group score: {scores or ABSENT}")
        if g.get("margin") is not None:
            out.append(
                f"- Margin: {_engine_text(g.get('winner'))} by {_score(g['margin'])} over "
                f"{_engine_text(g.get('runner_up'))}"
            )
        elif g.get("winner"):
            out.append("- Margin: no other engine could take the whole group")
    else:
        out.append("- Placed by: no group placement is recorded in the move ledger")
    poor = g.get("poor_fit_count") or 0
    guidance = g.get("guidance_recorded")
    # A co-dependency group marks poor fits only from the carried-write record.
    marked = guidance or g.get("kind") == "write"
    if marked:
        out.append(f"- Fits poorly: {fmt_num(poor)} {plural_noun(poor, 'member')}")
    out.append("")
    header = "| Query | Engine | Own score |" + (" Fits poorly |" if marked else "")
    rule = "|---|---|---|" + ("---|" if marked else "")
    if guidance:
        header += " Intent | Target-native approach |"
        rule += "---|---|"
    out += [header, rule]
    for m in g.get("members") or []:
        row = (
            f"| {_cell_code(m['query_id'])} | {_engine(m.get('engine'))} | "
            f"{_score(m.get('own_score'))} |"
            + (f" {'yes' if m.get('poor_fit') else 'no'} |" if marked else "")
        )
        if guidance:
            row += (
                f" {escaping.md_cell(m.get('intent_name') or '—')} | "
                f"{escaping.md_cell(m.get('approach') or '—')} |"
            )
        out.append(row)
    return out + [""]


def _groups_md(detail: dict[str, Any]) -> list[str]:
    n = detail.get("write_group_count") or 0
    single = detail.get("single_statement_write_group_count") or 0
    out = [
        f"## Write groups ({fmt_num(n)})",
        "",
        "A table has one writer: every statement that writes it is placed with the others "
        "as one unit, with any co-dependency group it touches. "
        f"{fmt_num(single)} more {plural_noun(single, 'table has', 'tables have')} a single "
        "write statement and no group to place.",
        "",
    ]
    for g in detail.get("write_groups") or []:
        out += _group_md(g)
    c = detail.get("co_dependency_group_count") or 0
    out += [
        f"## Co-dependency groups ({fmt_num(c)})",
        "",
        "Queries that must stay on one engine together, placed on the group's mean score.",
        "",
    ]
    if not detail.get("co_dependency_groups"):
        out += ["None in this assignment.", ""]
    for g in detail.get("co_dependency_groups") or []:
        out += _group_md(g)
    return out


def render_detailed_engineering_report_md(
    report: dict[str, Any],
    detail: dict[str, Any],
    prov: dict[str, str] | None = None,
) -> str:
    """The detailed engineering report (Markdown) from the routing detail
    (``routing_detail_facts``) of ``report``'s job."""
    db = report.get("database_name", "?")
    out: list[str] = []
    if prov:
        out += ["---"]
        out += [f'{k}: "{escaping.md_yaml_value(v)}"' for k, v in prov.items() if v]
        out += ["---", ""]
    version = detail.get("effective_version")
    out += [
        "# Database Modernization: Detailed Engineering Report",
        "",
        f"Source database: {_code(db)}. The routing record behind the engineering report "
        "(`engineering-report.md`), for an engineer who audits or debugs the result: why "
        "each query is on its engine, step by step. It is read from the same saved "
        "assessment as the other files; nothing here is scored again.",
        "",
        f"Assignment version: v{version}." if version else "Assignment version: none saved.",
        "",
    ]
    if detail.get("engine_scope"):
        out += [engine_scope_statement(detail["engine_scope"], escaping.md_text) + ".", ""]
    absent = _absences(detail)
    if absent:
        out += ["## Records not available", ""] + [f"- {a}" for a in absent] + [""]
    if not version:
        # #1166: the code commit of the render, the file's last line.
        return "\n".join(out + commit_footer(prov))
    engines = [str(e) for e in detail.get("analysed_engines") or []]
    out += [
        "Analysed engines: " + (", ".join(_engine_text(e) for e in engines) or "none") + ".",
        "",
        "## Contents",
        "",
        "- Tables: each table's owner and every query that touches it",
        "- Queries: each query's scores by dimension, move history, ties and key candidates",
        "- Write groups and co-dependency groups: members, group score, margin and poor fits",
        "",
    ]
    out += _how_to_read(detail)
    out += _tables_md(detail, engines)
    out += _queries_md(detail)
    out += _groups_md(detail)
    return "\n".join(out + commit_footer(prov))
