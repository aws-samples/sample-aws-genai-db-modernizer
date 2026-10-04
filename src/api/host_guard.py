"""Host-header allowlist for the local API.

The local API listens on the loopback interface. A Host-header allowlist stops
a browser page served from another name that resolves to 127.0.0.1 from
talking to it. Defaults to localhost, 127.0.0.1 and ::1; set
MODERNIZER_ALLOWED_HOSTS to a comma-separated list to change it, or to ``*``
to turn the check off.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

ALLOWED_HOSTS_ENV = "MODERNIZER_ALLOWED_HOSTS"
DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "::1")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def allowed_hosts_from_env() -> frozenset[str] | None:
    """The configured host names, or None when the check is turned off."""
    raw = os.environ.get(ALLOWED_HOSTS_ENV)
    if raw is None:
        return frozenset(DEFAULT_ALLOWED_HOSTS)
    if raw.strip() == "*":
        return None
    return frozenset(_normalise(h) for h in raw.split(",") if h.strip())


def _normalise(host: str) -> str:
    return host.strip().lower().strip("[]").rstrip(".")


def host_name(header: str) -> str:
    """The host name from a Host header value, without port or IPv6 brackets."""
    value = header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else ""
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


class HostAllowlistMiddleware:
    """Reject HTTP and WebSocket requests whose Host header is not allowed."""

    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]):
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        header = ""
        for key, value in scope.get("headers", []):
            if key == b"host":
                header = value.decode("latin-1")
                break
        if host_name(header) in self.allowed_hosts:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b"Invalid host header."
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
