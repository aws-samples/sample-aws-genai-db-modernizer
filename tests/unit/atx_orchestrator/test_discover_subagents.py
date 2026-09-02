"""Unit tests for subagent discovery wiring.

Note (Y-3): ``discover_subagents`` is INTENTIONALLY not in ``PIPELINE_TOOLS``.
As of SDK v1.0.2 this method is a hardcoded mock returning a
"dynamic-showcase-subagent" (weather agent), which caused the LLM to
mis-conclude our real subagents weren't deployed when unrelated A2A errors
occurred. The function stays imported for future re-enablement when the SDK
ships a real registry-backed implementation.

See:
- ``agent_builder_sdk/orchestrator_strands/tools/subagent_registry_tools.py``
  (SDK source with the mock)
- ATX_POC_STATE.md F8 for the F8 fix context
"""

from __future__ import annotations

import pytest

from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS, discover_subagents


class TestDiscoverSubagentsRegistration:
    def test_discover_subagents_NOT_in_pipeline_tools(self) -> None:
        """Y-3 fix: intentionally unregistered because the SDK mock misleads the LLM."""
        tool_names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in PIPELINE_TOOLS]
        assert "discover_subagents" not in tool_names

    def test_pipeline_tools_registry_is_exactly_as_expected(self) -> None:
        """Assert the registry by name, not by count.

        This test previously asserted a bare integer and its name drifted out of
        step with the value twice (named ``_is_10`` while asserting 16, against a
        real registry of 17). Comparing the name set instead makes a change fail
        with the tool that caused it, and documents what is registered.
        """
        expected = {
            # plan declaration
            "declare_pipeline_plan",
            # A2A pipeline phases, in execution order. One consolidated
            # assessment-core tool (ADR-025, ADR-026) replaced the four separate
            # run_collect / run_triage / run_analysis / run_assignment _via_a2a
            # tools; it runs Collect -> Triage -> Analyze -> Assign -> Reality
            # Check in one agent.
            "run_assessment_core_via_a2a",
            # schema design, one per target engine, run in parallel between
            # the assessment core and synthesis
            "run_schema_design_dynamodb_via_a2a",
            "run_schema_design_documentdb_via_a2a",
            "run_schema_design_elasticache_via_a2a",
            "run_schema_design_opensearch_via_a2a",
            "run_schema_design_aurora_pg_via_a2a",
            "run_schema_design_aurora_mysql_via_a2a",
            "run_synthesis_via_a2a",
            # in-process legacy paths — registered but the system prompt directs
            # the LLM never to call them. run_schema_design and run_synthesis are
            # superseded by the _via_a2a tools above; run_reality_check has no
            # deployed subagent.
            "run_reality_check",
            "run_schema_design",
            "run_synthesis",
            "run_full_assessment",
            # status / read-only
            "get_job_status",
            "get_synthesis_report",
        }
        actual = {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in PIPELINE_TOOLS}
        assert actual == expected

    def test_discover_subagents_still_importable(self) -> None:
        """The function itself remains available for future re-enablement."""
        assert discover_subagents is not None


class TestDiscoverSubagentsCallable:
    """Async invocation via the SDK's mock — proves the wiring is intact (even
    though we've unregistered it from PIPELINE_TOOLS to protect the LLM from
    the mock's misleading output).
    """

    @pytest.mark.asyncio
    async def test_returns_list_of_agent_versions(self) -> None:
        result = await discover_subagents()
        assert isinstance(result, list)
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_mock_shape_has_expected_fields(self) -> None:
        """SDK mock returns GetAgentVersionOutput with version+metadata+configuration."""
        result = await discover_subagents()
        first = result[0]
        assert getattr(first, "version", None) is not None
        assert getattr(first, "metadata", None) is not None
        assert getattr(first, "configuration", None) is not None
