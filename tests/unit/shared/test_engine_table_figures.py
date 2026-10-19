"""A migration target's table figures come from its routing, design or not (#949).

The query is the unit everywhere (ADR-031): the tables an engine serves and the
ones that move to it are the tables its routed queries touch and own, which its
migration wave states. With a completed schema design, ``engine_facts`` read
them from the design's table map instead, so one report said "18 tables
migrate" beside wave 3's "18 source tables: 15 to move, 3 kept in sync".
"""

from __future__ import annotations

from typing import Any

from src.shared.report_facts import compute_report_facts, engine_facts

TABLES = [f"wp.t{i}" for i in range(18)]


def _wave() -> dict[str, Any]:
    return {
        "wave": 3,
        "engines": ["dynamodb"],
        "moves_from": "aurora_mysql",
        "tables": TABLES,
        "table_count": 18,
        "table_owners": [{"table": t, "owner": "aurora_mysql"} for t in TABLES[:3]],
    }


def _report(design_tables: int, mapped: int) -> dict[str, Any]:
    return {
        "ranking": [
            {"target": "aurora_mysql", "workload_percent": 63.6},
            {"target": "dynamodb", "workload_percent": 36.4},
        ],
        "recommended_architecture": {
            "databases": [{"service": "dynamodb", "table_count": mapped}],
        },
        "schema_designs": {
            "aurora_mysql": {"status": "not_available"},
            "dynamodb": {
                "status": "completed",
                "tables": [
                    {"table_name": f"T{i}", "source_tables": [f"wp.x{i}"]}
                    for i in range(design_tables)
                ],
            },
        },
        "migration_waves": [_wave()],
    }


def _ddb(report: dict[str, Any]) -> dict[str, Any]:
    rows = engine_facts(report, report["migration_waves"])
    return next(r for r in rows if r["engine"] == "dynamodb")


def test_a_designed_target_reads_its_tables_from_its_wave() -> None:
    row = _ddb(_report(design_tables=18, mapped=18))
    assert row["engine_source_tables"] == 18
    assert row["tables_migrate"] == 15


def test_the_design_map_can_be_larger_or_smaller_than_the_routing() -> None:
    for mapped in (12, 20):
        row = _ddb(_report(design_tables=mapped, mapped=mapped))
        assert (row["engine_source_tables"], row["tables_migrate"]) == (18, 15)


def test_the_headline_total_agrees_with_the_wave() -> None:
    facts = compute_report_facts(_report(design_tables=18, mapped=18))
    wave = next(w for w in facts["waves"] if w["wave"] == 3)
    assert facts["tables_migrate"] == wave["moving_tables"] == 15


def test_with_no_wave_the_design_map_is_still_the_figure() -> None:
    """What must not change: a report with no wave for the engine (written before
    waves existed) keeps the design's table count."""
    report = _report(design_tables=18, mapped=18)
    report["migration_waves"] = []
    row = _ddb(report)
    assert (row["engine_source_tables"], row["tables_migrate"]) == (18, 18)
