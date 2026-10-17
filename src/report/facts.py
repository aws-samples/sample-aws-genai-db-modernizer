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

from src.shared.report_facts import access_pattern_facts, compute_report_facts, query_move_facts

from .waves import resolve_migration_waves


def report_facts(report: dict[str, Any]) -> dict[str, Any]:
    """``report["facts"]`` when synthesis wrote it, else the same block computed now.

    A block written before contract 1.26 has no ``access_pattern_totals`` (#792), and one
    written before 1.27 no query move counts (#1020); those figures are filled in from
    ``query_groups`` and ``ranking``, the rest stays as stored.
    """
    facts = report.get("facts")
    if isinstance(facts, dict) and isinstance(facts.get("engines"), list):
        missing: dict[str, Any] = {}
        if "access_pattern_totals" not in facts:
            missing["access_pattern_totals"] = access_pattern_facts(report)
        if "moving_query_count" not in facts:
            missing.update(query_move_facts(report, facts["engines"]))
        return {**facts, **missing} if missing else facts
    return compute_report_facts(report, waves=resolve_migration_waves(report))


def wave_facts_by_number(report: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """wave number -> that wave's facts."""
    return {int(w.get("wave") or 0): w for w in report_facts(report).get("waves") or []}
