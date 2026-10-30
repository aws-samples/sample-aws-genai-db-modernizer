# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
``query_ids`` as evidence, and a mitigation that points at that wave's gate
(``by_gate``: "See the Wave 3 gate in the roadmap."). :func:`add_migration_risks`
ends each wave's gate in the roadmap with the risks to close before it (#1510:
the roadmap owns the gate, so no mitigation or strategy states it again).

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
                           LOW       the wave's only copies are of tables no captured query
                                     writes: a one-time copy, both ways (a table the wave reads
                                     from another owner, ``facts.waves[].kept_in_sync``, and one
                                     the wave owns that another engine reads); with CDC copies
                                     too, the risk states the total the two counts add up to
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
``design.app_layer``       MEDIUM    a design notes source objects whose logic moves into the
                                     application (an extension, or any object on an engine
                                     that is not wave 1's for a homogeneous source); one
                                     risk per engine
                           LOW       every noted object is a trigger or routine on wave 1's
                                     engine for a homogeneous source: it carries over with
                                     the schema and is tested after cutover (#1642)
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
from dataclasses import dataclass, field
from typing import Any

from src.agents.referee.cache_overlay import is_write_query
from src.shared.catalog.access_classes import access_classes
from src.shared.design_text import (
    carried_write_detail,
    close_call_description,
    design_text_item,
    unsupported_pattern_detail,
)
from src.shared.engine_names import canonical_engine_id, display_engine
from src.shared.migration_risk_rules import (
    GATE_IDS_NAMED,
    LISTED_ITEMS,
    RULES,
    RiskRule,
    add_gate_lines,
    by_gate,
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
from src.shared.report_facts import (
    CLOSE_CALL_MARGIN,
    RELATIONAL_ENGINES,
    non_owner_read_tables_by_wave,
    wave_facts,
)
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
    copy_once: set[str]  # read on an engine that does not own it, written by no captured query
    queries: set[str]
    split_queries: set[str] = field(default_factory=set)  # the split tables' writes


def _sync_risks(
    ctx: _Context,
    table_sources: list[dict[str, Any]] | None,
    normalise: Callable[[Iterable[str]], set[str]],
    copy_lines: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if copy_lines is not None and table_sources is not None:
        return _sync_risks_from_lines(ctx, table_sources, normalise, list(copy_lines))
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
                    writes = {str(q) for q in row.get("write_query_ids") or [] if q}
                    s.queries |= writes
                    s.split_queries |= writes
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
        if table_sources is not None:
            _owned_copies_read_elsewhere(ctx, w, engines, written | covered, slot)
    out: list[dict[str, Any]] = []
    for n in sorted(by_wave):
        out.append(_wave_sync_risk(ctx, n, by_wave[n]))
    return out


def _sync_risks_from_lines(
    ctx: _Context,
    table_sources: list[dict[str, Any]],
    normalise: Callable[[Iterable[str]], set[str]],
    lines: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The sync risks from the tables' owner lines (#1642): each wave's tables read off
    their owner are ``non_owner_read_tables_by_wave``'s, the computation the facts
    layer's ``also_read_table_count`` adds up, so the risk and the count never disagree.
    A written table is a CDC copy from its owner to each reader, one written by no
    captured query a one-time copy; a table with no single writer keeps its own
    sentence (two-way sync), as before. The evidence is the readers' queries."""
    by_wave: dict[int, _WaveSync] = {}

    def slot(n: int) -> _WaveSync:
        return by_wave.setdefault(n, _WaveSync(set(), set(), {}, set(), set(), set()))

    rows = {
        next(iter(normalise([str(r["table"])])), str(r["table"])): r
        for r in table_sources
        if isinstance(r, dict) and r.get("table")
    }
    readers_of = {
        next(iter(normalise([str(line["table"])])), str(line["table"])): [
            str(e) for e in line.get("also_read_by") or [] if e
        ]
        for line in lines
        if line.get("table")
    }
    owner_of = {
        next(iter(normalise([str(line["table"])])), str(line["table"])): str(
            line.get("owner") or ""
        )
        for line in lines
        if line.get("table")
    }
    per_wave = non_owner_read_tables_by_wave(lines, ctx.wave_of_engine)
    total = sum(len(t) for t in per_wave.values())
    for n, tables in per_wave.items():
        resolved = sorted(normalise(tables))
        if n is None or not resolved:
            continue  # no wave, or no collected table left to copy: nothing to raise
        s = slot(n)
        for table in resolved:
            row = rows.get(table)
            readers = readers_of.get(table) or []
            reads = {
                q
                for q, e in ctx.engine_of.items()
                if e in readers and table in ctx.tables_of.get(q, set())
            }
            if row is not None and not row.get("source_of_truth"):
                writers = {str(e) for e in row.get("write_engines") or [] if e}
                s.split.add(table)
                s.split_writers |= writers
                writes = {str(q) for q in row.get("write_query_ids") or [] if q}
                s.queries |= writes
                s.split_queries |= writes
                continue
            replicas = [
                r
                for r in (row or {}).get("replicas") or []
                if isinstance(r, dict) and r.get("engine")
            ]
            if row is None:
                s.copy_once.add(table)
                s.queries |= reads
                continue
            if not replicas:
                # Written, but its readers are not recorded as replicas: CDC to each.
                replicas = [
                    {"engine": e, "read_query_ids": [q for q in reads if ctx.engine_of[q] == e]}
                    for e in readers
                ]
            # A written table: CDC from its source of truth to each replica, with the
            # replica's reads (``table_sources``, #992).
            source = str(row.get("source_of_truth") or owner_of.get(table) or "")
            for replica in replicas:
                replica_reads = {str(q) for q in replica.get("read_query_ids") or [] if q}
                pair = s.pairs.setdefault((source, str(replica["engine"])), (set(), set()))
                pair[0].add(table)
                pair[1].update(replica_reads)
                s.queries |= replica_reads
    migrating = sum(1 for w in ctx.waves if wave_facts(w)["migrating"])
    return [
        _wave_sync_risk(ctx, n, by_wave[n], plan_total=total if migrating > 1 else None)
        for n in sorted(by_wave)
        if by_wave[n].pairs or by_wave[n].copy_once or by_wave[n].split
    ]


def _owned_copies_read_elsewhere(
    ctx: _Context,
    wave: Mapping[str, Any],
    engines: list[str],
    skip: set[str],
    slot: Callable[[int], _WaveSync],
) -> None:
    """The other direction of a wave's copies: the tables its own engines own that a
    query on another engine reads, written by no captured query (``skip`` is every
    written table), go to the later of the two engines' waves as a one-time copy, the
    rule the CDC pairs follow, with the reads as evidence. Without this the risk counted
    only the copies the wave reads (the discourse judge's "81 tables" plus "37 more"
    against the gate's 70 dual-read and 64 kept-in-sync tables)."""
    owners = {str(o.get("table")) for o in _wave_owner_entries(wave)}
    owned = {str(t) for t in wave.get("tables") or []} - owners
    here = set(engines)
    n = int(wave_facts(dict(wave))["wave"])
    for q, engine in ctx.engine_of.items():
        if engine in here:
            continue
        hit = (ctx.tables_of.get(q, set()) & owned) - skip
        if not hit:
            continue
        other = ctx.wave_of_engine.get(engine)
        target = slot(max(n, other) if other else n)
        target.copy_once |= hit
        target.queries.add(q)


def _wave_owner_entries(wave: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [o for o in wave.get("table_owners") or [] if isinstance(o, Mapping) and o.get("table")]


def _wave_sync_risk(
    ctx: _Context, n: int, s: _WaveSync, plan_total: int | None = None
) -> dict[str, Any]:
    """``plan_total``, when a plan has several migrating waves, is every table read off
    its owner across the plan: the risk then says how many this wave's gate brings in."""
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
    # One-time copies are only the tables no CDC pair already carries.
    once = s.copy_once - synced - s.split
    both = bool(s.pairs) and bool(once)
    if s.pairs:
        reads = set().union(*(r for _, r in s.pairs.values()))
        k, m, pairs = len(synced), len(reads), len(s.pairs)
        sources = sorted({src for src, _ in s.pairs})
        across = f"across {_n(pairs, 'engine pair', 'engine pairs')}"
        if both:
            # Both kinds of copy: every query and table first (the query count is the
            # risk's evidence, ``query_ids``), then the ones on a CDC copy, so the
            # counts add up (the judge read "435 reads" against 531 query ids).
            readers = len(s.queries - (s.split_queries - reads))
            sentences.append(
                f"{_n(readers, 'query reads', 'queries read')} {len(synced | once)} tables "
                "on an engine that does not own them."
            )
            sentences.append(
                f"{m} of them {'reads' if m == 1 else 'read'} a copy of "
                f"{_n(k, 'table', 'tables')} that CDC keeps in sync from {_names(sources)} "
                f"{across}, so a read there can miss a recent write."
            )
        else:
            sentences.append(
                f"{_n(m, 'read runs', 'reads run')} on a copy of {_n(k, 'table', 'tables')} "
                f"that CDC keeps in sync from {_names(sources)} {across}, so a read there "
                "can miss a recent write."
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
    if once:
        k = len(once)
        if both:
            other = "The other table is" if k == 1 else f"The other {k} tables are"
            sentences.append(
                f"{other} written by no captured query, so a one-time copy serves {_them(k)}."
            )
        else:
            sentences.append(
                f"{_n(k, 'more table is', 'more tables are')} read on an engine that does not "
                f"own {_them(k)} and written by no captured query, so a one-time copy serves "
                f"{_them(k)}."
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
    if plan_total is not None:
        mine = len(synced | once | s.split | s.kept)
        sentences.append(
            f"Across the plan, {plan_total} tables are read on an engine that does not own "
            f"them; this wave's gate brings in {mine}."
        )
    mitigation = (
        f"Before Wave {n} starts, "
        + "; ".join(steps)
        + ". "
        + by_gate("Compare the row counts on both sides", n)
    )
    return _risk(
        "sync.wave",
        _wave_tag(ctx, n),
        " ".join(sentences),
        mitigation,
        s.split | synced | s.kept | once,
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
        description = close_call_description(f"{head}{decided}", parts, _them(total))
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
                by_gate(
                    f"Redesign these queries or accept {name} for them, then re-run the "
                    "assignment review",
                    wave,
                ),
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
                by_gate(
                    f"Load-test these reads on {target} without the cache, or add a cache "
                    "design that serves them",
                    wave,
                ),
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
                by_gate(
                    f"Serve {_these(k, 'pattern', 'patterns')} from "
                    f"{'its' if k == 1 else 'their'} owner engine or add "
                    f"{'it' if k == 1 else 'them'} to the cache design, as the design's "
                    "workaround for each says",
                    wave,
                ),
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


# Source logic a source-compatible engine runs as the source does: wave 1 moves a
# homogeneous source 1:1, so these carry over with the schema (#1642).
_CARRIED_FEATURES = frozenset({"trigger", "function", "procedure", "stored procedure", "routine"})


def _homogeneous_engines(ctx: _Context) -> set[str]:
    return {
        str(e)
        for w in ctx.waves
        if w.get("homogeneity") == "homogeneous"
        for e in w.get("engines") or []
    }


def _app_layer_risks(
    ctx: _Context,
    schema_designs: Mapping[str, Mapping[str, Any]],
    trigger_tables: Mapping[str, str],
) -> list[dict]:
    """One risk per engine for the source objects its design notes. On wave 1's engine
    for a homogeneous source a trigger or routine carries over with the schema (#1642):
    LOW, resting on the engine family alone (the collector reports no feature
    compatibility), and the risk defers to the design's note for any application work,
    never stating an outcome the note may contradict. Any other noted object needs
    application code: MEDIUM."""
    out: list[dict[str, Any]] = []
    same_family = _homogeneous_engines(ctx)
    for engine in sorted(schema_designs):
        notes = [
            n
            for n in (schema_designs[engine] or {}).get("app_layer_notes") or []
            if isinstance(n, dict)
        ]
        if not notes:
            continue
        carried: dict[str, list[str]] = {}
        coded: dict[str, list[str]] = {}
        for note in notes:
            feature = str(note.get("feature") or "object").strip().replace("_", " ").lower()
            feature = feature or "object"
            into = carried if engine in same_family and feature in _CARRIED_FEATURES else coded
            into.setdefault(feature, []).append(str(note.get("source_object") or "").strip())
        name = display_engine(engine)
        wave = ctx.wave_of_engine.get(engine)
        sentences: list[str] = []
        steps: list[str] = []
        named_all: set[str] = set()
        for group, carries in ((coded, False), (carried, True)):
            if not group:
                continue
            # Only a collected object is named (the executive report strips the
            # collected names); a model-written name stays out of the text.
            named = sorted({o for objs in group.values() for o in objs if o in trigger_tables})
            named_all |= set(named)
            kinds = [
                _n(len(objs), feature, feature if feature.endswith("s") else f"{feature}s")
                for feature, objs in sorted(group.items())
            ]
            k = sum(len(objs) for objs in group.values())
            listed = f" ({', '.join(named)})" if named else ""
            things = _join(sorted(group))
            if carries:
                # The rating rests on the engine family only: the collector reports no
                # feature compatibility, and the design's note says what application work
                # the object needs, so the risk defers to it (the re-review on #1644).
                sentences.append(
                    f"{_n(k, 'source object carries', 'source objects carry')} over to {name} "
                    f"with the schema{listed}: {listed_items(kinds)}. This rests on {name} "
                    "being the source's engine family; the collector reports no feature "
                    "compatibility, so the schema design's note for each says what "
                    "application work it needs."
                )
                steps.append(
                    f"test the {things} on {name} against the source's behaviour and do the "
                    "application work its note names"
                )
            else:
                sentences.append(
                    f"{_n(k, 'source object needs', 'source objects need')} application code "
                    f"on {name}{listed}: {listed_items(kinds)}. The schema carries {_them(k)} "
                    "over without the database logic."
                )
                steps.append(f"move the logic of the {things} into the application")
        if coded and carried:
            mitigation = by_gate(
                f"{steps[0][:1].upper()}{steps[0][1:]}, and {steps[1]}, as the schema "
                "design's note for each says",
                wave,
            )
        elif coded:
            mitigation = by_gate(
                "Move the logic of each one into the application, as the schema design's "
                "note for it says",
                wave,
            )
        else:
            mitigation = by_gate(
                f"Test each one on {name} against the source's behaviour and do the "
                "application work the schema design's note for it names",
                wave,
            )
        out.append(
            _risk(
                "design.app_layer",
                engine,
                " ".join(sentences),
                mitigation,
                {trigger_tables[o] for o in named_all},
                [],
                wave,
                severity=None if coded else "LOW",
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
                by_gate(
                    "Spread the load on these partitions (write sharding or a different "
                    "partition key), and load-test them at peak",
                    wave,
                ),
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
        details = _redesign_details([queries[qid] for qid in sorted(queries)])
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
                by_gate(
                    (
                        f"Give each of {_these(k, 'query', 'queries')} the keys routing "
                        f"assumed in the {name} design, check a key with few or unknown "
                        "distinct values for hot partitions, and serve none of them as a scan"
                        if keys
                        else f"Design {_these(k, 'query', 'queries')} in {name} as each "
                        "one's evidence says, and serve none of them as a scan"
                    ),
                    wave,
                ),
                {t for q in queries for t in ctx.tables_of.get(q, ())},
                queries,
                wave,
                details=sorted(set(details)),
            )
        )
    return out


def _redesign_details(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Each query's redesign as one detail line, except that queries with no evidence of
    their own whose keys change the read the same way are one line naming every key
    ("Keys on a.x and b.y: each read as ..."): the same sentence once per key read as
    repetition (the discourse judge counted one line eight times)."""
    lines: list[str] = []
    shared: dict[str, set[str]] = {}
    for r in rows:
        columns = [str(key.get("column")) for key in r.get("assumed_keys") or []]
        read_as = _read_as(r)
        evidence = [text for e in r.get("evidence") or [] if (text := _item(str(e)))]
        if columns and read_as and not evidence:
            shared.setdefault(read_as, set()).update(columns)
        elif line := _redesign_detail(r):
            lines.append(line)
    for read_as, key_columns in shared.items():
        keys = _join(sorted(key_columns))
        lines.append(
            f"A key on {keys}: {read_as}."
            if len(key_columns) == 1
            else f"Keys on {keys}: each {read_as}."
        )
    return lines


def _read_as(r: Mapping[str, Any]) -> str:
    """The read a redesign makes of a query ("read as X instead of Y"), or ''."""
    after = r.get("class_after_redesign")
    if not after:
        return ""
    return f"read as {_class_words(after)} instead of {_class_words(r.get('class_as_written'))}"


def _redesign_detail(r: Mapping[str, Any]) -> str:
    """One query's redesign as sentences (#1410): the key it gains and the read it
    becomes, then its evidence; a query that gains no key states only what it reads
    as, or its evidence. Access-pattern classes are named in words, never by id."""
    columns = ", ".join(str(key.get("column")) for key in r.get("assumed_keys") or [])
    read_as = _read_as(r)
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
        mitigation = by_gate(
            f"Design {_these(k, 'statement', 'statements')} in {name} for what "
            f"{'it is' if k == 1 else 'they are'} for, the target's own way, not as a copy of "
            "the source's set-based SQL; each statement's detail names the approach",
            wave,
        )
        details = [
            carried_write_detail(
                str(r.get("statement") or "Statement"),
                str(r.get("table_id") or ""),
                str(r.get("intent_name") or ""),
                _score_text(float(r.get("score") or 0)),
                str(r.get("approach") or ""),
            )
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
    copy_lines: Iterable[Mapping[str, Any]] | None = None,
    start_id: int = 0,
    source_engine: str,
) -> list[dict[str, Any]]:
    """The migration risks the facts imply, numbered from ``RISK-{start_id + 1}``.

    ``schema_designs`` are the raw schema-design outputs by engine;
    ``trade_offs`` the schema-design trade-offs synthesis collected;
    ``cache_only_engines`` the engines that only front reads (never an
    ``assigned_engine``); ``trigger_tables`` maps a collected trigger's name
    to its table; ``normalise`` maps table names to the collector's ids;
    ``copy_lines`` are the tables' owner lines (``table_owner_fields``: owner and the
    engines that also read each), when the caller has them: the sync risk then reads its
    per-wave tables from ``report_facts.non_owner_read_tables_by_wave``, the computation
    behind ``table_owner_lines.also_read_table_count`` (#1642, ADR-032).

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
        *_sync_risks(ctx, table_sources, norm, copy_lines),
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


def add_migration_risks(
    risk_assessment: dict[str, Any],
    risks: list[dict[str, Any]],
    migration_waves: list[dict[str, Any]] | None = None,
) -> None:
    """Append ``risks`` to ``risk_assessment``: the open list and the overall level.
    Each wave's gate in ``migration_waves`` ends with the risks to close before it
    (#1510: the roadmap owns the gate); a risk no wave there holds gets its line in
    ``mitigation_strategies``. In place."""
    if not risks:
        return
    risk_assessment["risks"] = [*(risk_assessment.get("risks") or []), *risks]
    risk_assessment["overall_risk_level"] = overall_risk_level(risk_assessment["risks"])
    risk_assessment["mitigation_strategies"] = [
        *(risk_assessment.get("mitigation_strategies") or []),
        *add_gate_lines(migration_waves, risks),
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
