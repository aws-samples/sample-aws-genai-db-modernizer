"""An engine the customer runs today and the plan retires has its own statement (#1076).

``metadata.engines_in_use`` (#963) says which engines the application already runs. When the
plan drops one of them, every deliverable states it once as a retirement, with its saving
when one is counted and the consolidation reason, and never lists it under "Other targets
evaluated" or as "considered, not recommended". The split is computed once in
``report.json``'s ``facts.retired_in_use_engines`` (ADR-032); renderers only format it.
"""

from __future__ import annotations

from typing import Any

from src.contracts.synthesis_output import ReportFacts, SynthesisOutputContract
from src.report.facts import report_facts
from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import retired_in_use_engine_facts
from tests.executive_pdf import engineering_text, executive_text

DOCDB = "DocumentDB provides no unique capabilities over Aurora MySQL"
WHY = "no queries justified it before the move"
OS = "OpenSearch provides no unique capabilities over Aurora MySQL"


def _report(**extra: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "shop",
        "engines_in_use": ["mysql", "documentdb", "opensearch"],
        "ranking": [{"target": "aurora_mysql", "assigned_queries": 10, "workload_percent": 100}],
        "recommended_architecture": {"databases": []},
        "schema_designs": {},
        "tco_analysis": {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 121.06,
            "savings_percent": 0,
            "cost_breakdown": [{"database": "aurora_mysql", "monthly_cost_usd": 121.06}],
            "retired_engine_savings": [{"database": "documentdb", "monthly_cost_usd": 367.39}],
        },
        "considered_not_recommended": [],
        "reality_check": {
            "consolidations": [
                {"from_engine": "documentdb", "to_engine": "aurora_mysql", "reason": DOCDB},
                {"from_engine": "opensearch", "to_engine": "aurora_mysql", "reason": OS},
            ],
            "before_distribution": {"aurora_mysql": 5, "documentdb": 4, "opensearch": 1},
            "after_distribution": {"aurora_mysql": 10},
        },
    }
    report.update(extra)
    return report


BOTH = [
    {"engine": "documentdb", "moved_to": []},
    {"engine": "opensearch", "moved_to": []},
]


class TestTheFactsRows:
    def test_a_saving_row_and_a_no_saving_row(self) -> None:
        rows = retired_in_use_engine_facts(_report(), BOTH)
        assert rows == [
            {
                "engine": "documentdb",
                "monthly_cost_usd": 367.39,
                "moved_to": [],
                "reason": DOCDB,
                "no_saving_reason": None,
            },
            {
                "engine": "opensearch",
                "monthly_cost_usd": None,
                "moved_to": [],
                "reason": OS,
                "no_saving_reason": WHY,
            },
        ]

    def test_moved_to_comes_from_the_removed_engines(self) -> None:
        removed = [{"engine": "documentdb", "moved_to": ["aurora_mysql", "dynamodb"]}]
        rows = retired_in_use_engine_facts(_report(), removed)
        assert [r["engine"] for r in rows] == ["documentdb"]
        assert rows[0]["moved_to"] == ["aurora_mysql", "dynamodb"]

    def test_a_dropped_candidate_the_source_never_ran_is_not_a_row(self) -> None:
        report = _report(engines_in_use=["mysql", "documentdb"])
        assert [r["engine"] for r in retired_in_use_engine_facts(report, BOTH)] == ["documentdb"]

    def test_a_declared_engine_still_in_the_plan_is_not_a_row(self) -> None:
        report = _report(engines_in_use=["mysql", "aurora_mysql"])
        report["tco_analysis"].pop("retired_engine_savings")
        assert retired_in_use_engine_facts(report, [{"engine": "dynamodb"}]) == []

    def test_a_report_before_the_field_reads_its_saving_rows_as_running_today(self) -> None:
        report = _report()
        del report["engines_in_use"]
        assert [r["engine"] for r in retired_in_use_engine_facts(report, [])] == ["documentdb"]

    def test_no_declared_engine_no_rows(self) -> None:
        report = _report(engines_in_use=["mysql"])
        report["tco_analysis"].pop("retired_engine_savings")
        assert retired_in_use_engine_facts(report, BOTH) == []

    def test_facts_block_carries_the_rows_and_a_stored_block_without_them_is_filled(self) -> None:
        facts = report_facts(_report())
        names = [r["engine"] for r in facts["retired_in_use_engines"]]
        assert names == ["documentdb", "opensearch"]
        stored = {k: v for k, v in facts.items() if k != "retired_in_use_engines"}
        again = report_facts(_report(facts=stored))
        assert [r["engine"] for r in again["retired_in_use_engines"]] == names


class TestADeclaredEngineRoutingNeverUsed:
    def test_it_is_retired_with_no_saving(self) -> None:
        report = _report(
            engines_in_use=["mysql", "elasticache", "opensearch"],
            ranking=[
                {"target": "aurora_mysql", "assigned_queries": 10, "workload_percent": 100},
                {"target": "opensearch", "assigned_queries": 0, "workload_percent": 0},
                {"target": "elasticache", "assigned_queries": 0, "workload_percent": 0},
            ],
        )
        report["tco_analysis"].pop("retired_engine_savings")
        rows = retired_in_use_engine_facts(report, [])
        # The cache layer owns no queries by design, so it is never "idle".
        assert [(r["engine"], r["monthly_cost_usd"]) for r in rows] == [("opensearch", None)]
        assert rows[0]["no_saving_reason"] == WHY

    def test_an_idle_engine_the_source_never_ran_is_not_a_row(self) -> None:
        report = _report(
            engines_in_use=["mysql"],
            ranking=[{"target": "opensearch", "assigned_queries": 0}],
        )
        report["tco_analysis"].pop("retired_engine_savings")
        assert retired_in_use_engine_facts(report, []) == []


class TestEveryDeliverableStatesIt:
    def test_engineering_report(self) -> None:
        text = engineering_text(_report())
        assert "Retired by this plan: DocumentDB (runs today), $367.39/mo." in text
        assert f"{DOCDB}." in text
        assert f"Retired by this plan: OpenSearch (runs today), no saving counted ({WHY})." in text
        assert f"{OS}." in text

    def test_engineering_markdown_escapes(self) -> None:
        md = render_engineering_report_md(_report())
        assert r"Retired by this plan: DocumentDB \(runs today\), $367.39/mo." in md

    def test_pdf(self) -> None:
        text = executive_text(_report())
        assert "Retired by this plan: DocumentDB (runs today), $367.39/mo" in text
        assert f"OpenSearch (runs today), no saving counted ({WHY})" in text

    def test_the_pdf_reasons_paragraph_hides_table_names(self) -> None:
        report = _report()
        report["reality_check"]["consolidations"][0][
            "reason"
        ] = "DocumentDB holds nothing the wp_postmeta table needs"
        report["known_tables"] = ["wp_postmeta"]
        text = executive_text(report)
        assert "DocumentDB:" in text
        assert "wp_postmeta" not in text

    def test_a_report_with_none_prints_no_line(self) -> None:
        report = _report(engines_in_use=["mysql"])
        report["tco_analysis"].pop("retired_engine_savings")
        assert "Retired by this plan" not in engineering_text(report)
        assert "Retired by this plan" not in executive_text(report)


class TestTheContract:
    def test_the_new_fields_default_to_absent(self) -> None:
        assert SynthesisOutputContract.model_fields["engines_in_use"].default is None
        assert ReportFacts.model_fields["retired_in_use_engines"].default_factory is list
