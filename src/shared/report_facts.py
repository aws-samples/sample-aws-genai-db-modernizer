"""One facts layer for every synthesis deliverable (#615, ADR-032).

Synthesis computes every customer-facing figure the deliverables print
(per-engine roles, table and cost figures, per-wave served/moving/kept-in-sync
counts, the table basis behind "N tables mapped", the open and resolved risk
counts, the tied-query count) exactly once, here, and writes the result to
``report.json`` as ``facts`` (``SynthesisOutputContract.facts``). The decision
report, the engineering report, the deck and its PDF, and the interactive
analysis report (the UI export) read those fields and only format them; none
of them adds, subtracts or filters a figure this module owns
(``tests/unit/report/test_renderers_only_format.py`` enforces that).

Before this module, each deliverable derived the same figure on its own, and
the copies drifted: a merged-wave headline subtracted the deduped sync list
while the "Tables migrate" tile summed the per-engine owned counts (#581), and
the deck's "Basis of the analysis" bullet trusted a partial ``table_mappings``
while the tile added the migration-wave fallback (#533).

The input is the ``report.json`` dict itself (every other field synthesis
writes), plus the migration waves it is based on -- passed in, because a
report written before ``migration_waves`` existed resolves them through
``src.report.roadmap.resolve_migration_waves``, which this layer does not
import. ``src.report.facts.report_facts`` is the one reader: it returns the
stored ``facts`` and, for a report synthesized before they existed, calls
:func:`compute_report_facts` -- the same function synthesis calls -- so even
an old report gets one answer per figure.

``src/shared`` imports nothing from ``src/agents`` or ``src/report``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from src.shared import catalog
from src.shared.cache_engines import CACHE_ENGINES
from src.shared.capability_labels import capability_label, flag_redesign_hints, redesign_hints
from src.shared.catalog.gaps import GAP_IMPACTS
from src.shared.engine_names import (
    ENGINE_DISPLAY_NAMES,
    SOURCE_DATABASE,
    SOURCE_ENGINE_DISPLAY_NAMES,
    display_engine,
)
from src.shared.migration_wave_engines import NON_OWNER_ENGINES, primary_owner_engine
from src.shared.risk_severity import (
    SEVERITY_ORDER,
    count_by_severity,
    overall_risk_level,
    severity_of,
    severity_rank,
)
from src.shared.signal_labels import SIGNAL_NOUNS
from src.shared.source_cache import cost_basis_sentence
from src.shared.target_databases import engine_scope_display_order

# ---------------------------------------------------------------------------
# Engine roles
# ---------------------------------------------------------------------------

RETAINED = "Retained"
MIGRATION_TARGET = "Migration target"
CACHE_LAYER = "Cache layer"
SEARCH_READ_MODEL = "Search read model"
EVALUATED = "Evaluated"
ASSESSED = "Assessed"
# A mapped table whose engine has no role in the architecture.
OTHER_ROLE = "Other"
# Roles whose engine needs no data migration of its own.
NO_MIGRATION_ROLES = (RETAINED, CACHE_LAYER)
ROLES = (RETAINED, MIGRATION_TARGET, CACHE_LAYER, SEARCH_READ_MODEL, EVALUATED, ASSESSED)

# Search engines are read models (#303): their data is synced from the engine
# that owns each table, so they never hold system-of-record data, never need a
# "data migration", and come after their owners in the wave plan.
SEARCH_ENGINES = catalog.engine_ids(status=catalog.ACTIVE, role="search")
# Only a relational engine can be the retained, source-compatible core. Legacy ids count
# too (the generic ``aurora`` older reports carry).
RELATIONAL_ENGINES = catalog.engine_ids(status=catalog.REPORTABLE, role="relational")


def engine_role(
    engine: str,
    recommended: set,
    schema_designs: dict[str, Any],
    workload: float | None = None,
) -> str:
    """Role of an engine in the target architecture.

    ``recommended_architecture.databases`` lists only net-new migration targets —
    it drops the retained relational core and cache engines because it filters on
    a ``schema_design_available`` flag that is False for both (defect (d) for the
    cache, which has a completed design the flag does not count; by design for a
    retained engine, which synthesis leaves out whether or not it has a design of
    its own, #949). Roles are therefore derived from the engine kind and its design
    status, not from that list alone.

    An engine carrying **no workload** is ``Evaluated``: triage selected it, analysis
    scored it, and the assignment then routed nothing to it because another engine won
    every query. It is *not* ``Retained`` — retained means the engine still serves
    queries, which is true of the source relational core and false of an engine that
    won none. Calling it Retained told the customer to provision an engine holding
    nothing, and (via ``no_move`` in the old deck renderer) pulled the Wave 1 confidence down
    to a floor set by an engine doing no work.

    Judged on workload rather than on the engine's name because the synthesis report
    carries no ``source_engine`` field. If every query migrated off the source engine it
    would read ``Evaluated`` rather than "retired" — rare, and Evaluated still claims
    nothing false.
    """
    if engine in CACHE_ENGINES:
        return CACHE_LAYER
    if engine in SEARCH_ENGINES and workload:
        # A read model whatever its design status: never "Retained" (#296/#303)
        return SEARCH_READ_MODEL
    if engine in recommended:
        return MIGRATION_TARGET
    # Ordered deliberately: a cache engine or a real migration target keeps its role
    # whatever its workload, and this test precedes the status checks because the
    # ``skipped``/``not_available`` branch below is the one being corrected.
    if not workload:
        return EVALUATED
    if engine in RELATIONAL_ENGINES:
        # #949: carrying queries and not a listed target, a relational engine is the
        # retained, source-compatible core whatever its design status: synthesis lists
        # a heterogeneous source's Aurora engine (a real target) and leaves the retained
        # one out, design or not. An Aurora design of its own made it a "Migration
        # target", so every one of its queries read as moving to a new engine.
        return RETAINED
    status = (schema_designs.get(engine) or {}).get("status")
    if status in ("not_available", "skipped"):
        # Any other engine carrying queries without a design is still a migration target
        return MIGRATION_TARGET
    if status == "completed":
        return MIGRATION_TARGET
    return ASSESSED


def design_source_tables(tables: Any) -> int:
    """Distinct source tables an engine's schema design reads from."""
    if not isinstance(tables, list):
        return 0
    return len(
        {
            str(src)
            for t in tables
            if isinstance(t, dict)
            for src in (t.get("source_tables") or [])
            if isinstance(t.get("source_tables"), list)
        }
    )


def cache_layer_counts(report: dict[str, Any], engine: str) -> tuple[int, float] | None:
    """``(cached queries, % of calls)`` of a cache-layer engine, or None (#296).

    The cache owns no query, so it has no workload share; it is described by the hot
    reads it fronts. ``None`` for a legacy report where the cache owned queries.
    """
    entry = next(
        (
            r
            for r in report.get("ranking") or []
            if isinstance(r, dict) and r.get("target") == engine
        ),
        {},
    )
    overlay = report.get("cache_overlay") or {}
    if entry.get("role") != "cache_layer" and overlay.get("engine") != engine:
        return None
    n = entry.get("cache_overlay_queries", overlay.get("query_count", 0))
    share = entry.get("cache_call_share_percent", overlay.get("call_share_percent", 0.0))
    return int(n or 0), float(share or 0.0)


def cache_layer_facts(report: dict[str, Any]) -> dict[str, Any] | None:
    """The cache layer's two read figures, each labelled for what it counts (#1252, ADR-032).

    ``candidate_*`` is what routing put under the cache overlay (the assignment gate's
    figure); ``designed_*`` is what the cache engine's schema design covers (the figure
    after schema design); ``owner_only_read_count`` is the candidates the design does not
    cover, which stay on their owner engine alone. This is the one source of the cache
    figures and of the change sentence (:func:`src.shared.cache_change.cache_change_sentence`).

    A report written before ``candidate_query_count`` has its candidates as the designed reads
    plus the reads it recorded as dropped (``dropped_query_ids``); their call share is then
    unknown (None) when anything was dropped. None for a report with no ``cache_overlay``.
    """
    overlay = report.get("cache_overlay")
    if not isinstance(overlay, dict):
        return None
    designed = int(overlay.get("query_count") or 0)
    designed_share = float(overlay.get("call_share_percent") or 0.0)
    candidates = overlay.get("candidate_query_count")
    share = overlay.get("candidate_call_share_percent")
    if isinstance(candidates, int) and not isinstance(candidates, bool):
        candidates = max(candidates, designed)
    else:
        dropped = overlay.get("dropped_query_ids")
        candidates = designed + (len(dropped) if isinstance(dropped, list) else 0)
        share = None
    if isinstance(share, (int, float)) and not isinstance(share, bool):
        candidate_share: float | None = float(share)
    else:
        candidate_share = designed_share if candidates == designed else None
    return {
        "engine": overlay.get("engine"),
        "candidate_read_count": candidates,
        "candidate_call_share_percent": candidate_share,
        "designed_read_count": designed,
        "designed_call_share_percent": designed_share,
        "owner_only_read_count": candidates - designed,
    }


def rationale_text(value: Any) -> str:
    """A rationale as text; legacy reports may carry a list of reasons or nothing."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v)
    return ""


# #1474: every cost figure is the whole plan's, not wave 1's, and says what it rests on.
COST_SCOPE_LABEL = "Full-modernization estimate: all waves done, every kept engine running."
COST_CAVEAT = (
    "Estimates from public on-demand list prices for the stated Region. Check them with "
    "your own pricing in the AWS Pricing Calculator (https://calculator.aws/)."
)


def cost_assumptions(report: dict[str, Any]) -> list[str]:
    """The TCO's assumptions as every deliverable states them (#1195): its own
    ``tco_analysis.assumptions`` lines, text only, in order."""
    lines = (report.get("tco_analysis") or {}).get("assumptions") or []
    return [line for line in lines if isinstance(line, str) and line.strip()]


def engine_cost_sizing(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """engine name -> what its TCO figure was sized for (#1195), from cost_breakdown.

    A row with no sizing (the analysis estimate, or a report written before #1195) is
    absent, so its engine states no basis."""
    out: dict[str, dict[str, Any]] = {}
    for row in (report.get("tco_analysis") or {}).get("cost_breakdown") or []:
        if isinstance(row, dict) and row.get("database") and isinstance(row.get("sizing"), dict):
            out[str(row["database"])] = dict(row["sizing"])
    return out


def engine_costs(report: dict[str, Any]) -> dict[str, float]:
    """engine name -> monthly USD, from tco_analysis.cost_breakdown."""
    out: dict[str, float] = {}
    for row in (report.get("tco_analysis") or {}).get("cost_breakdown") or []:
        if not isinstance(row, dict):
            continue
        db, c = row.get("database"), row.get("monthly_cost_usd")
        if db is not None and isinstance(c, (int, float)) and not isinstance(c, bool):
            out[db] = c
    return out


# ---------------------------------------------------------------------------
# Migration waves
# ---------------------------------------------------------------------------


def is_migrating_wave(wave: dict[str, Any]) -> bool:
    """A wave that moves tables off Aurora to a purpose-built engine.

    Wave 1 (``homogeneity`` set) carries the whole collected schema to Aurora:
    it is not a purpose-built-engine move, and its tables are a superset of
    every later wave's (#321 review, finding 4). A homogeneous wave 1 never
    counts towards "Tables migrate"; a cross-engine one counts in full, once
    (:func:`cross_engine_wave_one`, #1120). The cache and search/analytics
    waves move nothing (``moves_from`` is empty).
    """
    return bool(wave.get("moves_from")) and wave.get("homogeneity") is None


def _owner_entries(wave: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        o
        for o in (wave.get("table_owners") or [])
        if isinstance(o, dict) and o.get("table") and o.get("owner")
    ]


def wave_facts(wave: dict[str, Any], view_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """The table figures one wave states: served, moving, kept in sync.

    ``served_tables`` is the wave's own ``table_count`` (every table its
    queries touch, #562). For a migrating wave, ``moving_tables`` is the
    tables one of the wave's own engines durably owns -- the only ones that
    actually move -- and ``kept_in_sync_tables`` is the rest, owned by an
    engine outside the wave and kept in sync from it.

    #581: for the merged DynamoDB/DocumentDB wave, ``moving_tables`` is the
    sum of ``owned_counts_by_engine`` -- the same figure the "Tables migrate"
    tile adds up -- never ``table_count - len(table_owners)``. The deduped
    ``table_owners`` also lists a table one wave engine serves while the
    other owns it (DocumentDB serving a DynamoDB table), which subtracted a
    table that does move. Such entries are ``shared_in_wave``; only an owner
    outside the wave is ``kept_in_sync``.

    ``view_ids``, when given, splits wave 1's whole-schema count into tables
    and views (#578): wave 1 carries every collected table *and* view, while
    ``source_table_count`` counts tables only.
    """
    served = int(wave.get("table_count") or 0)
    engines = [str(e) for e in (wave.get("engines") or []) if e]
    out: dict[str, Any] = {
        "wave": int(wave.get("wave") or 0),
        "engines": engines,
        "migrating": is_migrating_wave(wave),
        "no_move": bool(wave.get("no_move_reason")),
        "served_tables": served,
        "moving_tables": None,
        "kept_in_sync_tables": None,
        "moving_by_engine": None,
        "served_by_engine": None,
        "kept_in_sync": [],
        "shared_in_wave": [],
        "views": None,
        "tables_only": None,
        "critical_high_risks": 0,
        "heterogeneous_note": None,
        "lists_relational_gaps": False,
        "moved_by_later_waves": None,
    }
    owners = _owner_entries(wave)
    outside: dict[str, dict[str, Any]] = {}
    inside: list[dict[str, Any]] = []
    for o in owners:
        if str(o["owner"]) in engines:
            inside.append(o)
        else:
            outside.setdefault(str(o["table"]), o)
    out["kept_in_sync"] = [outside[t] for t in sorted(outside)]
    out["shared_in_wave"] = sorted(inside, key=lambda o: (str(o["table"]), str(o["owner"])))
    if view_ids is not None and wave.get("homogeneity") is not None:
        views = set(view_ids)
        out["views"] = sum(1 for t in wave.get("tables") or [] if str(t) in views)
        out["tables_only"] = max(served - out["views"], 0)
    if wave.get("homogeneity") is not None:
        # Wave 1 carries the whole collected schema to Aurora: no served/moving split.
        return out
    if not is_migrating_wave(wave):
        # A read-model or cache wave moves nothing of its own; its headline
        # still says how many of its tables are kept in sync from an owner.
        out["kept_in_sync_tables"] = len(outside)
        out["moving_tables"] = max(served - len(outside), 0)
        return out
    per_served = wave.get("table_counts_by_engine")
    per_owned = wave.get("owned_counts_by_engine")
    if isinstance(per_served, dict) and isinstance(per_owned, dict) and per_owned:
        moving_by_engine = {e: int(per_owned.get(e) or 0) for e in engines}
        served_by_engine: dict[str, int] | None = {e: int(per_served.get(e) or 0) for e in engines}
        moving = sum(moving_by_engine.values())
    elif len(engines) == 1:
        moving = max(served - len(outside), 0)
        moving_by_engine = {engines[0]: moving}
        served_by_engine = {engines[0]: served}
    else:
        # A multi-engine wave written before the per-engine split existed:
        # the wave-level figure is still right, the per-engine one is unknown.
        moving = max(served - len(outside), 0)
        moving_by_engine = {}
        served_by_engine = None
    out["moving_tables"] = moving
    out["kept_in_sync_tables"] = max(served - moving, 0)
    out["moving_by_engine"] = moving_by_engine or None
    out["served_by_engine"] = served_by_engine
    return out


# Each impact as the note's count phrase, singular and plural ("1 needs a rewrite", "5
# need a rewrite"); one entry per GAP_IMPACTS value (tests/unit/report/
# test_heterogeneous_wave_one.py).
_IMPACT_VERBS: dict[str, tuple[str, str]] = {
    "blocks_move": ("blocks the move", "block the move"),
    "needs_rewrite": ("needs a rewrite", "need a rewrite"),
    "behaves_differently": ("behaves differently", "behave differently"),
    "informational": ("is informational", "are informational"),
}


def gap_count_phrase(impact: str, count: int) -> str:
    """ "22 need a rewrite", "1 is informational": one impact's count, as the wave-1 note
    and the executive report's grouped gap list say it (#1330). ``count`` is the gap
    check's own ``gaps_by_impact`` figure."""
    singular, plural = _IMPACT_VERBS.get(impact, (impact, impact))
    return f"{count} {singular if count == 1 else plural}"


def _gap_sentence(relational_gaps: Mapping[str, Any] | None) -> str:
    """ " The relational gap check found 11 gaps in this conversion: 5 need a rewrite,
    ..." for the heterogeneous wave-1 note; empty with no gap check (#1120).

    The counts are the gap check's own ``gaps_by_impact``, so the facts layer states one
    figure per impact."""
    if relational_gaps is None:
        return ""
    if relational_gaps.get("feature_rules_checked") == 0:
        # #1327: a conversion the gap catalog has no feature rules for was not
        # checked, so "no gaps" would claim a check that did not run (since #1330 the
        # catalog has feature rules for every source and Aurora engine).
        return (
            " The relational gap check has no feature rules for this conversion yet, so "
            "its feature gaps were not checked."
        )
    stored = relational_gaps.get("gaps_by_impact")
    counts = {
        k: int(n)
        for k, n in (stored.items() if isinstance(stored, Mapping) else ())
        if k in _IMPACT_VERBS and isinstance(n, int) and not isinstance(n, bool) and n > 0
    }
    total = sum(counts.values())
    if not total:
        return " The relational gap check found no gaps in this conversion."
    parts = [
        f"{counts[k]} {_IMPACT_VERBS[k][0 if counts[k] == 1 else 1]}"
        for k in GAP_IMPACTS
        if k in counts
    ]
    noun = "gap" if total == 1 else "gaps"
    return (
        f" The relational gap check found {total} {noun} in this conversion: "
        f"{', '.join(parts)}."
    )


def heterogeneous_wave_note(
    wave: Mapping[str, Any], tables: int, relational_gaps: Mapping[str, Any] | None
) -> str:
    """What a cross-engine wave 1 does and what the plan has not analyzed yet (#1120).

    Until a heterogeneous SQL-to-SQL analysis exists, wave 1 converts every source
    table to the chosen Aurora engine with its current data model (the wave's
    rationale and gate say the same). Every deliverable states this sentence under
    wave 1, then lists the detected relational gaps (``facts.relational_gaps``, #471).
    ``relational_gaps`` is the gap check for wave 1's own engine, or None.
    """
    engines = [str(e) for e in wave.get("engines") or [] if e]
    target = display_engine(engines[0]) if engines else "Aurora"
    source_ids = [str(e) for e in wave.get("moves_from") or [] if e]
    if not source_ids and relational_gaps and relational_gaps.get("source_engine"):
        source_ids = [str(relational_gaps["source_engine"])]
    source = (
        f" from {SOURCE_ENGINE_DISPLAY_NAMES.get(source_ids[0], display_engine(source_ids[0]))}"
        if source_ids
        else ""
    )
    what = "the 1 source table" if tables == 1 else f"all {tables} source tables"
    model = "its current data model" if tables == 1 else "their current data model"
    return (
        f"Wave 1 is not yet optimized for heterogeneous migrations: it converts {what}"
        f"{source} to {target} with {model}, and the plan does not yet analyze how to "
        f"redesign the schema or the queries for {target}.{_gap_sentence(relational_gaps)}"
    )


def apply_cross_engine_wave_one(
    waves: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    engines: list[dict[str, Any]],
    tables_analyzed: int | None,
    relational_gaps: Mapping[str, Any] | None,
) -> int | None:
    """Count a cross-engine wave 1 in full (#1120); the tables it moves, or None.

    For the wave's facts ``row``: ``moving_tables`` / ``moving_by_engine`` are every
    source table (views not included), ``heterogeneous_note`` states the move, and
    ``lists_relational_gaps`` says whether the gap check is this wave's (its
    ``target_engine`` is the wave's engine; a gap check for the other Aurora engine is
    neither counted nor listed). The chosen engine's row in ``engines`` gets every
    source table as its Scope and ``tables_migrate``. Rows are changed in place.
    Synthesis calls it from :func:`compute_report_facts`; the facts reader calls it for
    a block stored before these fields existed (``src.report.facts.report_facts``).
    """
    found = cross_engine_wave_one(waves, rows)
    if found is None:
        return None
    wave1, row = found
    tables_only = row.get("tables_only")
    moved: int = (
        int(tables_only)
        if tables_only is not None
        else (tables_analyzed if tables_analyzed is not None else int(row["served_tables"]))
    )
    target = row["engines"][0]
    gaps = (
        relational_gaps
        if relational_gaps is not None and relational_gaps.get("target_engine") == target
        else None
    )
    row["moving_tables"] = moved
    row["moving_by_engine"] = {target: moved}
    row["heterogeneous_note"] = heterogeneous_wave_note(wave1, moved, gaps)
    row["lists_relational_gaps"] = gaps is not None
    for e in engines:
        if e["engine"] == target and e["role"] == MIGRATION_TARGET:
            e["engine_source_tables"] = moved
            e["tables_migrate"] = moved
            shared = e.get("shared_source_tables")
            if shared is not None and shared <= moved:
                # "incl. shared" counts tables beyond the engine's own; it now has all.
                e["shared_source_tables"] = None
    return moved


def cross_engine_wave_one(
    waves: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """``(wave, its facts row)`` of a cross-engine wave 1, or None (#1120).

    A heterogeneous source (SQL Server, Oracle, Db2) has no Aurora engine of its own
    dialect; the resolver picks one (#381) and wave 1 converts every source table to
    it. ``homogeneity="heterogeneous"`` (no Aurora engine picked) names no engine to
    move the tables to, so it is not one.
    """
    for w, row in zip(waves, rows, strict=True):
        if w.get("homogeneity") == "cross_engine" and row["engines"]:
            return w, row
    return None


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------


def engine_served_split(waves: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each migration target's served tables that it does not own, split the way the
    wave plan splits them (#581, #1169).

    ``kept_in_sync``: tables an engine outside the wave owns; they stay with their
    owner and are kept in sync from it (``wave_facts``' ``kept_in_sync``).
    ``moves_to``: owner -> tables another engine of the same wave owns; they move in
    that wave, to that owner (``wave_facts``' ``shared_in_wave``). An engine's own
    tables are its wave's ``moving_by_engine``, so served = own + moves_to +
    kept_in_sync. Only migrating waves with a per-engine split count.
    """
    out: dict[str, dict[str, Any]] = {}
    for w in waves:
        wf = wave_facts(w)
        served_by = wf["served_by_engine"]
        moving_by = wf["moving_by_engine"] or {}
        if not wf["migrating"] or served_by is None:
            continue
        engines = wf["engines"]
        for e in engines:
            moves_to: dict[str, int] = {}
            # In a two-engine wave an entry owned by the other engine is one this
            # engine serves; with more engines it cannot be attributed, so none is.
            if len(engines) == 2:
                for o in wf["shared_in_wave"]:
                    owner = str(o["owner"])
                    if owner != e:
                        moves_to[owner] = moves_to.get(owner, 0) + 1
            row = out.setdefault(e, {"moves_to": {}, "kept_in_sync": 0})
            for owner, k in moves_to.items():
                row["moves_to"][owner] = row["moves_to"].get(owner, 0) + k
            row["kept_in_sync"] += max(
                int(served_by.get(e) or 0) - int(moving_by.get(e) or 0) - sum(moves_to.values()),
                0,
            )
    return out


def engine_table_split_facts(
    engine: str, source_tables: int | None, migrates: int, split: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """``kept_in_sync_tables`` and ``moves_to_tables`` for one migration target (#1169);
    None for an engine that states no served count."""
    if not isinstance(source_tables, int) or isinstance(source_tables, bool):
        return {"kept_in_sync_tables": None, "moves_to_tables": None}
    row = split.get(engine)
    if row is None:
        # No migrating wave splits it (a report written before waves did).
        return {"kept_in_sync_tables": max(source_tables - migrates, 0), "moves_to_tables": {}}
    return {
        "kept_in_sync_tables": int(row["kept_in_sync"]),
        "moves_to_tables": dict(sorted(row["moves_to"].items())),
    }


def engine_facts(report: dict[str, Any], waves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The full target architecture, one entry per engine, ordered by workload.

    Derived from ``ranking`` (every engine that carries workload) joined with
    ``tco_analysis.cost_breakdown``, ``recommended_architecture.databases`` (source
    table counts for migration targets) and ``schema_designs`` (design status and
    cache-object counts). This is the picture synthesis actually computed;
    ``recommended_architecture.databases`` alone is a lossy projection of it that
    neither reconciles to the cost total nor matches the executive summary.

    A migration target reads its tables from its migration wave (#524; with a
    completed schema design too since #949, so the headline agrees with the wave):
    ``source_tables`` is the wave's served count, and ``tables_migrate`` only the
    subset the engine durably owns (#568 review) -- from ``owned_counts_by_engine``
    for the merged DynamoDB/DocumentDB wave, never its union ``table_count``
    credited to both engines. Only an engine with no wave (a report written before
    waves) falls back to ``databases[].table_count``, the design's table map.
    """
    ranking = [r for r in (report.get("ranking") or []) if isinstance(r, dict)]
    arch = report.get("recommended_architecture") or {}
    dbs = [d for d in (arch.get("databases") or []) if isinstance(d, dict)]
    recommended = {d.get("service") for d in dbs}
    src_tables = {d.get("service"): d.get("table_count") for d in dbs}
    schema_designs = report.get("schema_designs") or {}
    costs = engine_costs(report)
    sizing = engine_cost_sizing(report)

    wave_served: dict[str, int] = {}
    wave_owned: dict[str, int] = {}
    for w in waves:
        wf = wave_facts(w)
        for e, k in (wf["served_by_engine"] or {}).items():
            wave_served[e] = wave_served.get(e, 0) + k
        for e, k in (wf["moving_by_engine"] or {}).items():
            wave_owned[e] = wave_owned.get(e, 0) + k

    split = engine_served_split(waves)
    rows: list[dict[str, Any]] = []
    for r in ranking:
        eng = r.get("target")
        if not eng:
            continue
        workload = r.get("workload_percent")
        role = engine_role(eng, recommended, schema_designs, workload)
        design = schema_designs.get(eng) or {}
        design_tables = design.get("tables")
        objs = len(design_tables) if isinstance(design_tables, list) else None
        cached = cache_layer_counts(report, eng) if role == CACHE_LAYER else None
        source_tables: int | None = None
        shared_tables: int | None = None
        migrates = 0
        if role == MIGRATION_TARGET:
            n = src_tables.get(eng)
            owned: int | None = None
            # #949: the routing's figure, design or not: the tables the engine's
            # queries touch (its wave's served count) and the ones it owns, which
            # move. A design's table map can list more or fewer, and then "N tables
            # migrate" disagreed with the wave's own "N to move".
            wave_n = wave_served.get(eng)
            if isinstance(wave_n, int) and wave_n > 0:
                n = wave_n
                owned = wave_owned.get(eng)
            migrates = (
                owned if isinstance(owned, int) else (n if isinstance(n, int) else (objs or 0))
            )
            source_tables = n if isinstance(n, int) else None
            served = design_source_tables(design_tables)
            if source_tables is not None and served > source_tables:
                shared_tables = served
        rows.append(
            {
                "engine": str(eng),
                "role": role,
                # The cache layer owns no query: no workload share, its cached reads
                # and their share of calls instead (#296)
                "workload_percent": (
                    None
                    if cached
                    else (
                        float(workload)
                        if isinstance(workload, (int, float)) and not isinstance(workload, bool)
                        else None
                    )
                ),
                "cached_queries": cached[0] if cached else None,
                "cached_call_share_percent": cached[1] if cached else None,
                "engine_source_tables": source_tables,
                "shared_source_tables": shared_tables,
                "target_objects": objs,
                "tables_migrate": int(migrates or 0),
                # #1169: the served tables it does not own, split as its wave splits
                # them, so the Scope cell adds up and agrees with the wave plan.
                **engine_table_split_facts(str(eng), source_tables, int(migrates or 0), split),
                "monthly_cost_usd": costs.get(eng),
                # #1195: what that figure was sized for (its assigned queries, or the
                # reads the cache layer fronts); None for the analysis estimate.
                "cost_sizing": sizing.get(str(eng)),
                # Synthesis's per-engine rationale, built from the routed workload
                # (#152); the ranking entry carries it for engines `databases` omits
                "rationale": rationale_text(
                    next((d.get("rationale") for d in dbs if d.get("service") == eng), None)
                    or r.get("rationale")
                ),
            }
        )
    rows.sort(
        key=lambda x: x["workload_percent"] if x["workload_percent"] is not None else -1,
        reverse=True,
    )
    return rows


# ---------------------------------------------------------------------------
# Table basis (#530, #533)
# ---------------------------------------------------------------------------

# "Analyzed 311 source tables and 1654 query patterns across 5 target
# database(s)." -- ``build_summary``'s own fixed, deterministic opening
# sentence. Read only for a report old enough to carry neither ``facts`` nor
# ``source_table_count``.
ANALYZED_SOURCE_TABLES = re.compile(r"\bAnalyzed (\d+) source tables?\b")


def _tables_analyzed(report: dict[str, Any], waves: list[dict[str, Any]]) -> int:
    count = report.get("source_table_count")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    m = ANALYZED_SOURCE_TABLES.search(str(report.get("summary_deterministic") or ""))
    if m:
        return int(m.group(1))
    tables: set[str] = set()
    for w in waves:
        tables.update(str(t) for t in (w.get("tables") or []) if t)
    return len(tables)


# The migration map's split of its tables (#1169), in the order the heading states it.
MAP_MIGRATE = "migrate"
MAP_KEPT_IN_SYNC = "kept_in_sync"
MAP_CACHE_LAYER = "cache_layer"
MAP_SEARCH_READ_MODEL = "search_read_model"
MAP_STAYS = "stays"
MAP_OTHER = "other"
MAP_SPLIT_ORDER = (
    MAP_MIGRATE,
    MAP_KEPT_IN_SYNC,
    MAP_CACHE_LAYER,
    MAP_SEARCH_READ_MODEL,
    MAP_STAYS,
    MAP_OTHER,
)
_MAP_BY_ROLE = {
    MIGRATION_TARGET: MAP_MIGRATE,
    CACHE_LAYER: MAP_CACHE_LAYER,
    SEARCH_READ_MODEL: MAP_SEARCH_READ_MODEL,
    RETAINED: MAP_STAYS,
}


def _migrating_wave_tables(
    waves: list[dict[str, Any]],
) -> tuple[set[str], set[str], set[str]]:
    """(tables a migrating wave moves, tables one keeps in sync and none moves, the
    engines of the migrating waves that list their tables)."""
    moving: set[str] = set()
    synced: set[str] = set()
    waved: set[str] = set()
    for w in waves:
        if not is_migrating_wave(w):
            continue
        tables = {str(t) for t in (w.get("tables") or []) if t}
        engines = {str(e) for e in (w.get("engines") or []) if e}
        if tables:
            waved |= engines
        outside = {str(o["table"]) for o in _owner_entries(w) if str(o["owner"]) not in engines}
        moving |= tables - outside
        synced |= outside
    return moving, synced - moving, waved


def wave_one_moved_by_later_waves(waves: list[dict[str, Any]]) -> int | None:
    """How many of wave 1's tables and views a later wave moves off its Aurora engine
    (#1284), or None when wave 1 names no Aurora engine (a heterogeneous source with
    none picked, a source already on Aurora, no waves).

    "Moves" is the migrating waves' own definition, the one behind "N tables migrate"
    and the Migration map's "N migrate": a table one of the wave's engines owns; a
    table a migrating wave only keeps in sync, the cache and the read-model waves move
    nothing. Wave 1's rationale states this figure next to the migration map's count,
    so a table mapped to the Aurora engine itself is never said to move.
    """
    first = next((w for w in waves if w.get("homogeneity") is not None), None)
    if first is None or first.get("homogeneity") not in ("homogeneous", "cross_engine"):
        return None
    if not first.get("engines"):
        return None
    moving, _, _ = _migrating_wave_tables(waves)
    return len({str(t) for t in first.get("tables") or [] if t} & moving)


def _map_engine(row: Mapping[str, Any]) -> str:
    """The engine a ``table_mappings`` row counts under: its owner (#212), or, for a
    row with no owner line or no owner, its recommended engine."""
    return str(row.get("owner") or row.get("recommended_database") or "")


def map_split_facts(
    mappings: list[dict[str, Any]], roles: Mapping[str, str], waves: list[dict[str, Any]]
) -> dict[str, Any]:
    """The mapped tables split by what the plan does with them (#1169).

    ``mapped_tables_by_move``: ``migrate`` counts a mapped table a migrating wave moves
    (one of the wave's engines owns it), the tables behind "N tables migrate";
    ``kept_in_sync`` one a migrating wave serves while an engine outside that wave
    owns it (the wave plan's rule, #581). The rest follow the role of their owner
    (#212: the engine that takes the table's writes, never a cache; a row with no owner
    line, the recommended engine): ``search_read_model``, ``stays`` (on the source
    engine) and ``other``, and ``cache_layer`` only for a table no engine but the cache
    design maps. A row mapped to a migration target whose wave does
    not serve it stays, and is named in ``mapped_tables_not_served`` (engine -> tables).
    A migration target with no migrating wave that lists its tables (a report written
    before waves did) counts its mapped tables as ``migrate``, as ``tables_migrate``
    counts its design's tables.

    ``migrating_tables_not_mapped`` (and ``..._names``): the moving tables no row
    lists, the tables a schema design has no target table for yet.
    """
    moving, synced, waved = _migrating_wave_tables(waves)
    by_move: dict[str, int] = {}
    not_served: dict[str, list[str]] = {}
    for m in mappings:
        table = str(m.get("source_table") or "")
        label = str(m.get("recommended_database") or "")
        role = roles.get(_map_engine(m), OTHER_ROLE)
        if table in moving:
            kind = MAP_MIGRATE
        elif table in synced:
            kind = MAP_KEPT_IN_SYNC
        elif roles.get(label) == MIGRATION_TARGET and label in waved:
            # The design maps it to a migration target no routed query reads it on.
            kind = MAP_STAYS
            not_served.setdefault(label, []).append(table)
        else:
            kind = _MAP_BY_ROLE.get(role, MAP_OTHER)
        by_move[kind] = by_move.get(kind, 0) + 1
    missing = (
        sorted(moving - {str(m.get("source_table") or "") for m in mappings}) if mappings else []
    )
    return {
        "mapped_tables_by_move": by_move,
        "migrating_tables_not_mapped": len(missing),
        "migrating_tables_not_mapped_names": missing,
        "mapped_tables_not_served": {e: sorted(t) for e, t in sorted(not_served.items())},
    }


def mapped_tables_by_role(
    mappings: Iterable[Mapping[str, Any]], roles: Mapping[str, str]
) -> dict[str, int]:
    """Mapped tables per role of their owner (#258, #212: never the cache layer for a
    table with an owner). The migration map's heading reads ``mapped_tables_by_move``
    instead (#1169)."""
    by_role: dict[str, int] = {}
    for m in mappings:
        role = roles.get(_map_engine(m), OTHER_ROLE)
        if role not in (MIGRATION_TARGET, CACHE_LAYER, RETAINED, SEARCH_READ_MODEL):
            role = OTHER_ROLE
        by_role[role] = by_role.get(role, 0) + 1
    return by_role


def table_basis(
    report: dict[str, Any], engines: list[dict[str, Any]], waves: list[dict[str, Any]]
) -> dict[str, Any]:
    """The figure behind the deck's "N tables mapped to a named target" (#530, #533).

    ``design_coverage`` says how much of it a completed schema design backs:

    - ``complete``: every migration target has a completed design, so
      ``table_mappings`` is the precise per-table count (each with its own
      fit score) and ``basis_table_count`` is its length;
    - ``partial``: only ``designed_engines`` have one (#533). ``table_mappings``
      covers those engines only, so its length is *not* the count: the
      "Tables migrate" tile also adds the wave fallback of every
      ``pending_engines`` entry. ``basis_table_count`` is then the tables analyzed, as
      with no design at all, and ``mapped_tables`` how many of them carry a
      per-table fit score;
    - ``none``: no design ran; ``basis_table_count`` is the tables analyzed
      (``source_table_count``, else the summary's own figure, else the
      union of the waves' tables).
    """
    mappings = owned_table_mappings(report, waves)
    roles = {e["engine"]: e["role"] for e in engines}
    by_role = mapped_tables_by_role(mappings, roles)
    split = map_split_facts(mappings, roles, waves)
    schema_designs = report.get("schema_designs") or {}
    targets = [e["engine"] for e in engines if e["role"] == MIGRATION_TARGET]
    designed = [e for e in targets if (schema_designs.get(e) or {}).get("status") == "completed"]
    pending = [e for e in targets if e not in designed]
    if mappings and not pending:
        return {
            "basis_table_count": len(mappings),
            "mapped_tables": len(mappings),
            "design_coverage": "complete",
            "designed_engines": designed,
            "pending_engines": [],
            "mapped_tables_by_role": by_role,
            **split,
        }
    return {
        "basis_table_count": _tables_analyzed(report, waves),
        "mapped_tables": len(mappings),
        "design_coverage": "partial" if mappings else "none",
        "designed_engines": designed if mappings else [],
        "pending_engines": pending if mappings else targets,
        "mapped_tables_by_role": by_role,
        **split,
    }


# ---------------------------------------------------------------------------
# Risks
# ---------------------------------------------------------------------------


def risk_engine_and_body(desc: Any) -> tuple[str, str]:
    """Split a risk description into its ``[engine]`` prefix and the remaining text.

    Risks carry no explicit engine field; the engine is encoded as a leading
    ``[documentdb]`` / ``[elasticache]`` tag in the description. Returns
    ``(engine, body)`` with ``engine`` defaulting to ``"(general)"``.
    """
    s = str(desc or "").strip()
    engine = "(general)"
    if s.startswith("["):
        j = s.find("]")
        if j != -1:
            engine = s[1:j].strip() or "(general)"
            s = s[j + 1 :].strip()
    return engine, s


def risk_has_content(desc: Any) -> bool:
    """False for a risk description with no real text after its ``[engine]``
    prefix (or only the historical ``unknown:`` placeholder, #210)."""
    _, body = risk_engine_and_body(desc)
    if body.lower().startswith("unknown:"):
        body = body[len("unknown:") :].strip()
    return bool(body)


def open_risks(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The one open-risk list every deliverable renders and counts from (#201)."""
    risk = report.get("risk_assessment") or {}
    return [
        r
        for r in (risk.get("risks") or [])
        if isinstance(r, dict) and risk_has_content(r.get("description"))
    ]


def resolved_risk_list(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Analysis risks the effective assignment resolved (``risk_assessment.resolved_risks``)."""
    risk = report.get("risk_assessment") or {}
    return [
        r
        for r in (risk.get("resolved_risks") or [])
        if isinstance(r, dict) and risk_has_content(r.get("description"))
    ]


def risk_facts(report: dict[str, Any]) -> dict[str, Any]:
    """Open/resolved risk counts, by severity, by type and by engine."""
    risks = open_risks(report)
    by_type: dict[str, int] = {}
    crit_high_by_engine: dict[str, int] = {}
    crit_high_types: set[str] = set()
    for r in risks:
        t = str(r.get("risk_type") or "other").replace("_", " ").lower()
        by_type[t] = by_type.get(t, 0) + 1
        # #270: CRITICAL is at least as severe as HIGH, so every "needs
        # escalating" count includes it.
        if severity_of(r) in ("CRITICAL", "HIGH"):
            crit_high_types.add(str(r.get("risk_type") or ""))
            engine, _ = risk_engine_and_body(r.get("description"))
            if engine != "(general)":
                crit_high_by_engine[engine] = crit_high_by_engine.get(engine, 0) + 1
    by_severity = count_by_severity(risks)
    return {
        "open_risk_count": len(risks),
        "open_risks_by_severity": {
            k: by_severity[k] for k in (*SEVERITY_ORDER, "UNRATED") if by_severity.get(k)
        },
        "critical_high_risk_count": sum(by_severity.get(k, 0) for k in ("CRITICAL", "HIGH")),
        "critical_high_risks_by_engine": dict(sorted(crit_high_by_engine.items())),
        "critical_high_risk_types": sorted(crit_high_types),
        # "All the same root cause" is claimed only when the CRITICAL/HIGH
        # risks share exactly one risk_type.
        "critical_high_root_cause": (
            next(iter(crit_high_types)) or None if len(crit_high_types) == 1 else None
        ),
        "open_risks_by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))),
        "resolved_risk_count": len(resolved_risk_list(report)),
        **resolved_risk_severity_facts(report),
    }


def refresh_risk_facts(facts: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """``facts`` with every risk figure counted again from ``report``'s risks: the
    open and resolved counts (``risk_facts``) and each wave's and the no-migration
    step's critical and high count, as ``compute_report_facts`` counts them. For a
    report whose risks were added after its facts block was written (#1148: a rule
    backfilled at render time); every other figure stays as stored."""
    risks = risk_facts(report)
    by_engine = risks["critical_high_risks_by_engine"]

    def high(engines: Any) -> int:
        return sum(by_engine.get(str(e), 0) for e in engines or [])

    out = {**facts, **risks}
    if isinstance(facts.get("waves"), list):
        out["waves"] = [
            {**w, "critical_high_risks": high(w.get("engines"))} if isinstance(w, dict) else w
            for w in facts["waves"]
        ]
    no_migration = facts.get("no_migration")
    if isinstance(no_migration, dict):
        out["no_migration"] = {
            **no_migration,
            "critical_high_risks": high(no_migration.get("engines")),
        }
    if isinstance(facts.get("executive_brief"), dict):
        # #1403: the overall level and the risks driving it, from the same risks.
        out["executive_brief"] = executive_brief_facts(
            report,
            [w for w in report.get("migration_waves") or [] if isinstance(w, dict)],
            [r for r in facts.get("retired_in_use_engines") or [] if isinstance(r, dict)],
            facts.get("query_total_count"),
        )
    return out


def resolved_risk_severity_facts(report: dict[str, Any]) -> dict[str, dict[str, int]]:
    """The resolved risks by severity (#1020), as ``open_risks_by_severity`` counts the
    open ones: every resolved risk once, a severity with none left out."""
    by_severity = count_by_severity(resolved_risk_list(report))
    return {
        "resolved_risks_by_severity": {
            k: by_severity[k] for k in (*SEVERITY_ORDER, "UNRATED") if by_severity.get(k)
        }
    }


# ---------------------------------------------------------------------------
# Labels and reasons (#584, #546)
# ---------------------------------------------------------------------------

# Reason and trade-off text names engines by display name ("Aurora MySQL") or,
# in reports written before #572, by raw id ("aurora_mysql"); both map back to
# the id. Text is read here, once, for the reality check's own consolidation
# lines and for an older report's override reasons -- never by a renderer.
ENGINE_BY_NAME = {
    **{v: k for k, v in ENGINE_DISPLAY_NAMES.items()},
    **{k: k for k in ENGINE_DISPLAY_NAMES},
}
_ENGINE_NAME_ALT = "|".join(re.escape(n) for n in sorted(ENGINE_BY_NAME, key=len, reverse=True))
# A "Consolidated N queries from <engine> -> <absorber>: ... no in-scope
# <engine> query remains ..." trade-off line (reality_check_patterns.
# _build_recommendations / reality_check_records.consolidations_from_moves): the
# "no in-scope ... remains" clause is the record fitting's own marker for a
# full elimination.
FULLY_ELIMINATED_RE = re.compile(rf"no in-scope ({_ENGINE_NAME_ALT}) query remains")
CONSOLIDATED_FROM_TO_RE = re.compile(
    rf"^Consolidated \d+ quer(?:y|ies) from ({_ENGINE_NAME_ALT}) → ({_ENGINE_NAME_ALT})"
)
_SIGNAL_BY_NAME = {**{v: k for k, v in SIGNAL_NOUNS.items()}}
# "signal override: <signal> → <engine>", the signal and the engine either raw
# ids (before #584) or readable names.
_SIGNAL_OVERRIDE_RE = re.compile(
    rf"signal override:\s*([^→|]+?)\s*(?:→|->)\s*({_ENGINE_NAME_ALT})(?![\w])"
)


def engine_id_from_name(name: str) -> str:
    """``Aurora MySQL`` or ``aurora_mysql`` -> ``aurora_mysql``."""
    return ENGINE_BY_NAME.get(name, name)


def _signal_id(name: str) -> str:
    name = name.strip()
    return _SIGNAL_BY_NAME.get(name, name.replace(" ", "_"))


def signal_overrides_from_reasons(report: dict[str, Any]) -> dict[str, list[str]]:
    """engine -> the triage signals that overrode queries onto it, read from the
    ranking's reason summaries: only for a report written before
    ``facts.engines[].signal_overrides`` existed. Synthesis passes the
    assignment's own ``signal_override`` fields instead (#584)."""
    out: dict[str, set[str]] = {}
    for r in report.get("ranking") or []:
        if not isinstance(r, dict):
            continue
        reasons = r.get("assignment_reason_summary") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        for reason in reasons:
            for m in _SIGNAL_OVERRIDE_RE.finditer(str(reason)):
                engine = engine_id_from_name(m.group(2))
                out.setdefault(engine, set()).add(_signal_id(m.group(1)))
    return {e: sorted(v) for e, v in out.items()}


def final_engines(engines: list[dict[str, Any]]) -> set[str]:
    """Engines in the final architecture: every engine the effective assignment
    gives work (owners) plus the cache layer. An ``Evaluated`` engine won
    nothing."""
    return {e["engine"] for e in engines if e["role"] != EVALUATED}


def _trade_off_engines(desc: str) -> tuple[str | None, str | None, bool]:
    """(from engine, to engine, fully eliminated) a reality-check line names."""
    m = CONSOLIDATED_FROM_TO_RE.match(desc)
    elim = FULLY_ELIMINATED_RE.search(desc)
    src = engine_id_from_name(m.group(1)) if m else None
    dst = engine_id_from_name(m.group(2)) if m else None
    if elim and not src:
        src = engine_id_from_name(elim.group(1))
    return src, dst, bool(elim)


def _query_count(n: int) -> str:
    return f"{n} {'query' if n == 1 else 'queries'}"


def _restated_consolidation(desc: str, moved: int, back: int) -> str:
    """A consolidation line restated for what stayed moved after an edit (#861)."""
    m = CONSOLIDATED_FROM_TO_RE.match(desc)
    src_name, dst_name = (m.group(1), m.group(2)) if m else ("its engine", "another engine")
    still = moved - back
    return (
        f"Consolidated {_query_count(still)} from {src_name} → {dst_name}: {still} of the "
        f"{_query_count(moved)} the reality check moved to {dst_name} "
        f"{'stays' if still == 1 else 'stay'} there; a later assignment edit moved "
        f"{back} back to {src_name}."
    )


def reality_check_trade_off_facts(report: dict[str, Any], final: set[str]) -> list[dict[str, Any]]:
    """Every reality-check trade-off, with the engines it names and whether a
    later assignment edit reversed it (#546).

    The reality check writes its consolidation lines once, against the
    assignment it settled. A customer edit can then move queries back onto
    the engine they left; the line still says what the check moved, and for
    an engine it removed, that the engine "leaves the architecture" and what
    removing it saves. No deliverable states such a line as current.

    Per query (#861): synthesis records, for each consolidation, the queries
    it moved (``moved_query_ids``) and those the effective assignment has put
    back on the source engine since (``moved_back_query_ids``). A line is
    ``reversed`` when every query it moved is back; when only some are, it
    stays current and ``description_now`` restates it for what stayed moved
    (``queries_moved_back`` / ``queries_still_moved``). A partial
    consolidation, or an engine a customer pin keeps (#852), with nothing
    moved back is current as written.

    A report without that record falls back to the engine: a line is
    reversed when its source engine is back in the final architecture and the
    check itself left it without a query (absent from its
    ``after_distribution``), or -- for a report with no distributions either
    -- when the source engine is in the final architecture at all.
    """
    rc = report.get("reality_check") or {}
    after = rc.get("after_distribution") if isinstance(rc, dict) else None
    kept_by_check = {e for e, n in after.items() if n} if isinstance(after, dict) else set()
    has_after = isinstance(after, dict) and bool(after)
    # The consolidation record behind each line, in order, by (from, to).
    records: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in (rc.get("consolidations") if isinstance(rc, dict) else None) or []:
        if isinstance(c, dict):
            key = (str(c.get("from_engine") or ""), str(c.get("to_engine") or ""))
            records.setdefault(key, []).append(c)
    out: list[dict[str, Any]] = []
    for i, t in enumerate(report.get("trade_offs") or []):
        if not isinstance(t, dict):
            continue
        desc = str(t.get("description") or "")
        src, dst, _ = _trade_off_engines(desc)
        # A report written before trade-offs carried an engine still has the
        # reality check's consolidation lines: recognised by their own shape.
        if t.get("engine") != "reality-check" and not (t.get("engine") is None and src):
            continue
        pending = records.get((src or "", dst or "")) if src and dst else None
        record = pending.pop(0) if pending else None
        moved_ids = record.get("moved_query_ids") if record else None
        row: dict[str, Any] = {
            "index": i,
            "from_engine": src,
            "to_engine": dst,
            "queries_moved_back": 0,
            "queries_still_moved": None,
            "description_now": None,
        }
        if record is not None and isinstance(moved_ids, list) and moved_ids:
            moved = {str(q) for q in moved_ids}
            back = moved & {str(q) for q in record.get("moved_back_query_ids") or []}
            row["queries_moved_back"] = len(back)
            row["queries_still_moved"] = len(moved) - len(back)
            row["reversed"] = not row["queries_still_moved"]
            if back and row["queries_still_moved"]:
                row["description_now"] = _restated_consolidation(desc, len(moved), len(back))
        else:
            row["reversed"] = bool(
                src and src in final and not (has_after and src in kept_by_check)
            )
        out.append(row)
    return out


_KEPT_RE = re.compile(rf"^({_ENGINE_NAME_ALT}) kept: ")
_PATTERN_PREFIX = "Recommended pattern: "


def reality_check_decision_facts(
    report: dict[str, Any],
    trade_offs: list[dict[str, Any]],
    removed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The reality-check trade-offs that still apply, as every deliverable shows them (#595).

    The reality check's own lines (consolidations, the sanity sweep's
    kept-engine verdicts, #593, and recommended patterns) used to reach only
    the engineering report: the decision report, the deck and the UI's
    trade-offs tab read trade-offs per schema-design engine, and
    "reality-check" is not one. Each entry here carries its ``kind``, the
    ``engine`` it is about, a short ``title`` and the ``detail``, split once
    from the line's own text; a renderer only formats them.
    ``with_removed_engine`` marks a consolidation of an engine
    ``removed_engines`` already lists with this same line as its reason.
    A reversed line (#546) is not here at all.
    """
    rows = report.get("trade_offs") or []
    removed_set = {e["engine"] for e in removed}
    out: list[dict[str, Any]] = []
    for t in trade_offs:
        if t["reversed"]:
            continue
        entry = rows[t["index"]]
        desc = str(t.get("description_now") or entry.get("description") or "").strip()
        consolidated = CONSOLIDATED_FROM_TO_RE.match(desc)
        kept = _KEPT_RE.match(desc)
        if consolidated:
            kind, engine = "consolidation", t["from_engine"]
            title, detail = consolidated.group(0), desc[consolidated.end() :]
        elif kept:
            kind, engine = "kept", engine_id_from_name(kept.group(1))
            title, detail = f"{kept.group(1)} kept", desc[kept.end() :]
        elif desc.startswith(_PATTERN_PREFIX):
            kind, engine = "pattern", None
            name, _, rest = desc[len(_PATTERN_PREFIX) :].partition(". ")
            title, detail = _PATTERN_PREFIX + name.rstrip("."), rest
        else:
            kind, engine, title, detail = "other", t["from_engine"], desc, ""
        out.append(
            {
                "index": t["index"],
                "kind": kind,
                "engine": engine,
                "title": title.strip(),
                "detail": detail.strip(" :"),
                "query_ids": [str(q) for q in entry.get("query_ids") or []],
                "with_removed_engine": kind == "consolidation" and engine in removed_set,
            }
        )
    return out


def removed_engine_facts(
    report: dict[str, Any], final: set[str], trade_offs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Engines the reality check removed, each with where its queries went and why.

    #547: an engine counts as removed when it owned queries before the reality
    check (``before_distribution``), a full consolidation moved it out, or the
    TCO lists its cost as already removed -- and it is not in the final
    architecture. #546: the final architecture is the effective assignment's
    (the ranking), not the reality check's own ``after_distribution``, so an
    engine a customer edit moved queries back onto is never listed. A trade-off
    line carrying the "no in-scope X query remains" marker also counts, for
    reports written before the distributions were recorded. A partial
    consolidation leaves its engine in the architecture, so it is never listed.
    """
    rc = report.get("reality_check") or {}
    after = rc.get("after_distribution") or {}
    cache = (report.get("cache_overlay") or {}).get("engine")
    rc_lines = [
        (str((report.get("trade_offs") or [])[t["index"]].get("description") or ""), t)
        for t in trade_offs
    ]
    candidates: list[str] = []
    marked: set[str] = set()
    for desc, _t in rc_lines:
        src, _, elim = _trade_off_engines(desc)
        if src:
            candidates.append(src)
        if elim and src:
            marked.add(src)
    consolidations = [c for c in rc.get("consolidations") or [] if isinstance(c, dict)]
    candidates += [
        str(c.get("from_engine") or "") for c in consolidations if c.get("action") == "full"
    ]
    candidates += sorted(e for e, n in (rc.get("before_distribution") or {}).items() if n)
    # A retired engine (#949) and, for a report before contract 1.25, a priced
    # candidate (``eliminated_engine_costs``) both say an engine left the plan.
    tco = report.get("tco_analysis") or {}
    eliminated = [
        str(row.get("database") or "")
        for key in ("retired_engine_savings", "eliminated_engine_costs")
        for row in tco.get(key) or []
        if isinstance(row, dict)
    ]
    candidates += eliminated
    marked |= set(eliminated)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in candidates:
        if not e or e in seen or e == cache or e in final or after.get(e):
            continue
        if not after and e not in marked:
            # No distributions recorded (an older report): only the marker or
            # an eliminated cost says an engine left; a bare line does not.
            continue
        seen.add(e)
        reasons: list[str] = []
        moved_to: list[str] = []
        for desc, t in rc_lines:
            if t["from_engine"] != e:
                continue
            if desc not in reasons:
                reasons.append(desc)
            if t["to_engine"] and t["to_engine"] not in moved_to:
                moved_to.append(t["to_engine"])
        for c in consolidations:
            if c.get("from_engine") == e:
                to = str(c.get("to_engine") or "")
                if to and to not in moved_to:
                    moved_to.append(to)
                if not reasons and c.get("reason"):
                    reasons.append(str(c["reason"]))
        out.append({"engine": e, "moved_to": moved_to, "reasons": reasons})
    return out


NO_SAVING_REASON = "no queries justified it before the move"


def retired_in_use_engine_facts(
    report: Mapping[str, Any], removed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Engines the customer runs today that the plan retires, each stated once (#1076).

    An engine in ``report.engines_in_use`` (the source's own engine and those the collection
    declares, #963) that ``removed`` lists, plus every engine with a row in
    ``tco_analysis.retired_engine_savings`` (a report from before the field existed has no
    ``engines_in_use``, but those rows are by definition engines the source runs). Each row
    carries ``monthly_cost_usd`` (the counted saving, None when none is counted), where its
    queries went and the reality check's own consolidation reason. A dropped candidate the
    source never ran is not here: ``considered_not_recommended`` names it. A declared engine
    the routing analysed but gave no queries (``ranking``, ``assigned_queries`` 0) is retired
    too. ``reason`` is the first consolidation's; ``no_saving_reason`` says why no saving is
    counted.
    """
    declared = {str(e).lower() for e in report.get("engines_in_use") or [] if isinstance(e, str)}
    tco = report.get("tco_analysis") or {}
    savings = {
        str(r["database"]): float(r["monthly_cost_usd"])
        for r in tco.get("retired_engine_savings") or []
        if isinstance(r, Mapping)
        and r.get("database")
        and isinstance(r.get("monthly_cost_usd"), (int, float))
    }
    moved = {str(r["engine"]): list(r.get("moved_to") or []) for r in removed}
    idle = {
        str(r["target"])
        for r in report.get("ranking") or []
        if isinstance(r, Mapping)
        and r.get("target") in declared
        and not r.get("assigned_queries")
        and r["target"] not in CACHE_ENGINES
    }
    engines = sorted({e for e in moved if e in declared} | set(savings) | idle)
    reasons: dict[str, str] = {}
    for c in (report.get("reality_check") or {}).get("consolidations") or []:
        if isinstance(c, Mapping) and c.get("reason") and c.get("from_engine"):
            reasons.setdefault(str(c["from_engine"]), str(c["reason"]))
    return [
        {
            "engine": e,
            "monthly_cost_usd": savings.get(e),
            "moved_to": moved.get(e, []),
            "reason": reasons.get(e),
            "no_saving_reason": None if e in savings else NO_SAVING_REASON,
        }
        for e in engines
    ]


# ---------------------------------------------------------------------------
# The whole facts block
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-query decision traces (#339)
# ---------------------------------------------------------------------------

# The rules that pin a query by a listed rule rather than by its score:
# full-text search. Deterministic, so counted apart from a customer's pick,
# its group propagation and a design review's flag (recognised by actor).
_PINNING_RULES = frozenset({"override.text_search"})

# At most this many listed queries carry their full trace in report.json and
# the engineering report's appendix (each is about 3.4 KB and 9 lines); the
# rest say only why they are listed (#339 review).
TRACE_FULL_CAP = 200


def trace_kinds(trace: Mapping[str, Any]) -> dict[str, bool]:
    """Why a query's decision trace is listed (``QueryTraceListing``).

    ``moved``: a later step changed its engine (even if a still later one
    moved it back); ``tied``: it was tied at some step, settled or not;
    ``open_tie``: the tie is still unresolved at the end; ``overridden``: a
    customer or a design review (actor ``model_flag``) placed it; ``pinned``:
    a listed rule (full-text search) placed it. ``trace`` is a
    ``QueryTraceResponse`` as JSON. Synthesis selects the queries
    ``query_traces`` lists with this, and the facts layer counts them with
    it, so the two never disagree.
    """
    first = trace.get("first_assignment")
    later = [m for m in trace.get("moves") or [] if isinstance(m, dict)]
    steps = ([first] if isinstance(first, dict) else []) + later
    open_tie = bool(trace.get("tied_engines"))
    return {
        "moved": any(
            m.get("from_engine") and m.get("from_engine") != m.get("to_engine") for m in later
        ),
        "tied": open_tie or any(s.get("tied_engines") for s in steps),
        "open_tie": open_tie,
        "overridden": any(s.get("actor") not in (None, "rule") for s in steps),
        "pinned": any(s.get("reason_code") in _PINNING_RULES for s in steps),
    }


def is_listed(kinds: Mapping[str, bool]) -> bool:
    """Whether a query with these ``trace_kinds`` is listed."""
    return any(kinds.get(k) for k in ("moved", "tied", "overridden", "pinned"))


def trace_order(row: Mapping[str, Any]) -> tuple[bool, bool, bool, bool, str]:
    """Most notable first: overridden, an open tie, pinned, moved, then by query id."""
    return (
        not row.get("overridden"),
        not row.get("open_tie"),
        not row.get("pinned"),
        not row.get("moved"),
        str(row.get("query_id")),
    )


_KIND_KEYS = ("moved", "tied", "open_tie", "overridden", "pinned")


SHORT_ID_LENGTH = 8


def _trace_signature(trace: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """What makes two listed queries read the same in the appendix (#1108).

    The same kinds of story, the same path, and at every step the same rule,
    sentence, engine move and candidate scores or gates, then the same final
    engine and scores, runner-up and outscored engines. Anything specific to
    one query (its id, tables, SQL, step ``seq``) is left out, so queries a
    rule moved together share a signature. A JSON string, so it hashes and
    sorts.
    """
    first = trace.get("first_assignment")
    steps = ([first] if isinstance(first, Mapping) else []) + [
        m for m in trace.get("moves") or [] if isinstance(m, Mapping)
    ]
    runner = trace.get("runner_up")
    return json.dumps(
        {
            "kinds": [bool(row.get(k)) for k in _KIND_KEYS],
            "steps": [
                [
                    s.get("version"),
                    s.get("reason_code"),
                    s.get("from_engine"),
                    s.get("to_engine"),
                    s.get("explanation"),
                    [
                        [c.get("engine"), c.get("score"), c.get("gate")]
                        for c in s.get("candidates") or []
                        if isinstance(c, Mapping)
                    ],
                ]
                for s in steps
            ],
            "final": [
                trace.get("final_engine"),
                trace.get("final_score"),
                trace.get("pinned"),
                trace.get("final_group_mean"),
                trace.get("final_version"),
                trace.get("effective_version"),
                sorted(trace.get("tied_engines") or []),
            ],
            # The appendix prints the runner-up's sentence and, from its basis and
            # score, whether it was compared on the group's mean scores.
            "runner_up": (
                [runner.get("explanation"), runner.get("basis"), runner.get("score")]
                if isinstance(runner, Mapping)
                else None
            ),
            "outscored": [
                o.get("explanation")
                for o in trace.get("outscored_by") or []
                if isinstance(o, Mapping)
            ],
            # The appendix prints each key the redesign considered (#1361), which
            # names the query's own columns: queries with other keys read differently.
            "key_candidates": [
                [k.get("column"), k.get("explanation")]
                for k in trace.get("key_candidates") or []
                if isinstance(k, Mapping)
            ],
        },
        sort_keys=True,
    )


def report_query_ids(value: Any) -> set[str]:
    """Every query id anywhere in ``report.json`` (#1108): each ``query_id`` and every
    id of a ``*query_ids`` list, at any depth, so a short id is checked against the
    queries the JSON and the UI name, not only the ones the appendix lists."""
    found: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key == "query_id" and isinstance(child, (str, int)) and child != "":
                    found.add(str(child))
                elif isinstance(key, str) and key.endswith("query_ids") and isinstance(child, list):
                    found.update(str(q) for q in child if isinstance(q, (str, int)) and q != "")
                else:
                    stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return found


def short_query_ids(query_ids: Iterable[str], known_ids: Iterable[str] = ()) -> dict[str, str]:
    """Each of ``query_ids``' short form: its first ``SHORT_ID_LENGTH`` characters, or
    the whole id when another id among them or ``known_ids`` (every other query of the
    report) shares the short form, so a short id never names two of the report's queries."""
    ids = sorted({str(q) for q in query_ids})
    counts: dict[str, int] = {}
    for q in {*ids, *(str(k) for k in known_ids)}:
        counts[q[:SHORT_ID_LENGTH]] = counts.get(q[:SHORT_ID_LENGTH], 0) + 1
    return {q: q if counts[q[:SHORT_ID_LENGTH]] > 1 else q[:SHORT_ID_LENGTH] for q in ids}


def trace_groups(
    traces: Iterable[Mapping[str, Any]],
    rows: Iterable[Mapping[str, Any]],
    known_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """The listed queries with a full trace, grouped by what their trace says (#1108).

    ``rows`` are the facts layer's listed queries (most notable first),
    ``traces`` the full traces. Queries whose trace reads the same
    (``_trace_signature``) form one group: ``query_ids`` in the rows' order,
    ``short_ids`` beside them (unique among them and ``known_ids``, the
    report's other query ids), ``query_count``, ``open_tie_count`` (those
    still in an unresolved tie, so the appendix's headings add up to
    "Unresolved engine ties"), and ``representative_id``, the first of them,
    whose trace the appendix renders once. Groups follow
    the order of their first member, so the output is deterministic. Every
    query with a full trace is in exactly one group; a lone query is a group
    of one.
    """
    by_id = {str(t.get("query_id")): t for t in traces}
    members: dict[str, list[str]] = {}
    open_ties: dict[str, int] = {}
    order: list[str] = []
    for row in rows:
        qid = str(row.get("query_id"))
        trace = by_id.get(qid)
        if not row.get("in_full") or trace is None:
            continue
        sig = _trace_signature(trace, row)
        if sig not in members:
            members[sig] = []
            open_ties[sig] = 0
            order.append(sig)
        members[sig].append(qid)
        open_ties[sig] += 1 if row.get("open_tie") else 0
    short = short_query_ids((q for ids in members.values() for q in ids), known_ids)
    return [
        {
            "representative_id": members[sig][0],
            "query_ids": members[sig],
            "short_ids": [short[q] for q in members[sig]],
            "query_count": len(members[sig]),
            "open_tie_count": open_ties[sig],
        }
        for sig in order
    ]


def query_trace_facts(report: dict[str, Any]) -> dict[str, Any] | None:
    """The figures of ``report["query_traces"]`` (``QueryTraceFacts``), or None without it."""
    digest = report.get("query_traces")
    if not isinstance(digest, dict):
        return None
    full = [t for t in digest.get("queries") or [] if isinstance(t, dict) and t.get("query_id")]
    rows: list[dict[str, Any]] = [
        {"query_id": str(t["query_id"]), **trace_kinds(t), "in_full": True} for t in full
    ]
    rows += [
        {
            "query_id": str(m["query_id"]),
            **{k: bool(m.get(k)) for k in _KIND_KEYS},
            "in_full": False,
        }
        for m in digest.get("more_listed") or []
        if isinstance(m, dict) and m.get("query_id")
    ]
    rows.sort(key=trace_order)
    total = digest.get("query_count")
    total = total if isinstance(total, int) and not isinstance(total, bool) else len(rows)
    in_full = sum(1 for row in rows if row["in_full"])
    groups = trace_groups(full, rows, report_query_ids(report))
    return {
        "has_ledger": bool(digest.get("has_ledger")),
        "traced_query_count": total,
        "listed_query_count": len(rows),
        "full_trace_cap": TRACE_FULL_CAP,
        "full_trace_count": in_full,
        "not_in_full_count": len(rows) - in_full,
        "moved_query_count": sum(1 for row in rows if row["moved"]),
        "ever_tied_query_count": sum(1 for row in rows if row["tied"]),
        "open_tie_query_count": sum(1 for row in rows if row["open_tie"]),
        "overridden_query_count": sum(1 for row in rows if row["overridden"]),
        "pinned_query_count": sum(1 for row in rows if row["pinned"]),
        "unlisted_query_count": max(total - len(rows), 0),
        "listed_queries": rows,
        "trace_group_count": len(groups),
        "trace_groups": groups,
        "unlisted_rules": [
            {
                "rule": str(r.get("rule")),
                "label": str(r.get("label") or r.get("rule")),
                "rule_query_count": int(r.get("query_count") or 0),
            }
            for r in digest.get("unlisted_rules") or []
            if isinstance(r, dict)
        ],
    }


# ---------------------------------------------------------------------------
# Close calls (#730)
# ---------------------------------------------------------------------------

# A query is a close call when its runner-up -- an engine the final
# assignment keeps that can take the query (the "Why this engine" trace's
# runner-up; a gated one has no gap) -- scores less than this many points
# below its final engine. Chosen from the runner-up gaps of the three samples
# (#730). Per point from 0 to 9, discourse has 214, 137, 97, 174, 142, 41, 96,
# 127, 42, 23 queries: a real dip at 5. Wordpress (5, 18, 9, 5, 3, 1, 3, 0, 0,
# 0) only falls off, and trailgear (0, 1, 10, 3, 1, 2, 7, 0, 5, 1) is too
# small to show a shape, so only discourse supports 5 specifically; neither
# contradicts it. The reality check's unique-value line (15 points, ADR-031
# Decision 7) answers another question, whether an engine is worth running at
# all: as a per-query line it would call 75% of discourse's queries close.
CLOSE_CALL_MARGIN = 5.0

# Each engine pair names at most this many of its queries (the closest close
# calls first, then the settled ties); report.json's
# ``query_traces.close_calls`` lists every one.
CLOSE_CALL_IDS_SHOWN = 5


def engine_choice_facts(report: Mapping[str, Any]) -> dict[str, Any]:
    """The pending engine choices' figures (#1003): how many choices (one per
    table) and the tied queries they cover, the same grouped by tied engines
    (``pending_choice_pairs``), and the choices ``--auto`` decided, grouped by the
    engine it placed them on (``auto_decided_by_engine``)."""
    pending = [c for c in report.get("pending_engine_choices") or [] if isinstance(c, Mapping)]
    decided = [d for d in report.get("auto_decided_choices") or [] if isinstance(d, Mapping)]
    pairs: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for c in pending:
        pairs.setdefault(tuple(sorted(c.get("tied_engines") or [])), []).append(c)
    by_engine: dict[str, list[Mapping[str, Any]]] = {}
    for d in decided:
        by_engine.setdefault(str(d.get("engine") or ""), []).append(d)
    return {
        "pending_choice_count": len(pending),
        "pending_choice_query_count": sum(len(c.get("query_ids") or []) for c in pending),
        "pending_choice_pairs": [
            {
                "engines": list(engines),
                "choice_count": len(rows),
                "query_count": sum(len(c.get("query_ids") or []) for c in rows),
                "tables": sorted(str(c.get("table") or "") for c in rows),
            }
            for engines, rows in sorted(pairs.items())
        ],
        "auto_decided_choice_count": len(decided),
        "auto_decided_query_count": sum(len(d.get("query_ids") or []) for d in decided),
        "auto_decided_by_engine": [
            {
                "engine": engine,
                "choice_count": len(rows),
                "query_count": sum(len(d.get("query_ids") or []) for d in rows),
                "tables": sorted(str(d.get("table") or "") for d in rows),
            }
            for engine, rows in sorted(by_engine.items())
        ],
    }


def is_close_call(trace: Mapping[str, Any]) -> bool:
    """Whether a query's decision trace (``QueryTraceResponse`` as JSON) is a close call.

    Its runner-up is less than ``CLOSE_CALL_MARGIN`` points behind the final
    engine (a gated runner-up has no gap, so it never is); an exact tie a
    routing rule settled is selected too, and counted apart
    (``CloseCall.settled_tie``, set from the Moves' tie rule). A tie still open at the end is not: it is a
    pending choice, listed as one (``tied_queries``). Synthesis selects the
    ``query_traces.close_calls`` rows with this, the facts layer groups them.
    """
    if trace.get("tied_engines"):
        return False
    runner = trace.get("runner_up")
    if not isinstance(runner, Mapping):
        return False
    gap = runner.get("gap")
    return (
        isinstance(gap, (int, float)) and not isinstance(gap, bool) and 0 <= gap < CLOSE_CALL_MARGIN
    )


def close_call_facts(report: dict[str, Any]) -> dict[str, Any] | None:
    """The close calls by engine and alternative (``CloseCallFacts``), or None.

    None for a report whose ``query_traces`` predates ``close_calls``.
    ``close_call_query_count`` counts the real close calls (a gap above 0);
    the exact ties a rule settled are ``settled_tie_count``, stated apart so
    the headline stays the queries a few points decided. Each pair says how
    many of each its engine won over the alternative, how many were compared
    on a co-dependency group's mean (they move with their group), the close
    calls' gap range, and the first ``CLOSE_CALL_IDS_SHOWN`` query ids
    (closest close calls first, then the ties). Largest pair first.
    """
    digest = report.get("query_traces")
    if not isinstance(digest, dict) or not isinstance(digest.get("close_calls"), list):
        return None
    rows = [
        r
        for r in digest["close_calls"]
        if isinstance(r, dict)
        and r.get("query_id")
        and r.get("engine")
        and r.get("alternative")
        and isinstance(r.get("gap"), (int, float))
    ]
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        by_pair.setdefault((str(r["engine"]), str(r["alternative"])), []).append(r)
    pairs: list[dict[str, Any]] = []
    for (engine, alternative), members in by_pair.items():
        members.sort(
            key=lambda r: (bool(r.get("settled_tie")), float(r["gap"]), str(r["query_id"]))
        )
        gaps = [float(r["gap"]) for r in members if not r.get("settled_tie")]
        ties = sum(1 for r in members if r.get("settled_tie"))
        pairs.append(
            {
                "engine": engine,
                "alternative": alternative,
                "pair_query_count": len(gaps),
                "exact_tie_count": ties,
                "group_mean_count": sum(1 for r in members if r.get("basis") == "group_mean"),
                "min_gap": min(gaps) if gaps else 0.0,
                "max_gap": max(gaps) if gaps else 0.0,
                "query_ids": [str(r["query_id"]) for r in members[:CLOSE_CALL_IDS_SHOWN]],
                "more_query_count": max(len(members) - CLOSE_CALL_IDS_SHOWN, 0),
            }
        )
    pairs.sort(
        key=lambda p: (-p["pair_query_count"], -p["exact_tie_count"], p["engine"], p["alternative"])
    )
    ties = sum(1 for r in rows if r.get("settled_tie"))
    return {
        "margin_points": CLOSE_CALL_MARGIN,
        "close_call_query_count": len(rows) - ties,
        "settled_tie_count": ties,
        "engine_pairs": pairs,
    }


# ---------------------------------------------------------------------------
# Queries no kept engine can serve: on a fallback engine (#729), flagged (#888)
# ---------------------------------------------------------------------------

# The rules that place a query no analyzed engine can take (ADR-031 Decision 2;
# ``move_ledger.ReasonCode``'s fallback codes).
FALLBACK_RULES = frozenset({"fallback.no_score", "fallback.no_owner", "fallback.no_group_owner"})

# At most this many of them are named in the facts and the deliverables;
# report.json's ``query_traces.unservable`` lists every one.
UNSERVABLE_SHOWN = 50


def deciding_step(trace: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The step of a decision trace (JSON) that put the query on its final engine.

    The last step that changed its engine onto the final one (the first
    assignment counts), else the last step: a later tie-only change or
    restatement does not decide the engine. The same rule as the trace
    builder's own ``_deciding_step``.
    """
    first = trace.get("first_assignment")
    steps = ([first] if isinstance(first, Mapping) else []) + [
        m for m in trace.get("moves") or [] if isinstance(m, Mapping)
    ]
    if not steps:
        return None
    final = trace.get("final_engine")
    for step in reversed(steps):
        if step.get("to_engine") == final and step.get("from_engine") != step.get("to_engine"):
            return step
    return steps[-1]


def is_unservable(trace: Mapping[str, Any]) -> bool:
    """Whether a query's decision trace (``QueryTraceResponse`` as JSON) ends
    on a fallback engine because no analyzed engine fit it.

    Its engine was decided by a fallback rule (no analyzed engine could take
    it), or every candidate of the deciding step is gated (the final engine
    included). Synthesis selects the ``query_traces.unservable`` rows with
    this; the facts layer states them.
    """
    step = deciding_step(trace)
    if step is None:
        return False
    if step.get("reason_code") in FALLBACK_RULES:
        return True
    candidates = [c for c in step.get("candidates") or [] if isinstance(c, Mapping)]
    return bool(candidates) and all(c.get("gate") for c in candidates)


def _flagged_unservable_rows(report: dict[str, Any], listed: set[str]) -> list[dict[str, Any]]:
    """``report.json``'s ``flagged_unservable`` rows (#888) as ``unservable`` rows of
    kind ``flagged``; a query the decision trace already lists keeps that row."""
    rows: list[dict[str, Any]] = []
    for r in report.get("flagged_unservable") or []:
        if not isinstance(r, dict) or not r.get("query_id") or str(r["query_id"]) in listed:
            continue
        engine = str(r.get("engine") or "")
        rows.append(
            {
                "query_id": str(r["query_id"]),
                "engine": engine,
                "kind": "held" if r.get("cause") == "held_by_pin" else "flagged",
                "rule": "",
                "explanation": str(r.get("explanation") or ""),
                "blocking_gates": [
                    {
                        "engine": engine,
                        "gate": str(r.get("gate") or "model_flag"),
                        "gate_text": str(r.get("gate_text") or ""),
                    }
                ],
                "required_capabilities": [str(c) for c in r.get("required_capabilities") or []],
                "reason_category": str(r.get("reason_category") or "other"),
            }
        )
    return rows


def flagged_unservable_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``flagged`` and ``held`` rows ``facts.unservable`` counts (#888, #1033):
    ``report.json``'s ``flagged_unservable`` less any query the decision trace
    already lists, by engine then query id. The migration risk register raises
    them from this same list (#1148), so its counts agree with the facts."""
    digest = report.get("query_traces")
    traced = digest.get("unservable") if isinstance(digest, dict) else None
    listed = {
        str(r["query_id"])
        for r in (traced if isinstance(traced, list) else [])
        if isinstance(r, dict) and r.get("query_id")
    }
    rows = _flagged_unservable_rows(report, listed)
    rows.sort(key=lambda r: (str(r.get("engine") or ""), str(r["query_id"])))
    return rows


# The kind of a query no engine in the customer's engine scope can serve (#1329).
NO_ENGINE_IN_SCOPE = "no_engine_in_scope"


def scope_unservable_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """``report.json``'s ``scope_unservable`` rows (#1329) as ``unservable`` rows of
    kind ``no_engine_in_scope``: no engine (``engine`` empty), the engine the
    fallback rule names in ``closest_engine``, each in-scope engine's gate, by query
    id. The migration risk register raises them from this same list, so the two agree."""
    rows: list[dict[str, Any]] = []
    for r in report.get("scope_unservable") or []:
        if not isinstance(r, dict) or not r.get("query_id"):
            continue
        gates = [g for g in r.get("gates") or [] if isinstance(g, dict)]
        rows.append(
            {
                "query_id": str(r["query_id"]),
                "engine": "",
                "kind": NO_ENGINE_IN_SCOPE,
                "rule": str(r.get("rule") or ""),
                "explanation": str(r.get("reason") or ""),
                "blocking_gates": [
                    {
                        "engine": str(g.get("engine") or ""),
                        "gate": str(g.get("gate") or ""),
                        "gate_text": str(g.get("gate_text") or ""),
                    }
                    for g in gates
                ],
                "required_capabilities": [str(c) for c in r.get("required_capabilities") or []],
                "closest_engine": str(r.get("closest_engine") or ""),
                "source_tables": [str(t) for t in r.get("source_tables") or [] if t],
            }
        )
    rows.sort(key=lambda r: r["query_id"])
    return rows


def unservable_query_facts(report: dict[str, Any]) -> dict[str, Any] | None:
    """The queries no kept engine can serve (``UnservableFacts``), or None.

    Five kinds: ``fallback`` and ``gated_everywhere`` from
    ``query_traces.unservable`` (#729), ``flagged`` and ``held`` from
    ``flagged_unservable`` (#888: a review flagged it and no other kept engine
    can take it, so it stays; #1033: a review flagged it and a customer pin
    keeps its table's writes there, so it stays), and ``no_engine_in_scope`` from
    ``scope_unservable`` (#1329: no engine in the customer's engine scope can serve
    it, so it has no engine; ``closest_engine`` names the one the scope left out).
    None for a report with none of these lists. Each
    query keeps its own sentence for why it sits where it does, the gate that
    ruled out each engine, the capabilities it needs and a redesign hint for
    each (``capability_labels.redesign_hints``; the flag's reason category,
    ``flag_redesign_hints``, for a flagged one), by engine then query id; at
    most ``UNSERVABLE_SHOWN`` are named.
    """
    digest = report.get("query_traces")
    traced = isinstance(digest, dict) and isinstance(digest.get("unservable"), list)
    flagged_list = report.get("flagged_unservable")
    # #1329: the queries no engine in the customer's scope can serve (no engine).
    scoped = scope_unservable_rows(report)
    if not traced and not (isinstance(flagged_list, list) and flagged_list) and not scoped:
        return None
    traced_rows = digest["unservable"] if traced else []  # type: ignore[index]
    rows = [r for r in traced_rows if isinstance(r, dict) and r.get("query_id")]
    rows += _flagged_unservable_rows(report, {str(r["query_id"]) for r in rows})
    rows += scoped
    rows.sort(key=lambda r: (str(r.get("engine") or ""), str(r["query_id"])))
    out: list[dict[str, Any]] = []
    for r in rows[:UNSERVABLE_SHOWN]:
        gates = [g for g in r.get("blocking_gates") or [] if isinstance(g, dict)]
        caps = [str(c) for c in r.get("required_capabilities") or []]
        kind = str(r.get("kind") or "fallback")
        hints = (
            flag_redesign_hints(str(r.get("engine") or ""), r.get("reason_category"))
            if kind in ("flagged", "held")
            else redesign_hints(
                caps, [(str(g.get("engine") or ""), str(g.get("gate") or "")) for g in gates]
            )
        )
        out.append(
            {
                "query_id": str(r["query_id"]),
                "engine": str(r.get("engine") or ""),
                "kind": kind,
                "rule": str(r.get("rule") or ""),
                "explanation": str(r.get("explanation") or ""),
                "blocking_gates": [
                    {
                        "engine": str(g.get("engine") or ""),
                        "gate": str(g.get("gate") or ""),
                        "gate_text": str(g.get("gate_text") or ""),
                    }
                    for g in gates
                ],
                "required_capabilities": sorted(set(caps)),
                "capability_labels": [capability_label(c) for c in sorted(set(caps))],
                "redesign_hints": hints,
                "closest_engine": str(r.get("closest_engine") or ""),
            }
        )
    kinds = [str(r.get("kind") or "fallback") for r in rows]
    return {
        "unservable_query_count": len(rows),
        "fallback_query_count": kinds.count("fallback"),
        "gated_everywhere_query_count": kinds.count("gated_everywhere"),
        "flagged_query_count": kinds.count("flagged"),
        "held_query_count": kinds.count("held"),
        "no_fit_query_count": kinds.count("fallback") + kinds.count("gated_everywhere"),
        "no_engine_in_scope_query_count": kinds.count(NO_ENGINE_IN_SCOPE),
        # #1329: the engines those queries would fall back to, which the scope leaves out.
        "no_engine_in_scope_closest_engines": sorted(
            {
                str(r.get("closest_engine"))
                for r in rows
                if r.get("kind") == NO_ENGINE_IN_SCOPE and r.get("closest_engine")
            }
        ),
        "unservable_queries": out,
        "more_unservable_count": max(len(rows) - UNSERVABLE_SHOWN, 0),
    }


# ---------------------------------------------------------------------------
# Query group counts (#1410)
# ---------------------------------------------------------------------------


# A relational group with no real source table (#478), as synthesis labels it: a
# utility/session/DDL statement, or a query the collector found no table for. Listed
# last under "Queries on Aurora by table": neither is a busiest table.
UTILITY_GROUP_LABEL = "Utility and session statements"
UNRESOLVED_TABLE_GROUP_LABEL = "Table not identified by the collector"


def is_listed_query_group(group: Mapping[str, Any]) -> bool:
    """Whether the "Query groups" table lists a group: a non-relational engine serves
    it. A group only relational engines serve has its queries listed per table under
    "Queries on Aurora by table" instead (#478)."""
    return not set(group.get("engines") or []).issubset(RELATIONAL_ENGINES)


def query_group_at(report: dict[str, Any], index: Any) -> dict[str, Any]:
    """The ``query_groups`` entry a ``QueryGroupFacts`` index names, or {} (#1410)."""
    groups = report.get("query_groups")
    if not isinstance(groups, list) or not isinstance(index, int) or index < 0:
        return {}
    group = groups[index] if index < len(groups) else None
    return group if isinstance(group, dict) else {}


def query_group_facts(report: dict[str, Any]) -> dict[str, Any] | None:
    """The engineering report's query group figures (``QueryGroupFacts``), or None.

    From ``query_groups``: every group; the ones the "Query groups" table lists
    (``is_listed_query_group``, by their index in ``query_groups``, so the heading
    and the rows are one list); the relational-only rest, with their engines; and the
    "Queries on Aurora by table" rows: per relational engine and group, that engine's
    own queries and calls/s, busiest first with the two blame groups last, and each
    engine's query total; and the in-scope queries no group of their own engine serves
    (``ungrouped_queries``, #226). None for a report with no ``query_groups`` field.
    """
    groups = report.get("query_groups")
    if not isinstance(groups, list):
        return None
    indexed = [(i, g) for i, g in enumerate(groups) if isinstance(g, dict)]
    listed = [i for i, g in indexed if is_listed_query_group(g)]
    relational_only = [g for _, g in indexed if not is_listed_query_group(g)]
    rows: list[dict[str, Any]] = []
    engines = sorted(
        {
            str(ap["engine"])
            for _, g in indexed
            for ap in g.get("access_patterns") or []
            if isinstance(ap, dict) and ap.get("engine") in RELATIONAL_ENGINES
        }
    )
    query_counts: dict[str, int] = {}
    for engine in engines:
        engine_rows: list[dict[str, Any]] = []
        for i, g in indexed:
            aps = [
                ap
                for ap in g.get("access_patterns") or []
                if isinstance(ap, dict) and ap.get("engine") == engine
            ]
            if not aps:
                continue
            query_ids = {str(q) for ap in aps for q in ap.get("query_ids") or []}
            engine_rows.append(
                {
                    "engine": engine,
                    "group_index": i,
                    "query_count": len(query_ids),
                    "calls_per_second": _unrounded(
                        sum(float(ap.get("design_rps") or 0) for ap in aps)
                    ),
                    "blame": str(g.get("group_name"))
                    in (UTILITY_GROUP_LABEL, UNRESOLVED_TABLE_GROUP_LABEL),
                }
            )
        engine_rows.sort(key=lambda r: (bool(r["blame"]), -float(r["calls_per_second"])))
        query_counts[engine] = sum(int(r["query_count"]) for r in engine_rows)
        rows += [{k: v for k, v in r.items() if k != "blame"} for r in engine_rows]
    return {
        "group_count": len(indexed),
        "listed_group_count": len(listed),
        "relational_only_group_count": len(relational_only),
        "relational_only_engines": sorted(
            {str(e) for g in relational_only for e in g.get("engines") or [] if e}
        ),
        "listed_group_indexes": listed,
        "relational_rows": rows,
        "relational_query_counts": query_counts,
        "ungrouped_queries": _ungrouped_query_rows(report, [g for _, g in indexed]),
    }


def _ungrouped_query_rows(report: dict[str, Any], groups: list[dict[str, Any]]) -> list[dict]:
    """Per owner engine, its in-scope queries that no group of its own serves (#226).

    The in-scope queries and their engines are ``query_pattern_groups``' members. A query
    is grouped when an access pattern of its own engine in an owner group (not one of
    the cache layer's, ``cache_layer``) lists it. ``schema_design_available`` is the
    engine's ranking row's: without a design, the engine has no access patterns to
    group its queries by yet. Sorted by engine id; empty with no ``query_pattern_groups``.
    """
    served: set[tuple[str, str]] = {
        (str(ap["engine"]), str(q))
        for g in groups
        if not g.get("cache_layer")
        for ap in g.get("access_patterns") or []
        if isinstance(ap, dict) and ap.get("engine")
        for q in ap.get("query_ids") or []
    }
    missing: dict[str, set[str]] = {}
    for pattern in report.get("query_pattern_groups") or []:
        if not isinstance(pattern, dict):
            continue
        for m in pattern.get("members") or []:
            engine = m.get("engine") if isinstance(m, dict) else None
            if not isinstance(engine, str) or not engine or engine in CACHE_ENGINES:
                continue
            qid = str(m.get("query_id") or "")
            if qid and (engine, qid) not in served:
                missing.setdefault(engine, set()).add(qid)
    designed = {
        str(r.get("target")): bool(r.get("schema_design_available"))
        for r in report.get("ranking") or []
        if isinstance(r, dict)
    }
    return [
        {
            "engine": engine,
            "query_count": len(ids),
            "schema_design_available": designed.get(engine, False),
        }
        for engine, ids in sorted(missing.items())
    ]


# ---------------------------------------------------------------------------
# Access-pattern totals (#792)
# ---------------------------------------------------------------------------


def access_pattern_facts(report: dict[str, Any]) -> dict[str, Any] | None:
    """The access-pattern totals the results page shows (``AccessPatternFacts``), or None.

    Counted from ``query_groups``, where every engine's designed patterns sit:
    one per ``(engine, pattern_id)`` (a pattern listed in several groups counts
    once; one with no id counts each time), in scope or not, as the explorer
    lists them. A relational query still awaiting its schema design is a
    routing record, not an access pattern, and is left out. The cache layer's
    own patterns are counted apart: they are never owned workload (#361). None
    for a report with no ``query_groups`` field.
    """
    groups = report.get("query_groups")
    if not isinstance(groups, list):
        return None
    seen: set[tuple[str, str]] = set()
    cache_layer = 0
    owned = 0
    is_cache: dict[str, bool] = {}
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("access_patterns"), list):
            continue
        for ap in group["access_patterns"]:
            engine = ap.get("engine") if isinstance(ap, dict) else None
            if not isinstance(engine, str) or not engine:
                continue
            pid = ap.get("pattern_id")
            if isinstance(pid, str) and pid:
                if pid.startswith(f"relational-{engine}-"):
                    continue
                if (engine, pid) in seen:
                    continue
                seen.add((engine, pid))
            if engine not in is_cache:
                is_cache[engine] = cache_layer_counts(report, engine) is not None
            if is_cache[engine]:
                cache_layer += 1
                continue
            owned += 1
    return {
        "owned_pattern_count": owned,
        "cache_layer_pattern_count": cache_layer,
        "listed_pattern_count": owned + cache_layer,
    }


# Roles whose queries leave the source engine: a migration target (the Aurora engine
# a heterogeneous source moves to included) and a search read model (#1020).
MOVING_ROLES = (MIGRATION_TARGET, SEARCH_READ_MODEL)


def _owns_queries(engine: Mapping[str, Any]) -> bool:
    """An owner engine: every engine but the cache layer, which owns no query (#296)."""
    return engine.get("role") != CACHE_LAYER and engine.get("cached_queries") is None


def query_move_facts(report: dict[str, Any], engines: list[dict[str, Any]]) -> dict[str, int]:
    """The in-scope queries the owner engines serve and how many of them move (#1020).

    Each engine's count is its ranking entry's ``assigned_queries_in_scope``
    (``assigned_queries`` for a report without it); the cache layer owns no query
    (#296), and a query moves when its engine's role is one of ``MOVING_ROLES``. The
    retained relational core keeps its queries where they run today.
    """
    counts: dict[str, int] = {}
    for r in report.get("ranking") or []:
        if not isinstance(r, dict) or not r.get("target"):
            continue
        n = r.get("assigned_queries_in_scope", r.get("assigned_queries"))
        if isinstance(n, int) and not isinstance(n, bool):
            counts[str(r["target"])] = n
    in_scope = moving = 0
    for e in engines:
        if not _owns_queries(e):
            continue
        n = counts.get(str(e.get("engine")), 0)
        in_scope += n
        if e.get("role") in MOVING_ROLES:
            moving += n
    return {"in_scope_query_count": in_scope, "moving_query_count": moving}


def query_total_facts(report: dict[str, Any], in_scope_query_count: int) -> dict[str, int]:
    """The queries with no engine in the customer's engine scope (#1329), in no engine's
    count, and the total the headline names with them ("65 of 107 queries move; 42 have
    no engine in scope"): ``in_scope_query_count`` plus that count."""
    no_engine = len(scope_unservable_rows(report))
    return {
        "no_engine_in_scope_query_count": no_engine,
        "query_total_count": in_scope_query_count + no_engine,
    }


# ---------------------------------------------------------------------------
# Queries and the distinct query patterns they make (#1146)
# ---------------------------------------------------------------------------


def query_pattern_facts(
    report: dict[str, Any], engines: list[dict[str, Any]]
) -> dict[str, int] | None:
    """The "N queries in M distinct query patterns" figures (``QueryPatternFacts``), or None
    for a report with no ``query_pattern_groups`` (written before #1146).

    Counted over the same queries as ``in_scope_query_count``: the in-scope queries whose
    engine is an owner engine of the plan (``query_move_facts``), so N agrees with it. A
    pattern counts once however many of those queries make it; a pattern with none of
    them is not counted. ``split_pattern_count``: patterns whose counted queries sit on
    more than one engine (routing is per query, ADR-031).
    """
    groups = report.get("query_pattern_groups")
    if not isinstance(groups, list):
        return None
    owners = {str(e.get("engine")) for e in engines if _owns_queries(e)}
    queries = patterns = repeated = repeated_queries = split = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        members = [
            m
            for m in group.get("members") or []
            if isinstance(m, dict) and str(m.get("engine")) in owners
        ]
        if not members:
            continue
        patterns += 1
        queries += len(members)
        if len(members) > 1:
            repeated += 1
            repeated_queries += len(members)
        if len({str(m.get("engine")) for m in members}) > 1:
            split += 1
    return {
        "query_count": queries,
        "pattern_count": patterns,
        "repeated_pattern_count": repeated,
        "repeated_query_count": repeated_queries,
        "split_pattern_count": split,
    }


# ---------------------------------------------------------------------------
# Sources of truth and replicas (#992, ADR-031 Amendment 9)
# ---------------------------------------------------------------------------

# How many replica-served reads a table's risk sentence names; the fact keeps them all.
REPLICA_READS_NAMED = 5
# How many replicated tables every deliverable lists (most replica reads first);
# report.json's ``table_sources`` holds every written table.
TABLE_SOURCES_SHOWN = 25


def replica_risk_text(source: str, replicas: list[str], read_count: int) -> str:
    """The eventual-consistency risk of a replicated table: the mechanism, its
    bound and the mitigation (#951). The reads are counted, not listed: their
    ids are ``replica_read_query_ids``, next to this text."""
    noun = "read" if read_count == 1 else "reads"
    # #1329: the source database, not an engine, when the scope left its writes no engine.
    source_name = "the source database" if source == SOURCE_DATABASE else display_engine(source)
    return (
        f"Eventually consistent: {read_count} {noun} on "
        f"{', '.join(display_engine(e) for e in replicas)} read a copy kept in sync from "
        f"{source_name} via CDC, so each sees a write only after CDC applies it, behind by "
        f"the replication lag; a read that must see its own write should read from "
        f"{source_name}."
    )


def shared_replica_risk_text(sources: list[str]) -> str:
    """The same risk, once for every replicated table whose source of truth is one
    of ``sources``: the engineering report's table gives each table's source, its
    replicas and its read count, so the sentence names none of them (#1170)."""
    names = [display_engine(s) for s in sources]
    if len(names) == 1:
        source, mitigation = names[0], names[0]
    else:
        listed = ", ".join(names[:-1]) + f" or {names[-1]}"
        source, mitigation = f"its source of truth ({listed})", "the source of truth"
    return (
        f"Eventually consistent: a read on a replica reads a copy kept in sync from {source} "
        "via CDC, so it sees a write only after CDC applies it, behind by the replication "
        f"lag; a read that must see its own write should read from {mitigation}."
    )


def _replica_risk(source: str, replicas: list[str], reads: list[str]) -> str:
    return replica_risk_text(source, replicas, len(reads))


def shares_replica_risk(row: dict[str, Any]) -> bool:
    """Whether a replicated table's stored ``risk`` is the standard sentence
    (``replica_risk_text`` over its own source, replicas and read count), so the
    deliverables state it once with ``shared_replica_risk_text`` (#1170, #1232).
    A risk stored with other wording (an older report's) is not: each deliverable
    prints it for its own table. Decided here once, never by a renderer."""
    count = row.get("replica_read_count")
    if not isinstance(count, int) or isinstance(count, bool):
        return False
    source = str(row.get("source_of_truth", ""))
    replicas = [str(e) for e in row.get("replica_engines") or []]
    return bool(row.get("risk") == replica_risk_text(source, replicas, count))


def table_source_facts(report: dict[str, Any]) -> dict[str, Any] | None:
    """Each written table's source of truth and replicas (``TableSourceFacts``), or None.

    From ``table_sources``: a written table whose reads all run on its source of
    truth is a single-engine table; one with a read on another engine is
    replicated, with its replica engines (CDC runs from the source of truth to
    each), its replica-served reads and the eventual-consistency risk naming them.
    None for a report with no ``table_sources``.
    """
    rows = report.get("table_sources")
    if not isinstance(rows, list):
        return None
    replicated: list[dict[str, Any]] = []
    single: list[str] = []
    written = split = 0
    for row in sorted(
        (r for r in rows if isinstance(r, dict) and r.get("table")), key=lambda r: str(r["table"])
    ):
        written += 1
        source = row.get("source_of_truth")
        if not source:
            split += 1
            continue
        replicas = [r for r in row.get("replicas") or [] if isinstance(r, dict) and r.get("engine")]
        if not replicas:
            single.append(str(row["table"]))
            continue
        engines = [str(r["engine"]) for r in replicas]
        reads = sorted({str(q) for r in replicas for q in r.get("read_query_ids") or []})
        replicated.append(
            {
                "table": str(row["table"]),
                "source_of_truth": str(source),
                "replica_engines": engines,
                "write_query_count": len(row.get("write_query_ids") or []),
                "replica_read_query_ids": reads,
                "replica_read_count": len(reads),
                "risk": _replica_risk(str(source), engines, reads),
            }
        )
        replicated[-1]["shares_replica_risk"] = shares_replica_risk(replicated[-1])
    replicated.sort(key=lambda r: (-r["replica_read_count"], r["table"]))
    return {
        "written_table_count": written,
        "replicated_table_count": len(replicated),
        "split_write_table_count": split,
        "replicated_tables": replicated[:TABLE_SOURCES_SHOWN],
        "more_replicated_count": max(len(replicated) - TABLE_SOURCES_SHOWN, 0),
        "single_engine_tables": single,
        "single_engine_table_count": len(single),
        # #1329: tables whose writes have no engine in the customer's scope, so the
        # source database stays their writer.
        "source_database_table_count": sum(
            1
            for r in rows
            if isinstance(r, dict)
            and r.get("table")
            and r.get("source_of_truth") == SOURCE_DATABASE
        ),
    }


# ---------------------------------------------------------------------------
# Table owners (#212, ADR-031 Amendment 9)
# ---------------------------------------------------------------------------

# Why a table's owner is its owner. The owner itself is always the wave plan's
# (``migration_waves.wave_table_owners``, ``_durable_owner``): its source of truth,
# else its ``primary_engine`` (``migration_wave_engines.primary_owner_engine``), else
# the retained engine; this only says which of those applied.
OWNER_BY_WRITES = "writes"  # the one engine its in-scope writes sit on (source of truth)
OWNER_BY_SPLIT_WRITES = "split_writes"  # writes on several engines: no single writer
OWNER_BY_MOST_QUERIES = "most_queries"  # no write: the engine serving most of its queries
OWNER_BY_RETAINED = "retained"  # no owner engine queries it: wave 1's retained engine
# A report written before #212 that carries no query of the table: its design's engine.
OWNER_BY_DESIGN = "design"
OWNER_BASES = (
    OWNER_BY_WRITES,
    OWNER_BY_SPLIT_WRITES,
    OWNER_BY_MOST_QUERIES,
    OWNER_BY_RETAINED,
    OWNER_BY_DESIGN,
)


def table_owner_line(
    writes: Mapping[str, int],
    queries: Mapping[str, int],
    cached_by: Iterable[str],
    design_engines: Iterable[str],
    owner: str | None = None,
    retained_engine: str | None = None,
    design_fallback: bool = False,
) -> dict[str, Any]:
    """One source table's owner, the engines that also read it and its cache (#212).

    ``writes`` and ``queries`` count the table's in-scope write statements and all its
    in-scope queries per assigned engine; ``cached_by`` names the cache engines in front
    of a read of it; ``design_engines`` are the schema designs that map it (its
    ``table_mappings`` row). ``owner`` is the wave plan's owner of the table
    (``migration_waves.wave_table_owners``), when the caller has it; ``retained_engine``
    is wave 1's retained engine.

    The owner is the wave plan's, so the Migration map, the wave plan's owner lines and
    kept-in-sync counts and Sources of truth never disagree (ADR-031 Amendment 9). With
    no ``owner`` given (a report written before #212) the same rule is applied here:
    the one engine its writes sit on, else the owner engine serving most of its queries
    (``primary_owner_engine``: a tie to the retained engine), else the retained engine.
    A cache or search engine (``NON_OWNER_ENGINES``) never owns a table.
    ``design_fallback`` is for such an older report only: a table it carries no query
    of (so the rule has nothing to count) takes its design's engine before the
    retained one (``design``).

    - **owner_basis**: ``writes`` (one writer), ``split_writes`` (writes on several
      engines: no single writer, as Sources of truth says; the owner is still the wave
      plan's), ``most_queries`` (no write), ``retained`` (no owner engine queries it).
    - **also_read_by**: every other engine with an in-scope query on the table, which
      reads a copy kept in sync from the owner via CDC; most queries first, then by id.
    - **cached_by**: the cache engine fronting one of its reads, or whose schema design
      keys it; None when neither.
    """
    w = [str(e) for e, n in writes.items() if e and n]
    q = {str(e): int(n) for e, n in queries.items() if e and n and e not in CACHE_ENGINES}
    retained = retained_engine if retained_engine not in NON_OWNER_ENGINES else None
    designs = [str(e) for e in design_engines if e]
    by_design = False
    if not owner or owner in NON_OWNER_ENGINES:
        design = next((e for e in designs if e not in NON_OWNER_ENGINES), None)
        if len(w) == 1 and w[0] not in NON_OWNER_ENGINES:
            owner = w[0]
        elif (counted := primary_owner_engine(q, retained)) is not None:
            owner = counted
        elif design_fallback and not w and not q and design:
            owner, by_design = design, True
        else:
            owner = retained
    if by_design:
        basis: str | None = OWNER_BY_DESIGN
    elif len(w) > 1:
        basis = OWNER_BY_SPLIT_WRITES
    elif w:
        basis = OWNER_BY_WRITES
    elif any(e not in NON_OWNER_ENGINES for e in q):
        basis = OWNER_BY_MOST_QUERIES
    else:
        basis = OWNER_BY_RETAINED if owner else None
    caches = [str(e) for e in cached_by if e] + [e for e in designs if e in CACHE_ENGINES]
    return {
        "owner": owner or None,
        "owner_basis": basis,
        "also_read_by": sorted((e for e in q if e != owner), key=lambda e: (-q[e], e)),
        "cached_by": caches[0] if caches else None,
    }


def _statements(n: int) -> str:
    return f"{n} {'statement' if n == 1 else 'statements'}"


def _engine_list(engines: Iterable[str]) -> str:
    names = [display_engine(str(e)) for e in engines]
    return ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else "".join(names)


def table_owner_reason(
    line: Mapping[str, Any],
    writes: Mapping[str, int],
    queries: Mapping[str, int],
    retained_engine: str | None = None,
) -> str | None:
    """Why a table's owner (:func:`table_owner_line`) is its owner, in one sentence (#1390).

    ``writes`` and ``queries`` are the table's usage the line was built from
    (:func:`assignment_table_usage`): its write statements and all its queries per
    engine; ``retained_engine`` is the one the line was given. The sentence states the
    owner basis with the counts behind it (and how a tie on queries was settled:
    ``primary_owner_engine``), then the engines that read a copy. None when the line
    has no owner basis.
    """
    owner = str(line.get("owner") or "")
    basis = line.get("owner_basis")
    if not basis or not owner:
        return None
    name = display_engine(owner)
    served = {str(e): int(n) for e, n in queries.items() if e and n and e not in NON_OWNER_ENGINES}
    total = sum(n for e, n in queries.items() if e and n and e not in CACHE_ENGINES)
    on_owner = served.get(owner, 0)
    tied = sorted(e for e, n in served.items() if e != owner and n == on_owner)
    if total == on_owner == 1:
        share = f"{name} serves its only query"
    elif total == on_owner:
        share = f"{name} serves all {total} of its queries"
    else:
        share = f"{name} serves {on_owner} of its {total} queries"
    if tied:
        # ``primary_owner_engine`` breaks the tie by engine id; the catalog's ids sort as
        # their display names do (pinned by test_assess_additions.py).
        rule = (
            "a tie goes to the retained relational engine"
            if owner == retained_engine
            else "a tie goes to the engine first in alphabetical order"
        )
        share += f", tied with {_engine_list(tied)}; {rule}"
    elif len(served) > 1:
        share += ", more than any other engine that can own a table"
    if basis == OWNER_BY_DESIGN:
        text = f"{name} owns it in the schema design; no statement in scope queries it"
    elif basis == OWNER_BY_WRITES:
        text = f"{name} takes every statement that writes it ({_statements(writes.get(owner, 0))})"
    elif basis == OWNER_BY_RETAINED or not on_owner:
        text = f"No engine that can own a table queries it, so it stays on {name}, the retained relational engine"
    elif basis == OWNER_BY_SPLIT_WRITES:
        writers = sorted(
            (str(e) for e, n in writes.items() if e and n), key=lambda e: (-writes[e], e)
        )
        text = f"Its writes are on {_engine_list(writers)}, so the owner is the engine serving most of its queries: {share}"
    else:
        text = f"No statement writes it; {share}"
    readers = [str(e) for e in line.get("also_read_by") or []]
    if readers:
        verb = "reads" if len(readers) == 1 else "read"
        text += f"; {_engine_list(readers)} {verb} a copy kept in sync from {name} via CDC"
    return text + "."


def wave_listed_owners(waves: Iterable[Mapping[str, Any]] | None) -> dict[str, str]:
    """``{table: owner}`` from the waves' own owner lines (``table_owners``: a table a
    wave serves and another engine owns), the wave plan's owner as it recorded it; the
    owner lines of a report written before #212."""
    out: dict[str, str] = {}
    for w in waves or []:
        for o in w.get("table_owners") or []:
            if isinstance(o, Mapping) and o.get("table") and o.get("owner"):
                out.setdefault(str(o["table"]), str(o["owner"]))
    return out


def wave_retained_engine(waves: Iterable[Mapping[str, Any]] | None) -> str | None:
    """Wave 1's retained Aurora engine (the wave builder's ``retained_engine``), or None
    when there is no wave 1 or it names no engine."""
    first = next((w for w in waves or [] if w.get("homogeneity") is not None), None)
    engines = [str(e) for e in (first or {}).get("engines") or [] if e]
    return engines[0] if engines else None


def assignment_table_usage(assignment: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """``{table: {"writes", "queries", "cached_by"}}`` from an assignment artifact: per
    table, its in-scope write statements per engine (its ``write_groups``), all its
    in-scope queries per engine, and the cache engines of the in-scope reads of it a
    cache still fronts (``cache_engine`` set, ``cache_dropped`` not)."""
    assignment = assignment or {}
    out: dict[str, dict[str, Any]] = {}

    def usage(table: str) -> dict[str, Any]:
        return out.setdefault(table, {"writes": {}, "queries": {}, "cached_by": []})

    engine_of: dict[str, str] = {}
    for qa in assignment.get("query_assignments") or []:
        if not isinstance(qa, Mapping) or not qa.get("in_scope", True):
            continue
        engine = str(qa.get("assigned_engine") or "")
        engine_of[str(qa.get("query_id"))] = engine
        cache = str(qa.get("cache_engine") or "") if not qa.get("cache_dropped") else ""
        for table in dict.fromkeys(str(t) for t in qa.get("source_tables") or [] if t):
            u = usage(table)
            if engine:
                u["queries"][engine] = u["queries"].get(engine, 0) + 1
            if cache and cache not in u["cached_by"]:
                u["cached_by"].append(cache)
    for table, members in sorted((assignment.get("write_groups") or {}).items()):
        for qid in members or []:
            writer = engine_of.get(str(qid))
            if writer:
                writes = usage(str(table))["writes"]
                writes[writer] = writes.get(writer, 0) + 1
    for u in out.values():
        u["cached_by"].sort()
    return out


def _design_engines(row: Mapping[str, Any]) -> list[str]:
    engines = [str(row.get("recommended_database") or "")]
    engines += [
        str(a.get("database") or "") for a in row.get("alternatives") or [] if isinstance(a, dict)
    ]
    return [e for e in dict.fromkeys(engines) if e]


def table_owner_fields(
    mappings: Iterable[Mapping[str, Any]],
    usage: Mapping[str, Mapping[str, Any]],
    owners: Mapping[str, str] | None = None,
    retained_engine: str | None = None,
    design_fallback: bool = False,
) -> list[dict[str, Any]]:
    """Each ``table_mappings`` row with its owner line (``owner``, ``owner_basis``,
    ``also_read_by``, ``cached_by``, :func:`table_owner_line`), from ``usage``
    (:func:`assignment_table_usage`), the wave plan's ``owners`` and wave 1's
    ``retained_engine``. New dicts; the input rows are not changed."""
    out: list[dict[str, Any]] = []
    for row in mappings:
        if not isinstance(row, Mapping):
            continue
        table = str(row.get("source_table") or "")
        u = usage.get(table) or {}
        line = table_owner_line(
            u.get("writes") or {},
            u.get("queries") or {},
            u.get("cached_by") or [],
            _design_engines(row),
            owner=(owners or {}).get(table),
            retained_engine=retained_engine,
            design_fallback=design_fallback,
        )
        out.append({**row, **line})
    return out


def report_table_usage(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """:func:`assignment_table_usage`'s shape, from what a report written before #212
    carries. A written table's writers and readers come from ``table_sources`` (its
    writes, and the reads each replica serves); any other table's from
    ``query_pattern_groups`` (every in-scope query's engine and its pattern's tables,
    bare names resolved against the mapped tables; a bare name two mapped tables share
    is skipped). A table's cache is the cache engine of a ``query_groups`` access
    pattern whose queries read it. A report with none of them gives each row its
    design owner (:func:`table_owner_line`)."""
    mapped = [
        str(m.get("source_table") or "")
        for m in report.get("table_mappings") or []
        if isinstance(m, Mapping)
    ]
    by_bare: dict[str, list[str]] = {}
    for table in dict.fromkeys(t for t in mapped if t):
        by_bare.setdefault(table.rsplit(".", 1)[-1], []).append(table)
    out: dict[str, dict[str, Any]] = {}

    def usage(table: str) -> dict[str, Any]:
        return out.setdefault(table, {"writes": {}, "queries": {}, "cached_by": []})

    groups = report.get("query_pattern_groups")
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, Mapping):
            continue
        tables = {
            by_bare[b][0]
            for b in (str(t).rsplit(".", 1)[-1] for t in g.get("tables") or [] if t)
            if len(by_bare.get(b, [])) == 1
        }
        for member in g.get("members") or []:
            engine = str(member.get("engine") or "") if isinstance(member, Mapping) else ""
            for table in sorted(tables) if engine else []:
                queries = usage(table)["queries"]
                queries[engine] = queries.get(engine, 0) + 1
    for row in report.get("table_sources") or []:
        if not isinstance(row, Mapping) or not row.get("table"):
            continue
        u = usage(str(row["table"]))
        source = str(row.get("source_of_truth") or "")
        n_writes = len(row.get("write_query_ids") or []) or 1
        writers = [source] if source else [str(e) for e in row.get("write_engines") or [] if e]
        u["writes"] = {e: n_writes if source else 1 for e in writers}
        u["queries"] = dict(u["writes"])
        for replica in row.get("replicas") or []:
            if isinstance(replica, Mapping) and replica.get("engine"):
                engine = str(replica["engine"])
                reads = len(replica.get("read_query_ids") or []) or 1
                u["queries"][engine] = u["queries"].get(engine, 0) + reads
    for g in report.get("query_groups") or []:
        if not isinstance(g, Mapping):
            continue
        tables_of = {
            str(q.get("query_id")): [str(t) for t in q.get("tables_accessed") or [] if t]
            for q in g.get("source_queries") or []
            if isinstance(q, Mapping)
        }
        for ap in g.get("access_patterns") or []:
            engine = str(ap.get("engine") or "") if isinstance(ap, Mapping) else ""
            if engine not in CACHE_ENGINES or not ap.get("in_scope", True):
                continue
            for qid in ap.get("query_ids") or []:
                for table in tables_of.get(str(qid)) or []:
                    cached = usage(table)["cached_by"]
                    if engine not in cached:
                        cached.append(engine)
    for u in out.values():
        u["cached_by"].sort()
    return out


def owned_table_mappings(
    report: Mapping[str, Any], waves: Iterable[Mapping[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """``table_mappings`` with every row's owner line: as synthesis stored it, or, for a
    report written before #212, from :func:`report_table_usage`. A stored line always
    has an ``also_read_by`` list; a row an older report re-dumped through the contract
    has None there, and is filled in like one with no field at all."""
    usage: dict[str, dict[str, Any]] | None = None
    retained: str | None = None
    listed: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for m in report.get("table_mappings") or []:
        if not isinstance(m, Mapping):
            continue
        if isinstance(m.get("also_read_by"), list):
            out.append(dict(m))
            continue
        if usage is None:
            usage = report_table_usage(report)
            if waves is None:
                waves = [w for w in report.get("migration_waves") or [] if isinstance(w, Mapping)]
            retained = wave_retained_engine(waves)
            listed = wave_listed_owners(waves)
        out += table_owner_fields(
            [m], usage, listed, retained_engine=retained, design_fallback=True
        )
    return out


def _owner_target(row: Mapping[str, Any]) -> dict[str, Any]:
    """The target table the owner's schema design gives the table (its row's own when
    the owner is the recommended engine, else the owner's alternative), or the row's
    own when the owner's design does not map it."""
    owner = row.get("owner")
    for alt in row.get("alternatives") or []:
        if (
            isinstance(alt, dict)
            and owner
            and alt.get("database") == owner
            and row.get("recommended_database") != owner
        ):
            return {
                "target_engine": owner,
                "target_table": alt.get("target_table"),
                "aggregate_pattern": None,
                "confidence_score": alt.get("confidence_score"),
            }
    return {
        "target_engine": row.get("recommended_database"),
        "target_table": row.get("target_table"),
        "aggregate_pattern": row.get("aggregate_pattern"),
        "confidence_score": row.get("confidence_score"),
    }


def table_ownership_facts(
    report: Mapping[str, Any], waves: Iterable[Mapping[str, Any]] | None = None
) -> dict[str, Any] | None:
    """One line per mapped source table (``TableOwnershipFacts``, #212): its owner, the
    engines that also read it, its cache and the owner's target table, plus the counts
    every deliverable states. None for a report with no ``table_mappings``."""
    rows = owned_table_mappings(report, waves)
    if not rows:
        return None
    tables = sorted(
        (
            {
                "table": str(m.get("source_table") or ""),
                "owner": m.get("owner"),
                "owner_basis": m.get("owner_basis"),
                "also_read_by": [str(e) for e in m.get("also_read_by") or []],
                "cached_by": m.get("cached_by"),
                **_owner_target(m),
            }
            for m in rows
        ),
        key=lambda r: r["table"],
    )
    owned: dict[str, int] = {}
    for r in tables:
        if r["owner"]:
            owned[r["owner"]] = owned.get(r["owner"], 0) + 1
    return {
        "table_count": len(tables),
        "owned_table_counts": dict(sorted(owned.items(), key=lambda kv: (-kv[1], kv[0]))),
        "no_owner_table_count": sum(1 for r in tables if not r["owner"]),
        "also_read_table_count": sum(1 for r in tables if r["also_read_by"]),
        "cached_table_count": sum(1 for r in tables if r["cached_by"]),
        "cache_engines": sorted({str(r["cached_by"]) for r in tables if r["cached_by"]}),
        "table_lines": tables,
    }


def _unrounded(x: float) -> float:
    """A sum of percentages, kept at full precision: each renderer rounds it
    once, to the places it prints (``99.45`` prints "99%", never a second
    rounding to "100%"). Only float noise (``17.460499999999996``) is cut."""
    return round(x, 9)


def projected_monthly_cost(report: dict[str, Any]) -> float:
    """The headline projected monthly cost (#358).

    ``tco_analysis.projected_monthly_cost`` when synthesis wrote it -- one
    source of truth, never a sum recomputed from ``cost_breakdown`` (which can
    still list an eliminated engine). A report written before that field
    existed sums the kept engines' rows: the reality check's owners
    (``after_distribution``) plus the cache layer, the set the interactive
    report summed before the facts layer existed.
    """
    reported = (report.get("tco_analysis") or {}).get("projected_monthly_cost")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        return float(reported)
    kept = set((report.get("reality_check") or {}).get("after_distribution") or {})
    cache = (report.get("cache_overlay") or {}).get("engine")
    if cache:
        kept.add(cache)
    return round(sum(c for e, c in engine_costs(report).items() if e in kept), 2)


def cost_baseline_known(tco: Mapping[str, Any]) -> bool:
    """Whether ``tco`` has a real current-cost baseline (#380, #334).

    ``current_cost_known`` false, or missing with a falsy ``current_monthly_cost`` (a
    report written before the field existed, whose ``0`` is the "no instance metadata"
    placeholder), is unknown. The one rule every deliverable and the executive summary
    share (``src.report.formatting.cost_baseline_known`` is this function)."""
    known = tco.get("current_cost_known")
    return not (known is False or (known is None and not tco.get("current_monthly_cost")))


def whole_dollars(x: float) -> int:
    """``x`` rounded half up to the nearest dollar (#1403): 504.5 is 505, 504.49 is 504."""
    return int(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# #1403: what each wave does, as the executive summary's recommendation words it.
BRIEF_WAVE_KINDS = (
    "homogeneous",
    "cross_engine",
    "heterogeneous",
    "cache",
    "migration",
    "read_model",
)
# The open risks the executive summary names as driving the overall level.
BRIEF_EXPOSURE_LIMIT = 2


def _brief_wave_kind(wave: Mapping[str, Any]) -> str:
    homogeneity = wave.get("homogeneity")
    if homogeneity in ("homogeneous", "cross_engine", "heterogeneous"):
        return str(homogeneity)
    if wave.get("fronts") or any(str(e) in CACHE_ENGINES for e in wave.get("engines") or []):
        return "cache"
    return "migration" if is_migrating_wave(dict(wave)) else "read_model"


def _source_engine(report: Mapping[str, Any], waves: list[dict[str, Any]]) -> str | None:
    """The source engine the plan moves off: wave 1's ``moves_from``, else the one engine
    the collection says runs today; None when neither names one."""
    for w in waves:
        if w.get("homogeneity") is not None:
            moves = [str(e) for e in w.get("moves_from") or [] if e]
            if moves:
                return moves[0]
    in_use = [str(e) for e in report.get("engines_in_use") or [] if e]
    return in_use[0] if len(in_use) == 1 else None


def _exposure_wave(
    risk: Mapping[str, Any], engine: str | None, waves: list[dict[str, Any]]
) -> int | None:
    """The wave a risk belongs to: its own ``wave``, else the first wave of its engine."""
    wave = risk.get("wave")
    if isinstance(wave, int) and not isinstance(wave, bool):
        return wave
    if engine:
        for w in waves:
            if engine in (w.get("engines") or []):
                return int(w.get("wave") or 0) or None
    return None


def _share_of_all_queries(report: dict[str, Any], query_total: int | None) -> dict[str, float]:
    """Each owner engine's share of every collected query, in scope or not (#1403): with
    queries no engine in the scope can serve, an engine's workload share (of the served
    queries) would read as more of the workload than it takes. Empty with none."""
    unservable = len(scope_unservable_rows(report))
    if not unservable or not query_total:
        return {}
    out: dict[str, float] = {}
    for r in report.get("ranking") or []:
        n = r.get("assigned_queries") if isinstance(r, dict) else None
        if isinstance(n, int) and not isinstance(n, bool) and n > 0 and r.get("target"):
            out[str(r["target"])] = round(100 * n / query_total, 1)
    return out


def executive_brief_facts(
    report: dict[str, Any],
    waves: list[dict[str, Any]],
    retired_in_use: list[dict[str, Any]],
    query_total: int | None = None,
) -> dict[str, Any]:
    """What the executive summary states beyond the headline figures (#1403).

    The summary leads with the recommendation, the risk and the decision. Its cost and
    shares are the facts' own (``projected_monthly_cost_usd``, ``price_list``,
    ``engines``); this block adds what it needs on top: the source engine, whether a
    current cost was collected (and its saving), what each wave does, the overall risk
    level (the highest open severity, ``risk_severity.overall_risk_level``), the one or
    two open risks at that level that drive it (first in register order), each with the
    wave to act before, and the wave the reader is asked to approve next.

    ``saving_counted`` is true only when a deliverable states a saving: a known
    current cost, or an engine the customer runs today that the plan retires with a
    counted saving (#1076). With neither, the summary claims no saving.
    """
    tco = report.get("tco_analysis") or {}
    known = cost_baseline_known(tco)
    current = tco.get("current_monthly_cost") if known else None
    savings = tco.get("savings_percent") if known else None
    risks = open_risks(report)
    level = overall_risk_level(risks)
    ranked = sorted(range(len(risks)), key=lambda i: (-severity_rank(severity_of(risks[i])), i))
    # The risks at the overall level are the ones driving it: the most severe, in order.
    top = [i for i in ranked if severity_of(risks[i]) == level] or ranked
    exposures = []
    for i in top[:BRIEF_EXPOSURE_LIMIT]:
        r = risks[i]
        engine, _ = risk_engine_and_body(r.get("description"))
        eng = None if engine == "(general)" else engine
        exposures.append(
            {
                "risk_id": r.get("risk_id"),
                "severity": severity_of(r),
                "risk_type": str(r.get("risk_type") or "") or None,
                "rule": r.get("rule"),
                "engine": eng,
                "wave": _exposure_wave(r, eng, waves),
            }
        )
    brief_waves = [
        {
            "wave": int(w.get("wave") or 0),
            "engines": [str(e) for e in w.get("engines") or [] if e],
            "kind": _brief_wave_kind(w),
        }
        for w in waves
    ]
    return {
        # The executive summary states the projected cost in whole dollars (maintainer
        # decision on #1403); cost tables and the engineering report keep the cents.
        "projected_monthly_cost_whole_usd": whole_dollars(projected_monthly_cost(report)),
        "source_engine": _source_engine(report, waves),
        "source_cost_known": known,
        "source_monthly_cost_usd": (
            float(current)
            if isinstance(current, (int, float)) and not isinstance(current, bool)
            else None
        ),
        "source_monthly_cost_whole_usd": (
            whole_dollars(float(current))
            if isinstance(current, (int, float)) and not isinstance(current, bool)
            else None
        ),
        "savings_percent": (
            float(savings)
            if isinstance(savings, (int, float)) and not isinstance(savings, bool)
            else None
        ),
        "saving_counted": known
        or any(isinstance(row.get("monthly_cost_usd"), (int, float)) for row in retired_in_use),
        # Each retired engine's counted saving in whole dollars, as the summary states it.
        "retired_saving_whole_usd": {
            str(row["engine"]): whole_dollars(float(row["monthly_cost_usd"]))
            for row in retired_in_use
            if row.get("engine")
            and isinstance(row.get("monthly_cost_usd"), (int, float))
            and not isinstance(row.get("monthly_cost_usd"), bool)
        },
        "waves": brief_waves,
        # #1329: with queries no engine in the scope serves, each engine's share of all
        # the queries, so the summary never reads 100% for an engine that takes 65 of 107.
        "share_of_all_queries_percent": _share_of_all_queries(report, query_total),
        "overall_risk_level": level,
        "exposures": exposures,
        "decision_wave": brief_waves[0]["wave"] if brief_waves else None,
    }


def cost_basis_notes(report: dict[str, Any], engines: list[dict[str, Any]]) -> list[str]:
    """How each priced engine's estimate billed its reads, where that rests on an
    assumption (#1197, ADR-032): one sentence per kept engine in ``engines`` and per
    retired engine whose TCO row states a ``read_io_hit_ratio_basis`` (DocumentDB,
    which bills a read only on a buffer-cache miss). Every deliverable prints these
    as they are, next to the cost."""
    tco = report.get("tco_analysis") or {}
    kept = {e["engine"] for e in engines}
    rows = [
        r
        for r in tco.get("cost_breakdown") or []
        if isinstance(r, dict) and r.get("database") in kept
    ] + [r for r in tco.get("retired_engine_savings") or [] if isinstance(r, dict)]
    notes: list[str] = []
    for row in rows:
        note = cost_basis_sentence(display_engine(str(row.get("database") or "")), row)
        if note is not None and note not in notes:
            notes.append(note)
    return notes


def unreviewed_move_count(report: dict[str, Any]) -> int:
    """How many reality-check moves no model reviewed (#746).

    The consolidation validator reviews each consolidation's moved queries in
    batches; a batch that got no usable verdict (a call error, or no
    structured answer) keeps its moves unreviewed and is recorded in the
    reality check's ``validation_incomplete`` (``batch`` is ``"start-end"``, 1-based).
    The moves of every such batch, counted once.
    """
    rows = (report.get("reality_check") or {}).get("validation_incomplete") or []
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        start, _, end = str(row.get("batch") or "").partition("-")
        try:
            total += max(int(end) - int(start) + 1, 0)
        except ValueError:
            continue
    return total


def compute_report_facts(
    report: dict[str, Any],
    waves: list[dict[str, Any]] | None = None,
    view_ids: Iterable[str] | None = None,
    signal_overrides: Mapping[str, Iterable[str]] | None = None,
    relational_gaps: Mapping[str, Any] | None = None,
    engine_scope: Iterable[str] | None = None,
    price_list: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Every customer-facing figure, computed once (``SynthesisOutputContract.facts``).

    ``waves`` defaults to ``report["migration_waves"]``; a renderer passes the
    resolved (legacy-fallback) waves for a report written before that field.
    ``view_ids``, the collected view ids, lets wave 1 state its tables and
    views separately (#578); without it (an older report) the view count is
    what wave 1 carries beyond ``source_table_count``. ``signal_overrides``
    (engine -> the triage signals the assignment's ``signal_override`` fields
    name) is what synthesis passes; an older report reads them from the
    ranking's reason text instead (#584). ``relational_gaps`` is the relational move's
    gap check (``src.tools.analysis.relational_gaps.detect_relational_gaps``, #471),
    which needs the collection: synthesis passes it, and a report rebuilt without the
    collection has None. ``engine_scope`` is the customer's scope triage recorded (#1328);
    synthesis passes it, and a report rebuilt without the triage has None.
    ``price_list`` is the price table every cost figure was estimated from
    (``src.shared.catalog.prices.estimates_price_basis``, #1196): synthesis passes it, and
    a report rebuilt later has None, since today's table may not be the one its figures used.
    """
    if waves is None:
        waves = [w for w in (report.get("migration_waves") or []) if isinstance(w, dict)]
    views = set(view_ids) if view_ids is not None else None
    engines = engine_facts(report, waves)
    overrides = (
        {e: sorted(set(v)) for e, v in signal_overrides.items()}
        if signal_overrides is not None
        else signal_overrides_from_reasons(report)
    )
    for e in engines:
        e["signal_overrides"] = list(overrides.get(e["engine"], []))
    final = final_engines(engines)
    rc_trade_offs = reality_check_trade_off_facts(report, final)
    n_trade_offs = sum(1 for t in report.get("trade_offs") or [] if isinstance(t, dict))
    removed = removed_engine_facts(report, final, rc_trade_offs)
    wave_rows = [wave_facts(w, views) for w in waves]
    tables_analyzed = report.get("source_table_count")
    if not isinstance(tables_analyzed, int) or isinstance(tables_analyzed, bool):
        tables_analyzed = None
    if views is None and tables_analyzed is not None:
        for row, w in zip(wave_rows, waves, strict=True):
            if w.get("homogeneity") is not None:
                row["views"] = max(row["served_tables"] - tables_analyzed, 0)
                row["tables_only"] = row["served_tables"] - row["views"]
    view_count = next((row["views"] for row in wave_rows if row["views"] is not None), None)
    # #1284: what later waves move off wave 1's Aurora engine, on wave 1's row.
    moved_later = wave_one_moved_by_later_waves(waves)
    for row, w in zip(wave_rows, waves, strict=True):
        if w.get("homogeneity") is not None:
            row["moved_by_later_waves"] = moved_later
    migration_total = sum(e["tables_migrate"] for e in engines if e["role"] == MIGRATION_TARGET)
    wave_total = sum(row["moving_tables"] or 0 for row in wave_rows if row["migrating"])
    tables_migrate = migration_total or wave_total
    # #1120: a cross-engine wave 1 moves every source table (not its views) to the
    # chosen Aurora engine. Every later wave moves a subset of those tables on, so
    # "Tables migrate" counts each table once: all of them.
    moved = apply_cross_engine_wave_one(waves, wave_rows, engines, tables_analyzed, relational_gaps)
    if moved is not None:
        tables_migrate = moved
    costs = [e["monthly_cost_usd"] for e in engines if e["monthly_cost_usd"] is not None]
    tied = [t for t in (report.get("tied_queries") or []) if isinstance(t, dict)]
    risks = risk_facts(report)
    by_engine = risks["critical_high_risks_by_engine"]
    for row in wave_rows:
        row["critical_high_risks"] = sum(by_engine.get(e, 0) for e in row["engines"])
    # The engines that need no data migration (the retained relational core and
    # the cache layer): the deck's "no-migration step" card and summary footer.
    no_move = [e for e in engines if e["role"] in NO_MIGRATION_ROLES]
    no_migration = {
        "engines": [e["engine"] for e in no_move],
        "workload_percent": _unrounded(
            sum(e["workload_percent"] or 0.0 for e in no_move if e["cached_queries"] is None)
        ),
        "cached_call_share_percent": _unrounded(
            sum(e["cached_call_share_percent"] or 0.0 for e in no_move)
        ),
        "critical_high_risks": sum(by_engine.get(e["engine"], 0) for e in no_move),
    }
    return {
        "projected_monthly_cost_usd": projected_monthly_cost(report),
        "source_table_count": tables_analyzed,
        "source_view_count": view_count,
        "engine_count": len(engines),
        "migration_wave_count": len(wave_rows),
        "engines": engines,
        "tables_migrate": tables_migrate,
        # #1020: the executive report's "Queries moved" total.
        **(moves := query_move_facts(report, engines)),
        # #1329: the queries with no engine in the scope, and the headline total.
        **query_total_facts(report, moves["in_scope_query_count"]),
        "workload_total_percent": _unrounded(sum(e["workload_percent"] or 0.0 for e in engines)),
        "engine_cost_total_usd": round(sum(costs), 2),
        # #1197: the read I/O basis a cost rests on, stated wherever the cost is shown.
        "cost_basis_notes": cost_basis_notes(report, engines),
        "table_basis": table_basis(report, engines, waves),
        "waves": wave_rows,
        "no_migration": no_migration,
        "removed_engines": removed,
        # #1076: the removed engines the customer runs today, stated once with their saving.
        "retired_in_use_engines": (retired := retired_in_use_engine_facts(report, removed)),
        "reality_check_decisions": reality_check_decision_facts(report, rc_trade_offs, removed),
        "reality_check_trade_offs": rc_trade_offs,
        "trade_off_count": n_trade_offs - sum(1 for t in rc_trade_offs if t["reversed"]),
        **risks,
        "tied_query_count": len(tied),
        # #1003: the open ties as one choice per table, and --auto's decisions.
        **engine_choice_facts(report),
        # #629: queries the post-schema router moved after the customer approved.
        "post_schema_reroute_count": sum(
            1 for r in report.get("post_schema_reroutes") or [] if isinstance(r, dict)
        ),
        # #746: reality-check moves no model reviewed (a failed review batch).
        "unreviewed_move_count": unreviewed_move_count(report),
        # #992: of those, the writes moved to one source of truth per table.
        "source_of_truth_reroute_count": sum(
            1
            for r in report.get("post_schema_reroutes") or []
            if isinstance(r, dict) and r.get("reason_category") == "source_of_truth"
        ),
        "design_review_reroute_count": sum(
            1
            for r in report.get("post_schema_reroutes") or []
            if isinstance(r, dict) and r.get("reason_category") != "source_of_truth"
        ),
        # #339: the per-query decision traces' figures.
        "query_trace": query_trace_facts(report),
        # #730: the queries a kept engine could serve almost as well.
        "close_calls": close_call_facts(report),
        # #729: the queries that landed on a fallback engine.
        "unservable": unservable_query_facts(report),
        # #792: the access-pattern totals the results page shows.
        "access_pattern_totals": access_pattern_facts(report),
        # #1410: the query groups the engineering report lists, and the rest.
        "query_group_counts": query_group_facts(report),
        # #992: each written table's source of truth, replicas and CDC risk.
        "table_sources": table_source_facts(report),
        # #212: each mapped table's owner, the engines that also read it, its cache.
        "table_owner_lines": table_ownership_facts(report, waves),
        # #471: the relational move's version and feature gaps.
        "relational_gaps": dict(relational_gaps) if relational_gaps is not None else None,
        # #1146: the queries and the distinct query patterns they make.
        "query_pattern_counts": query_pattern_facts(report, engines),
        # #1328: the customer's engine scope, in the order the deliverables name it.
        "engine_scope": (
            engine_scope_display_order(engine_scope) if engine_scope is not None else None
        ),
        # #1196: the source, region and date of the list prices behind every cost figure.
        "price_list": dict(price_list) if price_list is not None else None,
        # #1195: the TCO's assumptions, stated once for every deliverable's cost section.
        "cost_assumptions": cost_assumptions(report),
        # #1474: what every cost figure is, and the one caveat every deliverable prints.
        "cost_scope_label": COST_SCOPE_LABEL,
        "cost_caveat": COST_CAVEAT,
        # #1403: what the executive summary states: the risk level, its drivers, the decision.
        "executive_brief": executive_brief_facts(
            report,
            waves,
            retired,
            query_total_facts(report, moves["in_scope_query_count"])["query_total_count"],
        ),
        # #1252: the cache layer's candidate and designed read figures.
        "cache_layer": cache_layer_facts(report),
    }


def table_fit_score(rec: Any) -> float:
    """A table recommendation's fit score; a missing or non-number score counts as 0 (#1312)."""
    score = rec.get("confidence_score") if isinstance(rec, Mapping) else None
    return score if isinstance(score, (int, float)) and not isinstance(score, bool) else 0


def mean_table_fit(analysis: Mapping[str, Any]) -> float:
    """The unrounded mean fit score of an analysis's table recommendations (#1312).

    A table with no numeric score counts as 0, so a missing or non-number score changes the
    mean the same way everywhere: synthesis (``report.json``), the analysis API and the job
    summary all read it from here.
    """
    recs = [r for r in (analysis.get("table_recommendations") or []) if isinstance(r, Mapping)]
    total = sum(table_fit_score(r) for r in recs)
    return total / len(recs) if recs else 0.0


def routed_cache_fit_facts(confidence: int | None, queries: int) -> dict[str, Any]:
    """The cache layer's routed fit, as the pattern analysis page shows it (#1325, ADR-032).

    ``confidence`` and ``queries`` are the cache entry's ``routed_confidence`` and
    ``routed_queries``: the mean fit of the reads the latest assignment routes to the cache
    layer, the figure the reports state. With no reads routed to it the figure is not
    applicable (``routed_cache_fit_applicable`` is false and the fit is ``None``); the page
    never falls back to the analysis average, which is a different quantity.
    """
    applicable = confidence is not None and queries > 0
    return {
        "routed_cache_fit": confidence if applicable else None,
        "routed_cache_queries": queries,
        "routed_cache_fit_applicable": applicable,
    }


def analysis_overview_facts(
    analysis: Mapping[str, Any],
    signals: Iterable[Any] | None = None,
    engine: str | None = None,
) -> dict[str, Any]:
    """The engine overview figures of one analysis artifact (#1312, ADR-032).

    The pattern analysis page, the job's agent summary and each ``report.json`` ranking entry
    show the average fit of the engine's tables, its table, pattern and anti-pattern counts,
    its cost and the queries its patterns cover. They are computed here, from ``analysis.json``,
    so a saved job of any age answers the same way and the page only formats them.
    ``anti_pattern_query_counts`` lists, in the order of ``anti_patterns_detected``, how many
    queries each one names. ``signal_query_total`` and ``signal_count`` (the page's "total
    queries" under the signal chart and the signal tab count) are added when the triage
    ``signals`` are given: those of the signals that name ``engine``.
    """
    recs = [r for r in (analysis.get("table_recommendations") or []) if isinstance(r, Mapping)]
    workload = analysis.get("workload_analysis") or {}
    raw_patterns = workload.get("patterns_detected") or []
    patterns = [p for p in raw_patterns if isinstance(p, Mapping)]
    anti_patterns = workload.get("anti_patterns_detected") or []
    cost = (analysis.get("cost_estimate") or {}).get("monthly_cost_usd")
    facts: dict[str, Any] = {
        "avg_confidence_score": round(mean_table_fit(analysis)),
        "tables_analyzed": len(recs),
        "monthly_cost_usd": cost if isinstance(cost, (int, float)) else 0,
        "patterns_detected": len(raw_patterns),
        "anti_patterns_detected": len(anti_patterns),
        "anti_pattern_query_counts": [
            len(a.get("query_ids") or []) if isinstance(a, Mapping) else 0 for a in anti_patterns
        ],
        "queries_covered": len({q for p in patterns for q in (p.get("query_ids") or [])}),
    }
    if signals is not None:
        mine = [s for s in signals if isinstance(s, Mapping) and engine in (s.get("targets") or [])]
        facts["signal_count"] = len(mine)
        facts["signal_query_total"] = sum(
            s["query_count"] for s in mine if isinstance(s.get("query_count"), (int, float))
        )
    return facts


def triage_signal_facts(triage: Mapping[str, Any] | None) -> dict[str, Any]:
    """The triage insight figures of the job summary page (#1319, ADR-032).

    ``signal_count`` is how many workload signals the triage found; ``top_signals`` lists the
    first five with the number of queries each names and its first target engine. An entry that
    is not a record is not counted (the page used to count every entry). The page formats them
    and counts nothing.
    """
    raw = triage.get("signals") if isinstance(triage, Mapping) else None
    signals = [s for s in raw if isinstance(s, Mapping)] if isinstance(raw, list) else []
    return {
        "signal_count": len(signals),
        "top_signals": [
            {
                "name": s.get("signal"),
                "query_count": len(s.get("query_ids") or []),
                "primary_target": (s.get("targets") or [None])[0],
            }
            for s in signals[:5]
        ],
    }


# ---------------------------------------------------------------------------
# The detailed engineering report's routing detail (#1364)
# ---------------------------------------------------------------------------

# The places a routing detail score is shown to, like every score of the trace.
_DETAIL_PLACES = 1

# Table names that stand for no table: the collector's placeholder and the dummy tables
# a SELECT with no FROM names (``table_resolution.PSEUDO_TABLES`` and
# ``synthesis_report._PLACEHOLDER_TABLES``, which the facts layer cannot import; a unit
# test keeps this set covering both).
PLACEHOLDER_TABLE_NAMES = frozenset(
    {"unknown", "UNKNOWN", "None", "null", "DUAL", "SYSIBM.SYSDUMMY1"}
)


def _detail_round(x: Any) -> float | None:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return round(float(x), _DETAIL_PLACES)
    return None


def _trace_steps(trace: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    first = trace.get("first_assignment")
    steps = [first] if isinstance(first, Mapping) else []
    return steps + [m for m in trace.get("moves") or [] if isinstance(m, Mapping)]


def _latest_ladder(trace: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The candidate ladder of the query's latest step that recorded one: each engine's
    routed score or gate, as the Move stored it."""
    for step in reversed(_trace_steps(trace)):
        rungs = [c for c in step.get("candidates") or [] if isinstance(c, Mapping)]
        if rungs:
            return {
                str(c.get("engine")): {
                    "engine": str(c.get("engine")),
                    "score": _detail_round(c.get("score")),
                    "gate": c.get("gate"),
                    "gate_text": c.get("gate_text"),
                }
                for c in rungs
                if c.get("engine")
            }
    return {}


def _score_cells(
    engines: Iterable[str], ladder: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return [dict(ladder.get(e) or {"engine": e}) for e in engines]


def _table_breakdowns(
    analyses: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Mapping[str, Any]]]:
    """``engine -> table -> score_breakdown`` from each engine's analysis."""
    out: dict[str, dict[str, Mapping[str, Any]]] = {}
    for engine, analysis in analyses.items():
        rows = (analysis or {}).get("table_recommendations") or []
        out[engine] = {
            str(r.get("table_id")): r.get("score_breakdown") or {}
            for r in rows
            if isinstance(r, Mapping) and r.get("table_id")
        }
    return out


def _dimension_rows(
    engines: list[str],
    ladder: Mapping[str, Mapping[str, Any]],
    row: Mapping[str, Any] | None,
    breakdowns: Mapping[str, Mapping[str, Mapping[str, Any]]],
    tables: list[str],
) -> list[dict[str, Any]]:
    """A query's scores by dimension per analysed engine, as the artifacts hold them: the
    routed score (the Move's ladder), the pattern dimension (its class score, and after
    redesign), the intrinsic anti-pattern penalty, and each table's complexity and
    performance in the engine's analysis."""
    row = row or {}
    written = row.get("class_scores") or {}
    redesigned = (row.get("redesign") or {}).get("class_scores") or {}
    anchors = row.get("parity_anchors") or {}
    findings = [f for f in row.get("anti_pattern_findings") or [] if isinstance(f, Mapping)]
    out = []
    for engine in engines:
        mine = [f for f in findings if f.get("engine") == engine]
        intrinsic = [f for f in mine if f.get("kind") != "fixable"]
        by_table = breakdowns.get(engine) or {}
        out.append(
            {
                "engine": engine,
                "routed": dict(ladder.get(engine) or {"engine": engine}),
                "pattern_score": _detail_round(written.get(engine)),
                "redesigned_pattern_score": _detail_round(redesigned.get(engine)),
                "parity_anchor": anchors.get(engine),
                # The biggest intrinsic penalty is the one routing applies (ADR-031
                # Decision 8, ``query_scoring.build_anti_pattern_map``), never their sum.
                "intrinsic_penalty": max(
                    (
                        float(f["penalty"])
                        for f in intrinsic
                        if isinstance(f.get("penalty"), (int, float))
                        and not isinstance(f.get("penalty"), bool)
                    ),
                    default=0.0,
                ),
                "intrinsic_findings": [str(f.get("pattern") or "") for f in intrinsic],
                "fixable_findings": [
                    str(f.get("pattern") or "") for f in mine if f.get("kind") == "fixable"
                ],
                "tables": [
                    {
                        "table": t,
                        "complexity": _detail_round(
                            (by_table.get(t) or {}).get("complexity_score")
                        ),
                        "performance": _detail_round(
                            (by_table.get(t) or {}).get("performance_score")
                        ),
                    }
                    for t in tables
                    if t in by_table
                ],
            }
        )
    return out


def _group_placements(
    lineage: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, tuple[int, Mapping[str, Any]]], dict[tuple[int, str], int]]:
    """Each query's latest group Move (one with group scores) as ``(version, move)``, and
    how many queries each ``(version, group_key)`` placement moved as one unit."""
    latest: dict[str, tuple[int, Mapping[str, Any]]] = {}
    units: dict[tuple[int, str], set[str]] = {}
    for assignment in lineage:
        version = int(assignment.get("version") or 0)
        for move in assignment.get("moves") or []:
            if not isinstance(move, Mapping) or not (move.get("evidence") or {}).get(
                "group_scores"
            ):
                continue
            qid = str(move.get("query_id") or "")
            seen = latest.get(qid)
            if seen is None or (version, int(move.get("seq") or 0)) >= (
                seen[0],
                int(seen[1].get("seq") or 0),
            ):
                latest[qid] = (version, move)
            if move.get("group_key"):
                units.setdefault((version, str(move["group_key"])), set()).add(qid)
    return latest, {k: len(v) for k, v in units.items()}


def _group_detail(
    kind: str,
    name: str,
    members: list[str],
    engine_of: Mapping[str, str],
    traces: Mapping[str, Mapping[str, Any]],
    placements: Mapping[str, tuple[int, Mapping[str, Any]]],
    units: Mapping[tuple[int, str], int],
    floor: float | None,
    carried: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """One group's detail (``GroupDetail``). ``carried`` maps a member to its carried-write
    record (#1362) when the report has them for a write group, else None: then a member
    fits poorly when its own routed score is below ``floor``."""
    placed = [placements[q] for q in members if q in placements]
    version, move = max(placed, key=lambda vm: (vm[0], int(vm[1].get("seq") or 0)), default=(0, {}))
    scores = {
        str(e): round(float(s), _DETAIL_PLACES)
        for e, s in ((move.get("evidence") or {}).get("group_scores") or {}).items()
        if isinstance(s, (int, float))
    }
    winner = str(move.get("to_engine") or "") or None if move else None
    others = sorted(
        ((s, e) for e, s in scores.items() if e != winner), key=lambda se: (-se[0], se[1])
    )
    runner = others[0][1] if others else None
    margin = (
        round(scores[winner] - others[0][0], _DETAIL_PLACES)
        if winner in scores and others
        else None
    )
    rows = []
    for qid in members:
        own = _detail_round((traces.get(qid) or {}).get("final_score"))
        record = (carried or {}).get(qid)
        poor = (
            record is not None
            if carried is not None
            else (floor is not None and own is not None and own < floor)
        )
        rows.append(
            {
                "query_id": qid,
                "engine": engine_of.get(qid),
                "own_score": own,
                "poor_fit": poor,
                "intent_name": (record or {}).get("intent_name"),
                "approach": (record or {}).get("approach"),
            }
        )
    key = str(move.get("group_key") or "") or None
    return {
        "kind": kind,
        "name": name,
        "member_count": len(members),
        "engines": sorted({engine_of[q] for q in members if engine_of.get(q)}),
        "placed_version": version or None,
        "placed_seq": int(move["seq"]) if move.get("seq") is not None else None,
        "placed_rule": move.get("reason_code"),
        "placement_group_key": key,
        "placement_member_count": units.get((version, key)) if key else None,
        "group_scores": dict(sorted(scores.items(), key=lambda es: (-es[1], es[0]))),
        "winner": winner,
        "runner_up": runner,
        "margin": margin,
        "members": rows,
        "poor_fit_count": sum(1 for r in rows if r["poor_fit"]),
        "guidance_recorded": carried is not None,
    }


def routing_detail_facts(
    report: Mapping[str, Any],
    lineage: Sequence[Mapping[str, Any]],
    traces: Mapping[str, Mapping[str, Any]],
    access_record: Mapping[str, Any] | None,
    analyses: Mapping[str, Mapping[str, Any]],
    query_excerpts: Mapping[str, str] | None = None,
    poor_fit_floor: float | None = None,
) -> dict[str, Any]:
    """The detailed engineering report's routing detail (``RoutingDetail``, #1364).

    Read from the job's saved routing records, never re-scored: ``lineage`` is the
    effective assignment's lineage, oldest first (its last entry the effective one),
    ``traces`` each of its queries' decision trace (``QueryTraceResponse`` as JSON, built
    from the Moves by ``src.shared.query_trace``), ``access_record`` the effective
    version's access-pattern record, ``analyses`` each analysed engine's analysis, and
    ``report`` the synthesis report (its owner lines, engine scope and carried writes).
    ``poor_fit_floor`` is the catalog's carried-write floor (#1362). A record the job
    does not have is an empty input; its ``has_*`` flag says so, so the report states the
    absence.
    """
    from src.shared.routing_detail_model import RoutingDetail

    effective = lineage[-1] if lineage else {}
    qas = [
        qa
        for qa in effective.get("query_assignments") or []
        if isinstance(qa, Mapping) and qa.get("query_id")
    ]
    rows = {
        str(r.get("query_id")): r
        for r in (access_record or {}).get("queries") or []
        if isinstance(r, Mapping) and r.get("query_id")
    }
    # The engines routing scored: those of the Moves' ladders, among the analysed ones (an
    # Aurora engine the heterogeneous engine choice dropped has an analysis but no rung).
    laddered = {e for t in traces.values() for e in _latest_ladder(t)}
    analyses = {e: a for e, a in analyses.items() if not laddered or e in laddered}
    breakdowns = _table_breakdowns(analyses)
    engines = sorted(analyses or laddered, key=lambda e: (display_engine(e), e))
    facts_block = report.get("facts") if isinstance(report.get("facts"), Mapping) else {}
    excerpts = query_excerpts or {}
    floor = float(poor_fit_floor) if poor_fit_floor is not None else 0.0
    # The close calls and settled ties as synthesis selected them (``query_traces.
    # close_calls``, the rows ``close_call_facts`` counts), so the counts and each
    # query's label match the engineering report; a report written before them has
    # none, and the trace's own runner-up decides (a gap of 0 is a settled tie).
    digest = report.get("query_traces")
    stored_close = (
        {
            str(r["query_id"]): bool(r.get("settled_tie"))
            for r in digest["close_calls"]
            if isinstance(r, Mapping) and r.get("query_id")
        }
        if isinstance(digest, Mapping) and isinstance(digest.get("close_calls"), list)
        else None
    )

    queries: list[dict[str, Any]] = []
    by_table: dict[str, list[dict[str, Any]]] = {}
    for qa in sorted(qas, key=lambda q: str(q["query_id"])):
        qid = str(qa["query_id"])
        trace = traces.get(qid) or {}
        ladder = _latest_ladder(trace)
        row = rows.get(qid)
        tables = [
            str(t)
            for t in dict.fromkeys(qa.get("source_tables") or [])
            if t and str(t) not in PLACEHOLDER_TABLE_NAMES
        ]
        record_tables = [str(t) for t in (row or {}).get("tables") or [] if t] or tables
        redesign = (row or {}).get("redesign") or {}
        engine = str(qa.get("assigned_engine") or "") or None
        if stored_close is not None:
            close = qid in stored_close
            settled = bool(stored_close.get(qid))
        else:
            close = is_close_call(trace)
            settled = close and (trace.get("runner_up") or {}).get("gap") == 0
        queries.append(
            {
                "query_id": qid,
                "excerpt": excerpts.get(qid),
                "tables": tables,
                "in_scope": bool(qa.get("in_scope", True)),
                "engine": engine,
                "final_score": _detail_round(trace.get("final_score")),
                "pinned": trace.get("pinned") or qa.get("pinned"),
                "access_class": (row or {}).get("class"),
                "access_reason": (row or {}).get("reason"),
                "redesign_class": redesign.get("class"),
                "redesign_reason": redesign.get("reason"),
                "assumed_keys": [
                    str(k.get("column"))
                    for k in redesign.get("assumed_keys") or []
                    if isinstance(k, Mapping) and k.get("column")
                ],
                "dimensions": _dimension_rows(engines, ladder, row, breakdowns, record_tables),
                "steps": [
                    {
                        "version": int(s.get("version") or 0),
                        "seq": int(s.get("seq") or 0),
                        "step": int(s.get("step") or 0),
                        "rule": str(s.get("rule") or s.get("reason_code") or ""),
                        "actor": str(s.get("actor") or ""),
                        "from_engine": s.get("from_engine"),
                        "to_engine": s.get("to_engine"),
                        "explanation": str(s.get("explanation") or ""),
                    }
                    for s in _trace_steps(trace)
                ],
                "summary": str(trace.get("summary") or ""),
                "tied_engines": [str(e) for e in trace.get("tied_engines") or []],
                "close_call": close and not settled,
                "settled_tie": settled,
                "runner_up": (trace.get("runner_up") or {}).get("explanation"),
                "outscored_by": [
                    str(o.get("explanation") or "")
                    for o in trace.get("outscored_by") or []
                    if isinstance(o, Mapping)
                ],
                "key_candidates": [
                    {
                        "column": str(k.get("column") or ""),
                        "outcome": str(k.get("outcome") or ""),
                        "explanation": str(k.get("explanation") or ""),
                    }
                    for k in trace.get("key_candidates") or []
                    if isinstance(k, Mapping)
                ],
            }
        )
        for table in tables:
            by_table.setdefault(table, []).append(
                {
                    "query_id": qid,
                    "engine": engine,
                    "access_class": (row or {}).get("class"),
                    "scores": _score_cells(engines, ladder),
                }
            )

    usage = assignment_table_usage(effective)
    stored = {
        str(r.get("table")): r
        for r in ((facts_block or {}).get("table_owner_lines") or {}).get("table_lines") or []
        if isinstance(r, Mapping) and r.get("table")
    }
    waves = [w for w in report.get("migration_waves") or [] if isinstance(w, Mapping)]
    retained = wave_retained_engine(waves)
    tables_out = []
    for table in sorted(by_table):
        u = usage.get(table) or {}
        # The owner line synthesis stored (#212); none is computed here when the report
        # maps no table (no schema design): the report states the absence.
        line = stored.get(table) or {}
        tables_out.append(
            {
                "table": table,
                "owner": line.get("owner"),
                "owner_basis": line.get("owner_basis"),
                "owner_reason": (
                    table_owner_reason(
                        line, u.get("writes") or {}, u.get("queries") or {}, retained
                    )
                    if line
                    else None
                ),
                "also_read_by": [str(e) for e in line.get("also_read_by") or []],
                "cached_by": line.get("cached_by"),
                "query_count": len(by_table[table]),
                "queries": by_table[table],
            }
        )

    engine_of = {str(qa["query_id"]): str(qa.get("assigned_engine") or "") for qa in qas}
    placements, units = _group_placements(lineage)
    raw_carried = report.get("carried_writes")
    carried_by_table: dict[str, dict[str, Mapping[str, Any]]] | None = None
    carried_by_query: dict[str, Mapping[str, Any]] | None = None
    if isinstance(raw_carried, list):
        carried_by_table, carried_by_query = {}, {}
        for c in raw_carried:
            if isinstance(c, Mapping) and c.get("table_id") and c.get("query_id"):
                carried_by_table.setdefault(str(c["table_id"]), {})[str(c["query_id"])] = c
                carried_by_query[str(c["query_id"])] = c
    write_groups = effective.get("write_groups") or {}
    multi = {
        str(t): [str(q) for q in dict.fromkeys(m or [])]
        for t, m in write_groups.items()
        if len(set(m or [])) > 1
    }
    write_out = [
        _group_detail(
            "write",
            table,
            members,
            engine_of,
            traces,
            placements,
            units,
            floor,
            None if carried_by_table is None else carried_by_table.get(table, {}),
        )
        for table, members in sorted(multi.items())
    ]
    co_out = [
        _group_detail(
            "co_dependency",
            f"Co-dependency group {i}",
            [str(q) for q in dict.fromkeys(g or [])],
            engine_of,
            traces,
            placements,
            units,
            # A co-dependency member fits poorly only as a carried write (#1362), the
            # one record of poor fits every deliverable states; no floor of its own.
            None,
            carried_by_query,
        )
        for i, g in enumerate(effective.get("co_dependency_groups") or [], start=1)
        if isinstance(g, list)
    ]
    detail = RoutingDetail.model_validate(
        {
            "effective_version": int(effective["version"]) if effective.get("version") else None,
            "has_ledger": any(t.get("has_ledger") for t in traces.values()),
            "has_access_record": bool(rows),
            "has_analysis": bool(analyses),
            "has_carried_writes": carried_by_table is not None,
            "analysed_engines": engines,
            "engine_scope": list((facts_block or {}).get("engine_scope") or []),
            "poor_fit_floor": poor_fit_floor,
            "query_count": len(queries),
            "table_count": len(tables_out),
            "moved_query_count": sum(1 for t in traces.values() if trace_kinds(t).get("moved")),
            "close_call_count": sum(1 for q in queries if q["close_call"]),
            "settled_tie_count": sum(1 for q in queries if q["settled_tie"]),
            "has_owner_lines": bool(stored),
            "tied_query_count": sum(1 for q in queries if q["tied_engines"]),
            "write_group_count": len(write_out),
            "single_statement_write_group_count": len(write_groups) - len(multi),
            "co_dependency_group_count": len(co_out),
            "tables": tables_out,
            "queries": queries,
            "write_groups": write_out,
            "co_dependency_groups": co_out,
        }
    )
    return detail.model_dump(mode="json")
