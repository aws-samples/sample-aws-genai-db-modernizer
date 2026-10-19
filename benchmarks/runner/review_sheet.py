"""Blind review sheets for a final-assignment answer key (#1074, step 2).

A reviewer writes the key for a whole sample from one Markdown sheet per
sample: one row per query with the facts routing reads (access-pattern class
and its reason, tables, read or write, call rate, co-dependency and write
groups), and empty columns for the acceptable set, the ideal engine and the
rationale. The sheet never shows the engine the pipeline picked or any score
(``class_scores``, confidence, fit), so the key is not anchored on today's
output -- the bias the benchmark exists to catch (ADR-030).

Rows are grouped so review is fast: a co-dependency group (its members move as
one) gets its own section, every other query sits under its table (writes,
the table's write group, first), and sections run busiest first. Each row also
carries a tier from its share of the sample's calls, and a "same shape as row
N" pointer when an earlier row has the same access path, so the reviewer can
answer ``=N`` instead of re-reading it.

    uv run python -m benchmarks.runner.review_sheet --sample discourse

writes ``benchmarks/cases/final_assignment/review/discourse.md``. It runs the
deterministic pipeline (no model, no AWS call) to read the collector output,
the access-pattern record and the placement groups, and reads nothing else.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.report.escaping import md_cell
from src.shared.engine_names import ENGINE_DISPLAY_NAMES, PLACEHOLDER_ENGINES

STAGE = "final_assignment"
REVIEW_DIR = Path(__file__).resolve().parents[1] / "cases" / STAGE / "review"

SQL_CHARS = 140
REASON_CHARS = 170
# Tiers by cumulative share of the sample's calls, busiest query first.
TIER_A_SHARE = 0.90
TIER_B_SHARE = 0.99
ROWS_MARKER = "<!-- rows -->"
UTILITY = "utility"
# The collector's name for a table reference it could not resolve.
UNKNOWN_TABLE = "unknown"


@dataclass
class Row:
    query_id: str
    sql: str
    op: str
    tables: list[str]
    calls: float
    share: float = 0.0
    cls: str = ""
    reason: str = ""
    facts: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    co_dep: str | None = None
    writes: list[str] = field(default_factory=list)
    tier: str = "C"
    number: int = 0
    shape_of: int | None = None


@dataclass
class Section:
    title: str
    kind: str  # "co_dependency" | "table" | "none" | "utility"
    rows: list[Row]
    table: str | None = None

    @property
    def calls(self) -> float:
        return sum(r.calls for r in self.rows)


def _bare(table: str) -> str:
    return table.split(".", 1)[1] if "." in table else table


def _table_title(table: str, bare_counts: dict[str, set[str]]) -> str:
    return table if len(bare_counts.get(_bare(table), ())) > 1 else _bare(table)


def _calls(q: dict) -> float:
    v = q.get("calls_per_second")
    return float(v) if isinstance(v, (int, float)) else 0.0


def _flags(q: dict) -> list[str]:
    out = []
    if q.get("has_joins"):
        out.append(f"joins={q.get('join_count') or '?'}")
    if q.get("has_aggregations"):
        out.append("agg")
    if q.get("has_subqueries"):
        out.append("subquery")
    if q.get("has_text_search"):
        out.append(f"text:{q.get('text_search_type') or 'yes'}")
    rows = q.get("rows_returned_avg")
    if isinstance(rows, (int, float)) and q.get("query_type") == "SELECT":
        out.append(f"rows~{rows:.3g}")
    p50 = q.get("execution_time_ms_p50") or q.get("execution_time_ms_avg")
    if isinstance(p50, (int, float)):
        out.append(f"{p50:.3g}ms")
    return out


def _short_ids(ids: list[str]) -> dict[str, str]:
    """A unique display id per query: the full id when short, else a unique prefix."""
    out: dict[str, str] = {}
    long_ids = [i for i in ids if len(i) > 20]
    width = 12
    while long_ids:
        prefixes = defaultdict(list)
        for i in long_ids:
            prefixes[i[:width]].append(i)
        if all(len(v) == 1 for v in prefixes.values()):
            break
        width += 4
    for i in ids:
        out[i] = i if len(i) <= 20 else i[:width]
    return out


def build_rows(collector: dict, access_patterns: dict, assignment: dict) -> list[Section]:
    """The sheet's sections, from engine-free facts only.

    Reads ``collector`` (query text, type, tables, call rate, flags),
    ``access_patterns`` (each query's ``class``, ``reason``, ``facts``) and the
    assignment's ``co_dependency_groups`` and ``write_groups`` (query ids and
    tables). Never reads an assigned engine, a score or a reason that names one.
    """
    queries = collector.get("queries", {}).get("query_patterns", [])
    ap = {q["query_id"]: q for q in access_patterns.get("queries", [])}
    total = sum(_calls(q) for q in queries) or 1.0

    co_dep_of: dict[str, str] = {}
    for n, group in enumerate(assignment.get("co_dependency_groups") or [], start=1):
        for qid in group:
            co_dep_of[qid] = f"CD{n}"
    writes_of: dict[str, list[str]] = defaultdict(list)
    for table, members in (assignment.get("write_groups") or {}).items():
        for qid in members:
            writes_of[qid].append(table)

    rows: list[Row] = []
    for q in queries:
        qid = q["query_id"]
        a = ap.get(qid, {})
        rows.append(
            Row(
                query_id=qid,
                sql=q.get("query_text") or "",
                op=q.get("query_type") or "",
                tables=list(q.get("tables_accessed") or a.get("tables") or []),
                calls=_calls(q),
                share=_calls(q) / total,
                cls=a.get("class", ""),
                reason=a.get("reason", ""),
                facts=list(a.get("facts") or []),
                flags=_flags(q),
                co_dep=co_dep_of.get(qid),
                writes=sorted(writes_of.get(qid, [])),
            )
        )

    # Tier by cumulative call share, busiest first (stable on query order). A
    # utility statement (BEGIN, SET, SHOW, ...) reads no table data and routing
    # pins it, so it takes no tier and does not count toward the shares.
    routed = [r for r in rows if r.cls != UTILITY]
    routed_total = sum(r.calls for r in routed) or 1.0
    cumulative = 0.0
    for r in sorted(routed, key=lambda r: -r.calls):
        r.tier = "A" if cumulative < TIER_A_SHARE else "B" if cumulative < TIER_B_SHARE else "C"
        if r.calls == 0:
            r.tier = "C"
        cumulative += r.calls / routed_total
    for r in rows:
        if r.cls == UTILITY:
            r.tier = "-"

    # A bare name two schemas share is titled with its schema, so the sections differ.
    bare_counts: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        for t in r.tables + r.writes:
            bare_counts[_bare(t)].add(t)
    sections: dict[tuple[str, str], Section] = {}
    for r in rows:
        known = [t for t in r.tables if _bare(t) != UNKNOWN_TABLE]
        if r.cls == UTILITY and not r.co_dep and not r.writes:
            key = ("utility", "")
            title = "Utility statements (no table data)"
        elif r.co_dep:
            key = ("co_dependency", r.co_dep)
            title = f"Co-dependency group {r.co_dep}"
        elif r.writes or known:
            table = r.writes[0] if r.writes else known[0]
            key = ("table", table)
            title = f"Table {_table_title(table, bare_counts)}"
        else:
            key = ("none", "")
            title = "Queries with no resolved table"
        if key not in sections:
            sections[key] = Section(title, key[0], [], table=key[1] if key[0] == "table" else None)
        sections[key].rows.append(r)

    # Busiest first; the utility statements last, whatever their calls.
    ordered = sorted(sections.values(), key=lambda s: (s.kind == "utility", -s.calls, s.title))
    for s in ordered:
        s.rows.sort(key=lambda r: (not r.writes, -r.calls))

    shapes: dict[tuple, int] = {}
    number = 0
    for s in ordered:
        for r in s.rows:
            number += 1
            r.number = number
            shape = (
                r.cls,
                r.reason,
                r.op,
                tuple(sorted(r.tables)),
                tuple(r.facts),
                "agg" in r.flags,
                "subquery" in r.flags,
            )
            if shape in shapes:
                r.shape_of = shapes[shape]
            else:
                shapes[shape] = number
    return ordered


def _sql(text: str) -> str:
    flat = " ".join(text.split()).replace("`", "")
    if len(flat) > SQL_CHARS:
        flat = flat[: SQL_CHARS - 3].rstrip() + "..."
    return f"`{md_cell(flat)}`"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _pct(x: float) -> str:
    return "0" if x == 0 else f"{x * 100:.2f}%" if x >= 0.0001 else "<0.01%"


HEADER = (
    "| # | id | SQL | op | tables | calls/s | share | tier | groups | class | access path "
    "| facts | shape | acceptable | ideal | rationale |"
)
DIVIDER = "|" + "---|" * 16


def render(sample: str, sections: list[Section], table_facts: dict[str, dict]) -> str:
    rows = [r for s in sections for r in s.rows]
    ids = _short_ids([r.query_id for r in rows])
    tiers = {t: [r for r in rows if r.tier == t] for t in "ABC"}
    shapes = sum(1 for r in rows if r.shape_of is None)
    writers: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        for t in r.writes:
            writers[t].append(r.number)
    engines = ", ".join(
        f"`{e}`" for e in ENGINE_DISPLAY_NAMES if e not in PLACEHOLDER_ENGINES and e != "aurora"
    )

    out: list[str] = []
    out.append(f"# Answer-key review sheet: {sample}")
    out.append("")
    out.append(
        f"Blind review sheet for the `{STAGE}` benchmark key of the `{sample}` sample "
        f"(`docs/examples/{sample}/{sample}.zip`, #1074). It lists {len(rows)} queries in "
        f"{len(sections)} sections, {shapes} distinct shapes. It shows the facts routing "
        "reads and never the engine the pipeline picked or any score, so the key is not "
        "anchored on today's output. Generated by "
        f"`uv run python -m benchmarks.runner.review_sheet --sample {sample}`; do not edit "
        "the facts columns by hand."
    )
    out.append("")
    out.append("## How to fill it in")
    out.append("")
    out.append(
        "- `acceptable`: every engine that would be a defensible home for the query, comma "
        f"separated, using these ids: {engines}."
    )
    out.append(
        "- `ideal`: the one best engine, only where one is clearly best (optional). It must be "
        "in `acceptable`."
    )
    out.append("- `rationale`: a few words a later reviewer can check against the facts.")
    out.append(
        "- `=N`: same answer as row N (acceptable, ideal and rationale). The `shape` column "
        "suggests N where an earlier row has the same access path, tables and operation."
    )
    out.append(
        "- A section's **Default** line: fill it in and leave a row empty to inherit it; a "
        "filled row overrides it."
    )
    out.append("- `?`: unsure, discuss before the key is written. `-`: leave out of the key.")
    out.append(
        "- A co-dependency group's members always land on one engine, and a table's writes "
        "(its write group, the `W:` marks) share one engine: answer them consistently. Reads "
        "may sit elsewhere as replicas."
    )
    out.append(
        "- When the sheet is done, the key (`expected.json`) is written from it with the "
        "maintainer and set `reviewed: true`, with the case's floor added to `gate.json`."
    )
    out.append("")
    out.append("Column meanings:")
    out.append("")
    out.append(
        "- `id`: the query id (a unique prefix when the id is long). `op`: the statement type. "
        "`calls/s` and `share`: its call rate and share of all the sample's calls."
    )
    out.append(
        f"- `tier`: **A** = the busiest queries that together make {TIER_A_SHARE:.0%} of calls, "
        f"**B** = the next ones up to {TIER_B_SHARE:.0%}, **C** = the rest (and every query "
        "with no recorded calls). Shares for tiers leave out utility statements (`-`): "
        "`BEGIN`, `SET`, `SHOW` and the like read no table data, so they sit in one section "
        "at the end, where one Default line can answer them all."
    )
    out.append(
        "- `groups`: `CDn` = member of co-dependency group n; `W:table` = writes that table "
        "(member of its write group)."
    )
    out.append(
        "- `class` and `access path`: the query's access-pattern class and the reason it was "
        "classed so (ADR-031 Decision 14). `facts`: pattern facts (write, upsert, "
        "partition_range, ...) and collector flags (joins, aggregation, subquery, text "
        "search, average rows returned, median latency)."
    )
    out.append("")
    out.append("## Proposed review order")
    out.append("")
    out.append(
        f"1. Tier A rows ({len(tiers['A'])} queries, {TIER_A_SHARE:.0%} of calls): read each one."
    )
    out.append(
        f"2. Tier B rows ({len(tiers['B'])} queries): answer with `=N` where the `shape` column "
        "offers one, otherwise read the row."
    )
    out.append(
        f"3. Tier C rows ({len(tiers['C'])} queries, under {1 - TIER_B_SHARE:.0%} of calls): set "
        "each section's Default, then only override the rows that differ; `=N` covers most "
        "of the rest."
    )
    out.append(
        "4. Sections run busiest first, so stopping early still covers the traffic that "
        "matters most; an unanswered row stays out of the key."
    )
    utility = sum(1 for r in rows if r.tier == "-")
    if utility:
        out.append(
            f"5. The utility section ({utility} statements) last; a section Default line "
            "answers them all at once."
        )
    out.append("")
    out.append("| section | queries | tier A | tier B | tier C | share of calls |")
    out.append("|---|---|---|---|---|---|")
    for s in sections:
        counts = {t: sum(1 for r in s.rows if r.tier == t) for t in "ABC"}
        share = sum(r.share for r in s.rows)
        out.append(
            f"| {md_cell(s.title)} | {len(s.rows)} | {counts['A']} | {counts['B']} | "
            f"{counts['C']} | {_pct(share)} |"
        )
    out.append("")
    out.append("## Queries")
    out.append("")
    out.append(ROWS_MARKER)
    for s in sections:
        out.append("")
        out.append(f"### {md_cell(s.title)}")
        out.append("")
        tables = sorted({t for r in s.rows for t in r.tables})
        facts = []
        if s.kind == "co_dependency":
            facts.append(
                f"{len(s.rows)} queries that move as one unit, over "
                f"{len(tables)} tables: {md_cell(', '.join(_bare(t) for t in tables[:12]))}"
                + (" ..." if len(tables) > 12 else "")
            )
        if s.table:
            tf = table_facts.get(s.table, {})
            if tf.get("row_count") is not None:
                facts.append(f"rows {tf['row_count']:,}")
            if tf.get("size_mb") is not None:
                facts.append(f"size {tf['size_mb']:.3g} MB")
            w = writers.get(s.table)
            facts.append(
                f"write group: rows {', '.join(map(str, w))}" if w else "no writes collected"
            )
        facts.append(f"{_pct(sum(r.share for r in s.rows))} of calls")
        out.append("; ".join(facts) + ".")
        out.append("")
        out.append("Default: acceptable = ; ideal = ; rationale =")
        out.append("")
        out.append(HEADER)
        out.append(DIVIDER)
        for r in s.rows:
            groups = [r.co_dep] if r.co_dep else []
            groups += [f"W:{_bare(t)}" for t in r.writes]
            same = r.shape_of is not None
            out.append(
                "| "
                + " | ".join(
                    [
                        str(r.number),
                        f"`{md_cell(ids[r.query_id])}`",
                        _sql(r.sql),
                        md_cell(r.op),
                        md_cell(", ".join(_bare(t) for t in r.tables)),
                        f"{r.calls:.3g}",
                        _pct(r.share),
                        r.tier,
                        md_cell(" ".join(g for g in groups if g)),
                        md_cell(r.cls),
                        (
                            "as row " + str(r.shape_of)
                            if same
                            else md_cell(_clip(r.reason, REASON_CHARS))
                        ),
                        md_cell(", ".join(r.facts + r.flags)),
                        f"={r.shape_of}" if same else "",
                        "",
                        "",
                        "",
                    ]
                )
                + " |"
            )
    out.append("")
    return "\n".join(out)


def generate(sample: str, work_dir: Path | None = None) -> str:
    """Run the sample's deterministic pipeline and render its blind review sheet."""
    from benchmarks.runner.loader import Case
    from benchmarks.runner.stage_final_assignment import run_pipeline_to_final_assignment

    work_dir = work_dir or Path(tempfile.mkdtemp(prefix=f"review_{sample}_"))
    case = Case(sample, work_dir, "", [], False, {}, {}, {}, {}, sample=sample)
    with contextlib.redirect_stdout(sys.stderr):
        store, prefix = run_pipeline_to_final_assignment(case, work_dir)
    collector = store.read_json(f"{prefix}/collector/output.json")
    # The first pass's record: the structural facts (classes, groups) are fixed
    # before any engine is chosen; nothing engine- or score-shaped is read from it.
    access = store.read_json(f"{prefix}/assignment/v1/access_patterns.json")
    v1 = store.read_json(f"{prefix}/assignment/v1/assignment.json")
    structure: dict[str, Any] = {
        "co_dependency_groups": v1.get("co_dependency_groups") or [],
        "write_groups": v1.get("write_groups") or {},
    }
    table_facts = {
        t.get("table_id"): {"row_count": t.get("row_count"), "size_mb": t.get("size_mb")}
        for t in collector.get("database_schema", {}).get("tables", [])
    }
    return render(sample, build_rows(collector, access, structure), table_facts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a blind answer-key review sheet")
    parser.add_argument("--sample", action="append", required=True, help="Bundled sample name")
    parser.add_argument("--out-dir", default=str(REVIEW_DIR))
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for sample in args.sample:
        path = out_dir / f"{sample}.md"
        path.write_text(generate(sample))
        print(f'{{"sample": "{sample}", "sheet": "{path}"}}')


if __name__ == "__main__":
    main()
