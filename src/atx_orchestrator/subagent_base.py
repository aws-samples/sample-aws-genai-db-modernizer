"""Shared subagent scaffolding for AWS Transform.

Each existing AGENT_TYPE becomes its own deployable subagent (one agent per
image). They all share the same A2A message handling, status management, and
local-mode fallback — only the work function differs.

CRITICAL SDK patterns (see steering/subagent-patterns.md):
  - The AsyncBaseSubagent subclass MUST be created inside a factory function
    (module-level subclasses hang in production containers).
  - process_message_async extracts the A2A text, runs the work, and MANUALLY
    sets COMPLETED/FAILED via the agent instance manager.
  - Use AgentRuntimeServer with delayed_timeout, never the stateless server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable

logger = logging.getLogger(__name__)


def extract_text(message) -> str:
    """Extract user text from a ProcessMessageRequest or A2A dict/str.

    Handles multiple delivery shapes empirically observed:
    - ``ProcessMessageRequest.message`` is a raw string with our text → return it
    - ``ProcessMessageRequest.message`` is a JSON string of a list like
      ``[{"text": "<inner>", "type": "text"}]`` — the SDK's serialized form of
      an A2A message's ``parts`` array. Unwrap and return the inner ``text``.
    - ``message`` (or ``.message``) is a dict with ``parts=[{"text": ..., "kind": "text"}]``
      — original A2A form. Return ``parts[0]["text"]``.
    - Anything else → ``str(msg)``.
    """
    # DIAG A16.10: log the RAW incoming message to see what the SDK actually
    # delivers when the orchestrator's invoke_and_wait -> send_message flow
    # is used. Test-8 + test-9 showed parse_invocation gets empty job_id
    # despite our envelope having the right text.
    logger.info("DIAG extract_text got message type=%s repr=%r", type(message).__name__, message)
    msg = getattr(message, "message", message)
    logger.info("DIAG after getattr .message: type=%s repr=%r", type(msg).__name__, msg)

    if isinstance(msg, str):
        # SDK sometimes delivers the parts array as a JSON string. Try to
        # unwrap `[{"text": "<payload>", "type": "text"}]` shape.
        try:
            parsed = json.loads(msg)
            if isinstance(parsed, list) and parsed:
                first = parsed[0]
                if isinstance(first, dict) and "text" in first:
                    inner = str(first["text"])
                    logger.info(
                        "DIAG unwrapped JSON-list-string shape → inner text=%r",
                        inner[:500],
                    )
                    return inner
        except json.JSONDecodeError:
            pass
        logger.info("DIAG non-JSON string, returning msg[:500]=%r", msg[:500])
        return msg

    if isinstance(msg, dict):
        parts = msg.get("parts") or [{}]
        first = parts[0] if parts else {}
        text = first.get("text", json.dumps(msg))
        logger.info("DIAG dict path: parts=%r first=%r text=%r", parts, first, text)
        return str(text)

    logger.info("DIAG fallback str(msg)=%r", str(msg)[:500])
    return str(msg)


def parse_invocation(text: str) -> dict:
    """Parse an invocation payload from text.

    Accepts a JSON object embedded in the text, or 'key: value' / 'key=value' lines.

    Guarantees ``job_id``, ``database_name`` and ``input_key`` are present as
    stripped strings, because the wrapper validates the first two and every
    subagent reads them.

    **Every other key in the JSON is preserved as sent.** Until 2026-08-21 this
    returned a hardcoded three-key dict, so any additional parameter was silently
    discarded. referee-synthesis was the first phase to need a fourth
    (``assignment_version``), and the drop surfaced as ``KeyError:
    'assignment_version'`` inside the subagent rather than as a bad payload at the
    caller — which pointed at the wrong side of the contract. Types are preserved
    too, so an int stays an int.
    """
    logger.info("DIAG parse_invocation received text=%r", text[:1000] if text else text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                parsed = dict(data)
                for key in ("job_id", "database_name", "input_key"):
                    parsed[key] = str(data.get(key, "")).strip()
                return parsed
        except json.JSONDecodeError:
            pass

    # Fallback for human- or LLM-typed text rather than a JSON payload.
    result = {"job_id": "", "database_name": "", "input_key": ""}
    for key in result:
        m = re.search(rf"{key}\s*[:=]\s*([^\s,;]+)", text, re.IGNORECASE)
        if m:
            result[key] = m.group(1).strip().strip("\"'")
    # Optional numeric parameters some phases accept. Only set when present, so
    # the consumer's own default still applies when they are absent.
    for key in ("assignment_version",):
        m = re.search(rf"{key}\s*[:=]\s*(\d+)", text, re.IGNORECASE)
        if m:
            result[key] = m.group(1)
    return result


def make_subagent_factory(
    system_prompt: str,
    work_fn: Callable[[dict], dict],
):
    """Build an agent_factory for a subagent whose work is `work_fn`.

    Args:
        system_prompt: The subagent's system prompt.
        work_fn: Callable taking the parsed invocation dict
            ({job_id, database_name, input_key}) and returning a summary dict.
            Raises on failure (the wrapper marks the instance FAILED).

    Returns:
        agent_factory(mcp_client, storage_dir=None) suitable for AgentRuntimeServer.
    """

    def agent_factory(mcp_client, storage_dir=None):
        import os

        from agent_builder_sdk.base_subagent.base_subagent import AsyncBaseSubagent

        class _Subagent(AsyncBaseSubagent):
            async def process_message_async(self, message):  # type: ignore[override]
                text = extract_text(message)
                params = parse_invocation(text)

                # Resolve status manager lazily (None when outside ATX runtime).
                manager = None
                instance_id = None
                try:
                    from agent_builder_sdk.agentic_framework.agent_lifecycle import (
                        get_agent_instance_manager,
                    )

                    manager = get_agent_instance_manager()
                    instance_id = manager.agent_instance_id
                except Exception as exc:  # noqa: BLE001
                    # Expected when running outside the ATX runtime (e.g. local
                    # tests): no agent context env vars. Status updates are skipped.
                    logger.debug("No ATX agent context, running in local mode: %s", exc)

                try:
                    if not params["job_id"] or not params["database_name"]:
                        raise ValueError(
                            "Invocation must include 'job_id' and 'database_name'. "
                            f"Parsed: {params}"
                        )
                    logger.info(
                        "Subagent starting work: job_id=%s database=%s",
                        params.get("job_id"),
                        params.get("database_name"),
                    )
                    # F3 fix (F16): offload the (potentially long) sync work_fn
                    # to a thread pool so it does NOT block Uvicorn's async
                    # event loop. Without this, LLM-heavy work_fn calls
                    # (analysis subagents doing 30-60min of Bedrock work) block
                    # the /ping healthcheck endpoint for minutes, causing
                    # AgentCore to SIGKILL the container as unresponsive.
                    # See claude.md §97 for the full diagnostic + ATX_POC_STATE §F16.
                    summary = await asyncio.to_thread(work_fn, params)
                    logger.info(
                        "Subagent work_fn returned: type=%s keys=%s",
                        type(summary).__name__,
                        list(summary.keys()) if isinstance(summary, dict) else "n/a",
                    )
                    payload = json.dumps({"response": summary})
                    if manager and instance_id:
                        # SDK's manager.update_status() passes agent_output as a
                        # plain string, but the Agentic API's update_agent_instance
                        # requires agentOutput to be a STRUCTURE with a
                        # serializedPayload field. Bypass update_status and call
                        # update_agent_instance directly with the correct shape.
                        try:
                            req = manager._inject_request_context(
                                {
                                    "agentInstanceId": instance_id,
                                    "agentInstanceStatus": "COMPLETED",
                                    "agentOutput": {"serializedPayload": payload},
                                }
                            )
                            manager.client.update_agent_instance(**req)
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "Failed to report COMPLETED status for instance=%s",
                                instance_id,
                            )
                    logger.info("Subagent COMPLETED: %s", summary)
                    return payload
                except Exception as e:  # noqa: BLE001
                    logger.exception("Subagent FAILED")
                    if manager and instance_id:
                        manager.update_status(instance_id, "FAILED", status_reason=str(e)[:1024])
                    raise

        model_id = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
        return _Subagent(
            system_prompt=system_prompt,
            mcp_clients=[mcp_client] if mcp_client is not None else None,
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            model_id=model_id,
        )

    return agent_factory


def run_server(agent_factory, default_storage_dir: str):
    """Start an AgentRuntimeServer for the given factory (shared CLI entry logic)."""
    import argparse

    parser = argparse.ArgumentParser(description="AWS Transform Subagent")
    parser.add_argument("--host", default="0.0.0.0")  # nosec B104
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--storage-dir", default=default_storage_dir)
    parser.add_argument(
        "--binary-location",
        default="/home/amazon/AgentBuilderAgenticMCP/bin/agent-builder-agentic-mcp",
    )
    args = parser.parse_args()

    from agent_builder_sdk.server.agent_runtime_server import AgentRuntimeServer

    server = AgentRuntimeServer(
        agent_factory=agent_factory,
        host=args.host,
        port=args.port,
        binary_location=args.binary_location,
        storage_dir=args.storage_dir,
        delayed_timeout=3600,
    )
    logger.info("Starting subagent on %s:%s", args.host, args.port)
    server.start()
