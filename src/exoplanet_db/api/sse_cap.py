"""A process-wide cap on concurrent MCP SSE sessions.

An SSE session is a GET that stays open for as long as the agent is connected, so a request-rate
limit does not bound how many are open at once. The per-client-IP cap is Traefik's ``inFlightReq``
(see ``deploy/k8s/base/ingressroute.yaml``), because only the edge sees the real client address;
this cap bounds the total across every client, host name and in-cluster caller, so one replica
cannot be held open by an unbounded number of idle streams.

Message POSTs (``.../messages/``) are short and belong to an existing session, so they pass through.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

RETRY_AFTER_SECONDS = 30


class SSESessionCap:
    """Counts open SSE streams across every app it wraps; refuses new ones with 503 at the cap."""

    def __init__(self, max_sessions: int):
        if max_sessions < 1:
            raise ValueError(f"max_sessions must be >= 1, got {max_sessions}")
        self.max_sessions = max_sessions
        self.active = 0

    def wrap(self, app: ASGIApp) -> _Capped:
        return _Capped(app, self)

    async def refuse(self, send: Send) -> None:
        body = json.dumps(
            {"detail": "Too many open MCP sessions on this server; retry shortly."}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(RETRY_AFTER_SECONDS).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _Capped:
    def __init__(self, app: ASGIApp, cap: SSESessionCap):
        self.app = app
        self.cap = cap

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return
        # Single-threaded event loop: the check and the increment cannot interleave.
        if self.cap.active >= self.cap.max_sessions:
            await self.cap.refuse(send)
            return
        self.cap.active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.cap.active -= 1
