"""The deck's "Scope" decision card counts CRITICAL risks alongside HIGH (#270).

Round-1 review of PR #506: ``pptx_report.derive``'s ``crit_high``/
``crit_high_by_engine`` fed the Scope card, the no-migration card, the engine
table, the per-wave counts and the worst-engine sentence from a HIGH-only
count, so an engine carrying only CRITICAL risks (and no HIGH ones) looked
risk-free in every one of those -- a CRITICAL risk is at least as severe as a
HIGH one everywhere else in this deliverable (``_risk_tile_class``,
``_risk_accent``), so it must outrank a HIGH risk here too, not disappear.

This fixture puts 2 CRITICAL risks on ``aurora_mysql`` and 1 HIGH risk on
``dynamodb``: the Scope card must point at ``aurora_mysql`` (2 crit/high
risks) even though it has no HIGH risk of its own, not at ``dynamodb`` (which
has 1).

Round-2 review adds a CRITICAL-only report (no HIGH risk at all) covering
two more consumers of the same count: the per-wave count and the
worst-engine sentence on the sequencing/decisions slide.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from src.report import pptx_report

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"


def _risk(risk_id: str, severity: str, engine: str) -> dict[str, Any]:
    return {
        "risk_id": risk_id,
        "risk_type": "MIGRATION_COMPLEXITY",
        "severity": severity,
        "description": f"[{engine}] {risk_id} needs a mitigation",
    }


def _report() -> dict[str, Any]:
    data: dict = json.loads(FIXTURE.read_text())
    data = copy.deepcopy(data)
    data["risk_assessment"]["risks"] = [
        _risk("RISK-001", "CRITICAL", "aurora_mysql"),
        _risk("RISK-002", "CRITICAL", "aurora_mysql"),
        _risk("RISK-003", "HIGH", "dynamodb"),
    ]
    data["risk_assessment"]["overall_risk_level"] = "CRITICAL"
    return data


def _critical_only_report() -> dict[str, Any]:
    """No HIGH risk at all -- just 2 CRITICAL ones, both on aurora_mysql."""
    data: dict = json.loads(FIXTURE.read_text())
    data = copy.deepcopy(data)
    data["risk_assessment"]["risks"] = [
        _risk("RISK-001", "CRITICAL", "aurora_mysql"),
        _risk("RISK-002", "CRITICAL", "aurora_mysql"),
    ]
    data["risk_assessment"]["overall_risk_level"] = "CRITICAL"
    return data


def test_scope_card_points_at_the_engine_with_the_most_critical_risks() -> None:
    f = pptx_report.derive(_report(), {})
    assert f["crit_high_by_engine"] == {"aurora_mysql": 2, "dynamodb": 1}
    scope = next(d for d in f["decisions"] if d["question"].startswith("Scope"))
    assert scope["question"] == "Scope Aurora MySQL?"
    assert scope["badge"] == "2 of 3 CRITICAL/HIGH risks"
    assert "Carries the most CRITICAL/HIGH risks" in scope["against"]


def test_engine_table_shows_the_critical_count_for_the_critical_only_engine() -> None:
    """``aurora_mysql`` has 0 HIGH-severity risks but 2 CRITICAL ones -- the
    engine table's count for it must be 2, not 0."""
    f = pptx_report.derive(_report(), {})
    prs = pptx_report.open_deck(keep=1)
    pptx_report.slide_decisions(prs, f)
    rows = next(
        shape.table.rows for slide in prs.slides for shape in slide.shapes if shape.has_table
    )
    by_engine = {row.cells[0].text_frame.text: row.cells[3].text_frame.text for row in rows}
    assert by_engine["Aurora MySQL"] == "2"
    assert by_engine["DynamoDB"] == "1"


def test_wave_count_and_worst_engine_sentence_on_a_critical_only_report() -> None:
    """No HIGH risk exists at all here -- only the per-wave count and the
    worst-engine sentence derived from a HIGH-only ``high``/``n_high`` would
    have read 0 for a wave genuinely carrying 2 CRITICAL risks."""
    f = pptx_report.derive(_critical_only_report(), {})
    aurora_wave = next(w for w in f["waves"] if "aurora_mysql" in w["engines"])
    assert aurora_wave["crit_high"] == 2

    prs = pptx_report.open_deck(keep=1)
    pptx_report.slide_decisions(prs, f)
    sentence = next(
        shape.text_frame.text
        for slide in prs.slides
        for shape in slide.shapes
        if shape.has_text_frame and "sit" in shape.text_frame.text
    )
    assert "2 of the 2 CRITICAL/HIGH risks sit on Aurora MySQL alone." in sentence
