"""Tests for the proactive welcome message and its server-lifecycle hook.

Two behaviours are pinned here:

* ``send_welcome_message`` sends an ``role="agent"`` A2A message to the reserved
  ``ATX_CHAT`` recipient with the DB-modernization welcome text and starter
  chips, using injected client/context; and it fails open (returns False, never
  raises) when the client is unavailable or ``send_message`` errors.
* The orchestrator server subclass greets only on the first job start
  (``agent_status == "INVOKED"``) and not on restart/recovery
  (``"RUNNING"``), and a greeting failure never breaks setup.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from src.atx_orchestrator import startup


class _FakeClient:
    """Records send_message calls; optionally raises to exercise fail-open."""

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._raise_exc = raise_exc

    def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise_exc is not None:
            raise self._raise_exc
        return {"messageId": "msg-1"}


# =============================================================================
# send_welcome_message


class TestSendWelcomeMessage:
    def test_sends_agent_message_to_atx_chat(self) -> None:
        client = _FakeClient()

        ok = startup.send_welcome_message(client=client, request_context={"jobId": "job-1"})

        assert ok is True
        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["agentInstanceId"] == startup.ATX_CHAT_AGENT_ID
        assert call["requestContext"] == {"jobId": "job-1"}

        message = call["params"]["message"]
        assert message["role"] == "agent"
        assert message["kind"] == "message"
        assert message["parts"][0]["text"] == startup.WELCOME_MESSAGE
        assert message["messageId"]
        assert message["contextId"]

    def test_includes_default_chat_suggestions(self) -> None:
        client = _FakeClient()

        startup.send_welcome_message(client=client, request_context={})

        message = client.calls[0]["params"]["message"]
        assert message["metadata"][startup.CHAT_SUGGESTIONS_EXT] == startup.DEFAULT_SUGGESTIONS
        assert startup.CHAT_SUGGESTIONS_EXT in message["extensions"]

    def test_custom_message_and_suggestions(self) -> None:
        client = _FakeClient()

        startup.send_welcome_message(
            message="hello",
            suggestions=["a", "b"],
            client=client,
            request_context={},
        )

        message = client.calls[0]["params"]["message"]
        assert message["parts"][0]["text"] == "hello"
        assert message["metadata"][startup.CHAT_SUGGESTIONS_EXT] == ["a", "b"]

    def test_empty_suggestions_omits_extension(self) -> None:
        client = _FakeClient()

        startup.send_welcome_message(suggestions=[], client=client, request_context={})

        message = client.calls[0]["params"]["message"]
        assert startup.CHAT_SUGGESTIONS_EXT not in message["metadata"]
        assert "extensions" not in message

    def test_fail_open_when_send_raises(self) -> None:
        client = _FakeClient(raise_exc=RuntimeError("boom"))

        ok = startup.send_welcome_message(client=client, request_context={})

        assert ok is False  # swallowed, not raised

    def test_returns_false_when_no_client_available(self) -> None:
        # _resolve_client raises outside the ATX runtime (no SDK). Simulate that.
        with patch(
            "src.atx_orchestrator.a2a._resolve_client",
            side_effect=ImportError("no agent_builder_sdk"),
        ):
            ok = startup.send_welcome_message(request_context={})

        assert ok is False


# =============================================================================
# Orchestrator server lifecycle hook


class _FakeBaseServer:
    """Stand-in for the SDK AgentRuntimeServer base.

    Records the status its ``_finalize_agent_setup`` was called with so the
    subclass's super() call can be asserted.
    """

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.finalized_with: list[str] = []

    async def _finalize_agent_setup(self, agent_status: str = "RUNNING") -> None:
        self.finalized_with.append(agent_status)


def _make_server():
    from src.atx_orchestrator.subagents.base import _make_orchestrator_server_class

    cls = _make_orchestrator_server_class(_FakeBaseServer)
    return cls()


class TestOrchestratorServerGreeting:
    def test_greets_on_first_start_invoked(self) -> None:
        server = _make_server()
        with patch("src.atx_orchestrator.startup.send_welcome_message") as mock_send:
            asyncio.run(server._finalize_agent_setup("INVOKED"))

        # base setup ran first, then the greeting fired exactly once
        assert server.finalized_with == ["INVOKED"]
        mock_send.assert_called_once_with()

    def test_no_greeting_on_running_restart(self) -> None:
        server = _make_server()
        with patch("src.atx_orchestrator.startup.send_welcome_message") as mock_send:
            asyncio.run(server._finalize_agent_setup("RUNNING"))

        assert server.finalized_with == ["RUNNING"]
        mock_send.assert_not_called()

    def test_greeting_failure_does_not_break_setup(self) -> None:
        server = _make_server()
        with patch(
            "src.atx_orchestrator.startup.send_welcome_message",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise — greeting is fail-open.
            asyncio.run(server._finalize_agent_setup("INVOKED"))

        assert server.finalized_with == ["INVOKED"]
