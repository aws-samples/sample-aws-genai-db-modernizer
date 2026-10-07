"""Why the generated executive summary was withheld (#393 review).

Three independent checks can reject it (``apply_synthesis_llm_output``:
``check_summary_grounding``, ``check_summary_internal_leaks``,
``check_summary_wave_order``), each with its own ``[high confidence] ...`` message in
``summary_validation_warnings``. The decision report used to always say the narrative
"named a table under an engine that does not serve it", even when a different check
(an internal field name, or a stated migration order contradicting the roadmap) was
the one that actually rejected it.
"""

from __future__ import annotations

from src.report.renderers import fallback_withheld_reason


def _report(warnings: list[str]) -> dict:
    return {"summary_validation_warnings": warnings}


class TestFallbackWithheldReason:
    def test_table_attribution_reason(self) -> None:
        warnings = [
            '[high confidence] Summary attributes wp_posts ("wp_posts") to DynamoDB, but no '
            "in-scope query of that engine touches it (served by Aurora MySQL): some sentence."
        ]
        assert fallback_withheld_reason(_report(warnings)) == (
            "named a table under an engine that does not serve it"
        )

    def test_internal_field_name_reason(self) -> None:
        warnings = [
            '[high confidence] Summary names an internal field, "tco_analysis", instead of '
            "just stating the figure."
        ]
        assert fallback_withheld_reason(_report(warnings)) == (
            "named one of this codebase's internal field names"
        )

    def test_wave_order_reason(self) -> None:
        warnings = [
            "[high confidence] Summary states ElastiCache before Aurora MySQL, but the "
            "migration waves move Aurora MySQL first (wave 1) and ElastiCache later "
            "(wave 2): some sentence."
        ]
        assert fallback_withheld_reason(_report(warnings)) == (
            "stated a migration order that contradicts the roadmap"
        )

    def test_multiple_reasons_are_joined(self) -> None:
        warnings = [
            '[high confidence] Summary names an internal field, "tco_analysis", instead of '
            "just stating the figure.",
            "[high confidence] Summary states ElastiCache before Aurora MySQL, but the "
            "migration waves move Aurora MySQL first (wave 1) and ElastiCache later "
            "(wave 2): some sentence.",
        ]
        reason = fallback_withheld_reason(_report(warnings))
        assert "named one of this codebase's internal field names" in reason
        assert "stated a migration order that contradicts the roadmap" in reason
        assert " and " in reason

    def test_low_confidence_only_falls_back_to_the_default(self) -> None:
        """A low-confidence warning never triggered the fallback in the first place
        (``apply_synthesis_llm_output`` only rejects on a high-confidence finding), so
        there is no real-world case with only low-confidence warnings and a
        ``deterministic_fallback`` source -- the historical default stands."""
        warnings = ["[low confidence] Summary mentions users loosely: some sentence."]
        assert fallback_withheld_reason(_report(warnings)) == (
            "named a table under an engine that does not serve it"
        )

    def test_no_warnings_falls_back_to_the_default(self) -> None:
        assert fallback_withheld_reason(_report([])) == (
            "named a table under an engine that does not serve it"
        )
        assert fallback_withheld_reason({}) == (
            "named a table under an engine that does not serve it"
        )
