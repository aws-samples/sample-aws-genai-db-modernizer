"""Unit tests for the A2A-wired variants in ``src.atx_orchestrator.tools``.

These test ``run_collect_via_a2a`` and ``run_triage_via_a2a`` without any
network or SDK — ``invoke_and_wait`` is patched at the tools module level so
we can verify: (1) the message payload the tool constructs, (2) that the
subagent NAME is passed correctly (not an instance ID — the LLM never knows
that), (3) that the result flows through as a JSON string, and (4) that A2A
errors are turned into ``{"error": ...}`` JSON dicts (not raised).

Y-3 (F8 fix): these tests reflect the refactored signatures — no more
``subagent_instance_id`` parameter. The tool takes only ``job_id`` and
``database_name`` (and optional ``input_key`` for collector), and resolves
the subagent by hard-coded name via the ``invoke_agent`` API.
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
        with patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload) as m:
            result_str = run_collect_via_a2a(
                job_id="job-1",
                database_name="discourse",
            )

        assert json.loads(result_str) == payload
        m.assert_called_once()

    def test_invokes_correct_agent_name(self) -> None:
        """The tool must invoke the ``db-modernization-collector`` agent by name."""
        with patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m:
            run_collect_via_a2a(job_id="job-1", database_name="discourse")

        args, _ = m.call_args
        agent_id, _message = args
        assert agent_id == "db-modernization-collector"

    def test_constructs_correct_message_envelope(self) -> None:
        """The tool sends a JSON blob containing job_id, database_name, input_key."""
        with patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m:
            run_collect_via_a2a(
                job_id="job-42",
                database_name="mydb",
                input_key="path/to/uploads/collector-output.json",
            )

        # invoke_and_wait(agent_id, message)
        args, _ = m.call_args
        _agent_id, message = args

        parsed = json.loads(message)
        assert parsed == {
            "job_id": "job-42",
            "database_name": "mydb",
            "input_key": "path/to/uploads/collector-output.json",
        }

    def test_empty_input_key_defaults_correctly(self) -> None:
        with patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m:
            run_collect_via_a2a(job_id="job-1", database_name="db-1")
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
        with patch("src.atx_orchestrator.tools.invoke_and_wait", side_effect=exc):
            result_str = run_collect_via_a2a(job_id="job-1", database_name="db-1")

        result = json.loads(result_str)
        assert match_substr in result["error"]
        assert result["error_type"] == exc_type.__name__
        assert result["job_id"] == "job-1"
        # New: error dict includes agent_id (subagent NAME), NOT subagent_instance_id
        assert result["agent_id"] == "db-modernization-collector"


# =============================================================================
# run_triage_via_a2a


class TestRunTriageViaA2AHappyPath:
    def test_returns_json_string_of_payload(self) -> None:
        payload = {"response": {"selected_engines": ["dynamodb"], "signal_count": 3}}
        with patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload) as m:
            result_str = run_triage_via_a2a(job_id="job-1", database_name="discourse")

        assert json.loads(result_str) == payload
        m.assert_called_once()

    def test_invokes_correct_agent_name(self) -> None:
        with patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m:
            run_triage_via_a2a(job_id="j", database_name="d")

        args, _ = m.call_args
        agent_id, _message = args
        assert agent_id == "db-modernization-triage"

    def test_constructs_correct_message_envelope_no_input_key(self) -> None:
        """Triage doesn't take input_key — message has only job_id + database_name."""
        with patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m:
            run_triage_via_a2a(job_id="job-42", database_name="mydb")

        message = m.call_args[0][1]
        parsed = json.loads(message)
        assert parsed == {"job_id": "job-42", "database_name": "mydb"}
        assert "input_key" not in parsed


class TestRunTriageViaA2AErrorPaths:
    def test_a2a_error_returned_as_json_error_dict(self) -> None:
        exc = A2AFailedError("subagent crashed")
        with patch("src.atx_orchestrator.tools.invoke_and_wait", side_effect=exc):
            result_str = run_triage_via_a2a(job_id="job-1", database_name="db-1")

        result = json.loads(result_str)
        assert "A2A triage failed" in result["error"]
        assert result["error_type"] == "A2AFailedError"
        assert result["job_id"] == "job-1"
        assert result["agent_id"] == "db-modernization-triage"


# =============================================================================
# Registration


class TestToolsRegistered:
    def test_a2a_tools_in_pipeline_tools(self) -> None:
        """Both A2A tools are registered."""
        from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS

        tool_names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in PIPELINE_TOOLS]
        assert "run_collect_via_a2a" in tool_names
        assert "run_triage_via_a2a" in tool_names

    def test_in_process_tools_removed_from_pipeline(self) -> None:
        """Y-3 refactor: in-process ``run_collect`` / ``run_triage`` /
        ``run_collect_and_triage`` are unregistered so the LLM can't
        silently fall back to them. They remain defined in the tools module
        (for direct programmatic use by test scripts) but are NOT in
        ``PIPELINE_TOOLS``.
        """
        from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS

        tool_names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in PIPELINE_TOOLS]
        assert "run_collect" not in tool_names
        assert "run_triage" not in tool_names
        assert "run_collect_and_triage" not in tool_names

    def test_pipeline_tools_count_is_ten(self) -> None:
        """Post-Y-3 tool count: 2 A2A + 5 pipeline + 2 status + 1 discovery = 10."""
        from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS

        assert len(PIPELINE_TOOLS) == 10
