"""Unit tests for the HA websocket client.

These tests exercise the client's message multiplexing, auth handshake,
event dispatch, and reconnect/resubscribe logic against a fake in-memory
websocket. No real network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import aiohttp
import pytest

from mylo.ha.ws_client import (
    CommandError,
    ConnectionClosed,
    HaWsClient,
    State,
    _derive_ws_url,
)

# ─── Fake websocket ───────────────────────────────────────────────────────────


class _FakeWS:
    """Stands in for :class:`aiohttp.ClientWebSocketResponse`.

    Test code pushes server-side frames with :meth:`push_server_text` and
    ``push_server_close``. Client sends are captured in ``sent``.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._queue: asyncio.Queue[aiohttp.WSMessage] = asyncio.Queue()
        self.exception: Exception | None = None

    async def send_json(self, data: dict[str, Any]) -> None:
        if self.closed:
            raise ConnectionResetError("fake ws closed")
        self.sent.append(data)

    async def receive_json(self) -> dict[str, Any]:
        msg = await self._queue.get()
        if msg.type is aiohttp.WSMsgType.TEXT:
            import json

            return json.loads(msg.data)
        raise ConnectionResetError("ws closed before json")

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None))

    def push_server_text(self, payload: dict[str, Any]) -> None:
        import json

        self._queue.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(payload), None))

    def push_server_close(self) -> None:
        self._queue.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None))

    # Async iteration used by the read loop.
    def __aiter__(self) -> _FakeWS:
        return self

    async def __anext__(self) -> aiohttp.WSMessage:
        msg = await self._queue.get()
        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
            raise StopAsyncIteration
        return msg

    # Context manager used by ws_connect(...).
    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True


class _FakeSession:
    """Supplies a sequence of :class:`_FakeWS` instances for ws_connect calls."""

    def __init__(self, sockets: list[_FakeWS]) -> None:
        self._sockets = list(sockets)
        self.connect_count = 0
        self.closed = False

    def ws_connect(self, _url: str, **_kwargs: Any) -> _FakeWS:  # returns an async context manager
        if not self._sockets:
            raise RuntimeError("fake session ran out of sockets")
        self.connect_count += 1
        return self._sockets.pop(0)

    async def close(self) -> None:
        self.closed = True


# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _auth_ok(ws: _FakeWS) -> None:
    ws.push_server_text({"type": "auth_required", "ha_version": "2026.4.0"})
    # The client will push auth; nothing to do here — we respond once it arrives.


async def _wait_for_sent(
    ws: _FakeWS, predicate: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    for _ in range(200):
        for msg in ws.sent:
            if predicate(msg):
                return msg
        await asyncio.sleep(0.01)
    raise AssertionError(f"no message matching predicate. sent={ws.sent!r}")


def _make_client(session: _FakeSession, token: str = "t") -> HaWsClient:
    return HaWsClient("http://ha.local:8123", token, session=session)  # type: ignore[arg-type]


# ─── URL derivation ───────────────────────────────────────────────────────────


def test_derive_ws_url_http() -> None:
    assert _derive_ws_url("http://ha.local:8123") == "ws://ha.local:8123/api/websocket"


def test_derive_ws_url_https() -> None:
    assert _derive_ws_url("https://abc.ui.nabu.casa") == "wss://abc.ui.nabu.casa/api/websocket"


def test_derive_ws_url_already_ws() -> None:
    assert _derive_ws_url("wss://x/api/websocket") == "wss://x/api/websocket"


def test_derive_ws_url_bad_scheme() -> None:
    with pytest.raises(ValueError):
        _derive_ws_url("ftp://x")


# ─── Auth ─────────────────────────────────────────────────────────────────────


async def test_auth_ok_then_ready() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = _make_client(session)

    await client.start()
    await _auth_ok(ws)

    sent_auth = await _wait_for_sent(ws, lambda m: m.get("type") == "auth")
    assert sent_auth == {"type": "auth", "access_token": "t"}
    ws.push_server_text({"type": "auth_ok", "ha_version": "2026.4.0"})

    await client.wait_ready(timeout=1.0)
    assert client.state is State.READY

    await client.close()


async def test_auth_invalid_raises_and_does_not_become_ready() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = _make_client(session, token="bad")

    await client.start()
    ws.push_server_text({"type": "auth_required"})
    await _wait_for_sent(ws, lambda m: m.get("type") == "auth")
    ws.push_server_text({"type": "auth_invalid", "message": "nope"})

    with pytest.raises(asyncio.TimeoutError):
        await client.wait_ready(timeout=0.2)
    assert client.state is not State.READY
    await client.close()


# ─── Commands ─────────────────────────────────────────────────────────────────


async def _get_ready(ws: _FakeWS, session: _FakeSession, token: str = "t") -> HaWsClient:
    client = _make_client(session, token)
    await client.start()
    ws.push_server_text({"type": "auth_required"})
    await _wait_for_sent(ws, lambda m: m.get("type") == "auth")
    ws.push_server_text({"type": "auth_ok"})
    await client.wait_ready(timeout=1.0)
    return client


async def test_send_command_resolves_by_id() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = await _get_ready(ws, session)

    async def responder() -> dict[str, Any]:
        return await client.send_command("get_config")

    task = asyncio.create_task(responder())
    sent = await _wait_for_sent(ws, lambda m: m.get("type") == "get_config")
    assert sent["id"] == 1  # first id after auth-in-band frames
    ws.push_server_text(
        {"id": sent["id"], "type": "result", "success": True, "result": {"version": "2026.4.0"}}
    )

    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == {"version": "2026.4.0"}
    await client.close()


async def test_send_command_error_raises() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = await _get_ready(ws, session)

    async def caller() -> Any:
        return await client.send_command("bad")

    task = asyncio.create_task(caller())
    sent = await _wait_for_sent(ws, lambda m: m.get("type") == "bad")
    ws.push_server_text(
        {
            "id": sent["id"],
            "type": "result",
            "success": False,
            "error": {"code": "invalid_format", "message": "bad cmd"},
        }
    )

    with pytest.raises(CommandError) as exc_info:
        await asyncio.wait_for(task, timeout=1.0)
    assert exc_info.value.code == "invalid_format"
    await client.close()


async def test_send_command_before_ready_raises() -> None:
    session = _FakeSession([_FakeWS()])
    client = _make_client(session)
    with pytest.raises(ConnectionClosed):
        await client.send_command("ping")


async def test_multiple_commands_interleave() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = await _get_ready(ws, session)

    t1 = asyncio.create_task(client.send_command("cmd_a"))
    t2 = asyncio.create_task(client.send_command("cmd_b"))

    sent_a = await _wait_for_sent(ws, lambda m: m.get("type") == "cmd_a")
    sent_b = await _wait_for_sent(ws, lambda m: m.get("type") == "cmd_b")
    assert sent_a["id"] != sent_b["id"]

    # Respond out of order.
    ws.push_server_text({"id": sent_b["id"], "type": "result", "success": True, "result": "B"})
    ws.push_server_text({"id": sent_a["id"], "type": "result", "success": True, "result": "A"})

    assert await asyncio.wait_for(t1, timeout=1.0) == "A"
    assert await asyncio.wait_for(t2, timeout=1.0) == "B"
    await client.close()


# ─── Subscriptions ────────────────────────────────────────────────────────────


async def test_subscribe_receives_events() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = await _get_ready(ws, session)

    received: list[dict[str, Any]] = []

    async def cb(event: dict[str, Any]) -> None:
        received.append(event)

    sub_task = asyncio.create_task(client.subscribe_events("state_changed", cb))
    sent = await _wait_for_sent(ws, lambda m: m.get("type") == "subscribe_events")
    assert sent["event_type"] == "state_changed"
    ws.push_server_text({"id": sent["id"], "type": "result", "success": True, "result": None})
    await asyncio.wait_for(sub_task, timeout=1.0)

    ws.push_server_text(
        {
            "id": sent["id"],
            "type": "event",
            "event": {"event_type": "state_changed", "data": {"x": 1}},
        }
    )

    # Give the event loop a tick to dispatch.
    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.01)
    assert received == [{"event_type": "state_changed", "data": {"x": 1}}]
    await client.close()


# ─── Reconnect ────────────────────────────────────────────────────────────────


async def test_reconnect_refires_on_ready_and_resubscribes() -> None:
    ws1 = _FakeWS()
    ws2 = _FakeWS()
    session = _FakeSession([ws1, ws2])
    # Speed up backoff so the test finishes quickly.
    client = _make_client(session)
    client._BACKOFF = (0.0,)  # type: ignore[misc]

    client = _make_client(session)
    client._BACKOFF = (0.0,)  # type: ignore[misc]

    ready_calls: list[int] = []

    async def on_ready() -> None:
        ready_calls.append(1)

    client.on_ready(on_ready)
    await client.start()

    # First session: auth + subscribe.
    ws1.push_server_text({"type": "auth_required"})
    await _wait_for_sent(ws1, lambda m: m.get("type") == "auth")
    ws1.push_server_text({"type": "auth_ok"})
    await client.wait_ready(timeout=1.0)

    received: list[dict[str, Any]] = []

    async def cb(event: dict[str, Any]) -> None:
        received.append(event)

    sub_task = asyncio.create_task(client.subscribe_events("state_changed", cb))
    sent = await _wait_for_sent(ws1, lambda m: m.get("type") == "subscribe_events")
    ws1.push_server_text({"id": sent["id"], "type": "result", "success": True, "result": None})
    await asyncio.wait_for(sub_task, timeout=1.0)

    # Simulate server disconnect — the runner should pick up ws2 next.
    ws1.push_server_close()

    # Second session: auth again, expect a resubscribe.
    for _ in range(200):
        if ws2.sent and ws2.sent[0].get("type") == "auth":
            break
        if any(m.get("type") == "auth_required" for m in ws2.sent):
            break
        await asyncio.sleep(0.01)

    ws2.push_server_text({"type": "auth_required"})
    await _wait_for_sent(ws2, lambda m: m.get("type") == "auth")
    ws2.push_server_text({"type": "auth_ok"})

    resub = await _wait_for_sent(ws2, lambda m: m.get("type") == "subscribe_events")
    ws2.push_server_text({"id": resub["id"], "type": "result", "success": True, "result": None})

    # Events on the new sub id flow to the same callback.
    ws2.push_server_text(
        {
            "id": resub["id"],
            "type": "event",
            "event": {"event_type": "state_changed", "data": {"n": 2}},
        }
    )
    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.01)
    assert received and received[-1]["data"] == {"n": 2}

    assert len(ready_calls) >= 2  # called on initial connect and after reconnect
    await client.close()
