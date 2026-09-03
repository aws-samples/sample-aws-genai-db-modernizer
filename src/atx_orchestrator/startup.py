"""Proactive job-start operations for the AWS Transform orchestrator.

The ATX platform invokes the orchestrator container when a job is created, but
it does NOT synthesize a first turn — the chat stays empty until the customer
types. That is confusing: the customer has no idea what to provide.

This module sends a proactive welcome message to the WebApp chat at job start so
the customer sees what to do before typing anything. It mirrors the "Opening
turn" guidance in ``orchestrator.SYSTEM_PROMPT`` (run the read-only collection
script, upload the resulting JSON, tell us the database name) in plain language,
with a few starter suggestions.

Why a separate proactive send rather than the ``objectiveNegotiationPrompt``
registry field: that field drives *objective validation* (is the customer's
stated objective in-domain), not an opening chat message. It never produces a
greeting, which is why the chat was empty even though the field was published.

The mechanism is the ATX A2A chat channel: send an ``role="agent"`` message to
the reserved ``ATX_CHAT`` recipient via the Agentic API client. This is the same
``client.send_message`` primitive ``a2a.py`` uses to dispatch to subagents; the
envelope and request-context helpers are reused from there. Outside the ATX
runtime (local Mac, unit tests) the client/context resolution degrades to a
no-op so callers never have to guard.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Reserved recipient id for the WebApp chat channel. Sending an ``agent`` message
# here renders it as an assistant turn in the customer's chat.
ATX_CHAT_AGENT_ID = "ATX_CHAT"

# A2A extension key the WebApp reads to render clickable starter chips under the
# message. A plain list of strings, matching send_message_tools / the
# MigrationAssessment agent's ChatSuggestions usage.
CHAT_SUGGESTIONS_EXT = "ATX_A2A.ChatSuggestions"

WELCOME_MESSAGE = """\
Welcome! I'm the AWS Database Modernization assistant. I help you find the \
best-fit AWS-native databases for an existing relational workload and produce a \
detailed migration plan.

To get started, there are three quick steps:

1. Run the read-only collection script for your source engine against your \
database to produce a collection file. The scripts ship with this project and \
only need SELECT access — they do not modify your database:
   - PostgreSQL: `psql ... -f scripts/collect-postgresql.sql > my-collection.json`
   - MySQL: `mysql ... < scripts/collect-mysql.sql > my-collection.json`
2. Upload the resulting JSON file to this job's file uploads (the paperclip / \
Add files control).
3. Tell me the name of the database you collected.

When you're ready, reply with something like "I've uploaded my collection, the \
database name is orders" and I'll take it from there."""

# Starter chips shown under the welcome message.
DEFAULT_SUGGESTIONS = [
    "I've uploaded my collection file",
    "Which collection script do I run?",
    "What does the assessment produce?",
]


def _build_welcome_a2a_message(message: str, suggestions: list[str] | None) -> dict[str, Any]:
    """Build the A2A envelope for a proactive assistant chat message.

    Matches the shape ``a2a.py`` sends to subagents (``role="agent"``,
    ``kind="message"``, a ``text`` part, a fresh ``messageId`` and ``contextId``),
    plus the ChatSuggestions extension so the WebApp renders starter chips.
    """
    msg: dict[str, Any] = {
        "role": "agent",
        "parts": [{"kind": "text", "text": message}],
        "messageId": str(uuid.uuid4()),
        "contextId": str(uuid.uuid4()),
        "kind": "message",
        "metadata": {},
    }
    if suggestions:
        msg["metadata"][CHAT_SUGGESTIONS_EXT] = list(suggestions)
        msg["extensions"] = [CHAT_SUGGESTIONS_EXT]
    return msg


def send_welcome_message(
    *,
    message: str = WELCOME_MESSAGE,
    suggestions: list[str] | None = None,
    client: Any = None,
    request_context: dict[str, Any] | None = None,
) -> bool:
    """Send the proactive welcome message to the WebApp chat.

    Reuses ``a2a._resolve_client`` / ``a2a._resolve_request_context`` so the SDK
    Agentic API client and requestContext are resolved from the runtime env, and
    both degrade safely outside the ATX runtime (local/tests) — in which case
    this returns ``False`` without raising.

    Fail-open: any error is logged and swallowed. A missing welcome must never
    block job start.

    Args:
        message: Message text (defaults to :data:`WELCOME_MESSAGE`).
        suggestions: Starter chips (defaults to :data:`DEFAULT_SUGGESTIONS`).
        client: Injectable Agentic API client (for tests). When None, resolves
            the SDK client via ``a2a._resolve_client``.
        request_context: Injectable requestContext (for tests). When None,
            resolves from the ATX env via ``a2a._resolve_request_context``.

    Returns:
        True if the message was sent, False if it was skipped or failed.
    """
    from src.atx_orchestrator.a2a import _resolve_client, _resolve_request_context

    if suggestions is None:
        suggestions = DEFAULT_SUGGESTIONS

    try:
        resolved_client = _resolve_client(client)
    except Exception as exc:  # noqa: BLE001
        # Outside the ATX runtime (no SDK / no env) — nothing to send to.
        logger.info("Skipping welcome message: no Agentic API client available: %s", exc)
        return False

    resolved_context = _resolve_request_context(request_context)
    a2a_message = _build_welcome_a2a_message(message, suggestions)

    try:
        resolved_client.send_message(
            agentInstanceId=ATX_CHAT_AGENT_ID,
            params={"message": a2a_message},
            requestContext=resolved_context,
        )
        logger.info(
            "Welcome message sent to %s with %d suggestion(s)",
            ATX_CHAT_AGENT_ID,
            len(suggestions),
        )
        return True
    except Exception:  # noqa: BLE001
        # Fail-open: a failed greeting must not block the job from starting.
        logger.warning("Failed to send welcome message to %s", ATX_CHAT_AGENT_ID, exc_info=True)
        return False
