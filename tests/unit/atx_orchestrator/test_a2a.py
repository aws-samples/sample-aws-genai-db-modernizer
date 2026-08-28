"""Unit tests for src.atx_orchestrator.a2a.

Exercises the ``invoke_and_wait`` primitive and the module's private helpers
with the hand-rolled ``StubAgenticApiClient`` — no SDK, no network, no AWS.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.atx_orchestrator.a2a import (
    A2AError,
    A2AFailedError,
    A2APayloadError,
    A2ATimeoutError,
    StubAgenticApiClient,
    _extract_instance_id,
    _extract_reason,
    _extract_status,
    _is_expected_send_error,
    _parse_payload,
    invoke_and_wait,
)

# Poll fast in tests — this only affects the STUB client's fake polling,
# so we can safely spin quickly.
FAST_POLL = 0.005


# =============================================================================
# Helpers — focused unit tests for each private helper


class TestIsExpectedSendError:
    def test_dash_32603_string_match(self) -> None:
        assert _is_expected_send_error(Exception("JSON-RPC error -32603"))

    def test_internal_error_string_match(self) -> None:
        assert _is_expected_send_error(Exception("Internal error"))

    def test_boto3_style_response(self) -> None:
        err = Exception("client error")
        err.response = {"Error": {"Code": "-32603", "Message": "..."}}  # type: ignore[attr-defined]
        assert _is_expected_send_error(err)

    def test_unrelated_error(self) -> None:
        assert not _is_expected_send_error(Exception("connection refused"))

    def test_no_response_attribute(self) -> None:
        assert not _is_expected_send_error(Exception("some other error"))


class TestExtractStatus:
    def test_from_dict_uppercases(self) -> None:
        assert _extract_status({"agentInstanceStatus": "completed"}) == "COMPLETED"

    def test_from_dict_missing_key(self) -> None:
        assert _extract_status({}) == ""

    def test_from_object(self) -> None:
        class R:
            agentInstanceStatus = "running"

        assert _extract_status(R()) == "RUNNING"


class TestExtractReason:
    def test_from_dict(self) -> None:
        assert _extract_reason({"statusReason": "boom"}) == "boom"

    def test_from_dict_missing(self) -> None:
        assert _extract_reason({}) == ""


class TestParsePayload:
    def test_happy_path(self) -> None:
        resp = {
            "agentOutput": {"serializedPayload": json.dumps({"key": "value"})},
        }
        assert _parse_payload(resp) == {"key": "value"}

    def test_object_style_agent_output(self) -> None:
        class Out:
            serializedPayload = json.dumps({"k": 1})

        class Resp:
            agentOutput = Out()

        assert _parse_payload(Resp()) == {"k": 1}

    def test_empty_string_payload_raises(self) -> None:
        resp = {"agentOutput": {"serializedPayload": ""}}
        with pytest.raises(A2APayloadError, match="missing or empty"):
            _parse_payload(resp)

    def test_scalar_payload_raises(self) -> None:
        resp = {"agentOutput": {"serializedPayload": '"just a string"'}}
        with pytest.raises(A2APayloadError, match="JSON object"):
            _parse_payload(resp)


# =============================================================================
# invoke_and_wait — F8 (Y-3) primitive using discover + send pattern
#
# NOTE: earlier iteration used ``invoke_agent`` which returned a "phantom"
# agentInstanceId that never routed to a running container. We now use the
# canonical SDK pattern: ``list_agent_instances`` to discover pre-provisioned
# subagents (auto-created by ATX runtime when the orchestrator's Registry
# entry declares the subagents in its ``agentDependencies`` extension),
# then ``send_message`` to dispatch work.
# =============================================================================


def _stub_with_subagents(
    *,
    collector_id: str = "sub-collector-1",
    triage_id: str = "sub-triage-1",
    other_agents: list[dict[str, Any]] | None = None,
) -> StubAgenticApiClient:
    """Helper: pre-configured stub with collector + triage in list_agent_instances."""
    summaries: list[dict[str, Any]] = [
        {
            "agentInstanceId": collector_id,
            "agentId": "db-modernization-collector",
            "agentType": "SUB_AGENT",
            "agentInstanceStatus": "RUNNING",
        },
        {
            "agentInstanceId": triage_id,
            "agentId": "db-modernization-triage",
            "agentType": "SUB_AGENT",
            "agentInstanceStatus": "RUNNING",
        },
    ]
    if other_agents:
        summaries.extend(other_agents)
    return StubAgenticApiClient(
        list_agent_instances_response={"agentInstanceSummaries": summaries},
    )


class TestInvokeAndWaitHappyPath:
    """Discover + send + poll → terminal payload."""

    def test_immediate_completion_returns_payload(self) -> None:
        client = _stub_with_subagents()
        client.status_sequence = ["COMPLETED"]
        client.terminal_payload = {"job_id": "j1", "signals": 5}
        payload = invoke_and_wait(
            "db-modernization-triage",
            '{"kind":"triage","job_id":"j1"}',
            client=client,
            poll_interval=FAST_POLL,
        )
        assert payload == {"job_id": "j1", "signals": 5}

    def test_no_invoke_agent_when_existing_instance_found(self) -> None:
        """When list_agent_instances returns a matching subagent, skip invoke_agent."""
        client = _stub_with_subagents()
        client.status_sequence = ["COMPLETED"]
        invoke_and_wait("db-modernization-triage", "x", client=client, poll_interval=FAST_POLL)
        assert client.invoke_calls == []  # invoke_agent skipped — existing instance used

    def test_discovers_subagent_by_agent_id(self) -> None:
        client = _stub_with_subagents(collector_id="collector-99", triage_id="triage-88")
        client.status_sequence = ["COMPLETED"]
        invoke_and_wait("db-modernization-triage", "x", client=client, poll_interval=FAST_POLL)
        # list_agent_instances called once with the orchestrator's own id as filter
        assert len(client.list_calls) == 1
        # send_message + poll target the DISCOVERED triage instance id
        assert len(client.send_calls) == 1
        assert client.send_calls[0]["agentInstanceId"] == "triage-88"
        assert all(pid == "triage-88" for pid in client.poll_calls)

    def test_sends_canonical_a2a_message_envelope(self) -> None:
        client = _stub_with_subagents()
        client.status_sequence = ["COMPLETED"]
        invoke_and_wait(
            "db-modernization-collector",
            "hello",
            client=client,
            poll_interval=FAST_POLL,
        )
        msg = client.send_calls[0]["params"]["message"]
        assert msg["role"] == "agent"
        assert msg["kind"] == "message"
        assert msg["parts"] == [{"kind": "text", "text": "hello"}]
        # messageId is a fresh UUID string
        assert isinstance(msg["messageId"], str)
        assert len(msg["messageId"]) >= 32

    def test_request_context_threaded_to_list_send_poll(self) -> None:
        ctx = {
            "workspaceId": "ws-1",
            "jobId": "j-1",
            "authorizationToken": "tok",
            "agentInstanceId": "orchestrator-me",
        }
        client = _stub_with_subagents()
        client.status_sequence = ["COMPLETED"]
        invoke_and_wait(
            "db-modernization-triage",
            "x",
            client=client,
            request_context=ctx,
            poll_interval=FAST_POLL,
        )
        assert client.list_calls[0]["requestContext"] == ctx
        assert client.list_calls[0]["agentFilter"] == {
            "requesterAgentInstanceId": "orchestrator-me"
        }
        assert client.send_calls[0]["requestContext"] == ctx

    def test_delayed_completion_polls_multiple_times(self) -> None:
        client = _stub_with_subagents()
        client.status_sequence = ["STARTING", "RUNNING", "RUNNING", "COMPLETED"]
        client.terminal_payload = {"done": True}
        payload = invoke_and_wait(
            "db-modernization-triage",
            "hello",
            client=client,
            poll_interval=FAST_POLL,
        )
        assert payload == {"done": True}
        assert len(client.poll_calls) == 4

    def test_send_error_minus32603_tolerated(self) -> None:
        """The -32603 error on send_message is normal for long-running subagents."""
        client = _stub_with_subagents()
        client.status_sequence = ["COMPLETED"]
        client.send_side_effect = RuntimeError(
            "JSON-RPC error code: -32603, message: Internal error"
        )
        payload = invoke_and_wait(
            "db-modernization-triage",
            "x",
            client=client,
            poll_interval=FAST_POLL,
        )
        # Even though send raised, we continued to poll and got the payload
        assert payload == {}


class TestInvokeAndWaitDiscoveryFailures:
    """list_agent_instances error, invoke_agent fallback path errors."""

    def test_empty_list_falls_back_to_invoke_agent(self) -> None:
        """When list returns empty, invoke_agent is called to spawn a new instance."""
        client = StubAgenticApiClient(
            list_agent_instances_response={"agentInstanceSummaries": []},
            invoke_agent_instance_id="spawned-instance-1",
            # RUNNING for wait_for_ready, then COMPLETED for wait_for_completion
            status_sequence=["RUNNING", "COMPLETED"],
            terminal_payload={"ok": True},
        )
        payload = invoke_and_wait(
            "db-modernization-triage",
            "x",
            client=client,
            poll_interval=FAST_POLL,
            post_ready_dwell=0.0,
        )
        # invoke_agent called once with the right agent id
        assert len(client.invoke_calls) == 1
        assert client.invoke_calls[0]["agentId"] == "db-modernization-triage"
        # send_message + poll target the SPAWNED instance
        assert client.send_calls[0]["agentInstanceId"] == "spawned-instance-1"
        assert all(p == "spawned-instance-1" for p in client.poll_calls)
        assert payload == {"ok": True}

    def test_wrong_agent_id_still_falls_back_to_invoke(self) -> None:
        """A subagent for a different agent_id doesn't count as 'existing'."""
        client = _stub_with_subagents()  # has collector + triage
        client.invoke_agent_instance_id = "analysis-spawned-1"
        client.status_sequence = ["RUNNING", "COMPLETED"]  # ready then done
        client.terminal_payload = {"done": True}
        invoke_and_wait(
            "db-modernization-analysis",  # not in the pre-provisioned list
            "x",
            client=client,
            poll_interval=FAST_POLL,
            post_ready_dwell=0.0,
        )
        assert len(client.invoke_calls) == 1
        assert client.invoke_calls[0]["agentId"] == "db-modernization-analysis"
        assert client.send_calls[0]["agentInstanceId"] == "analysis-spawned-1"

    def test_unusable_status_treated_as_missing(self) -> None:
        """A subagent in SHUTDOWN state should trigger a fresh invoke_agent."""
        client = _stub_with_subagents()
        # Mutate — set triage to SHUTDOWN
        client.list_agent_instances_response["agentInstanceSummaries"][1][
            "agentInstanceStatus"
        ] = "SHUTDOWN"
        client.invoke_agent_instance_id = "triage-fresh-1"
        client.status_sequence = ["RUNNING", "COMPLETED"]
        invoke_and_wait(
            "db-modernization-triage",
            "x",
            client=client,
            poll_interval=FAST_POLL,
            post_ready_dwell=0.0,
        )
        # Fresh invoke happened
        assert len(client.invoke_calls) == 1
        assert client.send_calls[0]["agentInstanceId"] == "triage-fresh-1"

    def test_list_agent_instances_error_wrapped(self) -> None:
        client = StubAgenticApiClient(list_side_effect=RuntimeError("boom"))
        with pytest.raises(A2AError, match="list_agent_instances failed"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
                post_ready_dwell=0.0,
            )

    def test_invoke_agent_error_wrapped(self) -> None:
        client = StubAgenticApiClient(
            list_agent_instances_response={"agentInstanceSummaries": []},
            invoke_side_effect=RuntimeError("agent create failed"),
        )
        with pytest.raises(A2AError, match="invoke_agent failed"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )

    def test_invoke_agent_missing_instance_id(self) -> None:
        """invoke_agent returned no agentInstanceId → error."""
        client = StubAgenticApiClient(
            list_agent_instances_response={"agentInstanceSummaries": []},
            invoke_agent_instance_id="",  # simulate empty response
        )
        with pytest.raises(A2AError, match="did not return"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )


class TestInvokeAndWaitDispatchFailures:
    """send_message failure, FAILED status, timeout, poll error."""

    def test_send_message_unexpected_error_wrapped(self) -> None:
        client = _stub_with_subagents()
        client.send_side_effect = RuntimeError("network unreachable")
        with pytest.raises(A2AError, match="send_message .* failed"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )

    def test_send_error_intolerance_flag(self) -> None:
        client = _stub_with_subagents()
        client.send_side_effect = RuntimeError("code: -32603")
        with pytest.raises(A2AError, match="send_message"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
                tolerate_send_errors=False,
            )

    def test_failed_status_raises_with_reason(self) -> None:
        client = _stub_with_subagents()
        client.status_sequence = ["FAILED"]
        client.failure_reason = "downstream error"
        with pytest.raises(A2AFailedError, match="downstream error"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )

    def test_failed_status_without_reason(self) -> None:
        client = _stub_with_subagents()
        client.status_sequence = ["FAILED"]
        with pytest.raises(A2AFailedError, match="<no reason>"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )

    def test_timeout_when_status_never_terminal(self) -> None:
        client = _stub_with_subagents()
        client.status_sequence = ["RUNNING"]  # repeats forever
        with pytest.raises(A2ATimeoutError, match="did not reach terminal status"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                timeout=0.05,
                poll_interval=FAST_POLL,
            )

    def test_get_agent_instance_error_wrapped(self) -> None:
        class BrokenPollClient(StubAgenticApiClient):
            def get_agent_instance(self, **_kw):
                raise RuntimeError("network broken")

        client = BrokenPollClient(
            list_agent_instances_response={
                "agentInstanceSummaries": [
                    {
                        "agentInstanceId": "sub-1",
                        "agentId": "db-modernization-triage",
                        "agentType": "SUB_AGENT",
                        "agentInstanceStatus": "RUNNING",
                    }
                ]
            },
        )
        with pytest.raises(A2AError, match="get_agent_instance failed"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )


class TestInvokeAndWaitPayloadParsing:
    """Payload validation edge cases (missing / malformed / non-dict)."""

    def test_missing_payload_raises(self) -> None:
        class SparseClient(StubAgenticApiClient):
            def get_agent_instance(self, **_kw):
                return {"agentInstanceStatus": "COMPLETED", "agentOutput": {}}

        client = SparseClient(
            list_agent_instances_response={
                "agentInstanceSummaries": [
                    {
                        "agentInstanceId": "sub-1",
                        "agentId": "db-modernization-triage",
                        "agentType": "SUB_AGENT",
                        "agentInstanceStatus": "RUNNING",
                    }
                ]
            }
        )
        with pytest.raises(A2APayloadError, match="missing or empty"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )

    def test_malformed_json_payload_raises(self) -> None:
        class BadJsonClient(StubAgenticApiClient):
            def get_agent_instance(self, **_kw):
                return {
                    "agentInstanceStatus": "COMPLETED",
                    "agentOutput": {"serializedPayload": "{not json"},
                }

        client = BadJsonClient(
            list_agent_instances_response={
                "agentInstanceSummaries": [
                    {
                        "agentInstanceId": "sub-1",
                        "agentId": "db-modernization-triage",
                        "agentType": "SUB_AGENT",
                        "agentInstanceStatus": "RUNNING",
                    }
                ]
            }
        )
        with pytest.raises(A2APayloadError, match="not valid JSON"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )

    def test_non_dict_payload_raises(self) -> None:
        class ListPayloadClient(StubAgenticApiClient):
            def get_agent_instance(self, **_kw):
                return {
                    "agentInstanceStatus": "COMPLETED",
                    "agentOutput": {"serializedPayload": json.dumps([1, 2, 3])},
                }

        client = ListPayloadClient(
            list_agent_instances_response={
                "agentInstanceSummaries": [
                    {
                        "agentInstanceId": "sub-1",
                        "agentId": "db-modernization-triage",
                        "agentType": "SUB_AGENT",
                        "agentInstanceStatus": "RUNNING",
                    }
                ]
            }
        )
        with pytest.raises(A2APayloadError, match="must be a JSON object"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )


class TestFindSubagentByAgentId:
    """Unit tests for _find_subagent_by_agent_id + _summaries_of."""

    def test_finds_matching_subagent(self) -> None:
        resp = {
            "agentInstanceSummaries": [
                {
                    "agentInstanceId": "sub-1",
                    "agentId": "db-modernization-collector",
                    "agentType": "SUB_AGENT",
                    "agentInstanceStatus": "RUNNING",
                }
            ]
        }
        from src.atx_orchestrator.a2a import _find_subagent_by_agent_id

        assert _find_subagent_by_agent_id(resp, "db-modernization-collector") == "sub-1"

    def test_returns_empty_when_no_match(self) -> None:
        from src.atx_orchestrator.a2a import _find_subagent_by_agent_id

        assert _find_subagent_by_agent_id({"agentInstanceSummaries": []}, "any") == ""

    def test_skips_orchestrator_type(self) -> None:
        """Even if agentId matches, non-SUB_AGENT types are ignored."""
        from src.atx_orchestrator.a2a import _find_subagent_by_agent_id

        resp = {
            "agentInstanceSummaries": [
                {
                    "agentInstanceId": "orch-1",
                    "agentId": "db-modernization-collector",
                    "agentType": "ORCHESTRATOR_AGENT",
                    "agentInstanceStatus": "RUNNING",
                }
            ]
        }
        assert _find_subagent_by_agent_id(resp, "db-modernization-collector") == ""

    def test_skips_shutdown_status(self) -> None:
        from src.atx_orchestrator.a2a import _find_subagent_by_agent_id

        resp = {
            "agentInstanceSummaries": [
                {
                    "agentInstanceId": "sub-dead",
                    "agentId": "db-modernization-collector",
                    "agentType": "SUB_AGENT",
                    "agentInstanceStatus": "SHUTDOWN",
                }
            ]
        }
        assert _find_subagent_by_agent_id(resp, "db-modernization-collector") == ""

    def test_returns_first_match_when_multiple(self) -> None:
        from src.atx_orchestrator.a2a import _find_subagent_by_agent_id

        resp = {
            "agentInstanceSummaries": [
                {
                    "agentInstanceId": "sub-a",
                    "agentId": "db-modernization-collector",
                    "agentType": "SUB_AGENT",
                    "agentInstanceStatus": "RUNNING",
                },
                {
                    "agentInstanceId": "sub-b",
                    "agentId": "db-modernization-collector",
                    "agentType": "SUB_AGENT",
                    "agentInstanceStatus": "RUNNING",
                },
            ]
        }
        assert _find_subagent_by_agent_id(resp, "db-modernization-collector") == "sub-a"


class TestExtractInstanceId:
    """_extract_instance_id: dict + object accessors (still used defensively)."""

    def test_dict_with_key(self) -> None:
        assert _extract_instance_id({"agentInstanceId": "abc"}) == "abc"

    def test_dict_without_key(self) -> None:
        assert _extract_instance_id({}) == ""

    def test_dict_with_none_value(self) -> None:
        assert _extract_instance_id({"agentInstanceId": None}) == ""


class TestStubListAgentInstances:
    """Stub-level tests for the new list_agent_instances method."""

    def test_records_call(self) -> None:
        client = StubAgenticApiClient(
            list_agent_instances_response={"agentInstanceSummaries": [{"agentInstanceId": "x"}]}
        )
        resp = client.list_agent_instances(
            requestContext={"workspaceId": "w1"},
            agentFilter={"requesterAgentInstanceId": "o1"},
        )
        assert resp == {"agentInstanceSummaries": [{"agentInstanceId": "x"}]}
        assert client.list_calls == [
            {
                "requestContext": {"workspaceId": "w1"},
                "agentFilter": {"requesterAgentInstanceId": "o1"},
                "maxResults": None,
                "nextToken": None,
            }
        ]

    def test_side_effect_raises(self) -> None:
        client = StubAgenticApiClient(list_side_effect=ValueError("bad"))
        with pytest.raises(ValueError, match="bad"):
            client.list_agent_instances()
