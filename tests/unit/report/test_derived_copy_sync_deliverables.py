"""The derived copies' stream sync is printed where its overlap risk sits (#1527).

The engineering report and the executive report PDF print ``facts.derived_copy_sync`` under
the design-conflict risk it belongs to, and nothing under any other risk. The executive
report names no table. A report written before the fact renders as it did.
"""

from __future__ import annotations

import copy
from typing import Any

from src.agents.referee.derived_copy_sync import derived_copy_sync_facts
from src.report import escaping, executive_report, renderers
from src.shared.report_facts import compute_report_facts
from tests.executive_pdf import body_text
from tests.unit.agents.referee.test_derived_copy_sync import _ap, _overlap, _risk, _schema


def _report(with_fact: bool = True) -> dict[str, Any]:
    conflict = _risk("RISK-002")
    conflict["description"] = "[dynamodb] posts are modelled independently in two designs"
    conflict["risk_type"] = "DATA_CONSISTENCY"
    other = {
        "risk_id": "RISK-001",
        "risk_type": "MIGRATION_COMPLEXITY",
        "severity": "MEDIUM",
        "description": "[dynamodb] a different finding",
    }
    report: dict[str, Any] = {
        "database_name": "wordpress",
        "risk_assessment": {
            "overall_risk_level": "MEDIUM",
            "risks": [other, conflict],
            "mitigation_strategies": [],
        },
    }
    puts = [
        _ap("wp_posts", "PutItem", 4.0, 1500, qid="w1"),
        _ap("wp_posts_b", "PutItem", 2.0, 1500, qid="w2"),
    ]
    schema = _schema(puts, targets=("WpPosts", "wp_posts", "wp_posts_b"))
    assert schema["trade_offs"] == [_overlap(["WpPosts", "wp_posts", "wp_posts_b"])]
    fact = derived_copy_sync_facts(schema, report["risk_assessment"]["risks"])
    report["facts"] = compute_report_facts(report, derived_copy_sync=fact if with_fact else None)
    return report


def test_the_engineering_report_prints_the_path_and_cost_under_the_overlap_risk() -> None:
    md = renderers.render_engineering_report_md(_report())
    lines = md.splitlines()
    at = next(i for i, line in enumerate(lines) if "RISK-002" in line)
    block = "\n".join(lines[at : at + 6])

    assert escaping.md_text("Sync path: writes to wp_posts go to WpPosts") in block
    assert "Sync cost: $39.42/mo at on-demand prices" in block
    assert "Basis: " in block and "GetRecords" in block


def test_no_other_risk_carries_the_sync() -> None:
    md = renderers.render_engineering_report_md(_report())
    lines = md.splitlines()
    at = next(i for i, line in enumerate(lines) if "RISK-001" in line)

    assert not any("Sync" in line for line in lines[at : at + 3])
    assert md.count("Sync path:") == 1 and md.count("Sync cost:") == 1


def test_an_older_report_without_the_fact_renders_without_it() -> None:
    old = _report(with_fact=False)
    assert old["facts"]["derived_copy_sync"] is None
    del old["facts"]["derived_copy_sync"]  # a facts block written before the field

    for report in (old, _report(with_fact=False)):
        md = renderers.render_engineering_report_md(report)
        assert "RISK-002" in md and "Sync path" not in md and "Basis:" not in md
        assert executive_report.render_executive_report_pdf(report)


def test_the_executive_report_states_the_sync_without_a_table_name() -> None:
    report = _report()
    text = " ".join(
        body_text(executive_report.render_executive_report_pdf(report), "wordpress").split()
    )

    assert "Stream sync" in text
    assert "2 derived copies are kept in sync from DynamoDB Streams" in text
    assert "$39.42 a month" in text
    assert "wp_posts" not in text and "WpPosts" not in text


def test_the_pdf_of_a_report_without_the_fact_is_unchanged_by_it() -> None:
    plain = _report(with_fact=False)
    again = copy.deepcopy(plain)
    del again["facts"]["derived_copy_sync"]

    assert executive_report.render_executive_report_pdf(
        plain
    ) == executive_report.render_executive_report_pdf(again)
