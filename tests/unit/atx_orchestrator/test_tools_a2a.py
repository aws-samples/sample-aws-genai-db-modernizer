"""Unit tests for the A2A-wired variants in ``src.atx_orchestrator.tools``.

These test ``run_deterministic_core_via_a2a`` without any network or SDK --
``invoke_and_wait`` is patched at the tools module level so we can verify:
(1) the message payload the tool constructs (including the auto-discovered
``input_key``), (2) that the subagent NAME is passed correctly (not an instance
ID -- the LLM never knows that), (3) that the result flows through as a JSON
string, and (4) that A2A errors are turned into ``{"error": ...}`` JSON dicts
(not raised).

ADR-025: the four separate collect / triage / analysis / assignment tools were
consolidated into one ``run_deterministic_core_via_a2a`` that drives the
``db-modernization-deterministic-core`` subagent. The subagent ticks its own
plan steps, so this tool passes ``step=""`` and the wrapper marks nothing.
"""

from __future__ import annotations

import json
from importlib.util import find_spec
from unittest.mock import patch

import pytest

from src.atx_orchestrator.a2a import A2AFailedError, A2APayloadError, A2ATimeoutError
from src.atx_orchestrator.tools import run_deterministic_core_via_a2a

_AGENT = "db-modernization-deterministic-core"

# =============================================================================
# run_deterministic_core_via_a2a -- happy path


class TestRunDeterministicCoreViaA2AHappyPath:
    def test_returns_json_string_of_payload(self) -> None:
        payload = {"response": {"collector": {"tables": 311}, "assignment": {"total_queries": 42}}}
        with patch("src.atx_orchestrator.tools.invoke_and_wait", return_value=payload) as m:
            result_str = run_deterministic_core_via_a2a(
                job_id="job-1",
                database_name="discourse",
            )

        assert json.loads(result_str) == payload
        m.assert_called_once()

    def test_invokes_correct_agent_name(self) -> None:
        """The tool must invoke the ``db-modernization-deterministic-core`` agent."""
        with patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m:
            run_deterministic_core_via_a2a(job_id="job-1", database_name="discourse")

        args, _ = m.call_args
        agent_id, _message = args
        assert agent_id == _AGENT

    def test_constructs_correct_message_envelope(self) -> None:
        """The tool sends a JSON blob with job_id, database_name, and the
        auto-discovered input_key (never an LLM-supplied path)."""
        discovered = "AWSTransform/Workspaces/ws/Jobs/uuid/User Uploads/mydb.json"
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m,
            patch(
                "src.atx_orchestrator.core._discover_uploaded_input",
                return_value=discovered,
            ),
        ):
            run_deterministic_core_via_a2a(job_id="job-42", database_name="mydb")

        args, _ = m.call_args
        _agent_id, message = args
        parsed = json.loads(message)
        assert parsed == {
            "job_id": "job-42",
            "database_name": "mydb",
            "input_key": discovered,
        }

    def test_empty_input_key_defaults_correctly(self) -> None:
        with patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m:
            run_deterministic_core_via_a2a(job_id="job-1", database_name="db-1")
        message = m.call_args[0][1]
        assert json.loads(message)["input_key"] == ""


class TestRunDeterministicCoreViaA2AErrorPaths:
    @pytest.mark.parametrize(
        ("exc_type", "match_substr"),
        [
            (A2ATimeoutError, "A2A deterministic-core failed"),
            (A2AFailedError, "A2A deterministic-core failed"),
            (A2APayloadError, "A2A deterministic-core failed"),
        ],
    )
    def test_a2a_errors_are_returned_as_json_error_dict(
        self, exc_type: type, match_substr: str
    ) -> None:
        """Any A2AError variant is caught and returned as a JSON error dict."""
        exc = exc_type("something went wrong")
        with patch("src.atx_orchestrator.tools.invoke_and_wait", side_effect=exc):
            result_str = run_deterministic_core_via_a2a(job_id="job-1", database_name="db-1")

        result = json.loads(result_str)
        assert match_substr in result["error"]
        assert result["error_type"] == exc_type.__name__
        assert result["job_id"] == "job-1"
        # error dict includes agent_id (subagent NAME), NOT an instance id
        assert result["agent_id"] == _AGENT


# =============================================================================
# Registration


@pytest.mark.skipif(
    find_spec("agent_builder_sdk") is None,
    reason="orchestrator import requires the AWS Transform SDK (absent in CI)",
)
class TestToolsRegistered:
    def test_deterministic_core_tool_in_pipeline_tools(self) -> None:
        from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS

        tool_names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in PIPELINE_TOOLS]
        assert "run_deterministic_core_via_a2a" in tool_names

    def test_old_per_phase_tools_removed_from_pipeline(self) -> None:
        """ADR-025: the four separate deterministic tools were consolidated. The
        LLM must not be able to call them individually anymore."""
        from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS

        tool_names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in PIPELINE_TOOLS]
        assert "run_collect_via_a2a" not in tool_names
        assert "run_triage_via_a2a" not in tool_names
        assert "run_analysis_via_a2a" not in tool_names
        assert "run_assignment_via_a2a" not in tool_names

    def test_synthesis_a2a_tool_is_registered(self) -> None:
        """The synthesis phase must still be reachable over A2A."""
        from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS

        names = {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in PIPELINE_TOOLS}
        assert "run_synthesis_via_a2a" in names
