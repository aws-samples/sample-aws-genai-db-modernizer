"""The decision report and engineering report must show a known TCO baseline,
or say it is unknown, instead of a bare $0.00 / 0% (#380 review).

An eliminated engine's own analysed cost (``tco_analysis.eliminated_engine_costs``)
must also be traceable wherever the deliverable states savings.
"""

from __future__ import annotations

from src.report.renderers import render_decision_report_html, render_engineering_report_md

RANKING = [{"target": "aurora_mysql", "workload_percent": 100.0, "confidence_score": 80}]


def _report(tco: dict) -> dict:
    return {
        "database_name": "shop",
        "recommended_architecture": {"architecture_type": "SINGLE_DATABASE", "databases": []},
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


class TestDecisionReportCostBaseline:
    def test_unknown_baseline_says_so_not_zero_dollars(self) -> None:
        tco = {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 121.06,
            "savings_percent": 0,
            "cost_breakdown": [],
        }
        html = render_decision_report_html(_report(tco))
        assert "<p>Current monthly</p><h3>source cost not provided</h3>" in html
        assert "<p>Savings</p><h3>not available</h3>" in html
        assert "<p>Current monthly</p><h3>$0.00</h3>" not in html
        assert "<p>Savings</p><h3>0%</h3>" not in html

    def test_known_baseline_shows_the_real_figures(self) -> None:
        tco = {
            "current_monthly_cost": 500.0,
            "current_cost_known": True,
            "projected_monthly_cost": 121.06,
            "savings_percent": 75.8,
            "cost_breakdown": [],
        }
        html = render_decision_report_html(_report(tco))
        assert "$500.00" in html
        assert "75.8%" in html
        assert "source cost not provided" not in html

    def test_missing_current_cost_known_defaults_to_the_old_behaviour(self) -> None:
        """A report written before current_cost_known existed still shows its
        number -- the field defaults to known, same as the contract's own
        default, so nothing changes for an old report."""
        tco = {
            "current_monthly_cost": 500.0,
            "projected_monthly_cost": 121.06,
            "savings_percent": 75.8,
            "cost_breakdown": [],
        }
        html = render_decision_report_html(_report(tco))
        assert "$500.00" in html

    def test_eliminated_engine_costs_are_traceable(self) -> None:
        tco = {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 136.26,
            "savings_percent": 0,
            "cost_breakdown": [],
            "eliminated_engine_costs": [
                {"database": "documentdb", "monthly_cost_usd": 271.80},
                {"database": "opensearch", "monthly_cost_usd": 240.96},
            ],
        }
        html = render_decision_report_html(_report(tco))
        assert "$271.80/mo" in html
        assert "$240.96/mo" in html

    def test_no_eliminated_engine_costs_adds_no_note(self) -> None:
        tco = {
            "current_monthly_cost": 100.0,
            "projected_monthly_cost": 50.0,
            "savings_percent": 50.0,
            "cost_breakdown": [],
        }
        html = render_decision_report_html(_report(tco))
        assert "Already removed from this comparison" not in html


class TestEngineeringReportCostBaseline:
    def test_unknown_baseline_says_so_not_zero_dollars(self) -> None:
        tco = {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 121.06,
            "savings_percent": 0,
            "cost_breakdown": [],
        }
        md = render_engineering_report_md(_report(tco))
        assert "source cost not provided" in md
        assert "not available" in md

    def test_known_baseline_shows_the_real_figures(self) -> None:
        tco = {
            "current_monthly_cost": 500.0,
            "current_cost_known": True,
            "projected_monthly_cost": 121.06,
            "savings_percent": 75.8,
            "cost_breakdown": [],
        }
        md = render_engineering_report_md(_report(tco))
        assert "$500.00" in md
        assert "75.8%" in md

    def test_eliminated_engine_costs_are_traceable(self) -> None:
        tco = {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 136.26,
            "savings_percent": 0,
            "cost_breakdown": [],
            "eliminated_engine_costs": [
                {"database": "documentdb", "monthly_cost_usd": 271.80},
            ],
        }
        md = render_engineering_report_md(_report(tco))
        assert "$271.80/mo" in md


class TestDecisionReportTileTextOrder:
    """A text reader (the quality judge, a screen reader) must see each tile's
    label before its value, or "source cost not provided" reads as the label of
    the next tile's figure (Discourse validation run on 7aa6ec1)."""

    def test_each_tile_label_comes_right_before_its_value(self) -> None:
        from ci.llm.judge import html_to_text

        tco = {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 301.36,
            "savings_percent": 0,
            "cost_breakdown": [],
        }
        lines = html_to_text(render_decision_report_html(_report(tco))).splitlines()
        pairs = {lines[i]: lines[i + 1] for i in range(len(lines) - 1)}
        assert pairs["Current monthly"] == "source cost not provided"
        assert pairs["Projected monthly"] == "$301.36"
        assert pairs["Savings"] == "not available"
        assert pairs["Engines"] == "1"
