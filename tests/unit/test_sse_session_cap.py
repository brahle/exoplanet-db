"""The process-wide cap on concurrent MCP SSE sessions (per-IP caps live at the Traefik edge)."""

from __future__ import annotations

import asyncio

import pytest

from exoplanet_db.api.app import app, sse_sessions
from exoplanet_db.api.sse_cap import SSESessionCap


class HeldStream:
    """An inner ASGI app whose GET responses stay open until released, like an SSE stream."""

    def __init__(self):
        self.release = asyncio.Event()
        self.started = 0

    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if scope["method"] == "GET":
            self.started += 1
            await self.release.wait()
        await send({"type": "http.response.body", "body": b""})


async def call(asgi, method="GET", path="/sse"):
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": path, "headers": []}
    await asgi(scope, receive, send)
    return sent


def status_of(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def test_sessions_beyond_the_cap_get_a_503_with_retry_after_until_one_closes():
    inner = HeldStream()
    cap = SSESessionCap(max_sessions=2)
    wrapped = cap.wrap(inner)
    held = [asyncio.create_task(call(wrapped)) for _ in range(2)]
    while inner.started < 2:
        await asyncio.sleep(0)
    assert cap.active == 2

    refused = await call(wrapped)
    assert status_of(refused) == 503
    headers = dict(next(m for m in refused if m["type"] == "http.response.start")["headers"])
    assert headers[b"retry-after"] == b"30"
    assert inner.started == 2  # the refused session never reached the MCP app

    inner.release.set()
    await asyncio.gather(*held)
    assert cap.active == 0
    inner.release.clear()
    reopened = asyncio.create_task(call(wrapped))
    while inner.started < 3:
        await asyncio.sleep(0)
    assert cap.active == 1
    inner.release.set()
    assert status_of(await reopened) == 200


async def test_message_posts_are_never_counted_or_refused():
    inner = HeldStream()
    cap = SSESessionCap(max_sessions=1)
    wrapped = cap.wrap(inner)
    held = asyncio.create_task(call(wrapped))
    while inner.started < 1:
        await asyncio.sleep(0)
    assert status_of(await call(wrapped, method="POST", path="/messages/")) == 200
    assert cap.active == 1
    inner.release.set()
    await held


async def test_a_session_that_errors_still_frees_its_slot():
    async def broken(scope, receive, send):
        raise RuntimeError("boom")

    cap = SSESessionCap(max_sessions=1)
    with pytest.raises(RuntimeError):
        await call(cap.wrap(broken))
    assert cap.active == 0


def test_the_cap_rejects_a_nonsense_limit():
    with pytest.raises(ValueError):
        SSESessionCap(max_sessions=0)


def test_both_mcp_mounts_share_one_cap():
    mounts = {route.path: route.app for route in app.routes if route.path in ("/mcp", "/sse")}
    assert set(mounts) == {"/mcp", "/sse"}
    assert all(getattr(m, "cap", None) is sse_sessions for m in mounts.values())
