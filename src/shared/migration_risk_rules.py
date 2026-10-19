"""The migration-risk rule table and the rules raised from report.json alone (#950, #1148).

``src.agents.referee.migration_risks`` raises the rules that read synthesis's
inputs (the collection, the assignment, the schema designs). The rules here
read only fields ``report.json`` carries, through the facts layer
(``src.shared.report_facts``), so a report written before a rule existed gets
the same risk at render time (:func:`backfill_report_risks`) without
re-running synthesis. Synthesis calls the same functions, so a new report
and a backfilled one carry the same risk (ADR-032: one computation, the
renderers only format it).

:data:`RULES` is the one rule table (``Risk.rule`` is a key of it), and
:func:`wave_gate_strategies` the one-line-per-wave gate strategies both
paths write.

Rules raised here (one severity constant each, a test per rule and kind):

=========================  ========  ==========================================================
rule                       severity  raised when
=========================  ========  ==========================================================
``routing.flagged``        HIGH      a review found an engine cannot serve queries and no
                                     other engine the plan keeps can take them, so they stay
                                     (``flagged_unservable``, kind ``flagged``); one risk per
                                     engine
                           MEDIUM    every such query on the engine is held there by a pin the
                                     customer set on its table's writes (kind ``held``):
                                     changing the pin is the way to move it
``relational.gaps``        HIGH      the relational gap check (``facts.relational_gaps``, #1079)
                                     finds gaps that block the move to wave 1's engine
                           MEDIUM    gaps that need a rewrite before cutover
                           LOW       gaps that behave differently on the target; one risk per
                                     impact, in Wave 1; an informational gap raises none, and
                                     a gap check for another engine than wave 1's raises none
=========================  ========  ==========================================================
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.contracts.synthesis_output import Risk
from src.shared.capability_labels import FLAG_REDESIGN_HINTS
from src.shared.engine_names import canonical_engine_id, display_engine
from src.shared.flag_reasons import flag_reason_text
from src.shared.report_facts import flagged_unservable_rows, refresh_risk_facts
from src.shared.risk_severity import overall_risk_level, severity_rank

# A wave-gate strategy line names its risk ids up to this many, else counts them.
GATE_IDS_NAMED = 5

# A risk's text names at most this many short labels (an app-layer feature, a
# relational gap), then how many more.
LISTED_ITEMS = 5


@dataclass(frozen=True)
class RiskRule:
    """One deterministic risk rule: its base severity, risk type and short label."""

    severity: str
    risk_type: str
    label: str


RULES: dict[str, RiskRule] = {
    "sync.wave": RiskRule("MEDIUM", "DATA_CONSISTENCY", "tables kept in sync"),
    "routing.close_calls": RiskRule("MEDIUM", "MIGRATION_COMPLEXITY", "close calls"),
    "routing.tie_open": RiskRule("MEDIUM", "MIGRATION_COMPLEXITY", "pending engine choices"),
    "routing.unservable": RiskRule("HIGH", "MIGRATION_COMPLEXITY", "queries no engine fits"),
    "routing.flagged": RiskRule(
        "HIGH", "MIGRATION_COMPLEXITY", "flagged queries no other engine takes"
    ),
    "relational.gaps": RiskRule("MEDIUM", "MIGRATION_COMPLEXITY", "relational gaps"),
    "cache.dropped": RiskRule("MEDIUM", "PERFORMANCE_DEGRADATION", "dropped cache reads"),
    "cache.unsupported": RiskRule("MEDIUM", "MIGRATION_COMPLEXITY", "unsupported cache patterns"),
    "design.app_layer": RiskRule("MEDIUM", "OPERATIONAL_RISK", "logic moved to the application"),
    "design.hot_partition": RiskRule("MEDIUM", "PERFORMANCE_DEGRADATION", "hot partitions"),
    "design.trade_offs": RiskRule("MEDIUM", "MIGRATION_COMPLEXITY", "design trade-offs"),
}

# A flagged query held on its engine by a customer pin (#1033): the pin, not a
# missing engine, keeps it there.
HELD_ONLY_SEVERITY = "MEDIUM"


# A relational gap's impact -> its risk's severity (#1149); an informational gap
# raises none.
GAP_RISK_SEVERITY: dict[str, str] = {
    "blocks_move": "HIGH",
    "needs_rewrite": "MEDIUM",
    "behaves_differently": "LOW",
}


def n_of(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def them(count: int) -> str:
    return "it" if count == 1 else "them"


def these(count: int, one: str, many: str) -> str:
    return f"this {one}" if count == 1 else f"these {many}"


def join_words(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def risk_sentence(n_open: int, level: str, n_resolved: int) -> str:
    """``8 open migration risks (overall: LOW); 4 more were resolved by the assignment.``

    The deterministic summary's risk sentence. The resolved risks are not part of
    the open ones; the Engineering Report lists them apart (#258).
    """
    text = f"{n_of(n_open, 'open migration risk', 'open migration risks')} (overall: {level})"
    if n_resolved:
        verb = "was" if n_resolved == 1 else "were"
        text += f"; {n_resolved} more {verb} resolved by the assignment"
    return text + "."


# The risk sentence a stored deterministic summary carries: today's wording, and the
# one before #258 ("8 risk(s) identified (overall: LOW; 4 resolved by the assignment).").
_RISK_SENTENCE = re.compile(
    r"\b\d+ open migration risks? \(overall: [A-Za-z]+\)"
    r"(?:; \d+ more (?:was|were) resolved by the assignment)?\."
    r"|\b\d+ risk\(s\) identified \(overall: [^;()]+(?:; \d+ resolved by the assignment)?\)\."
)
# A generated summary that states a risk count or an overall level: a number (or
# "no") before "risk", or a severity word before "risk".
_STATES_RISKS = re.compile(
    r"\b(?:\d+|no|one|two|three|four|five|six|seven|eight|nine|ten)\b[^.;]{0,40}\brisks?\b"
    r"|\b(?:overall|critical|high|medium|low)\b[^.;]{0,30}\brisk",
    re.IGNORECASE,
)


def listed_items(items: list[str]) -> str:
    """Items joined by "; ", the first ``LISTED_ITEMS`` then how many more."""
    shown = list(items[:LISTED_ITEMS])
    more = len(items) - len(shown)
    return "; ".join(shown) + (f"; and {more} more" if more > 0 else "")


def gate_of(wave: int | None) -> str:
    return f"Wave {wave} gate" if wave else "cutover"


def wave_number(wave: Mapping[str, Any]) -> int | None:
    """A wave's number, or None when it has none (never 0: ``Risk.wave`` is >= 1)."""
    n = wave.get("wave")
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 1 else None


def wave_of_engine(migration_waves: Iterable[Any] | None) -> dict[str, int]:
    """engine id -> the first numbered wave that brings it in."""
    out: dict[str, int] = {}
    for w in migration_waves or []:
        if not isinstance(w, Mapping):
            continue
        n = wave_number(w)
        if n is None:
            continue
        for e in w.get("engines") or []:
            out.setdefault(canonical_engine_id(str(e)), n)
    return out


def make_risk(
    rule: str,
    engine: str | None,
    description: str,
    mitigation: str,
    tables: Iterable[str],
    query_ids: Iterable[str],
    wave: int | None,
    severity: str | None = None,
    details: list[str] | None = None,
) -> dict[str, Any]:
    """One risk record (unnumbered) for ``rule``."""
    spec = RULES[rule]
    extra = {"details": details} if details else {}
    return {
        "risk_type": spec.risk_type,
        "severity": severity or spec.severity,
        "description": f"[{engine}] {description}" if engine else description,
        "affected_tables": sorted({t for t in tables if t}),
        "mitigation": mitigation,
        "query_ids": sorted({q for q in query_ids if q}),
        "rule": rule,
        "wave": wave,
        **extra,
    }


# ---------------------------------------------------------------------------
# Flagged queries no other kept engine takes (#1148)
# ---------------------------------------------------------------------------


def flagged_unservable_risks(
    report: Mapping[str, Any],
    waves_by_engine: Mapping[str, int],
    tables_of: Mapping[str, Iterable[str]],
) -> list[dict[str, Any]]:
    """One ``routing.flagged`` risk per engine: the flagged and held queries
    ``facts.unservable`` counts (``report_facts.flagged_unservable_rows``), so the
    two agree. ``report`` needs ``flagged_unservable`` and ``query_traces``.

    A row's tables are its own ``source_tables`` (1.43); ``tables_of`` (query id
    -> tables) fills them in for a row written before."""
    own = {
        str(r["query_id"]): [str(t) for t in r.get("source_tables") or [] if t]
        for r in report.get("flagged_unservable") or []
        if isinstance(r, Mapping) and r.get("query_id")
    }
    tables = {q: own.get(q) or sorted(tables_of.get(q, ())) for q in {*own, *tables_of}}
    by_engine: dict[str, list[dict[str, Any]]] = {}
    for r in flagged_unservable_rows(dict(report)):
        engine = canonical_engine_id(str(r.get("engine") or ""))
        if engine:
            by_engine.setdefault(engine, []).append(r)
    out: list[dict[str, Any]] = []
    for engine, rows in sorted(by_engine.items()):
        out.append(_flagged_risk(engine, rows, waves_by_engine.get(engine), tables))
    return out


def _hints(engine: str, categories: list[str], k: int) -> str:
    """``For DynamoDB: give each query a key ...; store the data ...`` (the engine
    named once, each reason category's redesign hint once, #888's wording)."""
    texts = list(
        dict.fromkeys(
            FLAG_REDESIGN_HINTS.get(c, FLAG_REDESIGN_HINTS["other"]).rstrip(". ")
            for c in categories
        )
    )
    if k > 1:
        texts = [t.replace("the query", "each query") for t in texts]
    return f"For {display_engine(engine)}: " + "; ".join(texts) + "."


def _writes_of(count: int) -> str:
    return "its table's writes" if count == 1 else "their tables' writes"


def _flagged_risk(
    engine: str,
    rows: list[dict[str, Any]],
    wave: int | None,
    tables_of: Mapping[str, Iterable[str]],
) -> dict[str, Any]:
    name = display_engine(engine)
    k = len(rows)
    flagged = sum(1 for r in rows if r.get("kind") != "held")
    held = k - flagged
    head = (
        f"{n_of(k, 'query stays', 'queries stay')} on {name} although a review found "
        f"{name} cannot serve {them(k)}"
    )
    if flagged and held:
        kinds = (
            f": for {flagged}, no other engine the plan keeps can take {them(flagged)}, and "
            f"{held} {'is' if held == 1 else 'are'} held there by a pin on {_writes_of(held)}."
        )
    elif held:
        kinds = f", and a pin on {_writes_of(k)} holds {them(k)} there."
    else:
        kinds = f", and no other engine the plan keeps can take {them(k)}."
    counts: dict[str, int] = {}
    for r in rows:
        category = str(r.get("reason_category") or "other")
        counts[category] = counts.get(category, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    reasons = "; ".join(
        flag_reason_text(c) if len(ordered) == 1 else f"{flag_reason_text(c)} ({n})"
        for c, n in ordered
    )
    description = f"{head}{kinds} Review {'reason' if len(ordered) == 1 else 'reasons'}: {reasons}."
    steps = [
        f"Before the {gate_of(wave)}, redesign {these(k, 'query', 'queries')} or accept "
        f"{name} for {them(k)}. {_hints(engine, [c for c, _ in ordered], k)}"
    ]
    if held:
        steps.append(
            f"To move {'the held query' if held == 1 else 'a held query'}, change the pin in "
            "the assignment review."
        )
    steps.append("Then re-run the assignment review.")
    qids = [str(r["query_id"]) for r in rows]
    return make_risk(
        "routing.flagged",
        engine,
        description,
        " ".join(steps),
        {t for q in qids for t in tables_of.get(q, ())},
        qids,
        wave,
        severity=None if flagged else HELD_ONLY_SEVERITY,
    )


# ---------------------------------------------------------------------------
# The relational move's version and feature gaps (#1149)
# ---------------------------------------------------------------------------


def _gap_head(impact: str, k: int, name: str) -> str:
    if impact == "blocks_move":
        return f"{n_of(k, 'relational gap blocks', 'relational gaps block')} the move to {name}"
    if impact == "needs_rewrite":
        return (
            f"{n_of(k, 'relational gap needs', 'relational gaps need')} a rewrite before "
            f"cutover to {name}"
        )
    return f"{n_of(k, 'relational gap behaves', 'relational gaps behave')} differently on {name}"


def _gap_steps(impact: str, k: int, name: str) -> str:
    if impact == "blocks_move":
        return (
            f"Before the Wave 1 gate, replace {these(k, 'feature', 'features')} outside the "
            f"database or remove {them(k)}, as each gap's reason says: the move cannot finish "
            "while the source uses them."
        )
    if impact == "needs_rewrite":
        return (
            "Before the Wave 1 gate, change the schema, the queries and the routines each gap "
            f"names, as its reason says, and test them on {name}."
        )
    return (
        f"Before the Wave 1 gate, test the queries and objects each gap names on {name} and "
        "compare their results with the source."
    )


def _gap_detail(gap: Mapping[str, Any]) -> str:
    """One gap in full: its feature, reason, documentation and the objects it was found in."""
    text = f"{gap.get('feature')}: {str(gap.get('reason') or '').strip()}"
    docs = str(gap.get("docs") or "").strip()
    if docs:
        text += f" Documentation: {docs}."
    objects = [
        f"{o.get('kind')} {o.get('id')}"
        for o in gap.get("objects") or []
        if isinstance(o, Mapping) and o.get("id")
    ]
    if objects:
        text += f" Found in {', '.join(objects)}."
    return text


def relational_gap_risks(
    relational_gaps: Mapping[str, Any] | None,
    migration_waves: Iterable[Any] | None,
) -> list[dict[str, Any]]:
    """One ``relational.gaps`` risk per impact (most severe first), in Wave 1.

    Raised only when the gap check's ``target_engine`` is wave 1's engine (the
    rule #1145 applies to wave 1's note): a check run against another Aurora
    engine is neither counted nor listed. Severity is the impact's
    (:data:`GAP_RISK_SEVERITY`); an informational gap raises none. The text names
    each gap's feature (a catalog label, up to ``LISTED_ITEMS``); its reason,
    documentation and objects are in ``details``. Evidence tables are the gaps'
    tables that wave 1 moves, so a table id the check could not resolve is left out.
    """
    if not isinstance(relational_gaps, Mapping):
        return []
    wave1 = next(
        (w for w in migration_waves or [] if isinstance(w, Mapping) and wave_number(w) == 1),
        None,
    )
    engines = [canonical_engine_id(str(e)) for e in (wave1 or {}).get("engines") or [] if e]
    target = canonical_engine_id(str(relational_gaps.get("target_engine") or ""))
    if not engines or target != engines[0]:
        return []
    moved = {str(t) for t in (wave1 or {}).get("tables") or []}
    by_impact: dict[str, list[Mapping[str, Any]]] = {}
    for g in relational_gaps.get("gaps") or []:
        if isinstance(g, Mapping) and g.get("feature") and g.get("impact") in GAP_RISK_SEVERITY:
            by_impact.setdefault(str(g["impact"]), []).append(g)
    name = display_engine(target)
    out: list[dict[str, Any]] = []
    for impact in GAP_RISK_SEVERITY:
        gaps = by_impact.get(impact)
        if not gaps:
            continue
        k = len(gaps)
        features = [str(g["feature"]) for g in gaps]
        tables = {str(t) for g in gaps for t in g.get("tables") or [] if t}
        out.append(
            make_risk(
                "relational.gaps",
                target,
                f"Wave 1: {_gap_head(impact, k, name)}: {listed_items(features)}. Each gap's "
                "reason and where it was found are in the engineering report.",
                _gap_steps(impact, k, name),
                tables & moved if moved else tables,
                {str(q) for g in gaps for q in g.get("queries") or [] if q},
                1,
                severity=GAP_RISK_SEVERITY[impact],
                details=[_gap_detail(g) for g in gaps],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Numbering and the wave-gate lines
# ---------------------------------------------------------------------------


def last_risk_number(risks: list[dict[str, Any]]) -> int:
    """The highest ``RISK-NNN`` number in ``risks`` (0 when none)."""
    best = 0
    for r in risks or []:
        m = re.fullmatch(r"RISK-(\d+)", str(r.get("risk_id") or ""))
        if m:
            best = max(best, int(m.group(1)))
    return best


def number_risks(risks: list[dict[str, Any]], start_id: int) -> list[dict[str, Any]]:
    """``risks`` numbered from ``RISK-{start_id + 1}``."""
    return [
        {"risk_id": f"RISK-{i:03d}", **risk} for i, risk in enumerate(risks, start=start_id + 1)
    ]


def wave_gate_strategies(risks: list[dict[str, Any]]) -> list[str]:
    """One mitigation-strategy line per wave: the risks to close before its gate,
    by id up to ``GATE_IDS_NAMED``, else by count (the cards carry the ids)."""
    by_wave: dict[int | None, list[dict[str, Any]]] = {}
    for r in risks:
        if r.get("rule") in RULES:
            by_wave.setdefault(r.get("wave"), []).append(r)
    lines = []
    for wave in sorted(by_wave, key=lambda w: (w is None, w or 0)):
        items = by_wave[wave]
        ids = (
            join_words([str(r["risk_id"]) for r in items])
            if len(items) <= GATE_IDS_NAMED
            else f"the {len(items)} Wave {wave} risks" if wave else f"the {len(items)} risks"
        )
        labels: list[str] = []
        for r in items:
            label = RULES[r["rule"]].label
            if label not in labels:
                labels.append(label)
        head = f"Before the Wave {wave} gate" if wave else "Before cutover"
        lines.append(f"{head}, close {ids} ({join_words(labels)}).")
    return lines


# ---------------------------------------------------------------------------
# A report written before a rule existed (render time)
# ---------------------------------------------------------------------------


def report_query_tables(report: Mapping[str, Any]) -> dict[str, set[str]]:
    """query id -> the tables it uses, as ``report.json`` lists them: the query groups'
    source queries, and ``table_sources`` (each table's writes and replica reads)."""
    out: dict[str, set[str]] = {}
    for row in report.get("table_sources") or []:
        if not isinstance(row, Mapping) or not row.get("table"):
            continue
        qids = [*(row.get("write_query_ids") or [])]
        for replica in row.get("replicas") or []:
            if isinstance(replica, Mapping):
                qids += replica.get("read_query_ids") or []
        for q in qids:
            if q:
                out.setdefault(str(q), set()).add(str(row["table"]))
    for g in report.get("query_groups") or []:
        if not isinstance(g, Mapping):
            continue
        for q in g.get("source_queries") or []:
            if isinstance(q, Mapping) and q.get("query_id"):
                out.setdefault(str(q["query_id"]), set()).update(
                    str(t) for t in q.get("tables_accessed") or [] if t
                )
    return out


def report_fact_risks(report: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """rule -> the unnumbered risks the rules here raise from ``report`` alone."""
    waves = wave_of_engine(report.get("migration_waves"))
    tables = report_query_tables(report)
    facts = report.get("facts")
    gaps = facts.get("relational_gaps") if isinstance(facts, Mapping) else None
    return {
        "routing.flagged": flagged_unservable_risks(report, waves, tables),
        "relational.gaps": relational_gap_risks(gaps, report.get("migration_waves")),
    }


# A wave-gate strategy line (``wave_gate_strategies``), whatever its labels.
_GATE_LINE = re.compile(r"^Before (?:the Wave \d+ gate|cutover), close ")


def _with_gate_lines(strategies: list[Any], new: list[str]) -> list[Any]:
    """``strategies`` with every stored wave-gate line replaced by ``new``, at the
    first one's place; with none stored (a report written before 1.40), before the
    post-wave checks, which start "After wave". A stored line whose labels differ
    from today's is replaced too, so no wave gets two gate lines."""
    gate = [i for i, s in enumerate(strategies) if isinstance(s, str) and _GATE_LINE.match(s)]
    if gate:
        at = gate[0]
    else:
        at = next(
            (
                i
                for i, s in enumerate(strategies)
                if isinstance(s, str) and s.startswith("After wave ")
            ),
            len(strategies),
        )
    rest = [s for i, s in enumerate(strategies) if i not in set(gate)]
    at -= sum(1 for i in gate if i < at)
    return [*rest[:at], *new, *rest[at:]]


def _with_risk_summary(report: dict[str, Any], out: dict[str, Any], added: int) -> None:
    """The summary of ``out`` stating the backfilled register's risk count and level.

    The deterministic summary's risk sentence is written again from the register
    (``risk_sentence``, the sentence synthesis writes). A generated summary that
    states a risk count or level was checked against the register it was written
    with, so it gives way to that corrected deterministic summary, recorded as a
    fallback with the reason; one that states none is kept. In place on ``out``."""
    assessment = out["risk_assessment"]
    risks = assessment.get("risks") or []
    sentence = risk_sentence(
        len(risks),
        str(assessment.get("overall_risk_level") or "LOW"),
        len(assessment.get("resolved_risks") or []),
    )
    stored = report.get("summary_deterministic")
    fixed = (
        _RISK_SENTENCE.sub(lambda _m: sentence, stored, count=1)
        if isinstance(stored, str)
        else stored
    )
    if fixed != stored:
        out["summary_deterministic"] = fixed
    summary = report.get("summary")
    if not isinstance(summary, str) or not summary:
        return
    if summary == stored:
        out["summary"] = fixed
    elif _STATES_RISKS.search(summary) and isinstance(fixed, str) and fixed:
        out["summary"] = fixed
        out["summary_source"] = "deterministic_fallback"
        out["summary_validation_warnings"] = [
            *(report.get("summary_validation_warnings") or []),
            f"[high confidence] The generated summary was checked against the risk register "
            f"before {added} more {'risk was' if added == 1 else 'risks were'} raised for "
            f"this report, but the risk register has {len(risks)} open risks (overall: "
            f"{assessment.get('overall_risk_level')}).",
        ]


def backfill_report_risks(report: dict[str, Any]) -> dict[str, Any]:
    """``report`` with the risks a rule here raises that it does not carry yet.

    A report synthesized before a rule existed has none of that rule's risks;
    the deliverables and the results payload read it through this, so it shows
    the same risk a new synthesis writes. The risks are numbered after the
    stored ones, the overall level only rises, the wave-gate lines are written
    again over every rule risk, the facts block's risk counts are recounted
    (``report_facts.refresh_risk_facts``) and the summary states the new count
    and level (``_with_risk_summary``). A report that already carries a
    rule's risks, or raises none, comes back unchanged (the same object).
    """
    assessment = report.get("risk_assessment")
    if not isinstance(assessment, dict):
        return report
    stored = [r for r in assessment.get("risks") or [] if isinstance(r, dict)]
    present = {r.get("rule") for r in stored}
    new = [
        risk
        for rule, risks in report_fact_risks(report).items()
        if rule not in present
        for risk in risks
    ]
    if not new:
        return report
    # Through the contract, so a backfilled risk has the stored shape (``details``
    # None and all).
    numbered = [
        Risk.model_validate(r).model_dump(mode="json")
        for r in number_risks(new, last_risk_number(stored))
    ]
    risks = [*(assessment.get("risks") or []), *numbered]
    old_rule_risks = [r for r in stored if r.get("rule") in RULES]
    level = str(assessment.get("overall_risk_level") or "LOW")
    computed = overall_risk_level(numbered)
    strategies = list(assessment.get("mitigation_strategies") or [])
    out = {
        **report,
        "risk_assessment": {
            **assessment,
            "risks": risks,
            "overall_risk_level": max(level, computed, key=severity_rank),
            "mitigation_strategies": _with_gate_lines(
                strategies, wave_gate_strategies([*old_rule_risks, *numbered])
            ),
        },
    }
    _with_risk_summary(report, out, len(numbered))
    if isinstance(report.get("facts"), dict):
        out["facts"] = refresh_risk_facts(report["facts"], out)
    return out
