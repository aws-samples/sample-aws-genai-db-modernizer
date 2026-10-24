"""Every deliverable states the read I/O basis DocumentDB's cost rests on (#1197, ADR-032).

DocumentDB's estimate bills a read only on a buffer-cache miss, using the source's hit
ratio, bounded: scaled by the target's memory over the source's, with at least 1% of the
reads billed as misses (maintainer decision on #1222). That assumption moves the figure,
so the facts layer states it once (``facts.cost_basis_notes``), for a kept engine's row
and for a retired engine's saving, and the engineering report, the PDF and the results
page print it next to the cost.
"""

from __future__ import annotations

from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import compute_report_facts
from tests.e2e.deliverable_figures import md_plain
from tests.executive_pdf import executive_text

RANKING = [
    {"target": "aurora_mysql", "workload_percent": 60.0, "confidence_score": 80},
    {"target": "documentdb", "workload_percent": 40.0, "confidence_score": 80},
]
FLOORED = {
    "read_io_hit_ratio_basis": "collection",
    "collection_hit_ratio_pct": 99.99993,
    "collection_hit_ratio_billed_pct": 99.0,
    "source_memory_gb": None,
    "target_memory_gb": 16.0,
    "min_billed_miss_pct": 1.0,
}
NOTE = (
    "DocumentDB's estimate bills a read I/O only on a buffer-cache miss, at the source's "
    "instance-wide buffer-cache hit ratio (99.99993%), not scaled to the target's memory "
    "(the source reported no memory size), billed at 99%. At least 1% of every read is "
    "billed as a miss."
)
SCALED = {
    "read_io_hit_ratio_basis": "collection",
    "collection_hit_ratio_pct": 99.551608,
    "collection_hit_ratio_billed_pct": 24.887902,
    "source_memory_gb": 64.0,
    "target_memory_gb": 16.0,
    "min_billed_miss_pct": 1.0,
}
SCALED_NOTE = (
    "DocumentDB's estimate bills a read I/O only on a buffer-cache miss, at the source's "
    "instance-wide buffer-cache hit ratio (99.551608%) scaled by the target's 16 GiB over "
    "the source's 64 GiB of memory, billed at 24.887902%. At least 1% of every read is "
    "billed as a miss."
)


def _report(tco: dict) -> dict:
    return {
        "database_name": "shop",
        "recommended_architecture": {"architecture_type": "MULTI_DATABASE", "databases": []},
        "ranking": RANKING,
        "risk_assessment": {"overall_risk_level": "LOW", "risks": [], "resolved_risks": []},
        "tco_analysis": tco,
        "trade_offs": [],
        "schema_designs": {},
        "migration_waves": None,
        "table_mappings": [],
        "query_groups": [],
        "cache_overlay": None,
    }


def _tco(**documentdb: object) -> dict:
    return {
        "current_monthly_cost": 0.0,
        "current_cost_known": False,
        "projected_monthly_cost": 584.0,
        "savings_percent": 0,
        "cost_breakdown": [
            {"database": "aurora_mysql", "monthly_cost_usd": 318.8},
            {"database": "documentdb", "monthly_cost_usd": 265.07, **documentdb},
        ],
    }


def test_a_kept_documentdb_row_states_its_basis_and_bound() -> None:
    report = _report(_tco(**FLOORED))
    assert compute_report_facts(report)["cost_basis_notes"] == [NOTE]
    assert f"- Cost basis: {NOTE}" in md_plain(render_engineering_report_md(report))
    pdf = " ".join(executive_text(report).split())
    assert "99.99993%" in pdf and "billed at 99%. At least 1% of every read is billed" in pdf


def test_a_scaled_ratio_states_both_memories() -> None:
    report = _report(_tco(**SCALED))
    assert compute_report_facts(report)["cost_basis_notes"] == [SCALED_NOTE]
    assert f"- Cost basis: {SCALED_NOTE}" in md_plain(render_engineering_report_md(report))
    pdf = " ".join(executive_text(report).split())
    assert "scaled by the target's 16 GiB over the source's 64 GiB of memory" in pdf


def test_a_row_written_before_the_bound_states_the_ratio_only() -> None:
    # A saved report from round 1 of #1197 has the ratio but no bound fields.
    report = _report(_tco(read_io_hit_ratio_basis="collection", collection_hit_ratio_pct=99.99993))
    assert compute_report_facts(report)["cost_basis_notes"] == [
        "DocumentDB's estimate bills a read I/O only on a buffer-cache miss, at the source's "
        "instance-wide buffer-cache hit ratio (99.99993%)."
    ]


def test_a_retired_engine_s_saving_states_its_basis() -> None:
    tco = _tco()
    tco["cost_breakdown"] = tco["cost_breakdown"][:1]
    tco["retired_engine_savings"] = [
        {"database": "documentdb", "monthly_cost_usd": 265.07, **FLOORED}
    ]
    report = _report(tco)
    assert compute_report_facts(report)["cost_basis_notes"] == [NOTE]


def test_no_hit_ratio_says_every_read_is_billed() -> None:
    report = _report(_tco(read_io_hit_ratio_basis="none", collection_hit_ratio_pct=None))
    [note] = compute_report_facts(report)["cost_basis_notes"]
    assert note.startswith("DocumentDB's estimate bills every read I/O")


def test_an_estimate_with_no_cache_basis_states_none() -> None:
    # Aurora is billed by capacity, and a report written before #1197 has no basis.
    report = _report(_tco())
    assert compute_report_facts(report)["cost_basis_notes"] == []
    assert "Cost basis:" not in render_engineering_report_md(report)


def test_a_query_s_own_ratio_states_the_floor_too() -> None:
    # The floor bounds every read, a query's own ratio included (decision on #1222).
    row = {"read_io_hit_ratio_basis": "query", "collection_hit_ratio_pct": None}
    report = _report(_tco(**row, min_billed_miss_pct=1.0))
    assert compute_report_facts(report)["cost_basis_notes"] == [
        "DocumentDB's estimate bills a read I/O only on a buffer-cache miss, at each "
        "query's own buffer-cache hit ratio. At least 1% of every read is billed as a miss."
    ]
