"""Migration risks raised from the facts synthesis already computes (#950).

The anti-pattern risks in ``synthesis_report.build_risk_assessment`` ask one
question: does an engine fit these queries. As routing improves they resolve,
and the register empties while the migration itself still carries real risk:
tables kept in sync across engines, engine choices won on a small margin,
pending ties, queries no engine fits, cache reads dropped, design trade-offs.
This module raises those risks deterministically from synthesis's own facts,
so every deliverable shows them from ``report.json`` (ADR-032: each figure is
computed here once; renderers only format the risk).

Every risk carries ``rule`` (a key of :data:`RULES`), ``wave`` (the migration
wave whose gate it must clear, or None), its ``affected_tables`` and
``query_ids`` as evidence, and a mitigation that names that wave's gate.
:func:`wave_gate_strategies` adds one mitigation-strategy line per wave that
names the risks to close before its gate.

The register stays short: one sync risk and one close-call risk per wave, the
detail (engine pairs, tables, queries) in the engineering report's sources of
truth and close-call sections and in each risk's evidence.

Severity rules (one constant each, a test per rule):

=========================  ========  ==========================================================
rule                       severity  raised when
=========================  ========  ==========================================================
``sync.wave``              HIGH      a table the wave brings in takes writes on several engines
                                     (``table_sources``: no single source of truth)
                           MEDIUM    the wave serves reads from copies CDC keeps in sync from a
                                     source of truth (``facts.table_sources.replicated_tables``;
                                     for an assignment written before write groups, the wave's
                                     ``kept_in_sync`` and ``shared_in_wave`` tables)
                           LOW       the wave only reads tables no captured query writes: a
                                     one-time copy (``facts.waves[].kept_in_sync``)
``routing.close_calls``    MEDIUM    a query in the wave moved to its engine by less than
                                     ``CLOSE_CALL_MARGIN`` points over the runner-up
                           LOW       every such query stays on the source-compatible engine
``routing.tie_open``       MEDIUM    queries tie exactly and no routing rule decides them
``routing.unservable``     HIGH      queries fit no analyzed engine (``query_traces.unservable``)
``cache.dropped``          MEDIUM    the cache safety net dropped reads the plan cached
``cache.unsupported``      MEDIUM    the cache layer's design lists unsupported patterns (an
                                     engine that only fronts reads is never raised by the
                                     per-engine risk loop); one risk per engine
``design.app_layer``       MEDIUM    a relational design moves source objects' logic
                                     (trigger, procedure, ...) into the application; one
                                     risk per engine
``design.hot_partition``   MEDIUM    design partitions run above 80% of their limit at peak;
                           HIGH      at ``HOT_PARTITION_HIGH_PCT`` or more; one per engine
``design.trade_offs``      MEDIUM    an engine's schema design records trade-offs to review;
                                     one risk per engine
=========================  ========  ==========================================================

Design trade-offs are model-written prose with no validated category
(``schema_design_output.TradeOff``), so their words never set a severity: an
engine's trade-offs are one MEDIUM "review the design trade-offs" risk that
counts them. The reality check's own ``trade_offs`` entries are routing
decisions, not design trade-offs, and are never raised.

Engine ids are normalised once (``engine_names.canonical_engine_id``) before
anything is grouped: a designer can write ``aurora-mysql`` and ``aurora_mysql``
in one design, and each risk takes its wave from the engine.

A risk's text states counts and short labels, never a clipped quote: the full
model texts it covers (each trade-off, each unsupported cache pattern with its
workaround) go in its ``details``, which the engineering report and the
results page list in full and the executive report leaves out.

The wording is the code's own, plain and assertive: no dashes as punctuation,
no hedges. A risk's description and mitigation quote no model text; its
``details`` carry the design's own texts, as data.

Facts on main that no rule reads yet are listed in :data:`PENDING_FACT_HOOKS`,
each a follow-up that needs its own rule, severity and test.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.agents.referee.cache_overlay import is_write_query
from src.shared.engine_names import canonical_engine_id, display_engine
from src.shared.report_facts import CLOSE_CALL_MARGIN, RELATIONAL_ENGINES, wave_facts
from src.shared.unsupported_pattern import (
    unsupported_pattern_ids,
    unsupported_pattern_label,
)

# A hot partition at or above this utilization is HIGH; below it (but at risk,
# above 80%) it is MEDIUM.
HOT_PARTITION_HIGH_PCT = 95.0

# A wave-gate strategy line names its risk ids up to this many, else counts them.
GATE_IDS_NAMED = 5

# A risk's text names at most this many short labels (an app-layer feature),
# then how many more.
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
    "cache.dropped": RiskRule("MEDIUM", "PERFORMANCE_DEGRADATION", "dropped cache reads"),
    "cache.unsupported": RiskRule("MEDIUM", "MIGRATION_COMPLEXITY", "unsupported cache patterns"),
    "design.app_layer": RiskRule("MEDIUM", "OPERATIONAL_RISK", "logic moved to the application"),
    "design.hot_partition": RiskRule("MEDIUM", "PERFORMANCE_DEGRADATION", "hot partitions"),
    "design.trade_offs": RiskRule("MEDIUM", "MIGRATION_COMPLEXITY", "design trade-offs"),
}

# Facts the risk register does not read yet (both landed on main while #950 was
# open). Each needs its own rule above (severity, test); until then nothing
# reads the field.
PENDING_FACT_HOOKS: dict[str, str] = {
    "flagged_unservable": "#1039: kept and terminal flagged queries no engine serves",
    "facts.relational_gaps": "#1079: version and feature gaps for the relational move",
}


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _them(count: int) -> str:
    return "it" if count == 1 else "them"


def _these(count: int, one: str, many: str) -> str:
    return f"this {one}" if count == 1 else f"these {many}"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _names(engines: Iterable[str]) -> str:
    return _join([display_engine(e) for e in engines])


def _gate(wave: int | None) -> str:
    return f"Wave {wave} gate" if wave else "cutover"


def _start(wave: int | None) -> str:
    return f"Wave {wave} starts" if wave else "cutover"


def _wave_number(wave: Mapping[str, Any]) -> int | None:
    """A wave's number, or None when it has none (never 0: ``Risk.wave`` is >= 1)."""
    n = wave.get("wave")
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 1 else None


@dataclass
class _Context:
    """What the rules read, indexed once."""

    waves: list[dict[str, Any]]
    engine_of: dict[str, str]
    tables_of: dict[str, set[str]]
    writes: set[str]
    wave_of_engine: dict[str, int]
    retained: frozenset[str]

    def queries_on(self, engines: Iterable[str]) -> list[str]:
        wanted = set(engines)
        return sorted(q for q, e in self.engine_of.items() if e in wanted)


def _context(
    migration_waves: list[dict[str, Any]],
    query_assignments: list[dict[str, Any]],
    source_queries: list[dict[str, Any]],
    normalise: Callable[[Iterable[str]], set[str]],
) -> _Context:
    engine_of = {
        str(qa["query_id"]): canonical_engine_id(str(qa["assigned_engine"]))
        for qa in query_assignments
        if isinstance(qa, dict)
        and qa.get("in_scope", True)
        and qa.get("query_id")
        and qa.get("assigned_engine")
    }
    collected = {
        str(q["query_id"]): q for q in source_queries if isinstance(q, dict) and q.get("query_id")
    }
    assigned_tables = {
        str(qa["query_id"]): qa.get("source_tables") or []
        for qa in query_assignments
        if isinstance(qa, dict) and qa.get("query_id")
    }
    tables_of = {
        q: normalise(
            (collected.get(q) or {}).get("tables_accessed") or assigned_tables.get(q) or []
        )
        for q in engine_of
    }
    writes = {q for q in engine_of if is_write_query(collected.get(q) or {})}
    # A wave with no number is left out: no risk can name its gate.
    waves = [w for w in migration_waves or [] if isinstance(w, dict) and _wave_number(w)]
    wave_of_engine: dict[str, int] = {}
    for w in waves:
        for e in w.get("engines") or []:
            wave_of_engine.setdefault(canonical_engine_id(str(e)), int(w["wave"]))
    retained = frozenset(
        str(e) for w in waves if w.get("homogeneity") is not None for e in w.get("engines") or []
    )
    return _Context(
        waves=waves,
        engine_of=engine_of,
        tables_of=tables_of,
        writes=writes,
        wave_of_engine=wave_of_engine,
        retained=retained or frozenset(RELATIONAL_ENGINES),
    )


def _risk(
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


def _wave_tag(ctx: _Context, wave: int) -> str | None:
    """The engine a wave's risk is filed under: the wave's own engine when it has
    one, else none (a merged wave's risk belongs to both engines equally)."""
    engines = [e for e, w in ctx.wave_of_engine.items() if w == wave]
    return engines[0] if len(engines) == 1 else None


# ---------------------------------------------------------------------------
# Tables kept in sync: one risk per wave
# ---------------------------------------------------------------------------


@dataclass
class _WaveSync:
    split: set[str]
    split_writers: set[str]
    pairs: dict[tuple[str, str], tuple[set[str], set[str]]]  # (source, replica) -> tables, reads
    kept: set[str]  # kept in sync by CDC from an owner outside the wave (older assignments)
    copy_once: set[str]  # read here, written by no captured query
    queries: set[str]


def _sync_risks(
    ctx: _Context,
    table_sources: list[dict[str, Any]] | None,
    normalise: Callable[[Iterable[str]], set[str]],
) -> list[dict[str, Any]]:
    by_wave: dict[int, _WaveSync] = {}

    def slot(n: int) -> _WaveSync:
        return by_wave.setdefault(n, _WaveSync(set(), set(), {}, set(), set(), set()))

    def later_wave(engines: Iterable[str]) -> int | None:
        waves = [ctx.wave_of_engine[e] for e in engines if e in ctx.wave_of_engine]
        return max(waves) if waves else None

    written: set[str] = set()
    covered: set[str] = set()
    if table_sources is not None:
        for row in sorted(
            (r for r in table_sources if isinstance(r, dict) and r.get("table")),
            key=lambda r: str(r["table"]),
        ):
            tables = normalise([str(row["table"])])
            written |= tables
            source = row.get("source_of_truth")
            if not source:
                writers = {str(e) for e in row.get("write_engines") or [] if e}
                n = later_wave(writers)
                if n:
                    s = slot(n)
                    s.split |= tables
                    s.split_writers |= writers
                    s.queries |= {str(q) for q in row.get("write_query_ids") or [] if q}
                    covered |= tables
                continue
            for replica in row.get("replicas") or []:
                if not isinstance(replica, dict) or not replica.get("engine"):
                    continue
                n = later_wave([str(source), str(replica["engine"])])
                if not n:
                    continue
                reads = {str(q) for q in replica.get("read_query_ids") or [] if q}
                pair = slot(n).pairs.setdefault(
                    (str(source), str(replica["engine"])), (set(), set())
                )
                pair[0].update(tables)
                pair[1].update(reads)
                slot(n).queries |= reads
                covered |= tables
    for w in ctx.waves:
        facts = wave_facts(w)
        if not facts["migrating"]:
            continue
        n = int(facts["wave"])
        engines = facts["engines"]
        mine = ctx.queries_on(engines)
        uses = {
            str(o["table"]) for o in [*facts["kept_in_sync"], *facts["shared_in_wave"]]
        } - covered
        if table_sources is not None:
            # Every written table is in ``table_sources``; the rest are never written.
            once = {t for t in uses if t not in written}
            if once:
                slot(n).copy_once |= once
        else:
            once = {t for t in uses if not any(t in ctx.tables_of.get(q, ()) for q in ctx.writes)}
            if once:
                slot(n).copy_once |= once
            if uses - once:
                slot(n).kept |= uses - once
        ws = by_wave.get(n)
        if ws is not None:
            ws.queries |= {
                q for q in mine if ctx.tables_of.get(q, set()) & (ws.copy_once | ws.kept)
            }
    out: list[dict[str, Any]] = []
    for n in sorted(by_wave):
        out.append(_wave_sync_risk(ctx, n, by_wave[n]))
    return out


def _wave_sync_risk(ctx: _Context, n: int, s: _WaveSync) -> dict[str, Any]:
    sentences: list[str] = []
    steps: list[str] = []
    if s.split:
        k = len(s.split)
        sentences.append(
            f"{_n(k, 'table takes', 'tables take')} writes on {_names(sorted(s.split_writers))}, "
            f"so {_them(k)} {'has' if k == 1 else 'have'} no single source of truth and "
            "need two-way sync."
        )
        steps.append(f"give {_these(k, 'table', 'tables')} one writer")
    synced = set().union(*(t for t, _ in s.pairs.values())) if s.pairs else set()
    if s.pairs:
        reads = set().union(*(r for _, r in s.pairs.values()))
        k, m, pairs = len(synced), len(reads), len(s.pairs)
        sources = sorted({src for src, _ in s.pairs})
        sentences.append(
            f"{_n(m, 'read runs', 'reads run')} on a copy of {_n(k, 'table', 'tables')} that CDC "
            f"keeps in sync from {_names(sources)} across {_n(pairs, 'engine pair', 'engine pairs')}, "
            "so a read there can miss a recent write."
        )
        steps.append(
            "set up CDC for each engine pair the sources of truth section lists, and send any "
            "read that must see its own write to its source of truth"
        )
    if s.kept:
        k = len(s.kept)
        sentences.append(
            f"{_n(k, 'table', 'tables')} the wave uses {'stays' if k == 1 else 'stay'} owned "
            f"outside it, so CDC must keep {'its copy' if k == 1 else 'each copy'} current."
        )
        steps.append("set up CDC for the tables owned outside the wave")
    if s.copy_once:
        k = len(s.copy_once)
        sentences.append(
            f"{_n(k, 'more table is', 'more tables are')} read here and written by no captured "
            f"query, so a one-time copy serves {_them(k)}."
        )
        steps.append(
            "copy the read-only tables once and confirm nothing outside the captured workload "
            "writes them"
        )
    if s.split:
        severity = "HIGH"
    elif s.pairs or s.kept:
        severity = "MEDIUM"
    else:
        severity = "LOW"
    first = sentences[0]
    if not (s.pairs or s.split or s.kept):
        first = first.replace("more table is", "table is").replace("more tables are", "tables are")
    sentences[0] = f"Wave {n}: {first}"
    mitigation = (
        f"Before Wave {n} starts, " + "; ".join(steps) + f". Compare the row counts on both "
        f"sides at the {_gate(n)}."
    )
    return _risk(
        "sync.wave",
        _wave_tag(ctx, n),
        " ".join(sentences),
        mitigation,
        s.split | synced | s.kept | s.copy_once,
        s.queries,
        n,
        severity=severity,
    )


# ---------------------------------------------------------------------------
# Engine choices: close calls, open ties, queries no engine fits
# ---------------------------------------------------------------------------


def _close_call_risks(ctx: _Context, query_traces: Mapping[str, Any] | None) -> list[dict]:
    """One risk per wave: the queries whose engine won by a small margin there.

    A query kept on the source-compatible engine counts in the wave of its
    runner-up (that wave would move it); a moved query in its own engine's wave.
    """
    rows = (query_traces or {}).get("close_calls")
    if not isinstance(rows, list):
        return []
    by_wave: dict[int | None, dict[tuple[str, str], list[Mapping[str, Any]]]] = {}
    for r in rows:
        if not (
            isinstance(r, Mapping)
            and r.get("query_id")
            and r.get("engine")
            and r.get("alternative")
            and isinstance(r.get("gap"), (int, float))
        ):
            continue
        engine, alternative = str(r["engine"]), str(r["alternative"])
        kept = engine in ctx.retained
        wave = ctx.wave_of_engine.get(alternative if kept else engine)
        by_wave.setdefault(wave, {}).setdefault((engine, alternative), []).append(r)
    margin = f"{CLOSE_CALL_MARGIN:g}"
    out: list[dict[str, Any]] = []
    for wave in sorted(by_wave, key=lambda w: (w is None, w or 0)):
        pairs = by_wave[wave]
        parts: list[str] = []
        qids: list[str] = []
        moved = False
        for (engine, alternative), members in sorted(
            pairs.items(), key=lambda kv: (-len(kv[1]), kv[0])
        ):
            kept = engine in ctx.retained
            moved = moved or not kept
            k = len(members)
            verb = (
                ("stays on" if kept else "goes to") if k == 1 else ("stay on" if kept else "go to")
            )
            parts.append(f"{k} {verb} {display_engine(engine)} over {display_engine(alternative)}")
            qids += [str(r["query_id"]) for r in members]
        total = len(qids)
        ties = sum(1 for p in pairs.values() for r in p if r.get("settled_tie"))
        tie_note = (
            f" ({_n(ties, 'exact tie', 'exact ties')} a routing rule settled)" if ties else ""
        )
        head = f"Wave {wave}: " if wave else ""
        description = (
            f"{head}{_n(total, 'query was', 'queries were')} decided by less than {margin} "
            f"points{tie_note}: {_join(parts)}. A small change in the scores moves "
            f"{_them(total)}."
        )
        mitigation = (
            f"Confirm the engine for {_these(total, 'query', 'queries')} in the assignment "
            f"review before {_start(wave)}, using the close calls section; move any that fit "
            "the runner-up better."
        )
        out.append(
            _risk(
                "routing.close_calls",
                _wave_tag(ctx, wave) if wave else None,
                description,
                mitigation,
                {t for q in qids for t in ctx.tables_of.get(q, ())},
                qids,
                wave,
                severity="MEDIUM" if moved else "LOW",
            )
        )
    return out


def _tie_risks(ctx: _Context, tied_queries: list[dict[str, Any]] | None) -> list[dict]:
    groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for t in tied_queries or []:
        if not isinstance(t, dict) or not t.get("query_id") or not t.get("tied_engines"):
            continue
        key = (str(t.get("assigned_engine") or ""), tuple(sorted(map(str, t["tied_engines"]))))
        groups.setdefault(key, []).append(str(t["query_id"]))
    out: list[dict[str, Any]] = []
    for (placeholder, tied), qids in sorted(groups.items()):
        waves = [ctx.wave_of_engine[e] for e in tied if e in ctx.wave_of_engine]
        wave = min(waves) if waves else None
        names = _join([display_engine(e) for e in tied])
        out.append(
            _risk(
                "routing.tie_open",
                placeholder or None,
                f"{_n(len(qids), 'query', 'queries')} {'scores' if len(qids) == 1 else 'score'} "
                f"exactly the same on {names} and no routing rule decides "
                f"{'it' if len(qids) == 1 else 'them'}, so the plan lists "
                f"{'it' if len(qids) == 1 else 'them'} on "
                f"{display_engine(placeholder) if placeholder else 'a placeholder engine'} "
                "until you choose.",
                f"Choose {' or '.join(display_engine(e) for e in tied)} for "
                f"{_these(len(qids), 'query', 'queries')} in the assignment review before "
                f"{_start(wave)}.",
                {tb for q in qids for tb in ctx.tables_of.get(q, ())},
                qids,
                wave,
            )
        )
    return out


def _unservable_risks(ctx: _Context, query_traces: Mapping[str, Any] | None) -> list[dict]:
    rows = (query_traces or {}).get("unservable")
    if not isinstance(rows, list):
        return []
    by_engine: dict[str, list[str]] = {}
    for r in rows:
        if isinstance(r, Mapping) and r.get("query_id"):
            by_engine.setdefault(str(r.get("engine") or ""), []).append(str(r["query_id"]))
    out: list[dict[str, Any]] = []
    for engine, qids in sorted(by_engine.items()):
        wave = ctx.wave_of_engine.get(engine)
        name = display_engine(engine) if engine else "a fallback engine"
        out.append(
            _risk(
                "routing.unservable",
                engine or None,
                f"{_n(len(qids), 'query', 'queries')} {'fits' if len(qids) == 1 else 'fit'} no "
                f"analyzed engine and {'sits' if len(qids) == 1 else 'sit'} on {name} as a "
                "fallback.",
                f"Redesign these queries or accept {name} for them, then re-run the assignment "
                f"review before the {_gate(wave)}.",
                {t for q in qids for t in ctx.tables_of.get(q, ())},
                qids,
                wave,
            )
        )
    return out


# ---------------------------------------------------------------------------
# The cache layer
# ---------------------------------------------------------------------------


def _cache_risks(
    ctx: _Context,
    cache_overlay: Mapping[str, Any] | None,
    schema_designs: Mapping[str, Mapping[str, Any]],
    cache_only_engines: Iterable[str],
) -> list[dict]:
    out: list[dict[str, Any]] = []
    overlay = cache_overlay or {}
    dropped = sorted({str(q) for q in overlay.get("dropped_query_ids") or [] if q})
    cache = str(overlay.get("engine") or "") or None
    if dropped:
        owners = sorted({ctx.engine_of[q] for q in dropped if q in ctx.engine_of})
        wave = ctx.wave_of_engine.get(cache or "")
        target = _names(owners) or "their owner engines"
        out.append(
            _risk(
                "cache.dropped",
                cache,
                f"{_n(len(dropped), 'read', 'reads')} the plan meant to cache "
                f"{'was' if len(dropped) == 1 else 'were'} dropped from the cache layer and "
                f"{'goes' if len(dropped) == 1 else 'go'} to {target} uncached.",
                f"Load-test these reads on {target} without the cache before the "
                f"{_gate(wave)}, or add a cache design that serves them.",
                {t for q in dropped for t in ctx.tables_of.get(q, ())},
                dropped,
                wave,
            )
        )
    for engine in sorted({canonical_engine_id(e) for e in cache_only_engines}):
        design = schema_designs.get(engine) or {}
        patterns = [up for up in design.get("unsupported_patterns") or [] if isinstance(up, dict)]
        if not patterns:
            continue
        wave = ctx.wave_of_engine.get(engine)
        qids = {q for up in patterns for q in unsupported_pattern_ids(up)}
        details = [_detail_of_pattern(up) for up in patterns]
        k = len(patterns)
        out.append(
            _risk(
                "cache.unsupported",
                engine,
                f"The cache design marks {_n(k, 'pattern', 'patterns')} unsupported; each "
                "one's workaround is in the engineering report.",
                f"Serve {_these(k, 'pattern', 'patterns')} from {'its' if k == 1 else 'their'} owner engine or "
                f"add {'it' if k == 1 else 'them'} to the cache design before the "
                f"{_gate(wave)}, as the design's workaround for each says.",
                {t for q in qids for t in ctx.tables_of.get(q, ())},
                qids,
                wave,
                details=details,
            )
        )
    return out


def _detail_of_pattern(up: Mapping[str, Any]) -> str:
    """One unsupported pattern in full: its label, reason and workaround."""
    label = unsupported_pattern_label(dict(up))
    reason = _item(str(up.get("reason") or "")) or "not served by the cache design"
    fix = _item(str(up.get("workaround") or up.get("recommendation") or ""))
    return f"{label}: {reason}" + (f". Workaround: {fix}" if fix else "")


# ---------------------------------------------------------------------------
# Schema designs: application-layer logic, hot partitions, trade-offs
# ---------------------------------------------------------------------------


def _app_layer_risks(
    ctx: _Context,
    schema_designs: Mapping[str, Mapping[str, Any]],
    trigger_tables: Mapping[str, str],
) -> list[dict]:
    """One risk per engine: the source objects whose logic moves to the application."""
    out: list[dict[str, Any]] = []
    for engine in sorted(schema_designs):
        notes = [
            n
            for n in (schema_designs[engine] or {}).get("app_layer_notes") or []
            if isinstance(n, dict)
        ]
        if not notes:
            continue
        by_feature: dict[str, list[str]] = {}
        for note in notes:
            feature = str(note.get("feature") or "object").strip().replace("_", " ").lower()
            by_feature.setdefault(feature or "object", []).append(
                str(note.get("source_object") or "").strip()
            )
        # Only a collected object is named (the executive report strips the
        # collected names); a model-written name stays out of the text.
        named = sorted({o for objs in by_feature.values() for o in objs if o in trigger_tables})
        kinds = [
            _n(len(objs), feature, feature if feature.endswith("s") else f"{feature}s")
            for feature, objs in sorted(by_feature.items())
        ]
        k = len(notes)
        wave = ctx.wave_of_engine.get(engine)
        listed = f" ({', '.join(named)})" if named else ""
        out.append(
            _risk(
                "design.app_layer",
                engine,
                f"{_n(k, 'source object needs', 'source objects need')} application code on "
                f"{display_engine(engine)}{listed}: {_listed(kinds)}. The schema carries "
                f"{_them(k)} over without the database logic.",
                f"Move the logic of each one into the application before the {_gate(wave)}, "
                "as the schema design's note for it says.",
                {trigger_tables[o] for o in named},
                [],
                wave,
            )
        )
    return out


def _hot_partition_risks(
    ctx: _Context,
    schema_designs: Mapping[str, Mapping[str, Any]],
    normalise: Callable[[Iterable[str]], set[str]],
) -> list[dict]:
    out: list[dict[str, Any]] = []
    for engine in sorted(schema_designs):
        design = schema_designs[engine] or {}
        hot = [
            h
            for h in design.get("hot_partition_analysis") or []
            if isinstance(h, dict) and h.get("at_risk")
        ]
        if not hot:
            continue
        sources = {
            str(t.get("table_name")): normalise(t.get("source_tables") or [])
            for t in design.get("table_definitions") or []
            if isinstance(t, dict) and t.get("table_name")
        }
        peak = max(float(h.get("utilization_pct") or 0.0) for h in hot)
        tables = sorted({str(h.get("table_name")) for h in hot if h.get("table_name")})
        qids = {str(q) for h in hot for q in h.get("contributing_patterns") or [] if q}
        wave = ctx.wave_of_engine.get(engine)
        out.append(
            _risk(
                "design.hot_partition",
                engine,
                f"{_n(len(tables), 'design table has', 'design tables have')} a partition above "
                f"80% of its throughput limit at the designed peak (the highest at "
                f"{peak:g}%).",
                f"Spread the load on these partitions before the {_gate(wave)} (write sharding "
                "or a different partition key), and load-test them at peak.",
                {s for t in tables for s in sources.get(t, set())},
                qids,
                wave,
                severity="HIGH" if peak >= HOT_PARTITION_HIGH_PCT else None,
            )
        )
    return out


def _trade_off_risks(
    ctx: _Context,
    trade_offs: list[dict[str, Any]] | None,
    normalise: Callable[[Iterable[str]], set[str]],
) -> list[dict]:
    """One MEDIUM "review the design trade-offs" risk per engine, listing them.

    A trade-off is model prose with no validated category, so its words never
    choose a severity or a specific risk (a keyword reads "no stale data" as
    stale data). Its engine id is normalised first: a designer can write
    ``aurora-mysql`` and ``aurora_mysql`` in one design.
    """
    by_engine: dict[str, dict[str, Mapping[str, Any]]] = {}
    for t in trade_offs or []:
        if not isinstance(t, dict):
            continue
        engine = canonical_engine_id(str(t.get("engine") or ""))
        title = _item(str(t.get("description") or ""))
        if not engine or engine == "reality-check" or not title:
            continue
        by_engine.setdefault(engine, {}).setdefault(title, t)
    out: list[dict[str, Any]] = []
    for engine in sorted(by_engine):
        unique = by_engine[engine]
        n = len(unique)
        wave = ctx.wave_of_engine.get(engine)
        qids = {str(q) for m in unique.values() for q in m.get("query_ids") or [] if q}
        tables = normalise(s for m in unique.values() for s in m.get("source_tables") or [])
        tables |= {tb for q in qids for tb in ctx.tables_of.get(q, ())}
        out.append(
            _risk(
                "design.trade_offs",
                engine,
                f"The {display_engine(engine)} schema design records "
                f"{_n(n, 'trade-off', 'trade-offs')} to review; each one is in the engineering "
                "report.",
                f"Review {_these(n, 'trade-off', 'trade-offs')} with the team that builds "
                f"{display_engine(engine)} before {_start(wave)}, and test each one the "
                "application depends on.",
                tables,
                qids,
                wave,
                details=sorted(unique),
            )
        )
    return out


# A leading review marker a designer puts on a line: "[PE note] ...", "PE note: ...".
_MARKER_RE = re.compile(r"^(?:\[[^\]]{1,30}\]\s*|(?:PE|review) note:\s*)+", re.IGNORECASE)


def _item(text: str) -> str:
    """One detail line in full: one line, its review marker and final full stop gone."""
    return _MARKER_RE.sub("", " ".join(str(text or "").split())).rstrip(" .;")


def _listed(items: list[str]) -> str:
    """Items joined by "; ", the first ``LISTED_ITEMS`` then how many more."""
    shown = list(items[:LISTED_ITEMS])
    more = len(items) - len(shown)
    return "; ".join(shown) + (f"; and {more} more" if more > 0 else "")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def build_migration_risks(
    *,
    migration_waves: list[dict[str, Any]] | None,
    query_assignments: list[dict[str, Any]] | None,
    source_queries: list[dict[str, Any]] | None,
    cache_overlay: Mapping[str, Any] | None = None,
    query_traces: Mapping[str, Any] | None = None,
    tied_queries: list[dict[str, Any]] | None = None,
    schema_designs: Mapping[str, Mapping[str, Any]] | None = None,
    trade_offs: list[dict[str, Any]] | None = None,
    cache_only_engines: Iterable[str] = (),
    trigger_tables: Mapping[str, str] | None = None,
    normalise: Callable[[Iterable[str]], set[str]] | None = None,
    table_sources: list[dict[str, Any]] | None = None,
    start_id: int = 0,
) -> list[dict[str, Any]]:
    """The migration risks the facts imply, numbered from ``RISK-{start_id + 1}``.

    ``schema_designs`` are the raw schema-design outputs by engine;
    ``trade_offs`` the schema-design trade-offs synthesis collected;
    ``cache_only_engines`` the engines that only front reads (never an
    ``assigned_engine``); ``trigger_tables`` maps a collected trigger's name
    to its table; ``normalise`` maps table names to the collector's ids;
    ``table_sources`` is report.json's ``table_sources`` (None for an
    assignment written before write groups).
    """
    norm = normalise or (lambda names: {str(n) for n in names if n})
    # One engine id per engine, whichever variant an artifact wrote (#950).
    designs: dict[str, Mapping[str, Any]] = {}
    for engine, design in (schema_designs or {}).items():
        designs.setdefault(canonical_engine_id(engine), design)
    ctx = _context(migration_waves or [], query_assignments or [], source_queries or [], norm)
    risks = [
        *_sync_risks(ctx, table_sources, norm),
        *_unservable_risks(ctx, query_traces),
        *_tie_risks(ctx, tied_queries),
        *_close_call_risks(ctx, query_traces),
        *_cache_risks(ctx, cache_overlay, designs, cache_only_engines),
        *_app_layer_risks(ctx, designs, trigger_tables or {}),
        *_hot_partition_risks(ctx, designs, norm),
        *_trade_off_risks(ctx, trade_offs, norm),
    ]
    numbered = []
    for i, risk in enumerate(risks, start=start_id + 1):
        numbered.append({"risk_id": f"RISK-{i:03d}", **risk})
    return numbered


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
            _join([str(r["risk_id"]) for r in items])
            if len(items) <= GATE_IDS_NAMED
            else f"the {len(items)} Wave {wave} risks" if wave else f"the {len(items)} risks"
        )
        labels: list[str] = []
        for r in items:
            label = RULES[r["rule"]].label
            if label not in labels:
                labels.append(label)
        head = f"Before the Wave {wave} gate" if wave else "Before cutover"
        lines.append(f"{head}, close {ids} ({_join(labels)}).")
    return lines


def add_migration_risks(risk_assessment: dict[str, Any], risks: list[dict[str, Any]]) -> None:
    """Append ``risks`` to ``risk_assessment``: the open list, the overall level
    and one wave-gate line per wave in ``mitigation_strategies``. In place."""
    if not risks:
        return
    from src.agents.referee.synthesis_report import overall_risk_level

    risk_assessment["risks"] = [*(risk_assessment.get("risks") or []), *risks]
    risk_assessment["overall_risk_level"] = overall_risk_level(risk_assessment["risks"])
    risk_assessment["mitigation_strategies"] = [
        *(risk_assessment.get("mitigation_strategies") or []),
        *wave_gate_strategies(risks),
    ]


def last_risk_number(risks: list[dict[str, Any]]) -> int:
    """The highest ``RISK-NNN`` number in ``risks`` (0 when none)."""
    best = 0
    for r in risks or []:
        m = re.fullmatch(r"RISK-(\d+)", str(r.get("risk_id") or ""))
        if m:
            best = max(best, int(m.group(1)))
    return best
