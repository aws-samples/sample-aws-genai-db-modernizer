"""Every engine the reality check removed is accounted for (#547, #987).

The retired decision report listed them under "Engines not used". The
engineering report and the PDF report state the same decisions as the reality
check's consolidation lines under "Migration trade-offs", which the "Retired by
this plan: ... (see Migration trade-offs below for why)" note points at. The
pointer once led nowhere on the wordpress sample (DocumentDB's two consolidation
lines carry no "no in-scope X query remains" marker); found by the
deliverable-figures e2e test (#573).
"""

from __future__ import annotations

from typing import Any

from src.report.facts import report_facts
from src.report.renderers import render_engineering_report_md
from tests.executive_pdf import engineering_text


def _report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "wordpress",
        "ranking": [
            {"target": "aurora_mysql", "assigned_queries": 63, "workload_percent": 58.9},
            {"target": "dynamodb", "assigned_queries": 44, "workload_percent": 41.1},
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 0}]},
        "schema_designs": {"dynamodb": {"status": "not_available"}},
        "tco_analysis": {
            "projected_monthly_cost": 417.21,
            "cost_breakdown": [
                {"database": "aurora_mysql", "monthly_cost_usd": 318.8},
                {"database": "dynamodb", "monthly_cost_usd": 98.41},
            ],
            "retired_engine_savings": [{"database": "documentdb", "monthly_cost_usd": 367.39}],
        },
        "trade_offs": [
            {
                "description": "Consolidated 3 queries from documentdb → aurora_mysql: documentdb "
                "provides no unique capabilities — 3 queries can be served by aurora_mysql. "
                "Removes $367.39/mo of infrastructure cost."
            },
            {
                "description": "Consolidated 5 queries from documentdb → dynamodb: documentdb "
                "provides no unique capabilities — 5 queries can be served by dynamodb. "
                "documentdb leaves the architecture."
            },
        ],
        "reality_check": {
            "consolidations": [
                {"from_engine": "documentdb", "to_engine": "aurora_mysql", "action": "full"},
                {"from_engine": "documentdb", "to_engine": "dynamodb", "action": "full"},
            ],
            "before_distribution": {"aurora_mysql": 60, "dynamodb": 39, "documentdb": 8},
            "after_distribution": {"aurora_mysql": 63, "dynamodb": 44},
        },
    }
    report.update(overrides)
    return report


def _trade_offs(md: str) -> str:
    start = md.index("## Migration trade-offs")
    end = md.find("\n## ", start + 1)
    return md[start:] if end == -1 else md[start:end]


def test_the_pointer_leads_to_the_removed_engines_consolidation_lines() -> None:
    md = render_engineering_report_md(_report())
    assert "see Migration trade-offs below for why" in md
    section = _trade_offs(md)
    assert "Consolidated 3 queries from documentdb → aurora\\_mysql" in section
    assert "documentdb leaves the architecture." in section
    text = engineering_text(_report())
    assert "see Migration trade-offs below for why" in text
    assert "documentdb leaves the architecture." in text


def test_the_removed_engines_are_the_facts_layers() -> None:
    removed = report_facts(_report())["removed_engines"]
    assert [e["engine"] for e in removed] == ["documentdb"]
    assert removed[0]["moved_to"] == ["aurora_mysql", "dynamodb"]


def test_engine_still_in_the_architecture_is_not_removed() -> None:
    # A partial consolidation: OpenSearch keeps some queries, so it was used.
    report = _report()
    report["trade_offs"].append(
        {"description": "Consolidated 2 queries from opensearch → aurora_mysql: kept 4."}
    )
    report["reality_check"]["consolidations"].append(
        {"from_engine": "opensearch", "to_engine": "aurora_mysql", "action": "partial"}
    )
    report["reality_check"]["before_distribution"]["opensearch"] = 6
    report["reality_check"]["after_distribution"]["opensearch"] = 4
    assert [e["engine"] for e in report_facts(report)["removed_engines"]] == ["documentdb"]


def test_no_dropped_engine_no_pointer() -> None:
    report = _report(trade_offs=[])
    report["tco_analysis"]["retired_engine_savings"] = None
    report["reality_check"] = {
        "consolidations": [],
        "before_distribution": {"aurora_mysql": 63, "dynamodb": 44},
        "after_distribution": {"aurora_mysql": 63, "dynamodb": 44},
    }
    assert report_facts(report)["removed_engines"] == []
    md = render_engineering_report_md(report)
    assert "Retired by this plan" not in md
    assert "## Migration trade-offs" not in md


def test_report_without_distributions_still_uses_the_marker() -> None:
    report = _report(reality_check={})
    report["tco_analysis"]["retired_engine_savings"] = None
    report["trade_offs"] = [
        {
            "description": "Consolidated 4 queries from opensearch → aurora_mysql: 4 opensearch "
            "queries moved to aurora_mysql; no in-scope opensearch query remains."
        },
        {"description": "Consolidated 5 queries from documentdb → dynamodb: kept 3."},
    ]
    assert [e["engine"] for e in report_facts(report)["removed_engines"]] == ["opensearch"]


def test_display_name_reasons_map_back_to_engine_ids():
    """Reasons name engines by display name since #572; each engine is listed once, by id."""
    report = {
        "reality_check": {
            "before_distribution": {"documentdb": 8, "aurora_mysql": 60},
            "after_distribution": {"aurora_mysql": 68},
        },
        "trade_offs": [
            {
                "description": "Consolidated 8 queries from DocumentDB → Aurora MySQL: no in-scope DocumentDB query remains."
            }
        ],
    }
    assert [e["engine"] for e in report_facts(report)["removed_engines"]] == ["documentdb"]
