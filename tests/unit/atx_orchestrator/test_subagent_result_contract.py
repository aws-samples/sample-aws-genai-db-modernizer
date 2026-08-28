"""The subagent return value must satisfy the SDK's AgentResult contract.

``AsyncBaseSubagent.process_message_async`` is annotated ``-> AgentResult`` and
the SDK's queue handler consumes it as one. Returning a bare JSON string raised
``'str' object has no attribute 'message'`` on every invocation of all eleven
subagents, which marked each task ``failed`` while the work itself succeeded and
its artifacts were written.

These tests pin the contract at the seam: they call the SDK's own extractor
rather than re-implementing what it does, so an SDK change that widens what it
reads fails here rather than in a runtime log.
"""

from __future__ import annotations

import json

import pytest
from agent_builder_sdk.utils import extract_text_from_strands_agent_response

from src.atx_orchestrator.subagent_base import _SubagentResult, _summary_line

SUMMARY = {
    "job_id": "v2-e2e-03",
    "database_name": "discourse",
    "assignment_version": 1,
    "engines_ranked": 5,
    "top_engine": "aurora_postgresql",
    "architecture_type": "HYBRID_WITH_CACHE",
    "overall_risk_level": "HIGH",
    "has_executive_summary": True,
    "warnings": ["no engine reported schema-design output"],
    "published_artifacts": {"Database Modernization Assessment: discourse": "abc-123"},
}


class TestSDKContract:
    """The two attributes the SDK actually reads off the returned object."""

    def test_sdk_extractor_accepts_our_result(self) -> None:
        """The call that used to raise must now return the summary text."""
        text = extract_text_from_strands_agent_response(_SubagentResult(_summary_line(SUMMARY)))
        assert "job_id=v2-e2e-03" in text
        assert "top_engine=aurora_postgresql" in text

    def test_state_is_present_and_mapping_like(self) -> None:
        """The handler does ``result.state.get("force_stop_response") if result.state``."""
        r = _SubagentResult("anything")
        assert r.state.get("force_stop_response") is None

    def test_a_bare_json_string_still_fails_the_contract(self) -> None:
        """Guards the regression itself.

        If this ever stops raising, the SDK has changed and _SubagentResult should
        be re-examined rather than assumed still necessary.
        """
        with pytest.raises(AttributeError, match="no attribute 'message'"):
            extract_text_from_strands_agent_response(json.dumps({"response": SUMMARY}))


class TestSummaryLine:
    """The rendered line is what a person sees on the task in the WebApp."""

    def test_scalars_included_containers_dropped(self) -> None:
        line = _summary_line(SUMMARY)
        assert "engines_ranked=5" in line
        # nested values would swamp a one-line summary
        assert "warnings" not in line
        assert "published_artifacts" not in line

    def test_empty_and_none_values_dropped(self) -> None:
        line = _summary_line({"kept": 1, "blank": "", "missing": None})
        assert line == "kept=1"

    def test_summary_with_no_scalars_is_not_empty(self) -> None:
        """An empty line would render as a blank task response."""
        assert _summary_line({"only": {"nested": True}}) == "completed"

    def test_non_dict_summary_does_not_raise(self) -> None:
        assert _summary_line("already text") == "already text"  # type: ignore[arg-type]
