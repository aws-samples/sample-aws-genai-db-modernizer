"""Key figures of every deliverable, read back from what each one renders (#573).

AGENTS.md "Deliverables agree": the engineering report and the executive report
render one synthesis ``report.json`` (the decision and interactive analysis
HTML reports were retired in #987), so a figure shown in both must be the
same figure.
This module reads the key figures back out of each rendered deliverable and
computes what ``report.json`` says they should be, so
``test_deliverable_figures.py`` can compare them one by one.

Extraction reads fixed labels and table shapes, never free prose: the
Markdown's tables and labels ("Projected monthly (target):", "N tables
migrate", "Kept in sync from X: N tables", ...) and the same labels in the
executive report's tiles and tables ("Projected monthly cost", "Removed by the
reality check", "Open risks", ...).

A figure is keyed by a short string (``"cost:dynamodb"``, ``"wave3:moving"``,
``"risks:HIGH"``...). Each deliverable records every observation of a key it
shows, with where it was seen, so one key shown twice in one deliverable is
checked twice.
"""

from __future__ import annotations

import html as html_lib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from src.shared.engine_names import ENGINE_DISPLAY_NAMES

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")

# Recorded when a hook or label is there but its figure is not, so the
# comparison reports "is '<unreadable>'" instead of passing silently.
UNREADABLE = "<unreadable>"

# Display name or raw id -> engine id. Longest first, so "Aurora PostgreSQL"
# wins over "Aurora" in an alternation.
_NAME_TO_ENGINE: dict[str, str] = {}
for _eid, _name in ENGINE_DISPLAY_NAMES.items():
    _NAME_TO_ENGINE[_name] = _eid
    _NAME_TO_ENGINE[_eid] = _eid
_NAMES_RE = "|".join(re.escape(n) for n in sorted(_NAME_TO_ENGINE, key=len, reverse=True))


_REMOVES_RE = re.compile(r"Removes \$([\d,]+\.\d\d)/mo")


def removal_savings(text: str) -> frozenset[float]:
    """Every "Removes $X/mo" saving a deliverable's text states (#793)."""
    return frozenset(_usd(m) for m in _REMOVES_RE.findall(text))


def engine_id(name: str) -> str:
    """Engine id for a display name or raw id; the input unchanged if unknown."""
    return _NAME_TO_ENGINE.get(name.strip(), name.strip())


def engines_in(text: str) -> set[str]:
    """Every engine named (by display name or raw id) in ``text``."""
    return {engine_id(m) for m in re.findall(rf"(?<![\w-])(?:{_NAMES_RE})(?![\w-])", text)}


def norm(text: str) -> str:
    """Collapse whitespace runs: the PDF text layer and deck shapes break lines
    differently, the figures do not."""
    return " ".join(text.split())


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def _usd(s: str) -> float:
    return round(float(s.replace("$", "").replace(",", "")), 2)


@dataclass
class Figures:
    """The figures one deliverable shows. ``values[key]`` is every
    ``(value, where)`` observation of ``key``; ``engines`` maps every engine
    the deliverable names in a structural slot (an engine row, a cost card, a
    wave's engines, a risk's engine, a tied query's engines) to where."""

    deliverable: str
    values: dict[str, list[tuple[Any, str]]] = field(default_factory=dict)
    engines: dict[str, set[str]] = field(default_factory=dict)

    def add(self, key: str, value: Any, where: str) -> None:
        self.values.setdefault(key, []).append((value, where))

    def name(self, engines: set[str] | list[str] | str, where: str) -> None:
        for e in [engines] if isinstance(engines, str) else engines:
            self.engines.setdefault(e, set()).add(where)


# --------------------------------------------------------------------------
# Expected figures, straight from report.json
# --------------------------------------------------------------------------


def final_architecture(report: dict[str, Any]) -> set[str]:
    """Engines in the final architecture: every engine that owns queries after
    the reality check, plus the cache layer (it owns none, #296)."""
    after = (report.get("reality_check") or {}).get("after_distribution") or {}
    engines = {e for e, n in after.items() if n}
    cache = (report.get("cache_overlay") or {}).get("engine")
    if cache:
        engines.add(cache)
    return engines


def removed_engines(report: dict[str, Any]) -> set[str]:
    """Engines the reality check removed: they owned queries before it, or a
    full consolidation moved them out, and none of them is in the final
    architecture."""
    rc = report.get("reality_check") or {}
    before = {e for e, n in (rc.get("before_distribution") or {}).items() if n}
    consolidated = {
        c.get("from_engine")
        for c in rc.get("consolidations") or []
        if isinstance(c, dict) and c.get("action") == "full"
    }
    return {e for e in before | consolidated if e} - final_architecture(report)


def migrating(wave: dict[str, Any]) -> bool:
    """A wave that moves tables off Aurora to a purpose-built engine. Wave 1
    (``homogeneity`` set) carries the whole schema to Aurora and is not one."""
    return bool(wave.get("moves_from")) and wave.get("homogeneity") is None


def wave_synced_tables(wave: dict[str, Any]) -> set[str]:
    """Tables a wave keeps in sync from an owner outside it. In the merged
    DynamoDB/DocumentDB wave, a table one engine serves from the other moves
    with its owner (#581): it is not kept in sync from anyone outside."""
    engines = set(wave.get("engines") or [])
    return {
        str(o.get("table"))
        for o in wave.get("table_owners") or []
        if isinstance(o, dict) and o.get("owner") not in engines
    }


def wave_moving_by_engine(wave: dict[str, Any]) -> dict[str, int]:
    """Tables each engine of a migrating wave owns, i.e. moves."""
    owned = wave.get("owned_counts_by_engine")
    if owned:
        return {e: int(n) for e, n in owned.items()}
    engines = wave.get("engines") or []
    if len(engines) == 1:
        return {engines[0]: int(wave.get("table_count") or 0) - len(wave.get("table_owners") or [])}
    return {}


def wave_served_by_engine(wave: dict[str, Any]) -> dict[str, int]:
    """Tables each engine of a migrating wave serves."""
    served = wave.get("table_counts_by_engine")
    if served:
        return {e: int(n) for e, n in served.items()}
    engines = wave.get("engines") or []
    if len(engines) == 1:
        return {engines[0]: int(wave.get("table_count") or 0)}
    return {}


def expected_figures(report: dict[str, Any]) -> dict[str, Any]:
    """Every key figure, computed from report.json fields only (never through a
    renderer helper, so a renderer bug cannot hide in both sides)."""
    exp: dict[str, Any] = {}
    after = (report.get("reality_check") or {}).get("after_distribution") or {}
    for e, n in after.items():
        if n:
            exp[f"queries:{e}"] = int(n)
    overlay = report.get("cache_overlay") or {}
    if overlay.get("engine"):
        exp[f"cached_reads:{overlay['engine']}"] = int(overlay.get("query_count") or 0)

    tables_migrate = 0
    for w in report.get("migration_waves") or []:
        n = w.get("wave")
        exp[f"wave{n}:queries"] = int(w.get("query_count") or 0)
        if w.get("cutover_query_count") is not None:
            exp[f"wave{n}:cutover_queries"] = int(w["cutover_query_count"])
        served = int(w.get("table_count") or 0)
        if served:
            exp[f"wave{n}:served"] = served
        if w.get("homogeneity") is not None and report.get("source_table_count") is not None:
            # #578: wave 1 carries every analyzed table plus the real views.
            exp[f"wave{n}:tables_only"] = int(report["source_table_count"])
            exp[f"wave{n}:views"] = int(((report.get("facts") or {}).get("source_view_count")) or 0)
        if migrating(w) and served:
            per_engine = wave_moving_by_engine(w)
            # #581: the tables that move are the ones the wave's engines own.
            moving = sum(per_engine.values()) if per_engine else served - len(wave_synced_tables(w))
            exp[f"wave{n}:synced"] = served - moving
            exp[f"wave{n}:moving"] = moving
            tables_migrate += moving
            for e, k in wave_moving_by_engine(w).items():
                exp[f"wave{n}:moving:{e}"] = k
            for e, k in wave_served_by_engine(w).items():
                exp[f"tables:{e}"] = exp.get(f"tables:{e}", 0) + k
    exp["tables_migrate"] = tables_migrate

    tco = report.get("tco_analysis") or {}
    for row in tco.get("cost_breakdown") or []:
        if isinstance(row, dict) and isinstance(row.get("monthly_cost_usd"), (int, float)):
            exp[f"cost:{row['database']}"] = round(float(row["monthly_cost_usd"]), 2)
    if isinstance(tco.get("projected_monthly_cost"), (int, float)):
        exp["cost:total"] = round(float(tco["projected_monthly_cost"]), 2)

    risks = [r for r in (report.get("risk_assessment") or {}).get("risks") or [] if r]
    counts = Counter(str(r.get("severity", "")).upper() for r in risks)
    for sev in SEVERITIES:
        exp[f"risks:{sev}"] = counts.get(sev, 0)
    exp["risks:total"] = len(risks)

    # #793: the removal saving of every retired engine, stated in each deliverable's
    # reality-check lines as "Removes $X/mo ...". #949: only an engine the source
    # runs today (``retired_engine_savings``); a dropped candidate states none.
    savings = frozenset(
        round(float(row["monthly_cost_usd"]), 2)
        for row in tco.get("retired_engine_savings") or []
        if isinstance(row, dict) and isinstance(row.get("monthly_cost_usd"), (int, float))
    )
    if savings:
        exp["removal_savings"] = savings

    exp["tied"] = len([t for t in report.get("tied_queries") or [] if isinstance(t, dict)])
    if report.get("auto_decided_choices") is not None:
        # #1003: the choices the decide-for-me rule settled, from the stored list.
        decided = [d for d in report["auto_decided_choices"] if isinstance(d, dict)]
        exp["auto_decided"] = len(decided)
        exp["auto_decided_queries"] = sum(len(d.get("query_ids") or []) for d in decided)
    if report.get("pending_engine_choices") is not None:
        # #1003: one choice per table, from the stored list (never the facts).
        exp["choices"] = len([c for c in report["pending_engine_choices"] if isinstance(c, dict)])
    # #730: the close calls and the settled ties, counted from query_traces.close_calls.
    digest = report.get("query_traces") or {}
    if isinstance(digest.get("close_calls"), list):
        rows = [r for r in digest["close_calls"] if isinstance(r, dict)]
        exp["close_calls"] = sum(1 for r in rows if not r.get("settled_tie"))
        exp["settled_ties"] = sum(1 for r in rows if r.get("settled_tie"))
    # #729: the queries on a fallback engine, from query_traces.unservable, and
    # #888: the flagged ones no kept engine can take, from flagged_unservable
    # (a query listed in both counts once).
    flagged = [r for r in report.get("flagged_unservable") or [] if isinstance(r, dict)]
    if isinstance(digest.get("unservable"), list) or flagged:
        ids = {
            str(r.get("query_id"))
            for r in [*(digest.get("unservable") or []), *flagged]
            if isinstance(r, dict)
        }
        exp["fallback_queries"] = len(ids)
    exp["engine_count"] = len(final_architecture(report))
    exp["removed"] = frozenset(removed_engines(report))
    return exp


# --------------------------------------------------------------------------
# Invariants: no deliverable may show an impossible figure
# --------------------------------------------------------------------------


def invariant_violations(
    report: dict[str, Any], figs: dict[str, list[tuple[Any, str]]], label: str
) -> list[tuple[str, str]]:
    """``(key, message)`` for every figure in ``figs`` that breaks an invariant:
    a table total above the tables analyzed, a query total above the queries
    analyzed, a wave whose moved and kept-in-sync tables do not add up to the
    tables it serves."""
    tables_analyzed = int(report.get("source_table_count") or 0)
    views = int(((report.get("facts") or {}).get("source_view_count")) or 0)
    whole_schema = {
        f"wave{w.get('wave')}:served"
        for w in report.get("migration_waves") or []
        if isinstance(w, dict) and w.get("homogeneity") is not None
    }
    queries_analyzed = int((report.get("assignment_summary") or {}).get("query_count") or 0)
    out: list[tuple[str, str]] = []
    for key, observations in figs.items():
        is_table = bool(re.match(r"tables|wave\d+:(served|moving|synced)", key))
        is_query = bool(
            re.match(
                r"(queries|cached_reads):|tied$|wave\d+:(queries|cutover_queries|group_queries)$",
                key,
            )
        )
        for value, where in observations:
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            # Wave 1 carries the whole schema: every table and view (#578).
            limit = tables_analyzed + (views if key in whole_schema else 0)
            if is_table and tables_analyzed and value > limit:
                out.append(
                    (
                        key,
                        f"{label} {where}: {key}={value} exceeds the {limit} "
                        "source tables (and views, for wave 1) analyzed",
                    )
                )
            if is_query and queries_analyzed and value > queries_analyzed:
                out.append(
                    (
                        key,
                        f"{label} {where}: {key}={value} exceeds the {queries_analyzed} "
                        "queries analyzed",
                    )
                )
        m = re.match(r"(wave\d+):group_queries$", key)
        if m:
            for grouped, where in observations:
                for total, _ in figs.get(f"{m.group(1)}:queries", []):
                    if grouped > total:
                        out.append(
                            (
                                key,
                                f"{label} {where}: its table groups hold {grouped} queries, "
                                f"more than the wave's {total}",
                            )
                        )
    for key in figs:
        m = re.match(r"(wave\d+):served$", key)
        if not m:
            continue
        w = m.group(1)
        for served, where in figs[key]:
            for moving, _ in figs.get(f"{w}:moving", []):
                for synced, _ in figs.get(f"{w}:synced", []):
                    if moving + synced != served:
                        out.append(
                            (
                                f"{w}:reconcile",
                                f"{label} {where}: {w} moves {moving} and keeps {synced} in "
                                f"sync, which is not the {served} it serves",
                            )
                        )
    return out


def report_invariant_violations(report: dict[str, Any]) -> list[tuple[str, str]]:
    """Invariants on report.json itself: wave counts reconcile with their
    table groups, sync lists and per-engine splits; costs add up; every engine
    it names is in the final architecture."""
    out: list[tuple[str, str]] = []
    final = final_architecture(report)
    queries_analyzed = int((report.get("assignment_summary") or {}).get("query_count") or 0)

    ranking = {str(r.get("target")) for r in report.get("ranking") or [] if isinstance(r, dict)}
    if ranking != final:
        out.append(("ranking", f"ranking engines {sorted(ranking)} != final {sorted(final)}"))
    for r in report.get("ranking") or []:
        after = (report.get("reality_check") or {}).get("after_distribution") or {}
        if r.get("target") in after and r.get("assigned_queries") != after[r["target"]]:
            out.append(
                (
                    f"queries:{r['target']}",
                    f"ranking says {r['target']} owns {r.get('assigned_queries')} queries, "
                    f"after_distribution says {after[r['target']]}",
                )
            )
    owned_total = sum(
        ((report.get("reality_check") or {}).get("after_distribution") or {}).values()
    )
    if queries_analyzed and owned_total != queries_analyzed:
        out.append(
            (
                "queries:total",
                f"owned queries add up to {owned_total}, not the {queries_analyzed} analyzed",
            )
        )

    for w in report.get("migration_waves") or []:
        n = w.get("wave")
        tables = list(w.get("tables") or [])
        if len(set(tables)) != len(tables):
            out.append((f"wave{n}:tables", f"wave {n} lists a table more than once"))
        if w.get("table_count") is not None and int(w["table_count"]) != len(tables):
            out.append(
                (
                    f"wave{n}:served",
                    f"wave {n} table_count {w['table_count']} != {len(tables)} tables listed",
                )
            )
        unknown = set(w.get("engines") or []) - final
        if unknown:
            out.append(
                (
                    f"wave{n}:engines",
                    f"wave {n} names {sorted(unknown)}, not in the final architecture",
                )
            )
        group_tables: list[str] = []
        group_queries = 0
        for g in w.get("table_groups") or []:
            group_tables += list(g.get("tables") or [])
            group_queries += int(g.get("query_count") or 0)
        if len(set(group_tables)) != len(group_tables):
            out.append((f"wave{n}:groups", f"wave {n} puts a table in two table groups"))
        stray = set(group_tables) - set(tables)
        if stray:
            out.append(
                (
                    f"wave{n}:groups",
                    f"wave {n} table groups hold {len(stray)} tables the wave does not list",
                )
            )
        if group_queries > int(w.get("query_count") or 0):
            out.append(
                (
                    f"wave{n}:groups",
                    f"wave {n} table groups hold {group_queries} queries, more than its "
                    f"{w.get('query_count')}",
                )
            )
        if w.get("homogeneity") is not None and report.get("source_table_count") is not None:
            views = int(((report.get("facts") or {}).get("source_view_count")) or 0)
            if int(w.get("table_count") or 0) != int(report["source_table_count"]) + views:
                out.append(
                    (
                        f"wave{n}:served",
                        f"wave {n} carries {w.get('table_count')} tables and views, not the "
                        f"{report['source_table_count']} tables analyzed plus {views} views",
                    )
                )
        owners = w.get("table_owners") or []
        owner_tables = [o.get("table") for o in owners]
        if set(owner_tables) - set(tables):
            out.append((f"wave{n}:synced", f"wave {n} keeps in sync tables it does not serve"))
        # A single-engine wave never lists its own engine as a table's owner;
        # the merged wave may (one engine serves what the other owns, #581).
        if len(w.get("engines") or []) == 1 and {o.get("owner") for o in owners} & set(
            w.get("engines") or []
        ):
            out.append((f"wave{n}:synced", f"wave {n} keeps a table in sync from its own engine"))
        if {o.get("owner") for o in owners} - final:
            out.append(
                (f"wave{n}:synced", f"wave {n} syncs from an engine not in the architecture")
            )
        if migrating(w) and w.get("table_count"):
            synced = wave_synced_tables(w)
            moving = int(w["table_count"]) - len(synced)
            per_engine = wave_moving_by_engine(w)
            if per_engine and sum(per_engine.values()) != moving:
                out.append(
                    (
                        f"wave{n}:moving",
                        f"wave {n} engines move {sum(per_engine.values())} tables between them, "
                        f"but it serves {w['table_count']} and keeps {len(synced)} in sync "
                        f"from outside it ({moving} move)",
                    )
                )
            served = wave_served_by_engine(w)
            for e, k in per_engine.items():
                if k > served.get(e, k):
                    out.append(
                        (f"wave{n}:moving:{e}", f"wave {n}: {e} moves {k} tables, serves fewer")
                    )

    tco = report.get("tco_analysis") or {}
    breakdown = {
        r["database"]: float(r["monthly_cost_usd"])
        for r in tco.get("cost_breakdown") or []
        if isinstance(r, dict) and isinstance(r.get("monthly_cost_usd"), (int, float))
    }
    if set(breakdown) - final:
        out.append(
            (
                "cost",
                f"cost breakdown prices {sorted(set(breakdown) - final)}, not in the architecture",
            )
        )
    if final - set(breakdown):
        out.append(("cost", f"cost breakdown has no price for {sorted(final - set(breakdown))}"))
    projected = tco.get("projected_monthly_cost")
    if isinstance(projected, (int, float)) and abs(sum(breakdown.values()) - projected) > 0.011:
        out.append(
            (
                "cost:total",
                f"per-engine costs add up to {sum(breakdown.values()):.2f}, not {projected}",
            )
        )

    for t in report.get("tied_queries") or []:
        named = set(t.get("tied_engines") or []) | {t.get("assigned_engine")}
        if named - final:
            out.append(("tied", f"tied query {t.get('query_id')} names {sorted(named - final)}"))
    summary_ties = (report.get("assignment_summary") or {}).get("tie_unresolved_count")
    if summary_ties is not None and summary_ties != len(report.get("tied_queries") or []):
        out.append(
            (
                "tied",
                f"tie_unresolved_count {summary_ties} != {len(report.get('tied_queries') or [])} tied queries",
            )
        )
    for r in (report.get("risk_assessment") or {}).get("risks") or []:
        m = re.match(r"\[(\w+)\]", str(r.get("description") or ""))
        if m and m.group(1) in ENGINE_DISPLAY_NAMES and m.group(1) not in final:
            out.append(("risks", f"{r.get('risk_id')} is on {m.group(1)}, not in the architecture"))
    return out


# --------------------------------------------------------------------------
# Labels shared by the readers
# --------------------------------------------------------------------------

_CLOSE_CALLS_TITLE = re.compile(
    r"Close calls \(([\d,]+) quer(?:y|ies)\)(?:, plus ([\d,]+) exact ties? a rule settled)?"
)


def _auto_decided(f: Figures, text: str, where: str) -> None:
    """#1003: "Decided automatically, review if needed (N tables, M queries)"; 0 and 0
    when the deliverable states none."""
    m = re.search(
        r"Decided automatically, review if needed \(([\d,]+) tables?, ([\d,]+) quer(?:y|ies)\)",
        text,
    )
    f.add("auto_decided", _int(m.group(1)) if m else 0, where)
    f.add("auto_decided_queries", _int(m.group(2)) if m else 0, where)


def _close_calls(f: Figures, text: str | None, where: str) -> None:
    """The close-call figures of one "Close calls (N queries), plus T ..." title (#730)."""
    m = _CLOSE_CALLS_TITLE.search(text or "")
    f.add("close_calls", _int(m.group(1)) if m else 0, where)
    f.add("settled_ties", _int(m.group(2)) if m and m.group(2) else 0, where)


_FALLBACK_TITLE = re.compile(
    r"Queries that cannot be served as designed \(([\d,]+) quer(?:y|ies)\)"
)


def _fallback_queries(f: Figures, text: str | None, where: str) -> None:
    """The count of one "Queries that cannot be served as designed (N queries)" title (#729)."""
    m = _FALLBACK_TITLE.search(text or "")
    f.add("fallback_queries", _int(m.group(1)) if m else 0, where)


# --------------------------------------------------------------------------
# The engineering report (Markdown)
# --------------------------------------------------------------------------


def md_plain(text: str) -> str:
    """Markdown source as the reader sees it: no backslash escapes or entities."""
    return html_lib.unescape(re.sub(r"\\([\\`*_{}\[\]()#+\-.!|<>~])", r"\1", text))


def _md_sections(md: str, level: int) -> list[tuple[str, str]]:
    """``(heading, body)`` for each heading of exactly ``level`` hashes."""
    marks = list(re.finditer(rf"^{'#' * level} (.+)$", md, re.M))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(md)
        nxt = re.search(rf"^#{{1,{level}}} ", md[m.end() : end], re.M)
        body_end = m.end() + nxt.start() if nxt else end
        out.append((m.group(1).strip(), md[m.end() : body_end]))
    return out


def _md_table_rows(body: str) -> list[list[str]]:
    rows = []
    for line in body.splitlines():
        if line.startswith("|") and not re.match(r"^\|[-| ]+\|$", line):
            rows.append([c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]])
    return rows[1:]  # drop the header row


def engineering_figures(md_text: str) -> Figures:
    """Figures the engineering report shows."""
    f = Figures("engineering report")
    md = md_text
    plain = md_plain(md)
    h2 = dict(_md_sections(md, 2))

    targets: set[str] = set()
    for row in _md_table_rows(h2.get("Target engines", "")):
        if md_plain(row[0]) == "Total":
            m = re.match(r"^([\d,]+) tables? migrates?$", md_plain(row[3]))
            f.add(
                "tables_migrate",
                _int(m.group(1)) if m else UNREADABLE,
                "Target engines Total",
            )
            continue
        eng = engine_id(md_plain(row[0]))
        targets.add(eng)
        f.name(eng, "Target engines table")
        m = re.match(r"^([\d,]+) cached reads?\b", md_plain(row[2]))
        if m:
            f.add(f"cached_reads:{eng}", _int(m.group(1)), "Target engines Workload")
        m = re.match(r"^([\d,]+) source tables?\b", md_plain(row[3]))
        if m:
            f.add(f"tables:{eng}", _int(m.group(1)), "Target engines Scope")
    f.add("engine_count", len(targets), "Target engines table")

    m = re.search(r"^- Projected monthly \(target\): \$([\d,.]+)$", plain, re.M)
    f.add("cost:total", _usd(m.group(1)) if m else UNREADABLE, "Cost: Projected monthly")
    f.add("removal_savings", removal_savings(plain), "Migration trade-offs")
    removed: set[str] = set()
    m = re.search(r"^- Retired by this plan: (.+)$", plain, re.M)
    if m:
        removed |= {engine_id(x) for x in re.findall(r"/mo \(([^)]+)\)", m.group(1))}

    for heading, body in _md_sections(md, 2):
        m = re.match(r"^Cache layer \((.+)\)$", heading)
        if m:
            k = re.search(r"fronts ([\d,]+) hot reads?", md_plain(body))
            if k:
                f.add(
                    f"cached_reads:{engine_id(m.group(1))}", _int(k.group(1)), "Cache layer section"
                )
        # #1003: the same ties as one choice per table, their queries counted.
        m = re.match(
            r"^Pending engine choices \(([\d,]+) tables?, ([\d,]+) quer(?:y|ies)\)$", heading
        )
        if m:
            f.add("tied", _int(m.group(2)), "Pending engine choices heading")
            rows = _md_table_rows(body)
            f.add("tied", sum(_int(row[1]) for row in rows), "Pending engine choices table")
            f.add("choices", _int(m.group(1)), "Pending engine choices heading")
            f.add("choices", len(rows), "Pending engine choices table")
            for row in rows:
                f.name(engines_in(md_plain(" ".join(row[2:]))), "Pending engine choices table")
        m = re.match(r"^Unresolved engine ties \(([\d,]+) quer(?:y|ies)\)$", heading)
        if m:
            f.add("tied", _int(m.group(1)), "Unresolved engine ties heading")
            rows = _md_table_rows(body)
            f.add("tied", len(rows), "Unresolved engine ties table")
            for row in rows:
                f.name(engines_in(md_plain(" ".join(row[1:]))), "Unresolved engine ties table")
        m = re.match(r"^Risk register \(([\d,]+)\)$", heading)
        if m:
            f.add("risks:total", _int(m.group(1)), "Risk register heading")
            sev = Counter(re.findall(r"^- \*\*RISK-\d+\*\* · (\w+) ·", body, re.M))
            for s in SEVERITIES:
                f.add(f"risks:{s}", sev.get(s, 0), "Risk register entries")
            for eng_heading, _ in _md_sections(body, 3):
                f.name(engine_id(md_plain(eng_heading)), "Risk register")
        if heading.startswith("Migration trade-offs"):
            for x in re.findall(r"Consolidated [\d,]+ quer(?:y|ies) from (\w+) →", md_plain(body)):
                if engine_id(x) not in targets:
                    removed.add(engine_id(x))
    _close_calls(
        f, next((h for h in h2 if h.startswith("Close calls (")), None), "Close calls heading"
    )
    _fallback_queries(
        f,
        next((h for h in h2 if h.startswith("Queries that cannot be served as designed (")), None),
        "fallback heading",
    )
    if not re.search(r"Unresolved engine ties|Pending engine choices", " ".join(h2)):
        f.add("tied", 0, "no Unresolved engine ties section")
    if "Pending engine choices" not in " ".join(h2):
        f.add("choices", 0, "no Pending engine choices section")
    _auto_decided(
        f,
        next((h for h in h2 if h.startswith("Decided automatically, review if needed (")), ""),
        "Decided automatically heading",
    )
    f.add("removed", frozenset(removed), "Retired line and Migration trade-offs")

    queries_on_aurora = h2.get("Queries on Aurora by table", "")
    for m in re.finditer(r"^### (.+) \(([\d,]+) quer(?:y|ies)\)$", queries_on_aurora, re.M):
        f.add(f"queries:{engine_id(m.group(1))}", _int(m.group(2)), "Queries on Aurora by table")

    for heading, body in _md_sections(md, 3):
        m = re.match(r"^Wave (\d+): ", heading)
        if not m:
            continue
        n = m.group(1)
        where = f"roadmap wave {n}"
        text = md_plain(body)
        e = re.search(r"^- Engines: (.+)$", text, re.M)
        if e:
            f.name({engine_id(x) for x in e.group(1).split(",")}, where)
        c = re.search(
            r"^- all ([\d,]+) quer(?:y|ies) at cutover; ([\d,]+) \(", text, re.M
        ) or re.search(r"^- ()([\d,]+) quer(?:y|ies) \([\d.]+% of", text, re.M)
        if c:
            if c.group(1):
                f.add(f"wave{n}:cutover_queries", _int(c.group(1)), where)
            f.add(f"wave{n}:queries", _int(c.group(2)), where)
        st = re.search(
            r"^- ([\d,]+) source (?:tables and views(?: \(([\d,]+) tables?, ([\d,]+) views?\))?"
            r"|tables?(?: \(([\d,]+) to move\))?): ",
            text,
            re.M,
        )
        if st:
            f.add(f"wave{n}:served", _int(st.group(1)), where)
            if st.group(2):
                f.add(f"wave{n}:tables_only", _int(st.group(2)), where)
                f.add(f"wave{n}:views", _int(st.group(3)), where)
            if st.group(4):
                # #951: the wave's tables that move, beside the kept-in-sync line.
                f.add(f"wave{n}:moving", _int(st.group(4)), where)
        k = re.search(r"^- ([\d,]+) tables? kept in sync from their owner: ", text, re.M)
        if k:
            f.add(f"wave{n}:synced", _int(k.group(1)), where)
        g = re.search(r"^- [\d,]+ table groups?:\n((?:  - .+\n?)+)", text, re.M)
        if g:
            gq = sum(_int(x) for x in re.findall(r"^  - ([\d,]+) quer", g.group(1), re.M))
            f.add(f"wave{n}:group_queries", gq, where)
    return f


# --------------------------------------------------------------------------
# The executive report (PDF)
# --------------------------------------------------------------------------


def pdf_page_texts(pdf_path: str) -> list[str]:
    from pypdf import PdfReader

    return [norm(p.extract_text() or "") for p in PdfReader(pdf_path).pages]


_PDF_ROLES = "Retained|Migration target|Cache layer|Search read model|Evaluated|Assessed"
_PDF_COST_ROW = re.compile(rf"({_NAMES_RE}) (?:{_PDF_ROLES}) \$([\d,]+\.\d\d)")
_PDF_SEVERITY = re.compile(r"([\d,]+) (critical|high|medium|low|unrated)")


def pdf_figures(text: str) -> Figures:
    """Figures the executive report shows, read from its extracted text (one string,
    whitespace collapsed; the page furniture may sit between two halves of a table
    that continues on the next page): the tiles, the cost table, the engine table's
    total, the risk tiles and the decisions. It shows no table and no close call."""
    f = Figures("PDF")
    m = re.search(r"\$([\d,]+\.\d\d) Projected monthly cost", text)
    f.add("cost:total", _usd(m.group(1)) if m else UNREADABLE, "Projected monthly cost tile")
    start = text.find("COST BY ENGINE Engine Role Est. monthly")
    if start != -1:
        table = text[start : text.find("Total", start) + 40]
        for name, cost in _PDF_COST_ROW.findall(table):
            eng = engine_id(name)
            f.name(eng, "Cost by engine")
            f.add(f"cost:{eng}", _usd(cost), "Cost by engine")
        t = re.search(r"Total \$([\d,]+\.\d\d)", table)
        f.add("cost:total", _usd(t.group(1)) if t else UNREADABLE, "Cost by engine Total")
    m = re.search(r"Total [\d.]+% ([\d,]+) tables? migrates?", text)
    f.add("tables_migrate", _int(m.group(1)) if m else UNREADABLE, "Engines table Total")
    m = re.search(r"([\d,]+) Engines in the target architecture", text)
    f.add("engine_count", _int(m.group(1)) if m else UNREADABLE, "Engines tile")
    m = re.search(r"([\d,]+) Open risks((?: [\d,]+ (?:critical|high|medium|low|unrated),?)*)", text)
    if m:
        f.add("risks:total", _int(m.group(1)), "Open risks tile")
        sev = {k.upper(): _int(n) for n, k in _PDF_SEVERITY.findall(m.group(2))}
        if _int(m.group(1)):
            for s in SEVERITIES:
                f.add(f"risks:{s}", sev.get(s, 0), "Open risks tile")
    f.add("removal_savings", removal_savings(text), "Reality-check decisions")
    # #951: a wave with tables kept in sync states both halves of its tables.
    for seg in re.finditer(r"WAVE (\d+) (.*?)(?=WAVE \d+ |$)", text):
        h = re.search(
            r"([\d,]+) source tables?: ([\d,]+) to move, ([\d,]+) kept in sync from",
            seg.group(2),
        )
        if h:
            n, where = seg.group(1), f"wave {seg.group(1)} headline"
            f.add(f"wave{n}:served", _int(h.group(1)), where)
            f.add(f"wave{n}:moving", _int(h.group(2)), where)
            f.add(f"wave{n}:synced", _int(h.group(3)), where)
    removed: set[str] = set()
    r = re.search(rf"((?:{_NAMES_RE})(?:, (?:{_NAMES_RE}))*) Removed by the reality check", text)
    if r:
        removed |= {engine_id(x) for x in r.group(1).split(", ")}
    r = re.search(r"Retired by this plan: ((?:\$[\d,.]+/mo \([^)]+\)(?:, )?)+)", text)
    if r:
        removed |= {engine_id(x) for x in re.findall(r"/mo \(([^)]+)\)", r.group(1))}
    f.add("removed", frozenset(removed), "Removed tile and Retired line")
    m = re.search(r"Unresolved engine ties \(([\d,]+) quer(?:y|ies)\)", text)
    c = re.search(r"Pending engine choices \(([\d,]+) tables?, ([\d,]+) quer(?:y|ies)\)", text)
    f.add(
        "tied",
        _int(c.group(2)) if c else (_int(m.group(1)) if m else 0),
        "Decisions needed: Pending engine choices",
    )
    f.add("choices", _int(c.group(1)) if c else 0, "Decisions needed: Pending engine choices")
    _auto_decided(f, text, "Decisions needed: Decided automatically")
    _fallback_queries(
        f,
        text if "Queries that cannot be served as designed (" in text else None,
        "Decisions needed",
    )
    return f
