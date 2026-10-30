# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cap the size of every request body the API accepts (#723).

The API only takes small JSON bodies (assessment requests, settings, answers, overrides);
collector files go to the artifact store, not through a request body. A larger body is
refused with a 413 before any route, dependency or parser sees it, so no handler can be
made to buffer or scan an unbounded input. ``MODERNIZER_MAX_BODY_BYTES`` overrides the
default of 1 MiB.
"""

from __future__ import annotations

import json
import os

from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_MAX_BODY_BYTES = 1024 * 1024


def max_body_bytes_from_env() -> int:
    raw = os.environ.get("MODERNIZER_MAX_BODY_BYTES", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_BODY_BYTES


class BodySizeLimitMiddleware:
    """Pure ASGI middleware: 413 for a body over ``max_bytes``, by header or by count."""

    def __init__(self, app: ASGIApp, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def _too_large(self, send: Send) -> None:
        body = json.dumps(
            {"detail": f"Request body is larger than {self.max_bytes} bytes."}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                if not value.isdigit() or int(value) > self.max_bytes:
                    await self._too_large(send)
                    return
        # Read the body here, counting, so a chunked body without a length is capped too.
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await self._too_large(send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
