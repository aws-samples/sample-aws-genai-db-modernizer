"""Unit tests for src.atx_orchestrator.a2a.

Exercises the send-and-wait primitive with the hand-rolled
``StubAgenticApiClient`` — no SDK, no network, no AWS.
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
    send_and_wait,
)

# Poll fast in tests — this only affects the STUB client's fake polling,
# so we can safely spin quickly.
FAST_POLL = 0.005


# =============================================================================
# Happy path


class TestSendAndWaitHappyPath:
    def test_immediate_completion_returns_parsed_payload(self) -> None:
        """COMPLETED on first poll → dict returned exactly as serialized."""
        client = StubAgenticApiClient(
            status_sequence=["COMPLETED"],
            terminal_payload={"response": {"tables": 311, "queries": 1654}},
        )

        result = send_and_wait(
            subagent_instance_id="test-instance-1",
            message="collect discourse job",
            client=client,
            timeout=5.0,
            poll_interval=FAST_POLL,
        )

        assert result == {"response": {"tables": 311, "queries": 1654}}
        assert len(client.send_calls) == 1
        assert client.send_calls[0]["agentInstanceId"] == "test-instance-1"
        assert client.poll_calls == ["test-instance-1"]

    def test_delayed_completion_polls_until_terminal(self) -> None:
        """RUNNING x3, then COMPLETED → returns payload after 4 polls."""
        client = StubAgenticApiClient(
            status_sequence=["STARTING", "RUNNING", "RUNNING", "COMPLETED"],
            terminal_payload={"response": "done"},
        )

        result = send_and_wait(
            subagent_instance_id="test-instance-2",
            message="test",
            client=client,
            timeout=5.0,
            poll_interval=FAST_POLL,
        )

        assert result == {"response": "done"}
        assert len(client.poll_calls) == 4

    def test_message_is_wrapped_in_a2a_envelope(self) -> None:
        """send_message receives a proper A2A message envelope."""
        client = StubAgenticApiClient(terminal_payload={"ok": True})
        send_and_wait(
            subagent_instance_id="test-instance-3",
            message="hello subagent",
            client=client,
            timeout=1.0,
            poll_interval=FAST_POLL,
        )

        call = client.send_calls[0]
        assert call["params"]["message"]["role"] == "agent"
        assert call["params"]["message"]["parts"][0]["kind"] == "text"
        assert call["params"]["message"]["parts"][0]["text"] == "hello subagent"

    def test_request_context_flows_through(self) -> None:
        """Injected request_context reaches both send and poll calls."""
        client = StubAgenticApiClient(terminal_payload={"ok": True})
        ctx = {"workspaceId": "ws-1", "jobId": "job-1"}

        send_and_wait(
            subagent_instance_id="test-instance-4",
            message="test",
            client=client,
            request_context=ctx,
            timeout=1.0,
            poll_interval=FAST_POLL,
        )

        assert client.send_calls[0]["requestContext"] == ctx


# =============================================================================
# Send error tolerance (the -32603 special case)


class TestSendErrorTolerance:
    def test_tolerates_dash_32603_in_error_message(self) -> None:
        """A -32603 JSON-RPC error on send is tolerated; polling proceeds."""
        err = Exception("JSON-RPC error -32603: Internal error")
        client = StubAgenticApiClient(
            status_sequence=["COMPLETED"],
            terminal_payload={"ok": True},
            send_side_effect=err,
        )

        result = send_and_wait(
            subagent_instance_id="test-instance-5",
            message="test",
            client=client,
            timeout=5.0,
            poll_interval=FAST_POLL,
        )

        assert result == {"ok": True}
        # Send was attempted and errored, but polling continued
        assert len(client.send_calls) == 1
        assert client.poll_calls == ["test-instance-5"]

    def test_tolerates_boto3_style_client_error(self) -> None:
        """boto3 ClientError with -32603 in response['Error']['Code'] is tolerated."""

        class Boto3StyleError(Exception):
            def __init__(self) -> None:
                super().__init__("botocore ClientError")
                self.response: dict[str, Any] = {
                    "Error": {"Code": "-32603", "Message": "Internal error"}
                }

        client = StubAgenticApiClient(
            status_sequence=["COMPLETED"],
            terminal_payload={"ok": True},
            send_side_effect=Boto3StyleError(),
        )
        result = send_and_wait(
            subagent_instance_id="test-instance-6",
            message="test",
            client=client,
            timeout=5.0,
            poll_interval=FAST_POLL,
        )
        assert result == {"ok": True}

    def test_send_error_not_tolerated_when_flag_off(self) -> None:
        err = Exception("JSON-RPC error -32603: Internal error")
        client = StubAgenticApiClient(
            status_sequence=["COMPLETED"],
            terminal_payload={"ok": True},
            send_side_effect=err,
        )

        with pytest.raises(A2AError, match="send_message failed"):
            send_and_wait(
                subagent_instance_id="test-instance-7",
                message="test",
                client=client,
                timeout=5.0,
                poll_interval=FAST_POLL,
                tolerate_send_errors=False,
            )

    def test_unrelated_send_error_raises_a2aerror(self) -> None:
        """Errors other than -32603 surface immediately (no tolerance)."""
        err = Exception("some unrelated network error")
        client = StubAgenticApiClient(send_side_effect=err)

        with pytest.raises(A2AError, match="send_message failed"):
            send_and_wait(
                subagent_instance_id="test-instance-8",
                message="test",
                client=client,
                timeout=5.0,
                poll_interval=FAST_POLL,
            )


# =============================================================================
# Failure paths


class TestFailurePaths:
    def test_failed_status_raises_with_reason(self) -> None:
        client = StubAgenticApiClient(
            status_sequence=["FAILED"],
            failure_reason="database unreachable",
        )

        with pytest.raises(A2AFailedError, match="database unreachable"):
            send_and_wait(
                subagent_instance_id="test-instance-9",
                message="test",
                client=client,
                timeout=5.0,
                poll_interval=FAST_POLL,
            )

    def test_failed_status_without_reason_still_raises(self) -> None:
        client = StubAgenticApiClient(status_sequence=["FAILED"])
        with pytest.raises(A2AFailedError, match="no reason"):
            send_and_wait(
                subagent_instance_id="test-instance-10",
                message="test",
                client=client,
                timeout=5.0,
                poll_interval=FAST_POLL,
            )

    def test_timeout_raises_when_terminal_never_reached(self) -> None:
        client = StubAgenticApiClient(status_sequence=["RUNNING"])
        with pytest.raises(A2ATimeoutError, match="did not reach terminal"):
            send_and_wait(
                subagent_instance_id="test-instance-11",
                message="test",
                client=client,
                timeout=0.05,
                poll_interval=FAST_POLL,
            )

    def test_get_agent_instance_exception_wraps_as_a2aerror(self) -> None:
        class BrokenClient:
            def send_message(self, **kwargs: Any) -> dict[str, Any]:
                return {"messageId": "x"}

            def get_agent_instance(self, **kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("network glitch")

        with pytest.raises(A2AError, match="get_agent_instance failed"):
            send_and_wait(
                subagent_instance_id="test-instance-12",
                message="test",
                client=BrokenClient(),
                timeout=5.0,
                poll_interval=FAST_POLL,
            )


# =============================================================================
# Payload parsing edge cases


class TestPayloadParsing:
    def _client_with_output(self, agent_output: Any) -> Any:
        """Build a minimal client that returns COMPLETED with the given agentOutput."""

        class _Client:
            def send_message(self, **kwargs: Any) -> dict[str, Any]:
                return {"messageId": "x"}

            def get_agent_instance(self, **kwargs: Any) -> dict[str, Any]:
                return {
                    "agentInstanceId": kwargs["agentInstanceId"],
                    "agentInstanceStatus": "COMPLETED",
                    "agentOutput": agent_output,
                }

        return _Client()

    def test_malformed_json_raises_payload_error(self) -> None:
        client = self._client_with_output({"serializedPayload": "not-json{"})
        with pytest.raises(A2APayloadError, match="not valid JSON"):
            send_and_wait(
                subagent_instance_id="test-instance-13",
                message="test",
                client=client,
                timeout=5.0,
                poll_interval=FAST_POLL,
            )

    def test_missing_payload_raises_payload_error(self) -> None:
        client = self._client_with_output({})  # no serializedPayload key
        with pytest.raises(A2APayloadError, match="missing or empty"):
            send_and_wait(
                subagent_instance_id="test-instance-14",
                message="test",
                client=client,
                timeout=5.0,
                poll_interval=FAST_POLL,
            )

    def test_empty_string_payload_raises_payload_error(self) -> None:
        client = self._client_with_output({"serializedPayload": ""})
        with pytest.raises(A2APayloadError, match="missing or empty"):
            send_and_wait(
                subagent_instance_id="test-instance-15",
                message="test",
                client=client,
                timeout=5.0,
                poll_interval=FAST_POLL,
            )

    def test_payload_must_be_dict_not_scalar(self) -> None:
        client = self._client_with_output({"serializedPayload": '"just a string"'})
        with pytest.raises(A2APayloadError, match="JSON object"):
            send_and_wait(
                subagent_instance_id="test-instance-16",
                message="test",
                client=client,
                timeout=5.0,
                poll_interval=FAST_POLL,
            )

    def test_payload_must_be_dict_not_list(self) -> None:
        client = self._client_with_output({"serializedPayload": "[1, 2, 3]"})
        with pytest.raises(A2APayloadError, match="JSON object"):
            send_and_wait(
                subagent_instance_id="test-instance-17",
                message="test",
                client=client,
                timeout=5.0,
                poll_interval=FAST_POLL,
            )


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


# =============================================================================
# invoke_and_wait — the F8 fix (Y-3) primitive
# =============================================================================


class TestInvokeAndWaitHappyPath:
    """invoke_agent + poll happy paths (immediate + delayed completion)."""

    def test_immediate_completion_returns_payload(self) -> None:
        client = StubAgenticApiClient(
            status_sequence=["COMPLETED"],
            terminal_payload={"job_id": "j1", "signals": 5},
        )
        payload = invoke_and_wait(
            "db-modernization-triage",
            '{"kind":"triage","job_id":"j1"}',
            client=client,
            poll_interval=FAST_POLL,
        )
        assert payload == {"job_id": "j1", "signals": 5}
        assert len(client.invoke_calls) == 1
        assert client.invoke_calls[0]["agentId"] == "db-modernization-triage"
        assert client.invoke_calls[0]["agentType"] == "SUB_AGENT"
        assert client.invoke_calls[0]["inputPayload"] == {
            "serializedPayload": '{"kind":"triage","job_id":"j1"}',
        }

    def test_delayed_completion_polls_multiple_times(self) -> None:
        client = StubAgenticApiClient(
            status_sequence=["STARTING", "RUNNING", "RUNNING", "COMPLETED"],
            terminal_payload={"done": True},
        )
        payload = invoke_and_wait(
            "db-modernization-triage",
            "hello",
            client=client,
            poll_interval=FAST_POLL,
        )
        assert payload == {"done": True}
        assert len(client.poll_calls) == 4
        # All polls address the SAME agentInstanceId that invoke_agent returned
        assert all(pid == client.invoke_agent_instance_id for pid in client.poll_calls)

    def test_no_send_message_call_ever(self) -> None:
        """invoke_and_wait uses invoke_agent, not send_message."""
        client = StubAgenticApiClient(
            status_sequence=["COMPLETED"],
            terminal_payload={},
        )
        invoke_and_wait("db-modernization-collector", "x", client=client, poll_interval=FAST_POLL)
        assert client.send_calls == []  # send_message never called

    def test_agent_type_override(self) -> None:
        client = StubAgenticApiClient(
            status_sequence=["COMPLETED"],
            terminal_payload={"ok": True},
        )
        invoke_and_wait(
            "db-modernization-orchestrator",
            "x",
            agent_type="ORCHESTRATOR_AGENT",
            client=client,
            poll_interval=FAST_POLL,
        )
        assert client.invoke_calls[0]["agentType"] == "ORCHESTRATOR_AGENT"

    def test_default_agent_type_is_sub_agent(self) -> None:
        client = StubAgenticApiClient(
            status_sequence=["COMPLETED"],
            terminal_payload={},
        )
        invoke_and_wait("db-modernization-triage", "x", client=client, poll_interval=FAST_POLL)
        assert client.invoke_calls[0]["agentType"] == "SUB_AGENT"

    def test_request_context_threaded_to_invoke_and_poll(self) -> None:
        ctx = {"workspaceId": "ws-1", "jobId": "j-1", "authorizationToken": "tok"}
        client = StubAgenticApiClient(
            status_sequence=["COMPLETED"],
            terminal_payload={},
        )
        invoke_and_wait(
            "db-modernization-triage",
            "x",
            client=client,
            request_context=ctx,
            poll_interval=FAST_POLL,
        )
        assert client.invoke_calls[0]["requestContext"] == ctx


class TestInvokeAndWaitFailurePaths:
    """invoke_agent errors, missing instance id, subagent FAILED, timeout."""

    def test_invoke_agent_raises_wrapped_as_a2a_error(self) -> None:
        client = StubAgenticApiClient(invoke_side_effect=RuntimeError("boom"))
        with pytest.raises(
            A2AError, match="invoke_agent failed for agent_id='db-modernization-triage'"
        ):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )

    def test_invoke_agent_missing_instance_id_raises(self) -> None:
        client = StubAgenticApiClient(invoke_agent_instance_id="")
        with pytest.raises(A2AError, match="did not return agentInstanceId"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )

    def test_failed_status_raises_with_reason(self) -> None:
        client = StubAgenticApiClient(
            status_sequence=["FAILED"],
            failure_reason="downstream error",
        )
        with pytest.raises(A2AFailedError, match="downstream error"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )

    def test_failed_status_without_reason(self) -> None:
        client = StubAgenticApiClient(status_sequence=["FAILED"], failure_reason="")
        with pytest.raises(A2AFailedError, match="<no reason>"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )

    def test_timeout_when_status_never_terminal(self) -> None:
        client = StubAgenticApiClient(status_sequence=["RUNNING"])  # repeats forever
        with pytest.raises(A2ATimeoutError, match="did not reach terminal status"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                timeout=0.05,
                poll_interval=FAST_POLL,
            )

    def test_get_agent_instance_error_wrapped_as_a2a_error(self) -> None:
        class BrokenClient(StubAgenticApiClient):
            def get_agent_instance(self, **_kw):
                raise RuntimeError("network broken")

        client = BrokenClient(status_sequence=["COMPLETED"])
        with pytest.raises(A2AError, match="get_agent_instance failed"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )


class TestInvokeAndWaitPayloadParsing:
    """Payload validation edge cases (missing / malformed)."""

    def test_missing_payload_raises(self) -> None:
        class SparseClient(StubAgenticApiClient):
            def get_agent_instance(self, **_kw):
                return {"agentInstanceStatus": "COMPLETED", "agentOutput": {}}

        client = SparseClient()
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

        client = BadJsonClient()
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

        client = ListPayloadClient()
        with pytest.raises(A2APayloadError, match="must be a JSON object"):
            invoke_and_wait(
                "db-modernization-triage",
                "x",
                client=client,
                poll_interval=FAST_POLL,
            )


class TestExtractInstanceId:
    """_extract_instance_id: dict + object accessors."""

    def test_dict_with_key(self) -> None:
        assert _extract_instance_id({"agentInstanceId": "abc"}) == "abc"

    def test_dict_without_key(self) -> None:
        assert _extract_instance_id({}) == ""

    def test_dict_with_none_value(self) -> None:
        assert _extract_instance_id({"agentInstanceId": None}) == ""

    def test_object_with_attr(self) -> None:
        class Resp:
            agentInstanceId = "xyz"

        assert _extract_instance_id(Resp()) == "xyz"

    def test_object_without_attr(self) -> None:
        class Resp:
            pass

        assert _extract_instance_id(Resp()) == ""


class TestStubAgenticApiClientInvokeAgent:
    """Stub-level tests for the new invoke_agent method."""

    def test_records_invoke_call_with_all_params(self) -> None:
        client = StubAgenticApiClient(invoke_agent_instance_id="fake-id-1")
        resp = client.invoke_agent(
            agentId="agent-x",
            agentType="SUB_AGENT",
            inputPayload={"serializedPayload": "hi"},
            requestContext={"workspaceId": "w1"},
        )
        assert resp == {"agentInstanceId": "fake-id-1"}
        assert client.invoke_calls == [
            {
                "agentId": "agent-x",
                "agentType": "SUB_AGENT",
                "inputPayload": {"serializedPayload": "hi"},
                "requestContext": {"workspaceId": "w1"},
            }
        ]

    def test_invoke_side_effect_raises(self) -> None:
        client = StubAgenticApiClient(invoke_side_effect=ValueError("bad"))
        with pytest.raises(ValueError, match="bad"):
            client.invoke_agent(agentId="a", inputPayload={"serializedPayload": ""})

    def test_default_agent_type_when_not_provided(self) -> None:
        client = StubAgenticApiClient()
        client.invoke_agent(agentId="a", inputPayload={"serializedPayload": ""})
        assert client.invoke_calls[0]["agentType"] == "SUB_AGENT"
