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
    _extract_reason,
    _extract_status,
    _is_expected_send_error,
    _parse_payload,
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
