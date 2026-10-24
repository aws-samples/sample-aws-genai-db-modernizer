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
``routing.flagged``        HIGH      a review found an engine cannot serve queries no other kept
                                     engine can take (``flagged_unservable``, #1148); one risk
                                     per engine
                           MEDIUM    every such query is held there by a customer pin
``routing.no_engine_in_scope``
                           HIGH      no engine in the customer's engine scope can serve queries
                                     (``scope_unservable``, #1329): they have no engine, no wave
                                     and no design, and are never forced onto one; one risk,
                                     at cutover (no wave)
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
``design.redesign``        MEDIUM    queries sit on an engine whose score for them is the
                                     redesigned one (ADR-031 Decision 2 Amendment 13, #1231):
                                     the target design must add the keys routing assumed;
                                     one risk per engine
``design.carried_writes``  MEDIUM    write groups keep members on their table's source of truth
                                     that score below the catalog's floor there (#1362): the
                                     design must serve each one's intent the target's way;
                                     one risk per engine, each statement in its details
``relational.gaps``        HIGH      the relational gap check finds gaps that block the move to
                                     wave 1's engine (``facts.relational_gaps``, #1149)
                           MEDIUM    gaps that need a rewrite before cutover
                           MEDIUM    gaps that behave differently (results change silently);
                                     one risk per impact, in Wave 1, only for a gap check
                                     against wave 1's engine
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

``routing.flagged`` and ``relational.gaps`` read only fields ``report.json``
carries, so they live in ``src.shared.migration_risk_rules`` (with :data:`RULES`
and the wave-gate lines): the deliverables raise them for a report synthesized
before they existed (``backfill_report_risks``), with the same functions
synthesis calls.

Every fact on main the register should read has its rule; a new one is listed in
:data:`PENDING_FACT_HOOKS` until it has its own rule, severity and test.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.agents.referee.cache_overlay import is_write_query
from src.shared.catalog.access_classes import access_classes
from src.shared.design_text import design_text_item, unsupported_pattern_detail
from src.shared.engine_names import canonical_engine_id, display_engine
from src.shared.migration_risk_rules import (
    GATE_IDS_NAMED,
    LISTED_ITEMS,
    RULES,
    RiskRule,
    flagged_unservable_risks,
    last_risk_number,
    listed_items,
    make_risk,
    number_risks,
    relational_gap_risks,
    scope_unservable_risks,
    wave_gate_strategies,
    wave_of_engine,
)
from src.shared.report_facts import CLOSE_CALL_MARGIN, RELATIONAL_ENGINES, wave_facts
from src.shared.risk_severity import overall_risk_level
from src.shared.unsupported_pattern import unsupported_pattern_ids

# A hot partition at or above this utilization is HIGH; below it (but at risk,
# above 80%) it is MEDIUM.
HOT_PARTITION_HIGH_PCT = 95.0


# Facts the risk register does not read yet. Each needs its own rule above
# (severity, test); until then nothing reads the field. Empty since #1149.
PENDING_FACT_HOOKS: dict[str, str] = {}


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
    source_engine: str,
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
    writes = {q for q in engine_of if is_write_query(collected.get(q) or {}, source_engine)}
    # A wave with no number is left out: no risk can name its gate.
    waves = [w for w in migration_waves or [] if isinstance(w, dict) and _wave_number(w)]
    retained = frozenset(
        str(e) for w in waves if w.get("homogeneity") is not None for e in w.get("engines") or []
    )
    return _Context(
        waves=waves,
        engine_of=engine_of,
        tables_of=tables_of,
        writes=writes,
        wave_of_engine=wave_of_engine(waves),
        retained=retained or frozenset(RELATIONAL_ENGINES),
    )


_risk = make_risk


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
            # #1410: close calls and settled ties counted apart, as the Close calls
            # section counts them, so neither count reads as the other.
            pair_ties = sum(1 for r in members if r.get("settled_tie"))
            won = [
                _n(k, label, f"{label}s")
                for k, label in ((len(members) - pair_ties, "close call"), (pair_ties, "tie"))
                if k
            ]
            parts.append(
                f"{display_engine(engine)} {'keeps' if kept else 'takes'} {_join(won)} over "
                f"{display_engine(alternative)}"
            )
            qids += [str(r["query_id"]) for r in members]
        total = len(qids)
        ties = sum(1 for p in pairs.values() for r in p if r.get("settled_tie"))
        close = total - ties
        head = f"Wave {wave}: " if wave else ""
        decided = (
            f"{_n(close, 'query was', 'queries were')} decided by less than {margin} points"
            if close
            else ""
        )
        if ties:
            tied = (
                f"{ties} more {'was an exact tie' if ties == 1 else 'were exact ties'}"
                if close
                else f"{_n(ties, 'query was an exact tie', 'queries were exact ties')}"
            )
            decided = (f"{decided}, and " if decided else "") + f"{tied} a routing rule settled"
        description = (
            f"{head}{decided}: {'; '.join(parts)}. A small change in the scores moves "
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
        details = [unsupported_pattern_detail(up) for up in patterns]
        k = len(patterns)
        out.append(
            _risk(
                "cache.unsupported",
                engine,
                f"The cache design marks {_n(k, 'pattern', 'patterns')} unsupported; the "
                "engineering report lists each one with its workaround under Target schemas by "
                "engine.",
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
                f"{display_engine(engine)}{listed}: {listed_items(kinds)}. The schema carries "
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
                f"{_n(n, 'trade-off', 'trade-offs')} to review, listed under Migration "
                "trade-offs in the engineering report.",
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


def _redesign_risks(
    ctx: _Context, redesign_requirements: Iterable[Mapping[str, Any]] | None
) -> list[dict]:
    """One ``design.redesign`` risk per engine: the queries it serves whose plan relies on
    a redesign (#1231, ``query_pattern.redesign_requirements`` with ``routing_depends``:
    the engine's redesigned score won, or a lifted finding stopped costing it points),
    each with the ``engine`` serving it. The plan holds only if that engine's design gives
    each query the keys routing assumed. A requirement the engine's plan does not rely on
    (it scores the query as well as written) is an index the design may add, not a gate;
    a requirement for another engine than the one serving the query raises nothing."""
    by_engine: dict[str, dict[str, Mapping[str, Any]]] = {}
    for r in redesign_requirements or []:
        if not isinstance(r, Mapping):
            continue
        qid, engine = str(r.get("query_id") or ""), canonical_engine_id(str(r.get("engine") or ""))
        if not qid or not engine or ctx.engine_of.get(qid) != engine:
            continue
        if not r.get("routing_depends", True):
            continue  # the engine scores the query as well as written: an index, no gate
        by_engine.setdefault(engine, {})[qid] = r
    out: list[dict[str, Any]] = []
    for engine in sorted(by_engine):
        queries = by_engine[engine]
        k = len(queries)
        keys = sorted(
            {
                str(key.get("column"))
                for r in queries.values()
                for key in r.get("assumed_keys") or []
            }
        )
        unknown = sorted(
            {
                str(key.get("column"))
                for r in queries.values()
                for key in r.get("assumed_keys") or []
                if not isinstance(key.get("cardinality"), int)
            }
        )
        lifted = sorted(
            {str(p) for r in queries.values() for p in r.get("lifted_anti_patterns") or []}
        )
        wave = ctx.wave_of_engine.get(engine)
        name = display_engine(engine)
        lifted_note = (
            f" The redesign also removes the {_join(lifted)} "
            f"{'finding' if len(lifted) == 1 else 'findings'} the source shows."
            if lifted
            else ""
        )
        cardinality_note = (
            f" {_n(len(unknown), 'key has', 'keys have')} no cardinality statistics."
            if unknown
            else ""
        )
        details = [d for qid in sorted(queries) if (d := _redesign_detail(queries[qid]))]
        if keys:
            body = (
                f": it scored {_them(k)} with {_n(len(keys), 'key', 'keys')} the source does "
                f"not have, so the plan holds only if the {name} design adds "
                f"{'it' if len(keys) == 1 else 'them'}.{lifted_note}{cardinality_note}"
            )
        else:
            finding = "finding" if len(lifted) == 1 else "findings"
            body = ""
        description = (
            f"Routing assumes a redesign for {_n(k, 'query', 'queries')} on {name}{body}"
            if keys
            else f"Routing did not penalise {name} for the {_join(lifted)} {finding} the "
            f"source shows on {_n(k, 'query', 'queries')}, because the design removes the "
            "read it is about (each query's evidence says how)."
        )
        out.append(
            _risk(
                "design.redesign",
                engine,
                description,
                (
                    f"Give each of {_these(k, 'query', 'queries')} the keys routing assumed in "
                    f"the {name} design before the {_gate(wave)}, check a key with few or "
                    "unknown distinct values for hot partitions, and serve none of them as a "
                    "scan."
                    if keys
                    else f"Design {_these(k, 'query', 'queries')} in {name} as each one's "
                    f"evidence says before the {_gate(wave)}, and serve none of them as a scan."
                ),
                {t for q in queries for t in ctx.tables_of.get(q, ())},
                queries,
                wave,
                details=sorted(set(details)),
            )
        )
    return out


def _redesign_detail(r: Mapping[str, Any]) -> str:
    """One query's redesign as sentences (#1410): the key it gains and the read it
    becomes, then its evidence; a query that gains no key states only what it reads
    as, or its evidence. Access-pattern classes are named in words, never by id."""
    columns = ", ".join(str(key.get("column")) for key in r.get("assumed_keys") or [])
    after = r.get("class_after_redesign")
    read_as = (
        f"read as {_class_words(after)} instead of {_class_words(r.get('class_as_written'))}"
        if after
        else ""
    )
    head = f"A key on {columns}" + (f": {read_as}" if read_as else "") if columns else read_as
    sentences = [head[:1].upper() + head[1:]] if head else []
    sentences += [_sentence(text) for e in r.get("evidence") or [] if (text := _item(str(e)))]
    return ". ".join(sentences) + "." if sentences else ""


def _sentence(text: str) -> str:
    """``text`` with a capital first letter, unless its first word is an identifier
    (``wp_posts``, ``c.region``), which is printed as written."""
    first = text.split(" ", 1)[0]
    return text[:1].upper() + text[1:] if re.fullmatch(r"[a-z]+", first) else text


def _class_words(cls: Any) -> str:
    """An access-pattern class by its catalog label, in running text ("key-scoped 1:N
    read"), never its id (#1410)."""
    text = str(cls or "").strip()
    label = next((c.label for c in access_classes() if c.id == text), "")
    return label[:1].lower() + label[1:] if label else text.replace("_", " ")


def _score_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _carried_write_risks(
    ctx: _Context, carried_writes: Iterable[Mapping[str, Any]] | None
) -> list[dict]:
    """One ``design.carried_writes`` risk per engine: the write-group members it serves
    poorly (#1362, ``carried_writes.carried_write_records``), counted in the description
    by intent, each one in full in ``details`` (its statement, table, intent, score and
    the target-native approach). A table has one writer (ADR-031 Decision 1 Amendment 9),
    so routing keeps them there; the design must not replay their set-based SQL. A record
    whose query the plan no longer places on that engine raises nothing."""
    by_engine: dict[str, list[Mapping[str, Any]]] = {}
    for r in carried_writes or []:
        if not isinstance(r, Mapping):
            continue
        qid = str(r.get("query_id") or "")
        engine = canonical_engine_id(str(r.get("engine") or ""))
        if not qid or not engine or ctx.engine_of.get(qid) != engine:
            continue
        by_engine.setdefault(engine, []).append(r)
    out: list[dict[str, Any]] = []
    for engine in sorted(by_engine):
        rows = sorted(
            by_engine[engine], key=lambda r: (str(r.get("table_id")), str(r.get("query_id")))
        )
        name = display_engine(engine)
        wave = ctx.wave_of_engine.get(engine)
        k = len(rows)
        tables = sorted({str(r.get("table_id") or "") for r in rows} - {""})
        scores = [float(r.get("score") or 0) for r in rows]
        floor = _score_text(min(float(r.get("floor") or 0) for r in rows))
        low, high = _score_text(min(scores)), _score_text(max(scores))
        scored = f"score {low}" if low == high else f"scores {low} to {high}"
        counts: dict[str, int] = {}
        for r in rows:
            intent = str(r.get("intent_name") or "").lower()
            if intent:
                counts[intent] = counts.get(intent, 0) + 1
        intents = [f"{i} ({n})" for i, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        description = (
            f"{name} serves {_n(k, 'statement', 'statements')} on "
            f"{_n(len(tables), 'table', 'tables')} poorly ({scored}, below {floor}): "
            f"{_join(intents)}. A table has one writer, so each moves with the other "
            "statements that write its table."
        )
        mitigation = (
            f"Before the {_gate(wave)}, design {_these(k, 'statement', 'statements')} in "
            f"{name} for what {'it is' if k == 1 else 'they are'} for, the target's own way, "
            "not as a copy of the source's set-based SQL; each statement's detail names the "
            "approach."
        )
        details = [
            f"{str(r.get('statement') or 'Statement').upper()} of "
            f"{str(r.get('table_id') or '').rsplit('.', 1)[-1]}, "
            f"{str(r.get('intent_name') or '').lower()} (score "
            f"{_score_text(float(r.get('score') or 0))}): {_item(str(r.get('approach') or ''))}"
            for r in rows
        ]
        out.append(
            _risk(
                "design.carried_writes",
                engine,
                description,
                mitigation,
                tables,
                [str(r["query_id"]) for r in rows],
                wave,
                details=details,
            )
        )
    return out


def _item(text: str) -> str:
    """One detail line in full (``design_text_item``)."""
    return design_text_item(text)


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
    flagged_unservable: list[dict[str, Any]] | None = None,
    relational_gaps: Mapping[str, Any] | None = None,
    scope_unservable: list[dict[str, Any]] | None = None,
    redesign_requirements: Iterable[Mapping[str, Any]] | None = None,
    carried_writes: Iterable[Mapping[str, Any]] | None = None,
    start_id: int = 0,
    source_engine: str,
) -> list[dict[str, Any]]:
    """The migration risks the facts imply, numbered from ``RISK-{start_id + 1}``.

    ``schema_designs`` are the raw schema-design outputs by engine;
    ``trade_offs`` the schema-design trade-offs synthesis collected;
    ``cache_only_engines`` the engines that only front reads (never an
    ``assigned_engine``); ``trigger_tables`` maps a collected trigger's name
    to its table; ``normalise`` maps table names to the collector's ids;
    ``table_sources`` is report.json's ``table_sources`` (None for an
    assignment written before write groups); ``flagged_unservable`` its
    ``flagged_unservable`` rows (#888, #1148); ``relational_gaps`` the gap check
    ``facts.relational_gaps`` stores (#1079, #1149); ``scope_unservable`` report.json's
    ``scope_unservable`` rows (#1329); ``redesign_requirements``
    the queries routing scored after redesign, each with its engine (#1231);
    ``carried_writes`` the write-group members their engine serves poorly (#1362);
    ``source_engine`` is the
    dialect the queries' text is read in.
    """
    norm = normalise or (lambda names: {str(n) for n in names if n})
    # One engine id per engine, whichever variant an artifact wrote (#950).
    designs: dict[str, Mapping[str, Any]] = {}
    for engine, design in (schema_designs or {}).items():
        designs.setdefault(canonical_engine_id(engine), design)
    ctx = _context(
        migration_waves or [], query_assignments or [], source_queries or [], norm, source_engine
    )
    risks = [
        *_sync_risks(ctx, table_sources, norm),
        *_unservable_risks(ctx, query_traces),
        *_tie_risks(ctx, tied_queries),
        *_close_call_risks(ctx, query_traces),
        *_cache_risks(ctx, cache_overlay, designs, cache_only_engines),
        *_app_layer_risks(ctx, designs, trigger_tables or {}),
        *_hot_partition_risks(ctx, designs, norm),
        *_trade_off_risks(ctx, trade_offs, norm),
        *_redesign_risks(ctx, redesign_requirements),
        *_carried_write_risks(ctx, carried_writes),
        # Last: a report written before this rule gets it appended at render time
        # (``backfill_report_risks``), so both number and order it the same way.
        *flagged_unservable_risks(
            {"flagged_unservable": flagged_unservable or [], "query_traces": query_traces or {}},
            ctx.wave_of_engine,
            ctx.tables_of,
        ),
        *relational_gap_risks(relational_gaps, ctx.waves),
        # #1329: the queries no engine in the customer's scope can serve, last, as the
        # render-time backfill (``report_fact_risks``) orders them.
        *scope_unservable_risks({"scope_unservable": scope_unservable or []}),
    ]
    return number_risks(risks, start_id)


def add_migration_risks(risk_assessment: dict[str, Any], risks: list[dict[str, Any]]) -> None:
    """Append ``risks`` to ``risk_assessment``: the open list, the overall level
    and one wave-gate line per wave in ``mitigation_strategies``. In place."""
    if not risks:
        return
    risk_assessment["risks"] = [*(risk_assessment.get("risks") or []), *risks]
    risk_assessment["overall_risk_level"] = overall_risk_level(risk_assessment["risks"])
    risk_assessment["mitigation_strategies"] = [
        *(risk_assessment.get("mitigation_strategies") or []),
        *wave_gate_strategies(risks),
    ]


__all__ = [
    "GATE_IDS_NAMED",
    "LISTED_ITEMS",
    "RULES",
    "RiskRule",
    "add_migration_risks",
    "build_migration_risks",
    "last_risk_number",
    "wave_gate_strategies",
]
