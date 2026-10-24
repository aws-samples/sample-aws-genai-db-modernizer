"""Every deliverable states what each engine's cost was sized for (#1195, ADR-032).

Synthesis records each kept engine's sizing on its ``tco_analysis.cost_breakdown`` row;
the facts layer states it once per engine (``facts.engines[].cost_sizing``), and the
engineering report and the PDF report only format it. A report written before has no
sizing and states none.
"""

from __future__ import annotations

from typing import Any

from src.contracts.synthesis_output import ReportFacts
from src.report.formatting import cost_sizing_text
from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import compute_report_facts
from tests.executive_pdf import executive_text

AURORA = {
    "basis": "assigned_queries",
    "sized_query_count": 67,
    "calls_per_second": 96.51,
    "read_calls_per_second": 70.2,
    "write_calls_per_second": 20.0,
    "sized_table_count": 31,
    "storage_gb": 0.1,
}
CACHE = {
    "basis": "cache_overlay",
    "sized_query_count": 20,
    "calls_per_second": 185.07,
    "read_calls_per_second": 185.07,
    "write_calls_per_second": 0.0,
    "sized_table_count": 12,
    "storage_gb": 0.05,
}


def _report(sizing: dict[str, Any] | None, *, stored: bool = True) -> dict[str, Any]:
    row: dict[str, Any] = {"database": "aurora_mysql", "monthly_cost_usd": 160.4}
    if sizing is not None:
        row["sizing"] = sizing
    report: dict[str, Any] = {
        "database_name": "shop",
        "recommended_architecture": {"architecture_type": "SINGLE_DATABASE", "databases": []},
        "ranking": [{"target": "aurora_mysql", "workload_percent": 100.0, "confidence_score": 80}],
        "risk_assessment": {"overall_risk_level": "LOW", "risks": [], "resolved_risks": []},
        "tco_analysis": {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 160.4,
            "savings_percent": 0,
            "cost_breakdown": [row],
        },
        "trade_offs": [],
        "schema_designs": {},
        "migration_waves": None,
        "table_mappings": [],
        "query_groups": [],
        "cache_overlay": None,
    }
    if stored:
        report["facts"] = compute_report_facts(report, waves=[])
    return report


def test_the_facts_state_each_engines_sizing_once() -> None:
    facts = compute_report_facts(_report(AURORA, stored=False), waves=[])
    [engine] = facts["engines"]
    assert engine["cost_sizing"] == AURORA
    assert ReportFacts.model_validate(facts).engines[0].cost_sizing is not None


def test_a_report_written_before_states_no_sizing() -> None:
    facts = compute_report_facts(_report(None, stored=False), waves=[])
    assert facts["engines"][0]["cost_sizing"] is None
    # A stored block from before #1195 has no such key; the deliverables still render.
    old = _report(None)
    for e in old["facts"]["engines"]:
        e.pop("cost_sizing", None)
    md = render_engineering_report_md(old)
    assert "| Aurora MySQL | Retained | $160.40 | - |" in md


def test_formatting() -> None:
    assert cost_sizing_text(AURORA) == "67 queries, 96.51 calls/s"
    assert cost_sizing_text(CACHE) == "20 cached reads, 185.07 calls/s, also billed on their owners"
    assert cost_sizing_text({**CACHE, "basis": "no_in_scope_queries"}) == "no in-scope queries"
    assert cost_sizing_text({**AURORA, "sized_query_count": 1, "calls_per_second": 1.0}) == (
        "1 query, 1 call/s"
    )
    assert cost_sizing_text({**CACHE, "sized_query_count": 0, "calls_per_second": 0.0}) == (
        "0 cached reads, 0 calls/s"
    )
    assert cost_sizing_text(None) == ""
    assert cost_sizing_text({}) == ""


def test_the_engineering_report_cost_table_states_it() -> None:
    md = render_engineering_report_md(_report(AURORA))
    assert "| Engine | Role | Est. monthly | Sized for |" in md
    assert "| Aurora MySQL | Retained | $160.40 | 67 queries, 96.51 calls/s |" in md


def test_the_pdf_cost_table_states_it() -> None:
    text = executive_text(_report(AURORA))
    assert "Sized for" in text
    assert "67 queries, 96.51 calls/s" in text


def test_a_real_old_report_renders_its_costs_with_no_sizing(tmp_path) -> None:
    """A report main's code wrote before #1195 (contract 1.41) still loads, validates and
    renders: its cost rows have no sizing, so every engine's "Sized for" cell is "-"."""
    import json
    from pathlib import Path

    from src.contracts.synthesis_output import SynthesisOutputContract
    from src.report import deliverables as dl
    from src.storage.local_store import LocalArtifactStore

    fixture = Path(__file__).resolve().parent / "fixtures" / "wordpress_1_41_flagged_report.json"
    old = json.loads(fixture.read_text())
    assert all("sizing" not in r for r in old["tco_analysis"]["cost_breakdown"])
    SynthesisOutputContract.model_validate(old)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    key = "wordpress/probe-old/synthesis/v2/report.json"
    store.write_json(key, old)
    out = {d.name: d for d in dl.render_deliverables(store, "probe-old", "wordpress", key).items}
    md = out["engineering-report"].content.decode("utf-8")
    assert "| Engine | Role | Est. monthly | Sized for |" in md
    cost = md.split("## Cost", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in cost.splitlines() if line.startswith("| ") and "$" in line]
    assert rows and all(line.endswith("| - |") or line.endswith("| |") for line in rows)
    assert "calls/s" not in cost


ASSUMPTION = (
    "Reads the cache layer fronts are also billed on their owner engines, as if every "
    "one missed the cache (conservative)"
)


def _with_assumptions(lines: list[str]) -> dict[str, Any]:
    report = _report(AURORA, stored=False)
    report["tco_analysis"]["assumptions"] = lines
    report["facts"] = compute_report_facts(report, waves=[])
    return report


def test_the_facts_state_the_tco_assumptions_once() -> None:
    report = _with_assumptions(["Region: us-east-1", ASSUMPTION])
    assert report["facts"]["cost_assumptions"] == ["Region: us-east-1", ASSUMPTION]


def test_every_report_states_the_cache_miss_assumption_from_the_facts() -> None:
    report = _with_assumptions(["Region: us-east-1", ASSUMPTION])
    # The renderers read the facts, not tco_analysis: a stale TCO list changes nothing.
    report["tco_analysis"]["assumptions"] = []
    md = render_engineering_report_md(report).replace("\\(", "(").replace("\\)", ")")
    assert f"Assumptions: Region: us-east-1; {ASSUMPTION}." in md
    assert ASSUMPTION in " ".join(executive_text(report).split())


def test_a_stored_block_from_before_gets_the_tco_assumptions() -> None:
    from src.report.facts import report_facts

    report = _with_assumptions(["Region: us-east-1"])
    report["facts"].pop("cost_assumptions")
    assert report_facts(report)["cost_assumptions"] == ["Region: us-east-1"]


def test_the_tables_no_query_reads_are_stated_with_the_engine_that_stores_them() -> None:
    """#1378: wave 1's Aurora engine also stores the tables no in-scope query reads."""
    assert cost_sizing_text({**AURORA, "unqueried_table_count": 28}) == (
        "67 queries, 96.51 calls/s, plus 28 tables no query reads"
    )
    assert cost_sizing_text({**AURORA, "unqueried_table_count": 1}) == (
        "67 queries, 96.51 calls/s, plus 1 table no query reads"
    )
    assert cost_sizing_text({**AURORA, "unqueried_table_count": 0}) == "67 queries, 96.51 calls/s"
