# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A trade-off with nothing of its own is never a decision (#2324).

When the prose seam rejects a model trade-off that names no table, its fallback reads
"Aurora MySQL design trade-off." with "Review this trade-off with the Aurora MySQL
schema design before migration.": a placeholder, not a decision. Synthesis drops it,
whether the fallback wrote it or a model did, and a report saved with one reads
without it in every deliverable and the results page.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.agents.referee.synthesis_prose import clean_schema_design_prose
from src.shared.design_text import design_prose_report, is_placeholder_trade_off
from src.shared.engine_names import canonical_engine_id

REPO = Path(__file__).resolve().parents[3]
OLD_REPORT = REPO / "tests" / "fixtures" / "real_runs" / "wordpress-both" / "report.json"

PLACEHOLDER = {
    "description": "Aurora MySQL design trade-off.",
    "impact": "Review this trade-off with the Aurora MySQL schema design before migration.",
    "source_tables": [],
    "target_tables": [],
    "query_ids": [],
    "engine": "aurora_mysql",
}


def _t(description: str, engine: str = "aurora_mysql", impact: str = "Impact.") -> dict:
    return {"description": description, "impact": impact, "engine": engine}


class TestThePredicate:
    def test_the_fallback_pair_is_a_placeholder(self) -> None:
        assert is_placeholder_trade_off(PLACEHOLDER)
        assert is_placeholder_trade_off("DynamoDB design trade-off.")
        assert is_placeholder_trade_off({**PLACEHOLDER, "impact": ""})

    @pytest.mark.parametrize(
        "entry",
        [
            {**PLACEHOLDER, "description": "Aurora MySQL design trade-off for wp_posts."},
            {**PLACEHOLDER, "description": "Aurora MySQL design trade-off: a maps to b."},
            # The model's own impact is content: the entry stays.
            {**PLACEHOLDER, "impact": "Reads go to the replica."},
            {**PLACEHOLDER, "description": "Customer design trade-off."},  # not an engine
            "Composite indexes on wp_postmeta.",
        ],
    )
    def test_an_entry_with_content_is_not(self, entry: Any) -> None:
        assert not is_placeholder_trade_off(entry)


class TestSynthesisDropsIt:
    def test_a_rejected_trade_off_naming_no_table_is_dropped(self) -> None:
        schema = {
            "trade_offs": [
                {"description": "A robust merge.", "impact": "Moreover, fine."},
                {"description": "Reads go to the replica.", "impact": "Lag is seconds."},
            ]
        }
        cleaned, rejections = clean_schema_design_prose("aurora_mysql", schema)
        assert cleaned is not None
        assert [t["description"] for t in cleaned["trade_offs"]] == ["Reads go to the replica."]
        assert rejections  # the rejection is still logged

    @pytest.mark.passes_on_main  # existing behaviour, guarded
    def test_a_rejected_trade_off_naming_a_table_stays(self) -> None:
        schema = {
            "trade_offs": [
                {"description": "A robust merge.", "impact": "fine", "source_tables": ["users"]}
            ]
        }
        cleaned, _ = clean_schema_design_prose("dynamodb", schema)
        assert cleaned is not None
        assert cleaned["trade_offs"][0]["description"] == "DynamoDB design trade-off for users."

    def test_a_model_that_writes_the_placeholder_itself_is_dropped_too(self) -> None:
        cleaned, rejections = clean_schema_design_prose(
            "aurora_mysql", {"trade_offs": [dict(PLACEHOLDER), _t("Reads go to the replica.")]}
        )
        assert cleaned is not None and rejections == []
        assert [t["description"] for t in cleaned["trade_offs"]] == ["Reads go to the replica."]

    def test_the_collected_list_and_its_risk_never_count_it(self) -> None:
        from src.agents.referee.synthesis_handler import _collect_trade_offs

        schema, _ = clean_schema_design_prose(
            "aurora_mysql", {"trade_offs": [dict(PLACEHOLDER), _t("Reads go to the replica.")]}
        )
        data = SimpleNamespace(engines={"aurora_mysql": SimpleNamespace(schema_design=schema)})
        assert [t["description"] for t in _collect_trade_offs(data)] == ["Reads go to the replica."]


class TestASavedReportReadsWithoutIt:
    def _report(self) -> dict[str, Any]:
        return {
            "trade_offs": [
                _t("Reads go to the replica."),
                dict(PLACEHOLDER),
                {"description": "Kept the cache.", "impact": "x", "engine": "reality-check"},
                _t("Zero dates are carried over."),
            ],
            "schema_designs": {
                "aurora_mysql": {"trade_offs": [_t("Reads go to the replica."), PLACEHOLDER]}
            },
            "risk_assessment": {
                "risks": [
                    {
                        "rule": "design.trade_offs",
                        "description": "[aurora_mysql] The Aurora MySQL schema design "
                        "records 3 trade-offs to review.",
                        "details": [
                            "Aurora MySQL design trade-off",
                            "Reads go to the replica",
                            "Zero dates are carried over",
                        ],
                    }
                ]
            },
            "facts": {
                "trade_off_count": 4,
                "results_tab_counts": {"queries": 9, "risks": 5},
                "reality_check_trade_offs": [{"index": 2, "reversed": False}],
                "reality_check_decisions": [{"index": 2, "title": "Kept the cache."}],
                "trade_off_topics": [
                    {"engine": "aurora_mysql", "topic": "other", "trade_off_count": 3,
                     "indexes": [0, 1, 3]},
                    {"engine": "dynamodb", "topic": "other", "trade_off_count": 1,
                     "indexes": [1]},
                ],
            },
        }  # fmt: skip

    def test_the_placeholder_is_gone_and_every_position_follows(self) -> None:
        report = self._report()
        before = copy.deepcopy(report)
        out = design_prose_report(report)
        assert report == before  # the input is not changed
        assert [t["description"] for t in out["trade_offs"]] == [
            "Reads go to the replica.",
            "Kept the cache.",
            "Zero dates are carried over.",
        ]
        assert out["schema_designs"]["aurora_mysql"]["trade_offs"] == [
            _t("Reads go to the replica.")
        ]
        assert out["risk_assessment"]["risks"][0]["details"] == [
            "Reads go to the replica",
            "Zero dates are carried over",
        ]
        facts = out["facts"]
        assert facts["trade_off_count"] == 3
        assert facts["results_tab_counts"] == {"queries": 9, "risks": 4}
        assert facts["reality_check_trade_offs"] == [{"index": 1, "reversed": False}]
        assert facts["reality_check_decisions"] == [{"index": 1, "title": "Kept the cache."}]
        assert facts["trade_off_topics"] == [
            {"engine": "aurora_mysql", "topic": "other", "trade_off_count": 2, "indexes": [0, 2]}
        ]
        assert design_prose_report(out) is out  # nothing left to change

    @pytest.mark.passes_on_main  # existing behaviour, guarded
    def test_a_report_with_no_placeholder_is_the_same_object(self) -> None:
        report = {"trade_offs": [_t("Reads go to the replica.")]}
        assert design_prose_report(report) is report

    def test_the_old_wordpress_report_loads_without_it(self) -> None:
        """A real report saved before #2324 carries the placeholder (Aurora MySQL tab)."""
        report = json.loads(OLD_REPORT.read_text())
        assert any(is_placeholder_trade_off(t) for t in report["trade_offs"])
        out = design_prose_report(report)
        texts = json.dumps(
            {k: out.get(k) for k in ("trade_offs", "schema_designs", "risk_assessment")}
        )
        assert "Aurora MySQL design trade-off." not in texts
        assert '"Aurora MySQL design trade-off"' not in texts
        assert "Review this trade-off with the" not in texts
        # Every stored position still points at the same trade-off.
        rows = out["trade_offs"]
        for rc in out["facts"]["reality_check_trade_offs"]:
            assert rows[rc["index"]]["engine"] == "reality-check"
        old_rc = [
            report["trade_offs"][t["index"]] for t in report["facts"]["reality_check_trade_offs"]
        ]
        assert [rows[t["index"]] for t in out["facts"]["reality_check_trade_offs"]] == old_rc

    def test_the_old_report_s_engineering_report_never_prints_it(self) -> None:
        from src.report.engineering_report import render_engineering_report_md

        report = design_prose_report(json.loads(OLD_REPORT.read_text()))
        md = render_engineering_report_md(report)
        assert "Aurora MySQL design trade-off." not in md
        assert "Review this trade-off with the Aurora MySQL schema design" not in md


class TestTheTradeOffRiskFollowsTheList:
    """The risk that counts an engine's trade-offs says the count left after the drop,
    worded by the same function synthesis uses."""

    def test_the_wordpress_risk_says_nine_in_every_deliverable(self) -> None:
        from src.report.engineering_report import render_engineering_report_md

        report = json.loads(OLD_REPORT.read_text())
        out = design_prose_report(report)
        (risk,) = [
            r
            for r in out["risk_assessment"]["risks"]
            if r.get("rule") == "design.trade_offs" and "Aurora MySQL" in r["description"]
        ]
        assert risk["description"] == (
            "[aurora_mysql] The Aurora MySQL schema design records 9 trade-offs to review, "
            "listed under Migration trade-offs in the engineering report."
        )
        assert len(risk["details"]) == 9
        md = render_engineering_report_md(out)
        assert "records 9 trade-offs" in md and "records 10 trade-offs" not in md
        old = report["facts"]["trade_off_count"]
        assert out["facts"]["trade_off_count"] == old - 1

    def test_synthesis_and_the_reader_word_it_with_one_function(self) -> None:
        from src.agents.referee import migration_risks
        from src.shared import design_text

        assert migration_risks.trade_off_risk_text is design_text.trade_off_risk_text
        assert design_text.trade_off_risk_text("aurora_mysql", 1, 2) == (
            "The Aurora MySQL schema design records 1 trade-off to review, listed under "
            "Migration trade-offs in the engineering report.",
            "Review this trade-off with the team that builds Aurora MySQL before Wave 2 "
            "starts, and test each one the application depends on.",
        )

    def test_a_risk_whose_only_trade_off_was_the_placeholder_is_gone(self) -> None:
        report: dict[str, Any] = {
            "trade_offs": [dict(PLACEHOLDER)],
            "risk_assessment": {
                "risks": [
                    {
                        "risk_id": "RISK-001",
                        "severity": "MEDIUM",
                        "rule": "design.trade_offs",
                        "wave": 1,
                        "description": "[aurora_mysql] The Aurora MySQL schema design records "
                        "1 trade-off to review, listed under Migration trade-offs in the "
                        "engineering report.",
                        "details": ["Aurora MySQL design trade-off"],
                    }
                ]
            },
            "facts": {"trade_off_count": 1, "open_risk_count": 1},
        }
        out = design_prose_report(report)
        assert out["trade_offs"] == [] and out["risk_assessment"]["risks"] == []
        assert out["facts"]["open_risk_count"] == 0


def _aurora_only_placeholder() -> dict[str, Any]:
    """The wordpress report with Aurora MySQL left with only its placeholder trade-off,
    so the reader removes its trade-off risk (RISK-006)."""
    report: dict[str, Any] = json.loads(OLD_REPORT.read_text())
    report["trade_offs"] = [
        t
        for t in report["trade_offs"]
        if canonical_engine_id(str(t.get("engine") or "")) != "aurora_mysql"
        or is_placeholder_trade_off(t)
    ]
    return report


class TestARemovedRiskIsNamedNowhere:
    """Review of #2404: the gate lines and strategies stop naming a removed risk."""

    def test_the_wordpress_aurora_risk_is_gone_from_every_deliverable(self) -> None:
        from src.report.deliverables import deliverable_report
        from src.report.engineering_report import render_engineering_report_md

        raw = _aurora_only_placeholder()
        assert "RISK-006" in json.dumps(raw["risk_assessment"]["mitigation_strategies"])
        out = deliverable_report(raw)
        ids = [r.get("risk_id") for r in out["risk_assessment"]["risks"]]
        assert "RISK-006" not in ids
        # What the executive report and the UI read, and the engineering report itself.
        for key in ("risk_assessment", "migration_waves", "summary", "summary_deterministic"):
            assert "RISK-006" not in json.dumps(out.get(key)), key
        assert "RISK-006" not in render_engineering_report_md(out)
        assert out["facts"]["open_risk_count"] == len(ids)


class TestTradeOffsAreGroupedByEngineId:
    """Review of #2404: a model can tag a trade-off "Aurora MySQL"; the tab, the topics
    and the risk count group it with the catalog id."""

    def test_the_wordpress_tab_lists_what_its_risk_counts(self) -> None:
        out = design_prose_report(json.loads(OLD_REPORT.read_text()))
        assert all(t.get("engine") != "Aurora MySQL" for t in out["trade_offs"])
        aurora = [t for t in out["trade_offs"] if t.get("engine") == "aurora_mysql"]
        assert len(aurora) == 9
        from src.report.facts import report_facts

        topics = report_facts(out)["trade_off_topics"]  # what the UI and reports read
        groups = [g for g in topics if g["engine"] == "aurora_mysql"]
        assert sum(g["trade_off_count"] for g in groups) == 9
        assert not [g for g in topics if g["engine"] == "Aurora MySQL"]
        rows = out["trade_offs"]
        assert all(rows[i]["engine"] == "aurora_mysql" for g in groups for i in g["indexes"])

    def test_synthesis_tags_a_trade_off_with_the_engine_id(self) -> None:
        from src.agents.referee.synthesis_handler import _collect_trade_offs

        schema = {"trade_offs": [_t("Reads go to the replica.", engine="Aurora MySQL")]}
        data = SimpleNamespace(engines={"aurora_mysql": SimpleNamespace(schema_design=schema)})
        assert [t["engine"] for t in _collect_trade_offs(data)] == ["aurora_mysql"]


def test_a_kept_trade_off_worded_like_the_placeholder_keeps_its_risk() -> None:
    """Review of #2404: one with its own impact stays (the rule synthesis uses), so its
    title stays in the risk while the dropped one goes."""
    kept = {**PLACEHOLDER, "impact": "Reads go to the replica."}
    report: dict[str, Any] = {
        "trade_offs": [dict(PLACEHOLDER), kept],
        "risk_assessment": {
            "risks": [
                {
                    "risk_id": "RISK-001",
                    "rule": "design.trade_offs",
                    "wave": 1,
                    "description": "[aurora_mysql] The Aurora MySQL schema design records "
                    "1 trade-off to review.",
                    "details": ["Aurora MySQL design trade-off"],
                }
            ]
        },
    }
    out = design_prose_report(report)
    assert out["trade_offs"] == [kept]
    (risk,) = out["risk_assessment"]["risks"]
    assert risk["details"] == ["Aurora MySQL design trade-off"]
    assert "records 1 trade-off to review" in risk["description"]


def test_stored_topic_groups_tagged_by_name_are_grouped_by_id_again() -> None:
    report: dict[str, Any] = {
        "contract_version": "1.99",
        "trade_offs": [
            {**_t("Reads go to the replica."), "topic": "other"},
            {**_t("Zero dates are carried over.", engine="Aurora MySQL"), "topic": "other"},
        ],
        "facts": {
            "trade_off_topics": [
                {"engine": "aurora_mysql", "topic": "other", "label": "Other",
                 "trade_off_count": 1, "indexes": [0]},
                {"engine": "Aurora MySQL", "topic": "other", "label": "Other",
                 "trade_off_count": 1, "indexes": [1]},
            ]
        },
    }  # fmt: skip
    out = design_prose_report(report)
    assert [(g["engine"], g["indexes"]) for g in out["facts"]["trade_off_topics"]] == [
        ("aurora_mysql", [0, 1])
    ]
