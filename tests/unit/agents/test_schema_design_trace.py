"""Tests for SchemaDesignTrace logging."""

from unittest.mock import MagicMock

from src.contracts.dynamodb_pe_review import (
    ChangeCategory,
    ChangeRequest,
    PEReviewResult,
    ReviewVerdict,
    Severity,
)


class TestSchemaDesignTrace:
    def _make_trace(self):
        from src.tools.schema.dynamodb_schema_agent import SchemaDesignTrace

        return SchemaDesignTrace()

    def _make_mock_output(self, tables=2, patterns=5):
        output = MagicMock()
        output.table_definitions = [MagicMock()] * tables
        output.access_patterns = [MagicMock()] * patterns
        output.unsupported_patterns = []
        output.hot_partition_analysis = [MagicMock()]
        output.validation_passed = True
        return output

    def test_log_designer_creates_entry(self):
        trace = self._make_trace()
        output = self._make_mock_output()
        trace.log_designer(0, 10.5, output)

        result = trace.to_dict()
        assert result["total_iterations"] == 1
        assert result["iterations"][0]["iteration"] == 1
        assert result["iterations"][0]["designer"]["duration_seconds"] == 10.5
        assert result["iterations"][0]["designer"]["tables"] == 2
        assert result["iterations"][0]["designer"]["access_patterns"] == 5

    def test_log_pe_review_approved(self):
        trace = self._make_trace()
        output = self._make_mock_output()
        trace.log_designer(0, 5.0, output)

        review = PEReviewResult(
            verdict=ReviewVerdict.APPROVED,
            summary="Looks good.",
            strengths=["Clean design"],
            pe_notes=["Consider PII spread"],
        )
        trace.log_pe_review(0, 3.0, review)

        result = trace.to_dict()
        pe = result["iterations"][0]["pe_review"]
        assert pe["verdict"] == "APPROVED"
        assert pe["duration_seconds"] == 3.0
        assert len(pe["strengths"]) == 1
        assert len(pe["pe_notes"]) == 1
        assert len(pe["change_requests"]) == 0

    def test_log_pe_review_with_changes(self):
        trace = self._make_trace()
        output = self._make_mock_output()
        trace.log_designer(0, 5.0, output)

        review = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            summary="Needs work.",
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.OVER_ENGINEERING,
                    severity=Severity.MAJOR,
                    target="UserLookup",
                    current_state="Dedicated table",
                    requested_change="Use GSI",
                    rationale="Simpler",
                )
            ],
        )
        trace.log_pe_review(0, 4.0, review)

        result = trace.to_dict()
        pe = result["iterations"][0]["pe_review"]
        assert pe["verdict"] == "CHANGES_REQUESTED"
        assert pe["change_requests"][0]["category"] == "over_engineering"
        assert pe["change_requests"][0]["severity"] == "major"

    def test_log_pe_error(self):
        trace = self._make_trace()
        output = self._make_mock_output()
        trace.log_designer(0, 5.0, output)
        trace.log_pe_error(0, "LLM timeout")

        result = trace.to_dict()
        assert result["iterations"][0]["pe_review"]["error"] == "LLM timeout"

    def test_multiple_iterations(self):
        trace = self._make_trace()
        output = self._make_mock_output()

        # Iteration 1: designer + PE requests changes
        trace.log_designer(0, 10.0, output)
        review1 = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            summary="Fix tables.",
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.TABLE_BOUNDARY,
                    severity=Severity.MAJOR,
                    target="T1",
                    current_state="Separate",
                    requested_change="Merge",
                    rationale="Co-access",
                )
            ],
        )
        trace.log_pe_review(0, 3.0, review1)

        # Iteration 2: revised designer + PE approves
        trace.log_designer(1, 8.0, output)
        review2 = PEReviewResult(
            verdict=ReviewVerdict.APPROVED,
            summary="Good now.",
        )
        trace.log_pe_review(1, 2.0, review2)

        result = trace.to_dict()
        assert result["total_iterations"] == 2
        assert result["iterations"][0]["pe_review"]["verdict"] == "CHANGES_REQUESTED"
        assert result["iterations"][1]["pe_review"]["verdict"] == "APPROVED"

    def test_total_duration_tracked(self):
        import time

        trace = self._make_trace()
        # nosemgrep: arbitrary-sleep -- needed to assert total_duration_seconds > 0
        time.sleep(0.01)
        result = trace.to_dict()
        assert result["total_duration_seconds"] >= 0.01
