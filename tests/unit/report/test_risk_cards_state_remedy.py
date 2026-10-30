# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1510: a risk named by id in Next steps has its mitigation on its executive card,
and two risks that share one mitigation state it once, the same way everywhere.

Next steps and the mitigation strategies say "Apply the mitigation of RISK-001", so the
card must state it. A card used to leave it out in two cases: the mitigation repeats
the description (the description is shown instead, but taking table names out of it
can drop that part), and the mitigation is empty once table names are taken out.
``card_mitigation`` shows it in both. The shared mitigation is ``shared_mitigation_refs``,
computed once and read by the engineering report, the executive report and the results
payload (which the UI prints).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from src.report.engineering_report import _risk_register_md
from src.report.executive_report import (
    RISK_MITIGATION_POINTER,
    _names_pattern,
    card_mitigation,
    without_table_names,
)
from src.report.risks import shared_mitigation_refs

NAMES = _names_pattern({"shop.orders", "orders"})
LOAD_TEST = (
    "Load-test the 1 flagged query on Aurora PostgreSQL with production-scale data before "
    "cutover and tune indexes or query plans where the measured latency misses the target."
)


def _risk(rid: str, description: str, mitigation: str | None) -> dict[str, Any]:
    return {
        "risk_id": rid,
        "risk_type": "PERFORMANCE_DEGRADATION",
        "severity": "MEDIUM",
        "description": f"[aurora_postgresql] {description}",
        "affected_tables": ["shop.orders"],
        "mitigation": mitigation,
        "query_ids": ["q1"],
    }


def _report(risks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"risk_assessment": {"risks": risks, "mitigation_strategies": []}}


def test_a_card_whose_description_repeats_the_mitigation_keeps_it_once() -> None:
    """The description states the remedy and still does without table names: no line."""
    risk = _risk(
        "RISK-001",
        "Slow scan. Add an index on the date column.",
        "Add an index on the date column.",
    )
    assert card_mitigation(risk, NAMES, {}) is None


def test_a_card_shows_a_repeated_mitigation_that_the_table_names_cut_from_its_description() -> None:
    """The description repeats the mitigation inside a sentence that names a table: that
    sentence leaves the card, so the card shows the mitigation itself (its own table
    name taken out)."""
    body = "Slow scan on shop.orders, Add an index (on shop.orders) for the date range."
    risk = _risk("RISK-001", body, "Add an index (on shop.orders) for the date range.")
    assert without_table_names(body, NAMES) == ""
    assert card_mitigation(risk, NAMES, {}) == "Add an index for the date range."


def test_a_card_says_where_a_mitigation_of_only_table_names_or_sql_is() -> None:
    """Nothing is left once table names and SQL are out, and the executive report never
    prints them: the card points at the engineering report, not at nothing."""
    for mitigation in ("Index shop.orders.", "COUNT(*) on orders by id: keep a counter."):
        assert without_table_names(mitigation, NAMES) == ""
        risk = _risk("RISK-001", "A slow scan.", mitigation)
        assert card_mitigation(risk, NAMES, {}) == RISK_MITIGATION_POINTER
        repeated = _risk("RISK-002", f"A slow scan. {mitigation}", mitigation)
        assert card_mitigation(repeated, NAMES, {}) == RISK_MITIGATION_POINTER


def test_two_risks_with_one_mitigation_state_it_once_in_every_deliverable() -> None:
    report = _report(
        [
            _risk("RISK-003", "Query a is slow.", LOAD_TEST),
            _risk("RISK-004", "Query b is slow.", LOAD_TEST),
        ]
    )
    refs = shared_mitigation_refs(report)
    assert refs == {"RISK-004": "RISK-003"}
    risks = report["risk_assessment"]["risks"]
    assert card_mitigation(risks[0], NAMES, refs) == LOAD_TEST
    assert card_mitigation(risks[1], NAMES, refs) == "as for RISK-003."
    md = "\n".join(_risk_register_md(report))
    assert md.count("Load-test the 1 flagged query") == 1
    assert "  - Mitigation: as for RISK-003." in md


def test_a_mitigation_its_own_description_states_is_never_the_one_referred_to() -> None:
    report = _report(
        [
            _risk("RISK-003", f"Query a is slow. {LOAD_TEST}", LOAD_TEST),
            _risk("RISK-004", "Query b is slow.", LOAD_TEST),
            _risk("RISK-005", "Query c is slow.", LOAD_TEST),
        ]
    )
    assert shared_mitigation_refs(report) == {"RISK-005": "RISK-004"}


def test_the_results_payload_carries_the_refs_the_reports_use() -> None:
    from src.api.routes import results

    report = _report(
        [
            _risk("RISK-003", "Query a is slow.", LOAD_TEST),
            _risk("RISK-004", "Query b is slow.", LOAD_TEST),
        ]
    )
    s3 = MagicMock()
    s3.read_synthesis.return_value = report
    s3.read_triage.return_value = None
    with (
        patch.object(results, "s3_service", s3),
        patch.object(results, "_get_database_name", return_value="shop"),
    ):
        payload = results.get_results("job-1")
    assert payload["mitigation_refs"] == {"RISK-004": "RISK-003"}
