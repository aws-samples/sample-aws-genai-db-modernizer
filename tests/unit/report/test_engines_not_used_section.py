"""The decision report's "Engines not used" section lists every dropped engine (#547).

The "Already removed from this comparison: ... see Engines not used below"
note pointed at a section that was only rendered when a trade-off line
carried the "no in-scope X query remains" marker. On the wordpress sample
DocumentDB's two consolidation lines carry no marker, so the pointer led
nowhere. Found by the deliverable-figures e2e test (#573).
"""

from __future__ import annotations

import re
from typing import Any

from src.report.renderers import render_decision_report_html


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
            "eliminated_engine_costs": [{"database": "documentdb", "monthly_cost_usd": 367.39}],
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


def _section(html: str) -> str | None:
    m = re.search(r"<div class=card data-figure=engines-not-used>.*?</ul></div></div>", html, re.S)
    return m.group(0) if m else None


def test_pointer_has_a_section_without_the_elimination_marker() -> None:
    html = render_decision_report_html(_report())
    assert "see Engines not used below" in html
    section = _section(html)
    assert section is not None
    assert re.findall(r'<li data-engine="([^"]+)"', section) == ["documentdb"]
    assert "<b>DocumentDB</b> → Aurora MySQL, DynamoDB:" in section
    assert "documentdb leaves the architecture." in section


def test_engine_still_in_the_architecture_is_not_listed() -> None:
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
    section = _section(render_decision_report_html(report))
    assert section is not None
    assert re.findall(r'<li data-engine="([^"]+)"', section) == ["documentdb"]


def test_no_dropped_engine_no_section() -> None:
    report = _report(trade_offs=[])
    report["tco_analysis"]["eliminated_engine_costs"] = None
    report["reality_check"] = {
        "consolidations": [],
        "before_distribution": {"aurora_mysql": 63, "dynamodb": 44},
        "after_distribution": {"aurora_mysql": 63, "dynamodb": 44},
    }
    html = render_decision_report_html(report)
    assert _section(html) is None
    assert "Engines not used" not in html


def test_report_without_distributions_still_uses_the_marker() -> None:
    report = _report(reality_check={})
    report["tco_analysis"]["eliminated_engine_costs"] = None
    report["trade_offs"] = [
        {
            "description": "Consolidated 4 queries from opensearch → aurora_mysql: 4 opensearch "
            "queries moved to aurora_mysql; no in-scope opensearch query remains."
        },
        {"description": "Consolidated 5 queries from documentdb → dynamodb: kept 3."},
    ]
    section = _section(render_decision_report_html(report))
    assert section is not None
    assert re.findall(r'<li data-engine="([^"]+)"', section) == ["opensearch"]


def test_display_name_reasons_map_back_to_engine_ids():
    """Reasons name engines by display name since #572; the section still lists each engine once, by id."""
    from src.report.engine_choices import _dropped_engines

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
    assert _dropped_engines(report) == ["documentdb"]
