"""Structured ground-truth facts for the rubric judge (``ci/llm/judge.py``).

The judge used to see the first 8,000 characters of ``report.json`` (about
4% of a wordpress-sized report), which cut off ``architecture_type``,
``table_mappings``, ``tco_analysis``, ``risk_assessment`` and
``reality_check``. ``build_facts`` instead pulls out exactly the fields the
rubric criteria are checked against, as one compact JSON-serialisable dict:

* ``totals`` -- headline counts (tables, queries, risks), architecture type,
  overall risk, and the sums the deliverables print: target objects, in-scope
  and out-of-scope access patterns (designed engines only), query groups (all, and the
  non-relational ones the Engineering Report heading counts), open and resolved risks,
  wave count (#646).
* ``engines`` -- per selected engine: workload, confidence (the routed fit the
  deliverables show, #152; ``analysis_confidence`` is the all-tables average), monthly cost,
  schema counts, assignment reasons, the tables its assigned queries touch
  (``tables_served``) and its ``primary_tables`` from ``table_mappings``.
* ``eliminated_engines`` -- engines dropped by reality-check, with the engine
  that absorbed their queries.
* ``reality_check`` -- before/after query distribution and each move.
* ``cache_overlay`` -- the cache layer (#296): ElastiCache owns no query, so it
  is never part of the workload distribution; this block gives the hot reads it
  fronts (count, share of calls, owner engines) and the safety-net notes.
* ``tco`` -- the full ``tco_analysis`` (small).
* ``risks`` -- one compact entry per risk, including the engine(s) its
  queries are actually assigned to.
* ``migration_waves`` -- from ``report.json`` when synthesis emits it, else an
  explicit "not in report.json" marker.
* ``wave_figures`` -- each wave's table figures as the report states them
  (``facts.waves[]``: served, moving and kept-in-sync tables, per engine), so
  a deliverable's "18 served, 15 move" is checked against the report's own
  numbers (#952).
* ``routing_margins`` -- the close calls (queries whose engine won by under
  the margin), per engine pair, without query ids (#952).
* ``migration_risk_facts`` -- what the shape of this migration implies a risk
  register should cover, taken from ``report.json`` (#952), the report's own
  figure wherever it states one: the tables each wave keeps in sync from an
  owner outside it (``facts.waves[].kept_in_sync``), the tables a wave's own
  engines share (``shared_in_wave``, listed apart, never counted into it), close calls, cached reads the
  overlay dropped, unsupported patterns, application-layer notes, hot
  partitions, unservable queries, unresolved ties and design trade-offs. The
  risks criterion grades completeness against it. ``None`` means the report
  does not carry that field (an older report), not zero.

Domain rule (the reason ``tables_served`` exists): assignments map *queries*
to engines, and a table can be served by several engines.
``table_mappings``' single ``recommended_database`` per table is not
ownership, so "does engine X touch table T" must be answered from
``tables_served`` -- ``effective_architecture.engines[].tables`` in the
synthesis ``llm_input.json`` when present, otherwise derived the same way
(tables accessed by the engine's assigned in-scope queries) from
``assignment.json``, or as a last resort from ``report.json`` ``query_groups``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

# Long free-text fields in the risk list are capped so one verbose risk can't
# crowd out the others; the cut is always marked.
RISK_TEXT_CHAR_CAP = 300

MIGRATION_WAVES_ABSENT = (
    "not in report.json; read the waves from the executive report PDF's " "Migration waves pages"
)

_RELATIONAL_ENGINES = {"aurora", "aurora_mysql", "aurora_postgresql"}

_ENGINE_PREFIX_RE = re.compile(r"^\[([a-z0-9_]+)\]\s*")


def _cap(text: Any, limit: int = RISK_TEXT_CHAR_CAP) -> str | None:
    if text is None:
        return None
    value = str(text)
    if len(value) <= limit:
        return value
    return value[:limit] + f" ...[truncated: {limit} of {len(value)} chars shown]"


def _strip_db(table: str, db: str | None) -> str:
    """``wordpress.wp_posts`` -> ``wp_posts``: report.json qualifies table
    names with the source database, effective_architecture doesn't. One
    spelling everywhere lets the judge compare lists directly."""
    if db and table.startswith(db + "."):
        return table[len(db) + 1 :]
    return table


def _assigned_queries(
    report: dict[str, Any], assignment: dict[str, Any] | None
) -> Iterator[tuple[str | None, list[str], set[str], bool]]:
    """Yield ``(query_id, tables, engines, in_scope)`` per assigned query.

    The assignment's ``query_assignments`` are authoritative (same source as
    ``synthesis_grounding.engine_table_scope``). Without one, fall back to
    ``report.json`` ``query_groups``: the engines of the access patterns each
    query is linked to, else the group's engines. ``query_groups`` only lists
    queries that belong to a design group, so the fallback can be partial.
    """
    qas = (assignment or {}).get("query_assignments") or []
    if qas:
        for qa in qas:
            # The cache layer serves the tables of the reads it fronts (#296)
            engines = {e for e in (qa.get("assigned_engine"), qa.get("cache_engine")) if e}
            yield (
                qa.get("query_id"),
                list(qa.get("source_tables") or []),
                engines,
                bool(qa.get("in_scope", True)),
            )
        return
    for group in report.get("query_groups") or []:
        pattern_engine = {
            ap.get("pattern_id"): ap.get("engine") for ap in group.get("access_patterns") or []
        }
        group_engines = {e for e in group.get("engines") or [] if e}
        for query in group.get("source_queries") or []:
            linked = {
                pattern_engine[p]
                for p in query.get("linked_patterns") or []
                if pattern_engine.get(p)
            }
            yield (
                query.get("query_id"),
                list(query.get("tables_accessed") or []),
                linked or group_engines,
                True,
            )


def query_engines(
    report: dict[str, Any], assignment: dict[str, Any] | None = None
) -> dict[str, set[str]]:
    """Map each query id to the engine(s) it's assigned to."""
    out: dict[str, set[str]] = {}
    for qid, _tables, engines, _in_scope in _assigned_queries(report, assignment):
        if qid:
            out.setdefault(qid, set()).update(engines)
    return out


def derive_tables_served(
    report: dict[str, Any], assignment: dict[str, Any] | None = None
) -> dict[str, list[str]]:
    """Per engine, the tables its assigned in-scope queries access (the
    fallback for ``effective_architecture.engines[].tables``). Pseudo-tables
    that aren't source tables (``unknown``, ``DUAL``, a column name a parser
    mistook for a table) are dropped when ``table_mappings`` lists the
    source tables."""
    db = report.get("database_name")
    known = {_strip_db(m.get("source_table", ""), db) for m in report.get("table_mappings") or []}
    out: dict[str, set[str]] = {}
    for _qid, tables, engines, in_scope in _assigned_queries(report, assignment):
        if not in_scope:
            continue
        names = {_strip_db(t, db) for t in tables}
        if known:
            names &= known
        for engine in engines:
            out.setdefault(engine, set()).update(names)
    return {engine: sorted(tables) for engine, tables in out.items()}


def _tables_served(
    tables: list[str] | None, assigned_queries: Any, partial_scope: bool
) -> list[str] | None:
    """``None`` (unknown) rather than ``[]`` when the scope came from the
    partial query_groups fallback and an engine with assigned queries got no
    tables from it: an empty list would claim the engine touches no table."""
    if tables:
        return tables
    if partial_scope and isinstance(assigned_queries, int) and assigned_queries > 0:
        return None
    return tables or []


def _designed_targets(ranking: list[dict[str, Any]]) -> set[str]:
    """Engines the deliverables count in their schema-design totals.

    Same definition as ``synthesis_report.designed_and_not_designed_engines``: an
    entry that records ``assigned_queries`` needs workload (assigned queries or a
    cache overlay), and every entry needs a schema design. The totals the PDF and the
    summary print ("N target objects", "M in-scope access patterns") are over these
    engines only.
    """
    out: set[str] = set()
    for entry in ranking:
        if "assigned_queries" in entry and not (
            (entry.get("assigned_queries") or 0) > 0
            or (entry.get("cache_overlay_queries") or 0) > 0
        ):
            continue
        if entry.get("schema_design_available") and entry.get("target"):
            out.add(str(entry["target"]))
    return out


def _access_pattern_totals(report: dict[str, Any], engines: set[str]) -> tuple[int, int] | None:
    """``(in_scope, out_of_scope)`` access patterns of ``engines`` across ``query_groups``.

    A pattern is one ``(engine, pattern_id)`` however many groups list it, and counts as
    in scope when any listing says so (``formatting.access_pattern_scope``). ``None``
    when the report has no query groups, so the judge reads "unknown" rather than 0.
    """
    groups = report.get("query_groups")
    if not isinstance(groups, list):
        return None
    seen: dict[tuple[str, str], bool] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        for ap in group.get("access_patterns") or []:
            if isinstance(ap, dict) and ap.get("engine") in engines and ap.get("pattern_id"):
                key = (str(ap["engine"]), str(ap["pattern_id"]))
                seen[key] = seen.get(key, False) or ap.get("in_scope", True) is not False
    n_in = sum(1 for in_scope in seen.values() if in_scope)
    return n_in, len(seen) - n_in


def _has_content(description: Any) -> bool:
    """A risk with text after its ``[engine]`` tag (``report_facts.risk_has_content``);
    the deliverables count only these."""
    text = str(description or "").strip()
    if text.startswith("[") and "]" in text:
        text = text[text.index("]") + 1 :].strip()
    if text.lower().startswith("unknown:"):
        text = text[len("unknown:") :].strip()
    return bool(text)


def _counted_risks(value: Any) -> int | None:
    """Count of the risks the deliverables count; ``None`` when the report has no list."""
    if not isinstance(value, list):
        return None
    return sum(1 for r in value if isinstance(r, dict) and _has_content(r.get("description")))


def _eliminated_engines(
    report: dict[str, Any], effective: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if effective and isinstance(effective.get("eliminated_engines"), list):
        return list(effective["eliminated_engines"])
    # Fallback: a full reality-check consolidation whose source engine no
    # longer carries any query.
    rc = report.get("reality_check") or {}
    after = rc.get("after_distribution") or {}
    out = []
    for move in rc.get("consolidations") or []:
        source = move.get("from_engine")
        if move.get("action") == "full" and source and not after.get(source):
            out.append({"engine": source, "absorbed_by": move.get("to_engine")})
    return out


def _count(value: Any) -> int | None:
    return len(value) if isinstance(value, list) else None


_WAVE_FIGURE_KEYS = (
    "wave",
    "engines",
    "migrating",
    "served_tables",
    "moving_tables",
    "kept_in_sync_tables",
    "moving_by_engine",
    "served_by_engine",
)


def _wave_figures(report: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The facts layer's per-wave table figures, verbatim; ``None`` when the
    report has no ``facts.waves``."""
    layer = (report.get("facts") or {}).get("waves")
    if not isinstance(layer, list):
        return None
    return [{k: w.get(k) for k in _WAVE_FIGURE_KEYS} for w in layer if isinstance(w, dict)]


def _routing_margins(report: dict[str, Any]) -> dict[str, Any] | None:
    """Close calls per engine pair, from the synthesis facts layer
    (``report.facts.close_calls``, ADR-032) or else counted from
    ``query_traces.close_calls``. ``None`` when the report has neither."""
    layer = (report.get("facts") or {}).get("close_calls")
    if isinstance(layer, dict):
        return {
            "margin_points": layer.get("margin_points"),
            "close_call_query_count": layer.get("close_call_query_count"),
            "settled_tie_count": layer.get("settled_tie_count"),
            "engine_pairs": [
                {k: v for k, v in pair.items() if k not in ("query_ids", "more_query_count")}
                for pair in layer.get("engine_pairs") or []
                if isinstance(pair, dict)
            ],
        }
    raw = (report.get("query_traces") or {}).get("close_calls")
    if not isinstance(raw, list):
        return None
    # A tie a rule settled is reported apart (``settled_tie_count``), as the
    # facts layer's ``close_call_facts`` does, not as a close call.
    calls = [c for c in raw if isinstance(c, dict) and not c.get("settled_tie")]
    settled = sum(1 for c in raw if isinstance(c, dict) and c.get("settled_tie"))
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for call in calls:
        key = (str(call.get("engine")), str(call.get("alternative")))
        slot = pairs.setdefault(
            key, {"engine": key[0], "alternative": key[1], "pair_query_count": 0, "gaps": []}
        )
        slot["pair_query_count"] += 1
        if isinstance(call.get("gap"), (int, float)):
            slot["gaps"].append(call["gap"])
    out = []
    for slot in sorted(pairs.values(), key=lambda p: (-p["pair_query_count"], p["engine"])):
        gaps = slot.pop("gaps")
        slot["min_gap"] = min(gaps) if gaps else None
        slot["max_gap"] = max(gaps) if gaps else None
        out.append(slot)
    return {"close_call_query_count": len(calls), "settled_tie_count": settled, "engine_pairs": out}


def _wave_sync(
    report: dict[str, Any], db: str | None
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    """``(kept_in_sync, shared_in_wave)`` per wave.

    The report's own figures win (ADR-032): the facts layer's
    ``facts.waves[].kept_in_sync`` / ``kept_in_sync_tables`` (tables whose
    owner is outside the wave, kept in sync from it) and
    ``facts.waves[].shared_in_wave`` (a table one wave engine serves while
    another engine of the same wave owns it, #581). Only a report without the
    facts layer falls back to ``migration_waves[].table_owners``, split by the
    same rule (``src.shared.report_facts.wave_facts``): owner in the wave's
    engines -> shared, else kept in sync, one entry per table. ``None`` when
    the report has neither."""
    layer = (report.get("facts") or {}).get("waves")
    waves = report.get("migration_waves")
    source: list[tuple[Any, Any, list, list, Any]] = []
    if isinstance(layer, list) and layer:
        for w in layer:
            if isinstance(w, dict):
                source.append(
                    (
                        w.get("wave"),
                        w.get("engines"),
                        list(w.get("kept_in_sync") or []),
                        list(w.get("shared_in_wave") or []),
                        w.get("kept_in_sync_tables"),
                    )
                )
    elif isinstance(waves, list) and waves:
        for w in waves:
            if not isinstance(w, dict):
                continue
            engines = [str(e) for e in w.get("engines") or [] if e]
            outside: dict[str, dict[str, Any]] = {}
            inside: list[dict[str, Any]] = []
            for o in w.get("table_owners") or []:
                if not isinstance(o, dict) or not o.get("table") or not o.get("owner"):
                    continue
                if str(o["owner"]) in engines:
                    inside.append(o)
                else:
                    outside.setdefault(str(o["table"]), o)
            source.append((w.get("wave"), w.get("engines"), list(outside.values()), inside, None))
    else:
        return None, None
    kept_out: list[dict[str, Any]] = []
    shared_out: list[dict[str, Any]] = []
    for wave, engines, kept, shared, kept_count in source:
        kept = [e for e in kept if isinstance(e, dict) and e.get("table")]
        shared = [e for e in shared if isinstance(e, dict) and e.get("table")]
        if kept:
            owners: Counter[str] = Counter(str(e.get("owner")) for e in kept)
            kept_out.append(
                {
                    "wave": wave,
                    "engines": engines,
                    "kept_in_sync_tables": kept_count if kept_count is not None else len(kept),
                    "by_owner": dict(owners),
                    "sync": sorted({str(e.get("sync")) for e in kept if e.get("sync")}),
                    "tables": sorted(_strip_db(str(e["table"]), db) for e in kept),
                }
            )
        if shared:
            shared_out.append(
                {
                    "wave": wave,
                    "engines": engines,
                    "entries": len(shared),
                    "tables": sorted(
                        (
                            {"table": _strip_db(str(e["table"]), db), "owner": e.get("owner")}
                            for e in shared
                        ),
                        key=lambda row: (row["table"], str(row["owner"])),
                    ),
                }
            )
    return kept_out, shared_out


def _shared_tables(
    served: dict[str, list[str] | None], cache_engines: set[str]
) -> dict[str, Any] | None:
    """Tables two or more owning engines serve (the cache layer left out): each
    one needs its copies kept in sync, whichever way the waves run it."""
    if not served:
        return None
    by_table: dict[str, set[str]] = {}
    for engine, tables in served.items():
        if not engine or engine in cache_engines or not tables:
            continue
        for table in tables:
            by_table.setdefault(table, set()).add(engine)
    shared: list[dict[str, Any]] = [
        {"table": t, "engines": sorted(by_table[t])}
        for t in sorted(by_table)
        if len(by_table[t]) > 1
    ]
    return {"count": len(shared), "tables": shared}


def _item_count(value: Any) -> int:
    """A design field as a count: synthesis writes counts (``hot_partitions_at_risk``) and lists.

    Anything else (None, a float, a string) counts as zero; the writer never emits them.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return max(value, 0)
    return len(value) if isinstance(value, (list, tuple, dict)) else 0


def _migration_risk_facts(
    report: dict[str, Any],
    db: str | None,
    served: dict[str, list[str] | None] | None = None,
    cache_engines: set[str] | None = None,
) -> dict[str, Any]:
    designs = report.get("schema_designs")
    designs = designs if isinstance(designs, dict) else {}

    def per_engine(field: str) -> dict[str, int] | None:
        if not designs:
            return None
        counts = {
            engine: _item_count(design.get(field))
            for engine, design in sorted(designs.items())
            if isinstance(design, dict)
        }
        return {engine: n for engine, n in counts.items() if n}

    unsupported = []
    app_notes = []
    for engine, design in sorted(designs.items()):
        if not isinstance(design, dict):
            continue
        for item in design.get("unsupported_patterns") or []:
            if isinstance(item, dict):
                unsupported.append(
                    {
                        "engine": engine,
                        "queries": len(item.get("source_query_ids") or []),
                        "reason": _cap(item.get("reason")),
                        "workaround": _cap(item.get("workaround")),
                    }
                )
        for note in design.get("app_layer_notes") or []:
            if isinstance(note, dict):
                app_notes.append(
                    {
                        "engine": engine,
                        "feature": note.get("feature"),
                        "source_object": _cap(note.get("source_object")),
                        "recommendation": _cap(note.get("recommendation")),
                    }
                )
    kept, shared = _wave_sync(report, db)
    trade_offs = report.get("trade_offs")
    unservable = (report.get("facts") or {}).get("unservable")
    if isinstance(unservable, dict):
        unservable_count = unservable.get("unservable_query_count")
    else:
        unservable_count = _count((report.get("query_traces") or {}).get("unservable"))
    overlay = report.get("cache_overlay")
    return {
        "rule": (
            "risks this migration's shape implies, computed from report.json; a "
            "deliverable that raises none of them while they are present has an "
            "incomplete risk register. null = the report does not carry the field"
        ),
        # The report's figure (facts.waves[].kept_in_sync_tables); in-wave
        # sharing is a separate, judge-only list, never added into it.
        "kept_in_sync": kept,
        "shared_in_wave": shared,
        "tables_served_by_several_engines": _shared_tables(served or {}, cache_engines or set()),
        "close_call_query_count": (_routing_margins(report) or {}).get("close_call_query_count"),
        "cache_reads_dropped": (
            _count(overlay.get("dropped_query_ids")) if isinstance(overlay, dict) else None
        ),
        "unsupported_patterns": unsupported if designs else None,
        "app_layer_notes": app_notes if designs else None,
        "hot_partitions_at_risk": per_engine("hot_partitions_at_risk"),
        "unservable_queries": unservable_count,
        "unresolved_ties": (report.get("assignment_summary") or {}).get("tie_unresolved_count"),
        "design_trade_offs_by_engine": (
            dict(
                Counter(
                    str(t.get("engine") or "unknown") for t in trade_offs if isinstance(t, dict)
                )
            )
            if isinstance(trade_offs, list)
            else None
        ),
    }


def build_facts(
    report: dict[str, Any],
    llm_input: dict[str, Any] | None = None,
    assignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the structured facts block for one job (see module docstring).

    ``llm_input`` is the synthesis ``llm_input.json`` (its
    ``effective_architecture`` is the preferred per-engine table scope) and
    ``assignment`` the matching ``assignment/v{N}/assignment.json`` (the
    query-to-engine map); both optional."""
    db = report.get("database_name")
    effective = (llm_input or {}).get("effective_architecture")
    if not isinstance(effective, dict):
        effective = None

    ranking = [r for r in report.get("ranking") or [] if isinstance(r, dict)]
    tco = report.get("tco_analysis") or {}
    cost_by_engine = {
        c.get("database"): c.get("monthly_cost_usd") for c in tco.get("cost_breakdown") or []
    }
    table_mappings = report.get("table_mappings") or []
    rec_arch = report.get("recommended_architecture") or {}
    rec_tables = {d.get("service"): d.get("table_count") for d in rec_arch.get("databases") or []}

    if effective:
        served_source = "effective_architecture (synthesis llm_input.json)"
        served = {
            e.get("engine"): sorted(e.get("tables") or []) for e in effective.get("engines") or []
        }
    elif (assignment or {}).get("query_assignments"):
        served_source = "derived from assignment.json query_assignments (no llm_input.json)"
        served = derive_tables_served(report, assignment)
    else:
        served_source = (
            "derived from report.json query_groups (no llm_input.json or assignment.json; "
            "covers only queries in a design group, so may be partial; null = unknown, "
            "not 'no tables')"
        )
        served = derive_tables_served(report)
    partial_scope = not effective and not (assignment or {}).get("query_assignments")

    # Names that aren't source tables ("unknown", "DUAL", a column a parser
    # took for a table) are dropped from risk table lists when the source
    # tables are known.
    known_tables = {_strip_db(m.get("source_table", ""), db) for m in table_mappings}
    for tables in served.values():
        known_tables.update(tables)

    engines = []
    for entry in ranking:
        engine = entry.get("target") or entry.get("engine")
        is_cache = entry.get("role") == "cache_layer"
        engines.append(
            {
                "engine": engine,
                "assigned_queries": entry.get("assigned_queries"),
                "workload_percent": None if is_cache else entry.get("workload_percent"),
                # The cache layer owns no query: workload_percent is null and the
                # cached_* fields describe it instead (#296)
                **(
                    {
                        "role": "cache_layer",
                        "cached_queries": entry.get("cache_overlay_queries"),
                        "cached_call_share_percent": entry.get("cache_call_share_percent"),
                    }
                    if is_cache
                    else {}
                ),
                # The deliverables show the fit of the routed queries (#152); the
                # all-analyzed-tables average is kept apart for audit
                "confidence": (
                    entry["routed_confidence"]
                    if entry.get("routed_confidence") is not None
                    else entry.get("confidence_score")
                ),
                "analysis_confidence": entry.get("confidence_score"),
                # Whether a rated source table backs the routed fit (#152/#312): a
                # "signal_only" or "partial" value means the deliverables show a
                # caveat the judge must not read as a contradiction.
                "confidence_evidence": (
                    entry.get("routed_confidence_evidence")
                    if entry.get("routed_confidence") is not None
                    else None
                ),
                "monthly_cost_usd": cost_by_engine.get(engine, entry.get("monthly_cost_usd")),
                "tables_served": _tables_served(
                    served.get(engine), entry.get("assigned_queries"), partial_scope
                ),
                "primary_tables": [
                    _strip_db(m.get("source_table", ""), db)
                    for m in table_mappings
                    if m.get("recommended_database") == engine
                ],
                "recommended_architecture_table_count": rec_tables.get(engine),
                "schema": {
                    "target_tables": entry.get("target_tables"),
                    "access_patterns": entry.get("access_patterns"),
                    "pattern_groups": entry.get("pattern_groups"),
                },
                "assignment_reasons": [
                    _cap(r) for r in entry.get("assignment_reason_summary") or []
                ],
            }
        )

    qeng = query_engines(report, assignment)
    risk_assessment = report.get("risk_assessment") or {}
    risks = []
    severity_counts: Counter[str] = Counter()
    for risk in risk_assessment.get("risks") or []:
        description = risk.get("description") or ""
        prefix = _ENGINE_PREFIX_RE.match(description)
        query_ids = risk.get("query_ids") or []
        on_engines: Counter[str] = Counter()
        for qid in query_ids:
            on_engines.update(qeng.get(qid) or {"not_in_assignment_data"})
        severity_counts[str(risk.get("severity"))] += 1
        risks.append(
            {
                "id": risk.get("risk_id"),
                "severity": risk.get("severity"),
                "engine": risk.get("engine") or (prefix.group(1) if prefix else None),
                "type": risk.get("risk_type"),
                "description": _cap(description),
                "mitigation": _cap(risk.get("mitigation")),
                "affected_tables": [
                    name
                    for name in (_strip_db(t, db) for t in risk.get("affected_tables") or [])
                    if not known_tables or name in known_tables
                ],
                "query_count": len(query_ids),
                "queries_assigned_to": dict(on_engines),
            }
        )

    summary = report.get("assignment_summary") or {}
    tables_analyzed = next(
        (r.get("tables_analyzed") for r in ranking if r.get("tables_analyzed") is not None), None
    )
    rc = report.get("reality_check") or {}
    waves = report.get("migration_waves")
    designed = _designed_targets(ranking)
    ap_totals = _access_pattern_totals(report, designed) if designed else None
    target_counts = [
        r.get("target_tables")
        for r in ranking
        if r.get("target") in designed and r.get("target_tables") is not None
    ]
    query_groups = report.get("query_groups")
    overlay = report.get("cache_overlay") or {}

    return {
        "source": {
            "report_json": "synthesis report.json",
            "tables_served": served_source,
            "risk_query_engines": (
                "assignment.json query_assignments"
                if (assignment or {}).get("query_assignments")
                else "report.json query_groups (partial: only queries in a design group)"
            ),
        },
        "totals": {
            "database": db,
            "architecture_type": rec_arch.get("architecture_type"),
            "architecture_rationale": _cap(rec_arch.get("rationale")),
            "engines_selected": len(engines),
            "tables_analyzed": tables_analyzed,
            "tables_mapped": len(table_mappings),
            "queries": summary.get("query_count"),
            "queries_in_scope": summary.get("in_scope_count"),
            "co_dependency_groups": summary.get("co_dependency_groups"),
            # Totals the deliverables print (#646), over the same engines and entries
            # they count: the schema-design sums cover designed engines only; risk
            # counts skip contentless entries; null means the report does not say.
            "target_objects": sum(target_counts) if target_counts else None,
            "in_scope_access_patterns": ap_totals[0] if ap_totals else None,
            "out_of_scope_access_patterns": ap_totals[1] if ap_totals else None,
            "query_groups": len(query_groups) if isinstance(query_groups, list) else None,
            # The Engineering Report heading "Query groups (N)" leaves out groups served
            # only by Aurora engines (listed per table elsewhere in that report).
            "query_groups_non_relational": (
                sum(
                    1
                    for g in query_groups
                    if isinstance(g, dict)
                    and not set(g.get("engines") or []).issubset(_RELATIONAL_ENGINES)
                )
                if isinstance(query_groups, list)
                else None
            ),
            "open_risks": _counted_risks(risk_assessment.get("risks")),
            "resolved_risks": _counted_risks(risk_assessment.get("resolved_risks")),
            "migration_wave_count": len(waves) if isinstance(waves, list) and waves else None,
            "overall_risk": risk_assessment.get("overall_risk_level"),
            "risks_by_severity": dict(severity_counts),
            "projected_monthly_cost_usd": tco.get("projected_monthly_cost"),
        },
        "engines": engines,
        "eliminated_engines": _eliminated_engines(report, effective),
        "reality_check": {
            "before_distribution": rc.get("before_distribution"),
            "after_distribution": rc.get("after_distribution"),
            "moves": [
                {
                    "from_engine": m.get("from_engine"),
                    "to_engine": m.get("to_engine"),
                    "action": m.get("action"),
                    "query_count": m.get("query_count"),
                    "queries_retained": len(m.get("queries_retained") or []),
                    "reason": _cap(m.get("reason")),
                    "retention_reason": _cap(m.get("retention_reason")),
                }
                for m in rc.get("consolidations") or []
            ],
        },
        **(
            {
                "cache_overlay": {
                    "engine": overlay.get("engine"),
                    "rule": (
                        "the cache owns no query; it fronts hot reads cache-aside and is "
                        "not part of the workload distribution"
                    ),
                    "cached_queries": overlay.get("query_count"),
                    "call_share_percent": overlay.get("call_share_percent"),
                    "owners": overlay.get("owners"),
                    "min_calls_per_second": overlay.get("min_calls_per_second"),
                    "max_rows_avg": overlay.get("max_rows_avg"),
                    "notes": [_cap(n) for n in overlay.get("notes") or []],
                }
            }
            if overlay
            else {}
        ),
        "tco": {
            **tco,
            "assumptions": [_cap(a) for a in tco.get("assumptions") or []],
        },
        "risks": risks,
        "mitigation_strategies": [
            _cap(m) for m in risk_assessment.get("mitigation_strategies") or []
        ],
        "migration_waves": waves if waves else MIGRATION_WAVES_ABSENT,
        "wave_figures": _wave_figures(report),
        "routing_margins": _routing_margins(report),
        "migration_risk_facts": _migration_risk_facts(
            report,
            db,
            {e["engine"]: e["tables_served"] for e in engines if e.get("engine")},
            {e["engine"] for e in engines if e.get("role") == "cache_layer"},
        ),
    }
