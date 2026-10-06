"""``check_summary_internal_leaks`` catches an internal JSON field/section name
leaked into a customer-facing executive summary (#380), independently of
``check_summary_grounding``'s table/engine attribution check.
"""

from __future__ import annotations

from src.agents.referee.synthesis_grounding import check_summary_internal_leaks


class TestCheckSummaryInternalLeaks:
    def test_empty_summary_is_clean(self) -> None:
        assert check_summary_internal_leaks("") == []

    def test_ordinary_summary_is_clean(self) -> None:
        summary = "Aurora PostgreSQL serves the relational majority of query patterns."
        assert check_summary_internal_leaks(summary) == []

    def test_tco_analysis_field_name_is_flagged(self) -> None:
        findings = check_summary_internal_leaks(
            "Removes infrastructure cost (see tco_analysis's cost_breakdown for the figure)."
        )
        assert len(findings) == 2  # tco_analysis and cost_breakdown
        assert all(f["high_confidence"] for f in findings)
        texts = {f["text"].lower() for f in findings}
        assert texts == {"tco_analysis", "cost_breakdown"}

    def test_matched_case_insensitively(self) -> None:
        findings = check_summary_internal_leaks("See Table_Mappings for the full list.")
        assert findings and findings[0]["text"] == "Table_Mappings"

    def test_whole_word_only(self) -> None:
        # "table_mappings_extended" is not the field name "table_mappings".
        assert check_summary_internal_leaks("See table_mappings_extended for details.") == []
