"""The PDF report and engineering report must show a known TCO baseline,
or say it is unknown, instead of a bare $0.00 / 0% (#380 review).

A retired engine's own analysed cost (``tco_analysis.retired_engine_savings``, #949:
an engine the source runs today) must also be traceable wherever the deliverable
states savings.
"""

from __future__ import annotations

from src.report.renderers import render_engineering_report_md
from tests.executive_pdf import executive_text

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


class TestExecutiveReportCostBaseline:
    def test_unknown_baseline_says_so_not_zero_dollars(self) -> None:
        tco = {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 121.06,
            "savings_percent": 0,
            "cost_breakdown": [],
        }
        text = executive_text(_report(tco))
        assert "Current monthly (source): source cost not provided" in text
        assert "Savings: not available" in text
        assert "Current monthly (source): $0.00" not in text
        assert "Savings: 0%" not in text

    def test_known_baseline_shows_the_real_figures(self) -> None:
        tco = {
            "current_monthly_cost": 500.0,
            "current_cost_known": True,
            "projected_monthly_cost": 121.06,
            "savings_percent": 75.8,
            "cost_breakdown": [],
        }
        text = executive_text(_report(tco))
        assert "$500.00" in text
        assert "75.8%" in text
        assert "source cost not provided" not in text

    def test_missing_current_cost_known_with_a_real_cost_shows_the_number(self) -> None:
        """A report written before current_cost_known existed, with a real
        nonzero current_monthly_cost, still shows its number -- that cost can't
        be the "no instance metadata" placeholder."""
        tco = {
            "current_monthly_cost": 500.0,
            "projected_monthly_cost": 121.06,
            "savings_percent": 75.8,
            "cost_breakdown": [],
        }
        text = executive_text(_report(tco))
        assert "$500.00" in text

    def test_missing_current_cost_known_with_a_zero_cost_is_unknown(self) -> None:
        """#334: a report written before current_cost_known existed (no field
        at all) whose current_monthly_cost is a literal 0 -- the real wordpress
        sample evidence shape -- must say the baseline is unknown, not "$0.00"/
        "0%": that 0 is overwhelmingly the "no RDS instance metadata"
        placeholder a missing current_cost_known can't distinguish from a real
        zero-cost database, so this must be treated the same as an explicit
        current_cost_known: False."""
        tco = {
            "current_monthly_cost": 0.0,
            "projected_monthly_cost": 823.72,
            "savings_percent": 0.0,
            "cost_breakdown": [],
        }
        text = executive_text(_report(tco))
        assert "Current monthly (source): source cost not provided" in text
        assert "Savings: not available" in text
        assert "Current monthly (source): $0.00" not in text
        assert "Savings: 0%" not in text

    def test_retired_engine_savings_are_traceable(self) -> None:
        tco = {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 136.26,
            "savings_percent": 0,
            "cost_breakdown": [],
            "retired_engine_savings": [
                {"database": "documentdb", "monthly_cost_usd": 271.80},
                {"database": "opensearch", "monthly_cost_usd": 240.96},
            ],
        }
        text = executive_text(_report(tco))
        assert "$271.80/mo" in text
        assert "$240.96/mo" in text

    def test_no_retired_engine_savings_adds_no_note(self) -> None:
        tco = {
            "current_monthly_cost": 100.0,
            "projected_monthly_cost": 50.0,
            "savings_percent": 50.0,
            "cost_breakdown": [],
        }
        text = executive_text(_report(tco))
        assert "Retired by this plan" not in text


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

    def test_retired_engine_savings_are_traceable(self) -> None:
        tco = {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 136.26,
            "savings_percent": 0,
            "cost_breakdown": [],
            "retired_engine_savings": [
                {"database": "documentdb", "monthly_cost_usd": 271.80},
            ],
        }
        md = render_engineering_report_md(_report(tco))
        assert "$271.80/mo" in md
