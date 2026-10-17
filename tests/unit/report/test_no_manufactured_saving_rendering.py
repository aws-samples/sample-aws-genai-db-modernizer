"""The deliverables state a retirement saving only for an engine the source runs today (#949).

``tco_analysis.retired_engine_savings`` (synthesis contract 1.25) lists the engines the
source runs today that the plan retires; the PDF and engineering reports print
those rows and nothing else. ``tco_analysis.eliminated_engine_costs``, which a report
before 1.25 filled with every dropped candidate engine, is never read as a saving: such a
report still loads, and its candidates read back as ``considered_not_recommended``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.contracts.synthesis_output import SynthesisOutputContract
from src.report.renderers import render_engineering_report_md
from src.shared.engines_in_use import considered_not_recommended, retired_engine_savings
from tests.executive_pdf import engineering_text

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_no_schema_design_report.json"


def _report(tco: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "database_name": "shop",
        "ranking": [{"target": "aurora_mysql", "assigned_queries": 10, "workload_percent": 100}],
        "recommended_architecture": {"databases": []},
        "schema_designs": {},
        "tco_analysis": {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 121.06,
            "savings_percent": 0,
            "cost_breakdown": [{"database": "aurora_mysql", "monthly_cost_usd": 121.06}],
            **tco,
        },
        **extra,
    }


class TestRetiredEngineSavingIsShown:
    def test_md_report(self) -> None:
        text = engineering_text(
            _report(
                {"retired_engine_savings": [{"database": "elasticache", "monthly_cost_usd": 88.5}]}
            )
        )
        assert "Retired by this plan: $88.50/mo (ElastiCache)" in text

    def test_engineering_report(self) -> None:
        md = render_engineering_report_md(
            _report(
                {"retired_engine_savings": [{"database": "elasticache", "monthly_cost_usd": 88.5}]}
            )
        )
        assert r"Retired by this plan: $88.50/mo \(ElastiCache\)" in md


class TestALegacyCandidateCostIsNeverASaving:
    def _legacy(self) -> dict[str, Any]:
        return _report(
            {"eliminated_engine_costs": [{"database": "documentdb", "monthly_cost_usd": 367.39}]}
        )

    def test_md_report_shows_no_retirement_line(self) -> None:
        text = engineering_text(self._legacy())
        assert "$367.39" not in text
        assert "Already removed from this comparison" not in text
        assert "Retired by this plan" not in text

    def test_engineering_report_shows_no_retirement_line(self) -> None:
        md = render_engineering_report_md(self._legacy())
        assert "$367.39" not in md
        assert "Already removed from this comparison" not in md
        assert "Retired by this plan" not in md

    def test_readers(self) -> None:
        legacy = self._legacy()
        assert retired_engine_savings(legacy) == []
        assert considered_not_recommended(legacy) == [{"engine": "documentdb", "absorbed_by": None}]

    def test_the_new_field_wins_over_the_legacy_one(self) -> None:
        report = _report(
            {"eliminated_engine_costs": [{"database": "documentdb", "monthly_cost_usd": 367.39}]},
            considered_not_recommended=[{"engine": "opensearch", "absorbed_by": "aurora_mysql"}],
        )
        assert considered_not_recommended(report) == [
            {"engine": "opensearch", "absorbed_by": "aurora_mysql"}
        ]

    def test_nothing_considered(self) -> None:
        assert considered_not_recommended(_report({})) == []
        assert retired_engine_savings({}) == []


class TestOldReportsStillLoad:
    def test_a_1_7_report_with_eliminated_engine_costs_validates(self) -> None:
        raw = json.loads(FIXTURE.read_text())
        assert raw["contract_version"] == "1.7"
        assert "considered_not_recommended" not in raw
        assert "retired_engine_savings" not in raw["tco_analysis"]
        report = SynthesisOutputContract.model_validate(raw)
        assert report.considered_not_recommended is None
        assert report.tco_analysis.retired_engine_savings is None
        dumped = report.model_dump(mode="json")
        assert considered_not_recommended(dumped) == [
            {"engine": "documentdb", "absorbed_by": None},
            {"engine": "opensearch", "absorbed_by": None},
        ]
        assert retired_engine_savings(dumped) == []
        for text in (engineering_text(dumped), render_engineering_report_md(dumped)):
            assert "Already removed from this comparison" not in text
            assert "Retired by this plan" not in text
