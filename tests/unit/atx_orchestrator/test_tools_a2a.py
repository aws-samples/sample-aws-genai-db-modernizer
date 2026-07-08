"""Unit tests for the A2A-wired variants in ``src.atx_orchestrator.tools``.

These test ``run_collect_via_a2a`` and ``run_triage_via_a2a`` without any
network or SDK — ``send_and_wait`` is patched at the tools module level so
we can verify: (1) the message payload the tool constructs, (2) that the
result flows through as a JSON string, and (3) that A2A errors are turned
into ``{"error": ...}`` JSON dicts (not raised).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.atx_orchestrator.a2a import A2AFailedError, A2APayloadError, A2ATimeoutError
from src.atx_orchestrator.tools import run_collect_via_a2a, run_triage_via_a2a

# =============================================================================
# run_collect_via_a2a


class TestRunCollectViaA2AHappyPath:
    def test_returns_json_string_of_payload(self) -> None:
        payload = {"response": {"tables": 311, "queries": 1654}}
        with patch("src.atx_orchestrator.tools.send_and_wait", return_value=payload) as m:
            result_str = run_collect_via_a2a(
                subagent_instance_id="subagent-abc",
                job_id="job-1",
                database_name="discourse",
            )

        assert json.loads(result_str) == payload
        m.assert_called_once()

    def test_constructs_correct_message_envelope(self) -> None:
        """The tool sends a JSON blob containing job_id, database_name, input_key."""
        with patch("src.atx_orchestrator.tools.send_and_wait", return_value={"ok": 1}) as m:
            run_collect_via_a2a(
                subagent_instance_id="subagent-xyz",
                job_id="job-42",
                database_name="mydb",
                input_key="path/to/uploads/collector-output.json",
            )

        # send_and_wait(subagent_instance_id, message)
        args, _ = m.call_args
        subagent_id, message = args
        assert subagent_id == "subagent-xyz"

        parsed = json.loads(message)
        assert parsed == {
            "job_id": "job-42",
            "database_name": "mydb",
            "input_key": "path/to/uploads/collector-output.json",
        }

    def test_empty_input_key_defaults_correctly(self) -> None:
        with patch("src.atx_orchestrator.tools.send_and_wait", return_value={"ok": 1}) as m:
            run_collect_via_a2a("subagent-1", "job-1", "db-1")
        message = m.call_args[0][1]
        assert json.loads(message)["input_key"] == ""


class TestRunCollectViaA2AErrorPaths:
    @pytest.mark.parametrize(
        ("exc_type", "match_substr"),
        [
            (A2ATimeoutError, "A2A collect failed"),
            (A2AFailedError, "A2A collect failed"),
            (A2APayloadError, "A2A collect failed"),
        ],
    )
    def test_a2a_errors_are_returned_as_json_error_dict(
        self, exc_type: type, match_substr: str
    ) -> None:
        """Any A2AError variant is caught and returned as a JSON error dict."""
        exc = exc_type("something went wrong")
        with patch("src.atx_orchestrator.tools.send_and_wait", side_effect=exc):
            result_str = run_collect_via_a2a("subagent-1", "job-1", "db-1")

        result = json.loads(result_str)
        assert match_substr in result["error"]
        assert result["error_type"] == exc_type.__name__
        assert result["job_id"] == "job-1"
        assert result["subagent_instance_id"] == "subagent-1"


# =============================================================================
# run_triage_via_a2a


class TestRunTriageViaA2AHappyPath:
    def test_returns_json_string_of_payload(self) -> None:
        payload = {"response": {"selected_engines": ["dynamodb"], "signal_count": 3}}
        with patch("src.atx_orchestrator.tools.send_and_wait", return_value=payload) as m:
            result_str = run_triage_via_a2a("subagent-triage-1", "job-1", "discourse")

        assert json.loads(result_str) == payload
        m.assert_called_once()

    def test_constructs_correct_message_envelope_no_input_key(self) -> None:
        """Triage doesn't take input_key — message has only job_id + database_name."""
        with patch("src.atx_orchestrator.tools.send_and_wait", return_value={"ok": 1}) as m:
            run_triage_via_a2a("subagent-t", "job-42", "mydb")

        message = m.call_args[0][1]
        parsed = json.loads(message)
        assert parsed == {"job_id": "job-42", "database_name": "mydb"}
        assert "input_key" not in parsed


class TestRunTriageViaA2AErrorPaths:
    def test_a2a_error_returned_as_json_error_dict(self) -> None:
        exc = A2AFailedError("subagent crashed")
        with patch("src.atx_orchestrator.tools.send_and_wait", side_effect=exc):
            result_str = run_triage_via_a2a("subagent-t", "job-1", "db-1")

        result = json.loads(result_str)
        assert "A2A triage failed" in result["error"]
        assert result["error_type"] == "A2AFailedError"
        assert result["job_id"] == "job-1"
        assert result["subagent_instance_id"] == "subagent-t"


# =============================================================================
# Registration


class TestToolsRegistered:
    def test_both_tools_in_pipeline_tools(self) -> None:
        """Both A2A tools are registered alongside the in-process versions."""
        from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS

        tool_names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in PIPELINE_TOOLS]
        assert "run_collect_via_a2a" in tool_names
        assert "run_triage_via_a2a" in tool_names
        # In-process versions still present
        assert "run_collect" in tool_names
        assert "run_triage" in tool_names

    def test_pipeline_tools_count_increased(self) -> None:
        """PIPELINE_TOOLS grew by exactly 2 (a2a variants) from the prior 10."""
        from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS

        assert len(PIPELINE_TOOLS) == 12
