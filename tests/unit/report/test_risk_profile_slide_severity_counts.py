"""The deck's Risk Profile slide counts every severity on its own (#270).

#270 found the decision report's risk posture line folding CRITICAL into
"high" and dropping LOW entirely. The deck already keeps CRITICAL and HIGH
as separate buckets (``pptx_report._severity``/``SEVERITY_ORDER``) -- these
are regression tests pinning that behaviour for a single CRITICAL risk and
for a mix of MEDIUM and LOW risks, so a future change to the counting logic
cannot silently reintroduce the same merge/drop bug here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from src.report import pptx_report

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"


def _risk(risk_id: str, severity: str) -> dict[str, Any]:
    return {
        "risk_id": risk_id,
        "risk_type": "MIGRATION_COMPLEXITY",
        "severity": severity,
        "description": f"[aurora_mysql] {risk_id} needs a mitigation",
    }


def _report(risks: list[dict[str, Any]], overall: str) -> dict[str, Any]:
    data: dict = json.loads(FIXTURE.read_text())
    data = copy.deepcopy(data)
    data["risk_assessment"]["risks"] = risks
    data["risk_assessment"]["overall_risk_level"] = overall
    return data


def _shape_texts(prs) -> list[str]:
    """Every shape/cell's exact text, as its own list entry (not joined) --
    the severity breakdown renders each severity label (e.g. "CRITICAL") as
    its own shape, so an exact-match check here is what proves a severity
    was never relabelled, rather than a substring check on the whole slide
    (which the #270 root-cause sentence, by design, can legitimately contain
    "CRITICAL/HIGH")."""
    out = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        out.append(cell.text_frame.text)
    return out


def _render_risk_slide(rep: dict[str, Any]):
    f = pptx_report.derive(rep, {})
    prs = pptx_report.open_deck(keep=1)
    pptx_report.slide_risk(prs, f)
    return prs


def test_a_single_critical_risk_is_its_own_bucket_not_folded_into_high() -> None:
    texts = _shape_texts(_render_risk_slide(_report([_risk("RISK-001", "CRITICAL")], "CRITICAL")))
    assert "1 risk: 1 CRITICAL" in "\n".join(texts)
    # The severity breakdown renders each present severity as its own exact-text
    # shape (e.g. "CRITICAL"); with no HIGH risk present, "HIGH" is never one of
    # them -- it only appears, correctly, inside the "CRITICAL/HIGH" root-cause
    # sentence (#270: CRITICAL counts alongside HIGH there too).
    assert "HIGH" not in texts
    assert any("CRITICAL/HIGH" in t for t in texts)


def test_a_mix_of_medium_and_low_risks_are_both_counted_and_sum_to_the_total() -> None:
    risks = [
        _risk("RISK-001", "MEDIUM"),
        _risk("RISK-002", "MEDIUM"),
        _risk("RISK-003", "LOW"),
    ]
    texts = _shape_texts(_render_risk_slide(_report(risks, "MEDIUM")))
    assert "3 risks: 2 MEDIUM, 1 LOW" in "\n".join(texts)
