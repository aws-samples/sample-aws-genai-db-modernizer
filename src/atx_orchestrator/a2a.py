"""A2A (agent-to-agent) wiring for the AWS Transform PoC.

Provides the fire-and-forget + poll primitive used by orchestrator tools to
invoke deployed subagents over the AWS Transform Agentic API.

The pattern (per Esteban's handoff §7):

    1. Call ``client.send_message(agentInstanceId, params={"message": ...})``.
       Expected to return error ``-32603`` (JSON-RPC "Internal error") after
       ~25s for long-running subagents. This is NORMAL — the subagent is
       still processing on the server. Do not treat it as failure.
    2. Poll ``client.get_agent_instance(agentInstanceId)`` until
       ``agentInstanceStatus`` is either ``"COMPLETED"`` or ``"FAILED"``.
    3. Parse ``agentOutput.serializedPayload`` (JSON string) into a dict and
       return.

Testing:
    ``StubAgenticApiClient`` — hand-rolled mock that can be primed with a
    status sequence and terminal payload. Inject via the ``client`` parameter
    of ``send_and_wait()``. No moto or heavy fixtures needed.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# Exceptions


class A2AError(Exception):
    """Base class for A2A wiring errors."""


class A2ATimeoutError(A2AError):
    """Polling exceeded the timeout without reaching a terminal status."""


class A2AFailedError(A2AError):
    """Subagent reported FAILED status."""


class A2APayloadError(A2AError):
    """agentOutput.serializedPayload could not be parsed as expected JSON."""


# =============================================================================
# Constants

# The JSON-RPC error code returned by SendMessage on long-running subagents.
# Per handoff §7: "expect A2A SendMessage to return error -32603 after ~25s
# (normal — keep going)".
JSONRPC_INTERNAL_ERROR = -32603

# Statuses considered terminal (loop exits).
TERMINAL_STATUSES: frozenset[str] = frozenset({"COMPLETED", "FAILED"})


# =============================================================================
# Primitive


def send_and_wait(
    subagent_instance_id: str,
    message: str,
    *,
    timeout: float = 300.0,
    poll_interval: float = 2.0,
    client: Any = None,
    request_context: dict[str, Any] | None = None,
    tolerate_send_errors: bool = True,
) -> dict[str, Any]:
    """Send an A2A message to a subagent and poll for its terminal payload.

    Args:
        subagent_instance_id: agentInstanceId of the deployed subagent.
        message: Natural-language message text (typically a JSON blob that
            the subagent's ``parse_invocation`` in ``subagent_base.py``
            recognises).
        timeout: Max seconds to wait for a terminal status. Default 300s.
        poll_interval: Seconds between ``get_agent_instance`` polls.
        client: Injectable Agentic API client (mainly for tests). When
            None, resolves ``get_agentic_api_client()`` from the SDK.
        request_context: Optional requestContext dict. When None, resolves
            ``get_agent_context_from_env().to_dict()``; falls back to
            ``{}`` if the SDK env vars aren't set (e.g. local tests).
        tolerate_send_errors: If True, ignore ``-32603`` (JSON-RPC
            "Internal error") on send. This is normal for long-running
            subagents. Any other send error surfaces immediately.

    Returns:
        Parsed dict from ``agentOutput.serializedPayload``.

    Raises:
        A2ATimeoutError: Terminal status not reached within ``timeout``.
        A2AFailedError: Subagent reported ``"FAILED"`` status.
        A2APayloadError: ``agentOutput.serializedPayload`` is missing,
            empty, invalid JSON, or not a JSON object.
        A2AError: Any other send/poll failure.
    """
    client = _resolve_client(client)
    request_context = _resolve_request_context(request_context)

    # 1. Fire-and-forget send. Tolerate the expected -32603 error.
    _send(client, subagent_instance_id, message, request_context, tolerate_send_errors)

    # 2. Poll get_agent_instance until COMPLETED or FAILED.
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed > timeout:
            raise A2ATimeoutError(
                f"Subagent {subagent_instance_id} did not reach terminal status "
                f"within {timeout}s (waited {elapsed:.1f}s)"
            )

        try:
            resp = client.get_agent_instance(
                agentInstanceId=subagent_instance_id,
                requestContext=request_context,
            )
        except Exception as e:
            raise A2AError(f"get_agent_instance failed: {e}") from e

        status = _extract_status(resp)
        if status == "COMPLETED":
            return _parse_payload(resp)
        if status == "FAILED":
            reason = _extract_reason(resp)
            raise A2AFailedError(
                f"Subagent {subagent_instance_id} FAILED: {reason or '<no reason>'}"
            )

        # Any other status is intermediate (STARTING, RUNNING, etc.).
        logger.debug(
            "Subagent %s status=%s, waited %.1fs — continuing to poll",
            subagent_instance_id,
            status,
            elapsed,
        )
        time.sleep(poll_interval)


# =============================================================================
# Internal helpers (extracted for readability + focused testing)


def _resolve_client(client: Any) -> Any:
    """Resolve the Agentic API client — SDK by default, injected in tests."""
    if client is not None:
        return client
    from agent_builder_sdk.agentic_framework.client_factory import (  # noqa: PLC0415
        get_agentic_api_client,
    )

    return get_agentic_api_client()


def _resolve_request_context(context: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve requestContext from env when not provided; safe outside ATX runtime."""
    if context is not None:
        return context
    try:
        from agent_builder_sdk.env_var import get_agent_context_from_env  # noqa: PLC0415

        return dict(get_agent_context_from_env().to_dict())
    except Exception as exc:
        logger.debug("Falling back to empty request_context (no ATX env): %s", exc)
        return {}


def _send(
    client: Any,
    subagent_instance_id: str,
    message: str,
    request_context: dict[str, Any],
    tolerate_send_errors: bool,
) -> None:
    """Fire-and-forget send. Tolerates -32603 by default."""
    try:
        client.send_message(
            agentInstanceId=subagent_instance_id,
            params={
                "message": {
                    "role": "agent",
                    "parts": [{"kind": "text", "text": message}],
                }
            },
            requestContext=request_context,
        )
    except Exception as e:
        if tolerate_send_errors and _is_expected_send_error(e):
            logger.info(
                "send_message returned expected -32603 (long-running subagent, "
                "continuing to poll): %s",
                e,
            )
            return
        raise A2AError(f"send_message failed: {e}") from e


def _is_expected_send_error(err: BaseException) -> bool:
    """Detect the -32603 error that's expected for long-running subagents.

    Handles boto3-style ClientError responses, JSON-RPC dict payloads, and
    generic exceptions that mention the code in the message.
    """
    response = getattr(err, "response", None)
    if isinstance(response, dict):
        error_code = response.get("Error", {}).get("Code")
        if str(error_code) == str(JSONRPC_INTERNAL_ERROR):
            return True
    msg = str(err).lower()
    return "-32603" in msg or "internal error" in msg


def _extract_status(response: Any) -> str:
    """Get ``agentInstanceStatus`` from response (dict or object)."""
    if isinstance(response, dict):
        return str(response.get("agentInstanceStatus", "")).upper()
    val = getattr(response, "agentInstanceStatus", None)
    return str(val or "").upper()


def _extract_reason(response: Any) -> str:
    """Get ``statusReason`` from response (dict or object)."""
    if isinstance(response, dict):
        return str(response.get("statusReason", "") or "")
    return str(getattr(response, "statusReason", "") or "")


def _parse_payload(response: Any) -> dict[str, Any]:
    """Extract and parse ``agentOutput.serializedPayload`` as a JSON object."""
    if isinstance(response, dict):
        agent_output = response.get("agentOutput") or {}
    else:
        agent_output = getattr(response, "agentOutput", None) or {}

    if isinstance(agent_output, dict):
        serialized = agent_output.get("serializedPayload")
    else:
        serialized = getattr(agent_output, "serializedPayload", None)

    if not serialized:
        raise A2APayloadError(
            f"agentOutput.serializedPayload is missing or empty: {agent_output!r}"
        )

    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as e:
        raise A2APayloadError(f"agentOutput.serializedPayload is not valid JSON: {e}") from e

    if not isinstance(payload, dict):
        raise A2APayloadError(
            f"agentOutput.serializedPayload must be a JSON object, " f"got {type(payload).__name__}"
        )

    return payload


# =============================================================================
# Local testing stub


@dataclass
class StubAgenticApiClient:
    """Hand-rolled mock of the Agentic API client for local testing.

    Configure with:
        status_sequence: Statuses to return from successive
            ``get_agent_instance()`` calls. The last item is the terminal
            status; earlier ones are intermediate. If callers poll more
            times than the sequence has items, the last status is
            repeated (useful for timeout tests where all polls should
            return "RUNNING").
        terminal_payload: Dict to serialize as
            ``agentOutput.serializedPayload`` when the terminal status
            is ``"COMPLETED"``. Ignored for other terminal statuses.
        failure_reason: ``statusReason`` to attach when terminal status
            is ``"FAILED"``.
        send_side_effect: Optional exception to raise from
            ``send_message()``. Used to simulate the -32603 tolerance
            path and unrelated-error surface path.
        send_calls / poll_calls: Recording lists; assert on these to
            verify what the primitive did.
    """

    status_sequence: list[str] = field(default_factory=lambda: ["COMPLETED"])
    terminal_payload: dict[str, Any] = field(default_factory=dict)
    failure_reason: str = ""
    send_side_effect: BaseException | None = None
    send_calls: list[dict[str, Any]] = field(default_factory=list)
    poll_calls: list[str] = field(default_factory=list)

    def send_message(
        self,
        *,
        agentInstanceId: str,
        params: dict[str, Any],
        requestContext: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.send_calls.append(
            {
                "agentInstanceId": agentInstanceId,
                "params": params,
                "requestContext": requestContext,
            }
        )
        if self.send_side_effect is not None:
            raise self.send_side_effect
        return {"messageId": "stub-msg-1"}

    def get_agent_instance(
        self,
        *,
        agentInstanceId: str,
        requestContext: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.poll_calls.append(agentInstanceId)
        idx = min(len(self.poll_calls) - 1, len(self.status_sequence) - 1)
        status = self.status_sequence[idx]
        response: dict[str, Any] = {
            "agentInstanceId": agentInstanceId,
            "agentInstanceStatus": status,
        }
        if status == "COMPLETED":
            response["agentOutput"] = {"serializedPayload": json.dumps(self.terminal_payload)}
        elif status == "FAILED":
            response["statusReason"] = self.failure_reason
        return response
