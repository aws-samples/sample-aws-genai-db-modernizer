"""Reject invalid database identifiers before constructing artifact keys."""

import json
from unittest.mock import Mock, patch

import pytest

from src.atx_orchestrator.subagents.base import make_subagent_factory, parse_invocation


@pytest.mark.parametrize("encoding", ["json", "text"])
@pytest.mark.parametrize("name", ["wordpress", "Demo_DB-42", "a", "a" * 128])
def test_accepts_database_identifiers(encoding: str, name: str) -> None:
    payload = (
        json.dumps({"job_id": "j1", "database_name": name, "assignment_version": 2})
        if encoding == "json"
        else f"job_id: j1\ndatabase_name: {name}\nassignment_version: 2"
    )
    result = parse_invocation(payload)
    assert result["database_name"] == name
    assert int(result["assignment_version"]) == 2


@pytest.mark.parametrize("encoding", ["json", "text"])
@pytest.mark.parametrize(
    "name",
    ["", "../demo", "a/b", "a\\b", "a.b", "café", "demo database", "a,b", "a;bad", "a" * 129],
)
def test_rejects_invalid_database_identifiers(encoding: str, name: str) -> None:
    payload = (
        json.dumps({"job_id": "j1", "database_name": name})
        if encoding == "json"
        else f"job_id: j1\ndatabase_name: {name}"
    )
    with pytest.raises(ValueError, match="database_name.*1.*128"):
        parse_invocation(payload)


def test_rejects_embedded_whitespace_in_json_database_name() -> None:
    with pytest.raises(ValueError, match="database_name"):
        parse_invocation(json.dumps({"database_name": "demo database"}))


@pytest.mark.parametrize("name", [None, True, 123, [], {}])
def test_rejects_non_string_database_names(name) -> None:
    with pytest.raises(ValueError, match="database_name"):
        parse_invocation(json.dumps({"job_id": "j1", "database_name": name}))


@pytest.mark.asyncio
async def test_invalid_name_reports_failure_without_running_work() -> None:
    manager = Mock(agent_instance_id="instance-1")
    work = Mock()
    with (
        patch(
            "agent_builder_sdk.base_subagent.base_subagent.AsyncBaseSubagent.__init__",
            return_value=None,
        ),
        patch(
            "agent_builder_sdk.agentic_framework.agent_lifecycle.get_agent_instance_manager",
            return_value=manager,
        ),
    ):
        agent = make_subagent_factory("Test prompt", work)(None)
        with pytest.raises(ValueError, match="database_name"):
            await agent.process_message_async(
                json.dumps({"job_id": "j1", "database_name": "../bad"})
            )
    work.assert_not_called()
    manager.update_status.assert_called_once()
    assert manager.update_status.call_args.args == ("instance-1", "FAILED")
