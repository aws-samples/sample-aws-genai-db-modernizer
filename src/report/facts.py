"""The one reader of ``report.json``'s ``facts`` block (#615, ADR-032).

Every deliverable reads the figures it prints from here and only formats
them: no renderer adds, subtracts or filters a figure the facts layer owns
(``tests/unit/report/test_renderers_only_format.py``). Synthesis writes the
block (``src.shared.report_facts.compute_report_facts``); a report written
before it existed gets the same block from the same function, over the
roadmap ``resolve_migration_waves`` resolves for it, so an old report still
has one answer per figure.
"""

from __future__ import annotations

from typing import Any

from src.shared.report_facts import (
    access_pattern_facts,
    apply_cross_engine_wave_one,
    compute_report_facts,
    engine_served_split,
    engine_table_split_facts,
    map_split_facts,
    mapped_tables_by_role,
    owned_table_mappings,
    query_move_facts,
    query_pattern_facts,
    query_total_facts,
    query_trace_facts,
    resolved_risk_severity_facts,
    retired_in_use_engine_facts,
    shares_replica_risk,
    table_ownership_facts,
    unservable_query_facts,
    wave_one_moved_by_later_waves,
)

from .waves import resolve_migration_waves


def report_facts(report: dict[str, Any]) -> dict[str, Any]:
    """``report["facts"]`` when synthesis wrote it, else the same block computed now.

    A block written before contract 1.26 has no ``access_pattern_totals`` (#792), one
    written before 1.28 no query move counts and no resolved risks by severity (#1020),
    one written before 1.38 no per-kind ``unservable`` counts (#888), one written
    before 1.41 no ``query_trace.trace_groups`` (#1108; a 1.40 block re-dumped through
    the contract has them empty while queries have a full trace), one written before
    1.42 no Migration map split and no per-engine kept-in-sync counts (#1169), one
    written before #1120 no cross-engine wave 1 figures or note, one written before #1284
    no ``waves[].moved_by_later_waves``, one written before #212 no ``table_owner_lines``
    (and a map split by recommended engine), one written before #1329 no
    ``unservable.no_engine_in_scope_query_count``, and one written before
    #1146 no ``query_pattern_counts`` (filled in from ``query_pattern_groups``: None for a
    report with no grouping, whose deliverables leave the query patterns out); those
    figures are filled in from ``query_groups``, ``ranking``, the resolved risks, the
    traces and the migration waves, the rest stays as stored.
    """
    facts = report.get("facts")
    if isinstance(facts, dict) and isinstance(facts.get("engines"), list):
        missing: dict[str, Any] = {}
        if "access_pattern_totals" not in facts:
            missing["access_pattern_totals"] = access_pattern_facts(report)
        if "moving_query_count" not in facts:
            moves = query_move_facts(report, facts["engines"])
            missing.update(moves)
            missing.update(query_total_facts(report, moves["in_scope_query_count"]))
        elif "query_total_count" not in facts:
            # A block written before #1329: the no-engine count and the total, from the
            # same function (no scope_unservable before it, so 0 and the stored count).
            missing.update(query_total_facts(report, int(facts.get("in_scope_query_count") or 0)))
        if "query_pattern_counts" not in facts:
            missing["query_pattern_counts"] = query_pattern_facts(report, facts["engines"])
        if "resolved_risks_by_severity" not in facts:
            missing.update(resolved_risk_severity_facts(report))
        basis = facts.get("table_basis")
        if isinstance(basis, dict) and (
            "mapped_tables_by_move" not in basis or "table_owner_lines" not in facts
        ):
            # A block written before 1.42 (#1169): the map's split, from the same function;
            # and one written before #212, whose split counted a table by its recommended
            # engine (a cache too): each table now counts under its owner.
            resolved = resolve_migration_waves(report)
            owned = owned_table_mappings(report, resolved)
            roles = {str(e.get("engine")): str(e.get("role")) for e in facts["engines"]}
            missing["table_basis"] = {
                **basis,
                "mapped_tables_by_role": mapped_tables_by_role(owned, roles),
                **map_split_facts(owned, roles, resolved),
            }
        if "table_owner_lines" not in facts:
            # A block written before #212: each mapped table's owner line, from the
            # report's table_sources and query_pattern_groups (the same function).
            missing["table_owner_lines"] = table_ownership_facts(
                report, resolve_migration_waves(report)
            )
        if any(isinstance(e, dict) and "kept_in_sync_tables" not in e for e in facts["engines"]):
            # And each migration target's split of its served tables (#1169).
            split = engine_served_split(resolve_migration_waves(report))
            missing["engines"] = [
                (
                    {
                        **e,
                        **engine_table_split_facts(
                            str(e.get("engine")),
                            e.get("engine_source_tables"),
                            int(e.get("tables_migrate") or 0),
                            split,
                        ),
                    }
                    if isinstance(e, dict) and "kept_in_sync_tables" not in e
                    else e
                )
                for e in facts["engines"]
            ]
        sources = facts.get("table_sources")
        if isinstance(sources, dict) and any(
            isinstance(r, dict) and "shares_replica_risk" not in r
            for r in sources.get("replicated_tables") or []
        ):
            # A block written before #1232: which rows share the one risk sentence,
            # from the same function synthesis calls (an older wording does not).
            missing["table_sources"] = {
                **sources,
                "replicated_tables": [
                    (
                        {**r, "shares_replica_risk": shares_replica_risk(r)}
                        if isinstance(r, dict) and "shares_replica_risk" not in r
                        else r
                    )
                    for r in sources.get("replicated_tables") or []
                ],
            }
        unservable = facts.get("unservable")
        if isinstance(unservable, dict) and "no_fit_query_count" not in unservable:
            # A block written before 1.38 (#888): its kinds, from the same function.
            missing["unservable"] = unservable_query_facts(report)
        elif isinstance(unservable, dict) and "no_engine_in_scope_query_count" not in unservable:
            # A block written before #1329: the no-engine-in-scope count, from the same
            # function (0, since such a report lists no scope_unservable queries).
            computed = unservable_query_facts(report) or {}
            missing["unservable"] = {
                **unservable,
                "no_engine_in_scope_query_count": int(
                    computed.get("no_engine_in_scope_query_count") or 0
                ),
                "no_engine_in_scope_closest_engines": list(
                    computed.get("no_engine_in_scope_closest_engines") or []
                ),
            }
        trace = facts.get("query_trace")
        if (
            isinstance(trace, dict)
            and not trace.get("trace_groups")
            and any(
                isinstance(r, dict) and r.get("in_full") for r in trace.get("listed_queries") or []
            )
        ):
            # A block written before 1.41 (#1108): the groups, from the same function.
            computed = query_trace_facts(report) or {}
            missing["query_trace"] = {
                **trace,
                "trace_groups": computed.get("trace_groups") or [],
                "trace_group_count": computed.get("trace_group_count") or 0,
            }
        if "retired_in_use_engines" not in facts:
            # A block written before this field existed (#1076): the same function over the stored rows.
            missing["retired_in_use_engines"] = retired_in_use_engine_facts(
                report, facts.get("removed_engines") or []
            )
        waves = facts.get("waves")
        if isinstance(waves, list) and any(
            isinstance(w, dict) and "heterogeneous_note" not in w for w in waves
        ):
            # A block written before #1120: a cross-engine wave 1 counted in full
            # and its note, from the same function synthesis calls.
            missing.update(_cross_engine_backfill(report, facts))
        rows = missing.get("waves", waves)
        if isinstance(rows, list) and any(
            isinstance(w, dict) and "moved_by_later_waves" not in w for w in rows
        ):
            # A block written before #1284: wave 1's later-wave moves, from the same function.
            missing["waves"] = _moved_by_later_waves_backfill(report, rows)
        return {**facts, **missing} if missing else facts
    return compute_report_facts(report, waves=resolve_migration_waves(report))


def _cross_engine_backfill(report: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """The #1120 wave and engine figures for a facts block stored before them."""
    rows = [
        {"heterogeneous_note": None, "lists_relational_gaps": False, **w}
        for w in facts["waves"]
        if isinstance(w, dict)
    ]
    engines = [dict(e) for e in facts["engines"] if isinstance(e, dict)]
    waves = resolve_migration_waves(report)
    by_number = {int(w.get("wave") or 0): w for w in waves}
    matched = [by_number.get(int(r.get("wave") or 0)) or {} for r in rows]
    analyzed = facts.get("source_table_count")
    moved = apply_cross_engine_wave_one(
        matched,
        rows,
        engines,
        analyzed if isinstance(analyzed, int) and not isinstance(analyzed, bool) else None,
        facts.get("relational_gaps"),
    )
    out: dict[str, Any] = {"waves": rows}
    if moved is not None:
        out["engines"] = engines
        out["tables_migrate"] = moved
    return out


def _moved_by_later_waves_backfill(report: dict[str, Any], rows: list[Any]) -> list[Any]:
    """The #1284 ``moved_by_later_waves`` on each stored wave row that lacks it; a row
    that is not a record is kept as stored (the contract refuses it, not this reader)."""
    waves = resolve_migration_waves(report)
    moved = wave_one_moved_by_later_waves(waves)
    first = next((int(w.get("wave") or 0) for w in waves if w.get("homogeneity") is not None), None)
    return [
        (
            {**r, "moved_by_later_waves": moved if int(r.get("wave") or 0) == first else None}
            if isinstance(r, dict) and "moved_by_later_waves" not in r
            else r
        )
        for r in rows
    ]


def wave_facts_by_number(report: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """wave number -> that wave's facts."""
    return {int(w.get("wave") or 0): w for w in report_facts(report).get("waves") or []}
