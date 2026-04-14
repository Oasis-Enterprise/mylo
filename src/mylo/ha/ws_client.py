"""Home Assistant websocket client.

Single persistent connection with a small state machine:

    DISCONNECTED → CONNECTING → AUTHING → READY → DISCONNECTED (loop)

Callers ``await wait_ready()`` and then ``send_command(...)`` / ``subscribe(...)``.
On disconnect we reconnect with exponential backoff; outstanding commands
issued against a dead connection fail fast with :class:`ConnectionClosed` so the
caller can retry after the next ``wait_ready()``.

Subscriptions are re-registered automatically on reconnect — the caller holds a
handle and its callback keeps receiving events across reconnects transparently.

HA protocol reference:
    https://developers.home-assistant.io/docs/api/websocket
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import urlparse, urlunparse

import aiohttp

from mylo.logging_setup import get_logger

log = get_logger(__name__)

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class HaError(Exception):
    """Base class for HA client errors."""


class AuthFailed(HaError):
    """Raised when HA rejects the access token."""


class ConnectionClosed(HaError):
    """Raised when a command is issued while the connection isn't ready."""


class CommandError(HaError):
    """Raised when HA returns a non-success result for a command."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class State(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AUTHING = "authing"
    READY = "ready"
    CLOSED = "closed"


@dataclass(slots=True)
class Subscription:
    """Handle to an active event subscription. Opaque to callers."""

    event_type: str | None
    callback: EventCallback
    # Current server-side subscription id; changes across reconnects.
    sub_id: int | None = None
    # Set to True when the caller has called ``unsubscribe()``.
    cancelled: bool = False


@dataclass(slots=True)
class _PendingCommand:
    future: asyncio.Future[dict[str, Any]] = field(
        default_factory=lambda: asyncio.get_event_loop().create_future()
    )


def _derive_ws_url(base_url: str) -> str:
    """Turn ``http(s)://host:port/...`` into ``ws(s)://host:port/api/websocket``."""
    parsed = urlparse(base_url)
    if parsed.scheme in ("ws", "wss"):
        ws_scheme = parsed.scheme
    elif parsed.scheme == "https":
        ws_scheme = "wss"
    elif parsed.scheme == "http":
        ws_scheme = "ws"
    else:
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r} in {base_url!r}")
    return urlunparse((ws_scheme, parsed.netloc, "/api/websocket", "", "", ""))


class HaWsClient:
    """Single-connection websocket client with reconnect.

    Typical usage::

        client = HaWsClient(url, token)
        await client.start()
        await client.wait_ready()
        result = await client.send_command("get_config")
        await client.close()
    """

    # Backoff schedule in seconds; jittered.
    _BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

    def __init__(
        self,
        url: str,
        token: str,
        *,
        session: aiohttp.ClientSession | None = None,
        heartbeat: float = 30.0,
    ) -> None:
        self._ws_url = _derive_ws_url(url)
        self._token = token
        self._session = session
        self._owns_session = session is None
        self._heartbeat = heartbeat

        self._state = State.DISCONNECTED
        self._ready_event = asyncio.Event()
        self._closed_event = asyncio.Event()

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._runner_task: asyncio.Task[None] | None = None

        self._msg_id = 0
        self._pending: dict[int, _PendingCommand] = {}
        self._subs_by_id: dict[int, Subscription] = {}
        self._subs: list[Subscription] = []

        self._on_ready_callbacks: list[Callable[[], Awaitable[None]]] = []

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._runner_task is not None:
            return
        if self._session is None:
            self._session = aiohttp.ClientSession()
        self._runner_task = asyncio.create_task(self._run(), name="mylo.ha.ws")

    async def close(self) -> None:
        self._closed_event.set()
        self._state = State.CLOSED
        self._ready_event.clear()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._runner_task is not None:
            self._runner_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._runner_task
            self._runner_task = None
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None
        # Fail any pending commands.
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(ConnectionClosed("client closed"))
        self._pending.clear()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # ─── State ───────────────────────────────────────────────────────────────

    @property
    def state(self) -> State:
        return self._state

    async def wait_ready(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        # Public API: callers pass a timeout; we map it to asyncio.wait_for.
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    def on_ready(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a coroutine to run every time the connection becomes READY.

        Called once after initial connect and again after every reconnect.
        Useful for registries that need to refetch on reconnect.
        """
        self._on_ready_callbacks.append(callback)

    # ─── Commands ────────────────────────────────────────────────────────────

    async def send_command(self, type_: str, **payload: Any) -> Any:
        """Send a command and await the ``result`` field of the response.

        The result is whatever HA returns — typically a dict, but several
        list-style endpoints (e.g. ``config/entity_registry/list``) return a
        list, and some return ``None``.

        Raises :class:`ConnectionClosed` if not ready, :class:`CommandError` if
        HA returns ``success: false``.
        """
        if self._state is not State.READY or self._ws is None:
            raise ConnectionClosed(f"not ready (state={self._state.value})")

        msg_id = self._next_id()
        pending = _PendingCommand()
        self._pending[msg_id] = pending

        message = {"id": msg_id, "type": type_, **payload}
        try:
            await self._ws.send_json(message)
        except Exception:
            self._pending.pop(msg_id, None)
            raise

        try:
            response = await pending.future
        finally:
            self._pending.pop(msg_id, None)

        if not response.get("success", False):
            err = response.get("error") or {}
            raise CommandError(err.get("code", "unknown"), err.get("message", ""))
        return response.get("result")

    # ─── Subscriptions ──────────────────────────────────────────────────────

    async def subscribe_events(
        self,
        event_type: str | None,
        callback: EventCallback,
    ) -> Subscription:
        """Subscribe to HA events; callback is awaited for each event payload.

        ``event_type=None`` subscribes to all events.  The returned handle
        survives reconnects — the client will re-subscribe automatically.
        """
        sub = Subscription(event_type=event_type, callback=callback)
        self._subs.append(sub)
        if self._state is State.READY:
            await self._register_subscription(sub)
        return sub

    async def unsubscribe(self, sub: Subscription) -> None:
        sub.cancelled = True
        if sub.sub_id is not None and self._state is State.READY:
            with contextlib.suppress(ConnectionClosed, CommandError):
                await self.send_command("unsubscribe_events", subscription=sub.sub_id)
            self._subs_by_id.pop(sub.sub_id, None)
            sub.sub_id = None
        if sub in self._subs:
            self._subs.remove(sub)

    async def _register_subscription(self, sub: Subscription) -> None:
        payload: dict[str, Any] = {}
        if sub.event_type is not None:
            payload["event_type"] = sub.event_type
        # Need the id before the result arrives because the event stream uses it.
        msg_id = self._next_id()
        sub.sub_id = msg_id
        self._subs_by_id[msg_id] = sub
        pending = _PendingCommand()
        self._pending[msg_id] = pending
        assert self._ws is not None
        await self._ws.send_json({"id": msg_id, "type": "subscribe_events", **payload})
        try:
            response = await pending.future
        finally:
            self._pending.pop(msg_id, None)
        if not response.get("success", False):
            self._subs_by_id.pop(msg_id, None)
            sub.sub_id = None
            err = response.get("error") or {}
            raise CommandError(err.get("code", "unknown"), err.get("message", ""))

    # ─── Runner ─────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _run(self) -> None:
        attempt = 0
        while not self._closed_event.is_set():
            try:
                await self._connect_once()
                attempt = 0  # reset backoff after a successful session
            except Exception as exc:
                log.warning(
                    "ha.ws.session_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            finally:
                self._state = State.DISCONNECTED
                self._ready_event.clear()
                # Subscription ids are per-connection; clear the id-index map.
                self._subs_by_id.clear()
                # Fail any in-flight commands; callers will retry after ready.
                for pending in list(self._pending.values()):
                    if not pending.future.done():
                        pending.future.set_exception(ConnectionClosed("reconnecting"))
                self._pending.clear()

            if self._closed_event.is_set():
                break

            delay = self._backoff(attempt)
            attempt += 1
            log.info("ha.ws.reconnecting", in_seconds=round(delay, 1))
            try:
                await asyncio.wait_for(self._closed_event.wait(), timeout=delay)
                break  # closed while sleeping
            except TimeoutError:
                pass

    def _backoff(self, attempt: int) -> float:
        base = self._BACKOFF[min(attempt, len(self._BACKOFF) - 1)]
        return base + random.uniform(0, base * 0.25)

    async def _connect_once(self) -> None:
        assert self._session is not None
        self._state = State.CONNECTING
        log.info("ha.ws.connecting", url=self._ws_url)

        async with self._session.ws_connect(
            self._ws_url, heartbeat=self._heartbeat, autoclose=True
        ) as ws:
            self._ws = ws
            await self._authenticate(ws)
            self._state = State.READY
            self._ready_event.set()
            log.info("ha.ws.ready")

            # Post-ready work (resubscriptions, on_ready callbacks) issues
            # commands that only resolve when the read loop is consuming
            # responses. Spawn as a task so it runs concurrently with the
            # read loop; otherwise a resubscribe awaits a response that
            # nothing will ever deliver.
            post_ready = asyncio.create_task(self._post_ready_setup())
            try:
                await self._read_loop(ws)
            finally:
                post_ready.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await post_ready
        self._ws = None

    async def _post_ready_setup(self) -> None:
        # Re-register any existing subscriptions after reconnect.
        for sub in list(self._subs):
            if sub.cancelled:
                continue
            try:
                await self._register_subscription(sub)
            except Exception as exc:
                log.warning(
                    "ha.ws.resubscribe_failed",
                    event_type=sub.event_type,
                    error=str(exc),
                )

        # Fire on_ready callbacks (e.g. registries refetch).
        for cb in list(self._on_ready_callbacks):
            try:
                await cb()
            except Exception as exc:
                log.warning("ha.ws.on_ready_callback_failed", error=str(exc))

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._state = State.AUTHING
        hello = await ws.receive_json()
        if hello.get("type") != "auth_required":
            raise HaError(f"unexpected first frame: {hello!r}")

        await ws.send_json({"type": "auth", "access_token": self._token})
        result = await ws.receive_json()
        if result.get("type") == "auth_ok":
            log.info("ha.ws.authenticated", ha_version=result.get("ha_version"))
            return
        if result.get("type") == "auth_invalid":
            raise AuthFailed(result.get("message", "invalid access token"))
        raise HaError(f"unexpected auth response: {result!r}")

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type is aiohttp.WSMsgType.TEXT:
                try:
                    data = msg.json()
                except Exception:
                    log.warning("ha.ws.bad_json", raw=msg.data[:200])
                    continue
                await self._dispatch(data)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                log.info("ha.ws.server_closed")
                return
            elif msg.type is aiohttp.WSMsgType.ERROR:
                raise HaError(f"ws error: {ws.exception()!r}")

    async def _dispatch(self, data: dict[str, Any]) -> None:
        msg_type = data.get("type")
        msg_id = data.get("id")

        if msg_type == "result" and isinstance(msg_id, int):
            pending = self._pending.get(msg_id)
            if pending and not pending.future.done():
                pending.future.set_result(data)
            return

        if msg_type == "event" and isinstance(msg_id, int):
            sub = self._subs_by_id.get(msg_id)
            if sub is None or sub.cancelled:
                return
            event = data.get("event") or {}
            try:
                await sub.callback(event)
            except Exception as exc:
                log.warning(
                    "ha.ws.subscription_callback_error",
                    event_type=sub.event_type,
                    error=str(exc),
                )
            return

        if msg_type == "pong":
            return

        log.debug("ha.ws.unhandled_message", data=data)
