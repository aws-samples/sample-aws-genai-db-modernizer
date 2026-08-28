"""A2A (agent-to-agent) wiring for the AWS Transform PoC.

Provides ``invoke_and_wait`` — the discover-or-spawn, send, and poll primitive
that orchestrator tools use to drive deployed subagents over the AWS Transform
Agentic API.

The pattern:

    1. Discover a pre-provisioned subagent instance by its registered NAME via
       ``client.list_agent_instances(...)``. If none exists, spawn one with
       ``client.invoke_agent(...)`` and wait for it to reach RUNNING/IDLE.
    2. Dispatch the work with ``client.send_message(agentInstanceId, ...)``.
       A ``-32603`` (JSON-RPC "Internal error") after ~25s is NORMAL for
       long-running subagents — the container is still processing, so keep
       polling rather than treating it as a failure.
    3. Poll ``client.get_agent_instance(agentInstanceId)`` until
       ``agentInstanceStatus`` is either ``"COMPLETED"`` or ``"FAILED"``, then
       parse ``agentOutput.serializedPayload`` (JSON string) into a dict.

Testing:
    ``StubAgenticApiClient`` — hand-rolled mock that can be primed with a
    status sequence and terminal payload. Inject via the ``client`` parameter
    of ``invoke_and_wait()``. No moto or heavy fixtures needed.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
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


def invoke_and_wait(
    agent_id: str,
    message: str,
    *,
    timeout: float = 1800.0,
    poll_interval: float = 2.0,
    post_ready_dwell: float = 15.0,
    client: Any = None,
    request_context: dict[str, Any] | None = None,
    tolerate_send_errors: bool = True,
) -> dict[str, Any]:
    """Discover a pre-provisioned subagent BY NAME, send a message, and poll for terminal payload.

    The AWS Transform runtime auto-provisions subagent instances alongside
    the orchestrator when a job is created — provided the orchestrator's
    Registry entry declares the subagents in the ``Agent Dependencies``
    extension. This helper discovers those pre-provisioned instances by
    ``agentId`` (registered agent NAME) and dispatches an A2A message to
    the matching instance.

    This is the primary A2A entry point for orchestrator LLM tools — the LLM
    knows subagent NAMES (from the tool's docstring), never instance IDs.
    Instance ID resolution happens inside this helper via
    ``list_agent_instances``.

    Note: this replaces an earlier (broken) implementation that used
    ``invoke_agent``. The Agentic API's ``invoke_agent`` returns an
    ``agentInstanceId`` but doesn't dispatch to a running container — it's
    only valid for orchestrator/test scenarios where the runtime has
    pre-provisioned instances via the ``agentDependencies`` mechanism.

    Args:
        agent_id: Registered agent name (e.g. ``"db-modernization-triage"``).
            Must appear in the orchestrator's Registry ``agentDependencies``.
        message: JSON-serialised message text that the subagent's
            ``parse_invocation`` in ``subagent_base.py`` recognises.
        timeout: Max seconds to wait for a terminal status. Default 1800s (30 min). LLM-heavy subagents (analysis, schema-design) should pass higher explicit values.
        poll_interval: Seconds between ``get_agent_instance`` polls.
        client: Injectable Agentic API client (mainly for tests). When
            None, resolves ``get_agentic_api_client()`` from the SDK.
        request_context: Optional requestContext dict. When None, resolves
            ``get_agent_context_from_env().to_dict()``; falls back to
            ``{}`` if the SDK env vars aren't set (e.g. local tests).
        tolerate_send_errors: If True, ignore ``-32603`` (JSON-RPC
            "Internal error") on ``send_message``. This is normal for
            long-running subagents.

    Returns:
        Parsed dict from ``agentOutput.serializedPayload``.

    Raises:
        A2AError: ``list_agent_instances`` failed, no matching subagent
            found, or ``send_message`` failed with an unexpected error.
        A2ATimeoutError: Terminal status not reached within ``timeout``.
        A2AFailedError: Subagent reported ``"FAILED"`` status.
        A2APayloadError: ``agentOutput.serializedPayload`` is missing,
            empty, invalid JSON, or not a JSON object.
    """
    client = _resolve_client(client)
    request_context = _resolve_request_context(request_context)
    own_instance_id = request_context.get("agentInstanceId") or ""

    # 1. Check for existing pre-provisioned subagent instance.
    try:
        list_resp = client.list_agent_instances(
            requestContext=request_context,
            agentFilter={"requesterAgentInstanceId": own_instance_id},
        )
        logger.info(
            "A2A list_agent_instances OK: requester=%s returned %d summaries",
            own_instance_id,
            len(_summaries_of(list_resp)),
        )
    except Exception as e:
        logger.exception(
            "A2A list_agent_instances FAILED for agent_id=%s requester=%s",
            agent_id,
            own_instance_id,
        )
        raise A2AError(
            f"list_agent_instances failed while looking for agent_id={agent_id!r}: {e}"
        ) from e

    subagent_instance_id = _find_subagent_by_agent_id(list_resp, agent_id)

    # 2. If no existing instance, spawn one via invoke_agent (SDK canonical pattern
    #    per prompts/test_orchestrator_prompt.md: "first check ... if not, invoke
    #    an instance ... then send a message to it").
    if not subagent_instance_id:
        logger.info(
            "A2A no existing instance for agent_id=%s — spawning via invoke_agent",
            agent_id,
        )
        try:
            invoke_resp = client.invoke_agent(
                agentId=agent_id,
                agentType="SUB_AGENT",
                requestContext=request_context,
            )
        except Exception as e:
            logger.exception("A2A invoke_agent FAILED for agent_id=%s", agent_id)
            raise A2AError(f"invoke_agent failed for agent_id={agent_id!r}: {e}") from e

        subagent_instance_id = _extract_instance_id(invoke_resp)
        if not subagent_instance_id:
            logger.error(
                "A2A invoke_agent returned no agentInstanceId for agent_id=%s: response=%r",
                agent_id,
                invoke_resp,
            )
            raise A2AError(
                f"invoke_agent for agent_id={agent_id!r} did not return "
                f"agentInstanceId; response={invoke_resp!r}"
            )
        logger.info(
            "A2A spawned new instance: agent_id=%s instance=%s",
            agent_id,
            subagent_instance_id,
        )

        # 2b. Wait for the freshly-spawned instance to reach RUNNING/IDLE state
        #     before sending. SendMessage returns ValidationException
        #     "Agent instance status is not RUNNING or IDLE" if called too
        #     soon after invoke_agent (per Agentic API validation).
        _wait_for_ready(
            client,
            subagent_instance_id,
            request_context,
            agent_id=agent_id,
            timeout=60.0,
            poll_interval=poll_interval,
        )
        # 2c. Additional dwell — the Agentic API reports RUNNING as soon as
        #     the container slot is allocated, BEFORE the app inside finishes
        #     booting (Python import + Uvicorn on port 8080). Empirically the
        #     Uvicorn "listening" log appears ~4-5 seconds after the RUNNING
        #     status transition. Without this dwell, send_message succeeds
        #     but AgentCore then can't route the actual HTTP call to the
        #     not-yet-listening container, and the instance transitions
        #     RUNNING → FAILED.
        _dwell_after_ready(post_ready_dwell)
    else:
        logger.info(
            "A2A found existing subagent: agent_id=%s instance=%s (requester=%s)",
            agent_id,
            subagent_instance_id,
            own_instance_id,
        )

    # 3. Dispatch the A2A message with the SDK's canonical envelope.
    # Match send_message_tools.py exactly — include metadata + extensions
    # so the SDK's message router doesn't strip anything unexpectedly.
    a2a_source_ext = "ATX_A2A.SourceInformation"
    a2a_message = {
        "role": "agent",
        "parts": [{"kind": "text", "text": message}],
        "messageId": str(uuid.uuid4()),
        "kind": "message",
        "metadata": {a2a_source_ext: {"senderAgentInstanceId": own_instance_id}},
        "extensions": [a2a_source_ext],
    }
    try:
        client.send_message(
            agentInstanceId=subagent_instance_id,
            params={"message": a2a_message},
            requestContext=request_context,
        )
        logger.info(
            "A2A send_message OK: agent_id=%s instance=%s",
            agent_id,
            subagent_instance_id,
        )
    except Exception as e:
        if tolerate_send_errors and _is_expected_send_error(e):
            logger.info(
                "send_message to %s (instance=%s) returned expected -32603 "
                "(long-running subagent, continuing to poll): %s",
                agent_id,
                subagent_instance_id,
                e,
            )
        else:
            logger.exception(
                "A2A send_message FAILED: agent_id=%s instance=%s",
                agent_id,
                subagent_instance_id,
            )
            raise A2AError(
                f"send_message to {agent_id} (instance={subagent_instance_id}) failed: {e}"
            ) from e

    # 3. Poll get_agent_instance until COMPLETED or FAILED.
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed > timeout:
            raise A2ATimeoutError(
                f"Subagent {agent_id} (instance={subagent_instance_id}) "
                f"did not reach terminal status within {timeout}s (waited {elapsed:.1f}s)"
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
                f"Subagent {agent_id} (instance={subagent_instance_id}) "
                f"FAILED: {reason or '<no reason>'}"
            )

        logger.debug(
            "Subagent %s (instance=%s) status=%s, waited %.1fs — continuing to poll",
            agent_id,
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


def _extract_instance_id(response: Any) -> str:
    """Get ``agentInstanceId`` from ``invoke_agent`` response (dict or object)."""
    if isinstance(response, dict):
        return str(response.get("agentInstanceId", "") or "")
    return str(getattr(response, "agentInstanceId", "") or "")


# States acceptable for send_message dispatch.
_READY_STATUSES = frozenset({"RUNNING", "IDLE"})


def _dwell_after_ready(dwell_seconds: float) -> None:
    """Block for ``dwell_seconds`` after subagent status becomes RUNNING/IDLE.

    Empirically necessary — see A16.9 discovery: AgentCore reports RUNNING
    when the container slot is allocated, but the Python app inside takes
    another ~4-5 seconds to import + start Uvicorn. Sending too early
    causes the instance to transition RUNNING → FAILED because AgentCore
    can't route the HTTP call to the not-yet-listening container.
    """
    if dwell_seconds > 0:
        logger.info(
            "A2A dwelling %.1fs after RUNNING to let subagent app finish booting",
            dwell_seconds,
        )
        time.sleep(dwell_seconds)


def _wait_for_ready(
    client: Any,
    instance_id: str,
    request_context: dict[str, Any],
    *,
    agent_id: str,
    timeout: float = 60.0,
    poll_interval: float = 2.0,
) -> None:
    """Poll get_agent_instance until the subagent reaches RUNNING or IDLE.

    Required after invoke_agent — the just-spawned instance starts in
    STARTING state, and send_message returns ValidationException
    "Agent instance status is not RUNNING or IDLE" until the container
    finishes boot + handshake.

    Raises:
        A2AError: instance never reached READY within ``timeout``.
    """
    start = time.monotonic()
    last_status = ""
    while True:
        elapsed = time.monotonic() - start
        if elapsed > timeout:
            raise A2AError(
                f"Subagent {agent_id} (instance={instance_id}) did not reach "
                f"RUNNING/IDLE state within {timeout}s (last status={last_status!r})"
            )
        try:
            resp = client.get_agent_instance(
                agentInstanceId=instance_id,
                requestContext=request_context,
            )
        except Exception as e:
            raise A2AError(f"get_agent_instance failed while waiting for ready: {e}") from e
        status = _extract_status(resp)
        last_status = status
        if status in _READY_STATUSES:
            logger.info(
                "A2A subagent ready: agent_id=%s instance=%s status=%s (waited %.1fs)",
                agent_id,
                instance_id,
                status,
                elapsed,
            )
            return
        if status in _UNUSABLE_STATUSES:
            raise A2AError(
                f"Subagent {agent_id} (instance={instance_id}) reached "
                f"terminal-unusable status {status!r} while waiting to become ready"
            )
        logger.debug(
            "Waiting for %s (instance=%s) to become ready — status=%s, waited %.1fs",
            agent_id,
            instance_id,
            status,
            elapsed,
        )
        time.sleep(poll_interval)


# Terminal / unusable states — a subagent in one of these can't accept new work.
_UNUSABLE_STATUSES = frozenset({"SHUTDOWN", "FAILED", "STOPPED", "STOPPING"})


def _summaries_of(list_response: Any) -> list[dict[str, Any]]:
    """Return the ``agentInstanceSummaries`` list from a ``list_agent_instances`` response."""
    if isinstance(list_response, dict):
        result = list_response.get("agentInstanceSummaries") or []
    else:
        result = getattr(list_response, "agentInstanceSummaries", None) or []
    return list(result)


def _find_subagent_by_agent_id(list_response: Any, agent_id: str) -> str:
    """Return the ``agentInstanceId`` of a matching SUB_AGENT from list_agent_instances.

    Filters ``agentInstanceSummaries`` for entries where:
      * ``agentType == "SUB_AGENT"``
      * ``agentId == agent_id``  (the registered agent NAME)
      * ``agentInstanceStatus`` is NOT in ``_UNUSABLE_STATUSES``

    Returns empty string if no match. Multiple matches → returns first
    (deterministic since Agentic API returns in a stable order per response).
    """
    for summary in _summaries_of(list_response):
        if not isinstance(summary, dict):
            continue
        if summary.get("agentType") != "SUB_AGENT":
            continue
        if summary.get("agentId") != agent_id:
            continue
        status = str(summary.get("agentInstanceStatus", "") or "").upper()
        if status in _UNUSABLE_STATUSES:
            continue
        instance_id = summary.get("agentInstanceId")
        if instance_id:
            return str(instance_id)
    return ""


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
    invoke_side_effect: BaseException | None = None
    invoke_agent_instance_id: str = "stub-instance-abc123"
    list_agent_instances_response: dict[str, Any] = field(default_factory=dict)
    list_side_effect: BaseException | None = None
    send_calls: list[dict[str, Any]] = field(default_factory=list)
    poll_calls: list[str] = field(default_factory=list)
    invoke_calls: list[dict[str, Any]] = field(default_factory=list)
    list_calls: list[dict[str, Any]] = field(default_factory=list)

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

    def invoke_agent(
        self,
        *,
        agentId: str,
        agentType: str = "SUB_AGENT",
        inputPayload: dict[str, Any] | None = None,
        requestContext: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stub of the Agentic API's ``invoke_agent``.

        Note: our production code no longer uses this (see :func:`invoke_and_wait`
        which uses ``list_agent_instances`` + ``send_message``). Retained on the
        stub for backward compatibility with older tests and diagnostic scripts.
        """
        self.invoke_calls.append(
            {
                "agentId": agentId,
                "agentType": agentType,
                "inputPayload": inputPayload,
                "requestContext": requestContext,
            }
        )
        if self.invoke_side_effect is not None:
            raise self.invoke_side_effect
        return {"agentInstanceId": self.invoke_agent_instance_id}

    def list_agent_instances(
        self,
        *,
        requestContext: dict[str, Any] | None = None,
        agentFilter: dict[str, Any] | None = None,
        maxResults: int | None = None,
        nextToken: str | None = None,
    ) -> dict[str, Any]:
        """Stub of the Agentic API's ``list_agent_instances``.

        Returns ``list_agent_instances_response`` (typically shaped like
        ``{"agentInstanceSummaries": [{"agentInstanceId","agentId","agentType",
        "agentInstanceStatus"}, ...]}``). Tests configure this to simulate
        pre-provisioned subagent instances.
        """
        self.list_calls.append(
            {
                "requestContext": requestContext,
                "agentFilter": agentFilter,
                "maxResults": maxResults,
                "nextToken": nextToken,
            }
        )
        if self.list_side_effect is not None:
            raise self.list_side_effect
        return dict(self.list_agent_instances_response)

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
