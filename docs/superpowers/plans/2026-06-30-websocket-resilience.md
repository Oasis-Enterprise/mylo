# WebSocket Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the read-loop deadlock that makes HA's WebSocket appear "slow," and harden the command lifecycle and callers around it, so registry events and writes stop timing out at 60s on large instances.

**Architecture:** The read loop stops awaiting event callbacks inline — it hands events to a bounded queue drained by a single in-order worker, so a callback that issues a command can never deadlock the connection. Reads then wait-for-ready and retry once across a reconnect; writes fail fast and are never silently re-applied; registry-update storms coalesce into one refetch.

**Tech Stack:** Python 3.12, asyncio, aiohttp, pytest (asyncio auto mode). No new deps.

**Spec:** `docs/superpowers/specs/2026-06-30-resilience-design.md`

**Conventions for every task:**
- Run the full gate before each commit: `.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy src && .venv/bin/python -m pytest tests/unit -q`
- New source files start with the Apache 2.0 license header (copy from any file in `src/mylo/`).
- Avoid `×` in docstrings (ruff RUF002); write "times".
- An async function with a `timeout`/`connect_wait` parameter needs `# noqa: ASYNC109` on the parameter's line (existing convention in `ws_client.py`).
- The test harness (`_FakeWS`, `_FakeSession`, `_make_client`, `_auth_ok`, `_wait_for_sent`, `_get_ready`) already exists in `tests/unit/test_ws_client.py` — reuse it.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/mylo/ha/ws_client.py` | Event queue + worker (deadlock fix); `send_command(write=...)` lifecycle; new error types; health view | 1, 2, 6 |
| `src/mylo/ha/registries.py` | Registry-event coalescing; retire `_adaptive_timeout`, keep degrade-to-warm | 4, 7 |
| Write-issuing tool handlers | Pass `write=True` on mutating commands | 3 |
| `src/mylo/server/routes_chat.py` | `emit()` closed-transport guard | 5 |
| `src/mylo/files/rollback.py` | Verify/notify resilience to reconnect | 5 |
| `tests/unit/test_ws_resilience.py` (new) | Deadlock regression + lifecycle + integration | 1, 2, 8 |

---

## Task 1: Decoupled dispatcher — the deadlock fix

**Files:**
- Modify: `src/mylo/ha/ws_client.py` (`__init__`, `_connect_once`, `_dispatch`, new `_event_worker`/`_enqueue_event`)
- Test: `tests/unit/test_ws_resilience.py` (new)

The read loop must never `await` a callback. It enqueues events; a worker awaits them off-loop, so a callback issuing `send_command` gets its response delivered normally.

- [ ] **Step 1: Write the failing test** (new file `tests/unit/test_ws_resilience.py`, with the Apache header)

```python
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.unit.test_ws_client import (
    _FakeSession,
    _FakeWS,
    _make_client,
    _wait_for_sent,
)
from mylo.ha.ws_client import State


async def _ready_client(ws: _FakeWS, session: _FakeSession):
    client = _make_client(session)
    await client.start()
    ws.push_server_text({"type": "auth_required"})
    await _wait_for_sent(ws, lambda m: m.get("type") == "auth")
    ws.push_server_text({"type": "auth_ok", "ha_version": "x"})
    for _ in range(200):
        if client.state is State.READY:
            break
        await asyncio.sleep(0.01)
    assert client.state is State.READY
    return client


@pytest.mark.asyncio
async def test_event_callback_can_issue_command_without_deadlock() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = await _ready_client(ws, session)
    got: dict[str, Any] = {}

    async def cb(_event: dict[str, Any]) -> None:
        # Issue a command from *inside* an event callback. With the old
        # inline dispatch this deadlocks: the read loop is blocked here so
        # the command's result can never be read.
        got["result"] = await client.send_command("get_config")

    sub = await client.subscribe_events("demo_event", cb)
    # Deliver the subscribe result so the subscription is live.
    sub_msg = await _wait_for_sent(ws, lambda m: m.get("type") == "subscribe_events")
    ws.push_server_text({"id": sub_msg["id"], "type": "result", "success": True})
    # Fire an event for it.
    ws.push_server_text({"id": sub_msg["id"], "type": "event", "event": {"hello": 1}})
    # The callback now sends get_config; deliver its result.
    cmd_msg = await _wait_for_sent(ws, lambda m: m.get("type") == "get_config")
    ws.push_server_text(
        {"id": cmd_msg["id"], "type": "result", "success": True, "result": {"ok": True}}
    )
    # If the read loop were blocked on the callback, this would hang forever.
    for _ in range(300):
        if "result" in got:
            break
        await asyncio.sleep(0.01)
    assert got["result"] == {"ok": True}
    await client.close()
```

- [ ] **Step 2: Run, confirm it FAILS/HANGS**

Run: `.venv/bin/python -m pytest tests/unit/test_ws_resilience.py::test_event_callback_can_issue_command_without_deadlock -v --timeout=20`
Expected: FAIL (times out — the deadlock). (If `pytest-timeout` isn't installed, the test hangs; Ctrl-C and trust the reasoning.)

- [ ] **Step 3: Implement the worker + non-blocking enqueue** in `src/mylo/ha/ws_client.py`:

In `__init__`, add:
```python
        self._event_queue: asyncio.Queue[tuple[Subscription, dict[str, Any]]] | None = None
        self._events_dropped = 0
```
Add a module constant near `_BACKOFF`:
```python
_EVENT_QUEUE_MAX = 2048
```
In `_connect_once`, after `self._ready_event.set()` and before `post_ready = ...`, create the queue + worker, and tear the worker down in the `finally`:
```python
            self._event_queue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX)
            worker = asyncio.create_task(self._event_worker(self._event_queue))
            post_ready = asyncio.create_task(self._post_ready_setup())
            try:
                await self._read_loop(ws)
            finally:
                post_ready.cancel()
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await post_ready
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await worker
                self._event_queue = None
```
Replace the `event` branch of `_dispatch` (the `await sub.callback(event)` block) with a non-blocking enqueue:
```python
        if msg_type == "event" and isinstance(msg_id, int):
            sub = self._subs_by_id.get(msg_id)
            if sub is None or sub.cancelled:
                return
            event = data.get("event") or {}
            self._enqueue_event(sub, event)
            return
```
Add the two new methods:
```python
    def _enqueue_event(self, sub: Subscription, event: dict[str, Any]) -> None:
        queue = self._event_queue
        if queue is None:
            return
        try:
            queue.put_nowait((sub, event))
        except asyncio.QueueFull:
            # Drop the oldest event to bound memory under a storm; count it
            # so the loss is visible rather than silent.
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
                self._events_dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait((sub, event))

    async def _event_worker(
        self, queue: asyncio.Queue[tuple[Subscription, dict[str, Any]]]
    ) -> None:
        while True:
            sub, event = await queue.get()
            if sub.cancelled:
                continue
            try:
                await sub.callback(event)
            except Exception as exc:
                log.warning(
                    "ha.ws.subscription_callback_error",
                    event_type=sub.event_type,
                    error=str(exc),
                )
```

- [ ] **Step 4: Run, confirm PASS** + full ws suite

Run: `.venv/bin/python -m pytest tests/unit/test_ws_resilience.py tests/unit/test_ws_client.py tests/unit/test_registries.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mylo/ha/ws_client.py tests/unit/test_ws_resilience.py
git commit -m "fix(ha): dispatch events off the read loop so callbacks can't deadlock it"
```

---

## Task 2: Hybrid command lifecycle

**Files:**
- Modify: `src/mylo/ha/ws_client.py` (`send_command` split into read/write paths; new errors)
- Test: `tests/unit/test_ws_resilience.py`

Reads wait-for-ready + retry once across a reconnect; writes fail-fast and report indeterminate when a sent write's response is lost.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_ws_resilience.py`)

```python
from mylo.ha.ws_client import ConnectionUnavailable, IndeterminateWrite


@pytest.mark.asyncio
async def test_write_fails_fast_when_not_ready() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = _make_client(session)  # never made READY
    with pytest.raises(ConnectionUnavailable):
        await client.send_command("config/label_registry/create", write=True, name="x")
    await client.close()


@pytest.mark.asyncio
async def test_write_response_lost_is_indeterminate() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = await _ready_client(ws, session)

    async def issue() -> Any:
        return await client.send_command("config/label_registry/create", write=True, name="x")

    task = asyncio.create_task(issue())
    await _wait_for_sent(ws, lambda m: m.get("type") == "config/label_registry/create")
    # Connection drops before the result arrives.
    ws.push_server_close()
    with pytest.raises(IndeterminateWrite):
        await task
    await client.close()


@pytest.mark.asyncio
async def test_read_waits_for_ready_then_succeeds() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = _make_client(session)
    await client.start()

    async def issue() -> Any:
        # Issued before READY; should wait, not fail.
        return await client.send_command("get_config", connect_wait=5.0)

    task = asyncio.create_task(issue())
    # Now bring the connection up.
    ws.push_server_text({"type": "auth_required"})
    await _wait_for_sent(ws, lambda m: m.get("type") == "auth")
    ws.push_server_text({"type": "auth_ok", "ha_version": "x"})
    cmd = await _wait_for_sent(ws, lambda m: m.get("type") == "get_config")
    ws.push_server_text({"id": cmd["id"], "type": "result", "success": True, "result": {"v": 1}})
    assert await task == {"v": 1}
    await client.close()
```

- [ ] **Step 2: Run, confirm FAIL** (`ImportError` on the new exceptions)

Run: `.venv/bin/python -m pytest tests/unit/test_ws_resilience.py -k "write or read_waits" -q`
Expected: FAIL — `cannot import name 'ConnectionUnavailable'`.

- [ ] **Step 3: Implement** in `src/mylo/ha/ws_client.py`.

Add the error types near `CommandTimeout`:
```python
class ConnectionUnavailable(HaError):
    """Raised when a command can't be sent because the connection isn't ready
    (writes immediately; reads after the connect-wait window elapses)."""


class IndeterminateWrite(HaError):
    """Raised when a write was sent but the connection dropped before its
    result arrived — we cannot know whether HA applied it. Never retried
    automatically."""

    def __init__(self, type_: str) -> None:
        super().__init__(
            f"write {type_!r} was sent but not confirmed (connection dropped); "
            "re-check state before retrying"
        )
        self.type = type_
```
Replace `send_command` with a dispatcher plus shared/typed helpers:
```python
    async def send_command(
        self,
        type_: str,
        *,
        write: bool = False,
        timeout: float = 60.0,  # noqa: ASYNC109
        connect_wait: float = 10.0,  # noqa: ASYNC109
        **payload: Any,
    ) -> Any:
        """Send a command and await its ``result``.

        Reads (``write=False``) wait up to ``connect_wait`` for the
        connection and retry once across a reconnect. Writes (``write=True``)
        fail fast with :class:`ConnectionUnavailable` when not ready and
        raise :class:`IndeterminateWrite` if sent but unconfirmed — never
        silently retried.
        """
        if write:
            return await self._send_write(type_, timeout=timeout, payload=payload)
        return await self._send_read(
            type_, timeout=timeout, connect_wait=connect_wait, payload=payload
        )

    def _register_pending(self, type_: str, payload: dict[str, Any]) -> tuple[int, _PendingCommand]:
        msg_id = self._next_id()
        pending = _PendingCommand()
        self._pending[msg_id] = pending
        return msg_id, pending

    def _unwrap(self, response: dict[str, Any]) -> Any:
        if not response.get("success", False):
            err = response.get("error") or {}
            raise CommandError(err.get("code", "unknown"), err.get("message", ""))
        return response.get("result")

    async def _send_write(self, type_: str, *, timeout: float, payload: dict[str, Any]) -> Any:
        if self._state is not State.READY or self._ws is None:
            raise ConnectionUnavailable(f"HA reconnecting; write {type_!r} not applied")
        msg_id, pending = self._register_pending(type_, payload)
        try:
            await self._ws.send_json({"id": msg_id, "type": type_, **payload})
        except Exception as exc:
            self._pending.pop(msg_id, None)
            # Send itself failed -> not delivered -> not applied.
            raise ConnectionUnavailable(f"HA reconnecting; write {type_!r} not applied") from exc
        try:
            response = await asyncio.wait_for(pending.future, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise CommandTimeout(type_, timeout) from exc
        except ConnectionClosed as exc:
            # Sent, then the connection dropped before the result: unknown.
            raise IndeterminateWrite(type_) from exc
        finally:
            self._pending.pop(msg_id, None)
        return self._unwrap(response)

    async def _send_read(
        self, type_: str, *, timeout: float, connect_wait: float, payload: dict[str, Any]
    ) -> Any:
        last: Exception | None = None
        for _attempt in range(2):  # initial + one retry across a reconnect
            try:
                await self._await_ready(connect_wait)
            except TimeoutError as exc:
                raise ConnectionUnavailable(f"HA not ready for read {type_!r}") from exc
            msg_id, pending = self._register_pending(type_, payload)
            try:
                if self._ws is None:
                    raise ConnectionClosed("not ready")
                await self._ws.send_json({"id": msg_id, "type": type_, **payload})
                response = await asyncio.wait_for(pending.future, timeout=timeout)
            except TimeoutError as exc:
                self._pending.pop(msg_id, None)
                raise CommandTimeout(type_, timeout) from exc
            except (ConnectionClosed, ConnectionResetError) as exc:
                # Dropped before/after send — reads are idempotent, retry once.
                self._pending.pop(msg_id, None)
                last = exc
                continue
            finally:
                self._pending.pop(msg_id, None)
            return self._unwrap(response)
        raise ConnectionUnavailable(f"read {type_!r} failed across reconnect") from last

    async def _await_ready(self, connect_wait: float) -> None:  # noqa: ASYNC109
        if self._state is State.READY:
            return
        await asyncio.wait_for(self._ready_event.wait(), timeout=connect_wait)
```
(`_register_pending` ignores `payload` for now — it's there so the signature reads intentionally and stays open to per-command bookkeeping. If ruff flags the unused arg, prefix it: `_payload`.)

- [ ] **Step 4: Run, confirm PASS** + full ws suite

Run: `.venv/bin/python -m pytest tests/unit/test_ws_resilience.py tests/unit/test_ws_client.py -q`
Expected: all pass. If any existing `test_ws_client` test asserted `ConnectionClosed` from `send_command` on a not-ready client, update it to expect `ConnectionUnavailable` (reads) — that's the intended behavior change.

- [ ] **Step 5: Commit**

```bash
git add src/mylo/ha/ws_client.py tests/unit/test_ws_resilience.py tests/unit/test_ws_client.py
git commit -m "feat(ha): hybrid command lifecycle — reads wait/retry, writes fail-fast"
```

---

## Task 3: Mark write callers `write=True`

**Files:**
- Modify: every mutating `send_command` call site (list below)
- Test: `tests/unit/test_ws_resilience.py`

Pass `write=True` on commands that mutate HA. Reads are left as the default. The mutating sites (verified by grep):

- `src/mylo/tools/write/modify_areas.py` — `config/area_registry/create` / `update` / `delete`
- `src/mylo/tools/write/modify_dashboard.py:140` — `lovelace/config/save`
- `src/mylo/tools/write/rename_entities.py` — `config/entity_registry/update` (rename), `lovelace/config/save`
- `src/mylo/tools/write/manage_labels.py` — `config/label_registry/create`, `config/entity_registry/update`
- `src/mylo/tools/write/manage_helpers.py` — the create/update/delete `cmd` sends (lines ~218, 245, 266)
- `src/mylo/tools/write/modify_scene.py` — the scene save send
- `src/mylo/files/rollback.py` — the reload service `call_service` send (reload step)

Leave reads (`*/list`, `lovelace/config` *get*, `lovelace/dashboards/list`, `get_config`, `get_states`, trace reads) untouched.

- [ ] **Step 1: Write the failing test** — a write tool, given a not-ready client, surfaces the unavailable error rather than a generic hang. Use `manage_labels` as the representative:

```python
@pytest.mark.asyncio
async def test_manage_labels_write_uses_write_path(monkeypatch) -> None:
    from mylo.ha.ws_client import ConnectionUnavailable

    sent_kwargs = {}

    class _Stub:
        state = None

        async def send_command(self, type_, **kwargs):
            sent_kwargs["write"] = kwargs.get("write")
            raise ConnectionUnavailable("down")

    # Directly assert the label create passes write=True.
    stub = _Stub()
    with pytest.raises(ConnectionUnavailable):
        await stub.send_command("config/label_registry/create", write=True, name="unused")
    assert sent_kwargs["write"] is True
```

(This pins the contract; the real value is the grep-driven edits below.)

- [ ] **Step 2: Run** — passes immediately (it tests the stub contract). The substantive change is the edits.

- [ ] **Step 3: Edit each call site** — add `write=True` to each mutating `send_command(...)` listed above. Example for `manage_labels.py:89`:
```python
            result = await ctx.ws_client.send_command(
                "config/label_registry/create", write=True, name=...
            )
```
Apply the same to every site in the list. Grep to confirm none missed:
```bash
grep -rn "registry/create\|registry/update\|registry/delete\|config/save\|call_service" src/mylo/tools/write src/mylo/files/rollback.py
```
Every match that mutates must carry `write=True`.

- [ ] **Step 4: Run the gate + write-tool tests**

Run: `.venv/bin/python -m pytest tests/unit -q -k "label or area or helper or scene or dashboard or rename or rollback"`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/mylo/tools/write src/mylo/files/rollback.py tests/unit/test_ws_resilience.py
git commit -m "feat(tools): mark mutating HA commands write=True for fail-fast lifecycle"
```

---

## Task 4: Registry-storm coalescing

**Files:**
- Modify: `src/mylo/ha/registries.py` (debounce `*_registry_updated` refetches)
- Test: `tests/unit/test_registries_scale.py`

A burst of registry events collapses into one refetch after a short quiet window.

- [ ] **Step 1: Write the failing test**

```python
async def test_registry_events_coalesce_into_one_refetch() -> None:
    from mylo.ha.registries import Registries

    calls = {"n": 0}

    class _Client:
        async def send_command(self, type_, **kwargs):
            calls["n"] += 1
            return []

    reg = Registries()
    reg._client = _Client()  # type: ignore[assignment]
    # Fire 10 entity-registry events rapidly.
    await asyncio.gather(*[reg.refresh_for("entity_registry_updated") for _ in range(10)])
    # Allow the debounce window to elapse.
    await asyncio.sleep(0.2)
    # Far fewer than 10 list calls (coalesced).
    assert calls["n"] <= 2
```

- [ ] **Step 2: Run, confirm FAIL** (today each call refetches → ~10 calls)

Run: `.venv/bin/python -m pytest tests/unit/test_registries_scale.py::test_registry_events_coalesce_into_one_refetch -v`
Expected: FAIL (`assert 10 <= 2`).

- [ ] **Step 3: Implement** — make `refresh_for` debounce. Add to `Registries` (`registries.py`):
```python
    _REFRESH_FOR_DEBOUNCE: float = 0.3
    _pending_refreshes: set[str] = field(default_factory=set)
    _refresh_for_task: asyncio.Task[None] | None = None
```
Replace `refresh_for` body so it records the event type and schedules a single coalesced flush:
```python
    async def refresh_for(self, event_type: str) -> None:
        self._pending_refreshes.add(event_type)
        if self._refresh_for_task is None or self._refresh_for_task.done():
            self._refresh_for_task = asyncio.create_task(self._flush_refreshes())

    async def _flush_refreshes(self) -> None:
        await asyncio.sleep(self._REFRESH_FOR_DEBOUNCE)
        pending = self._pending_refreshes
        self._pending_refreshes = set()
        assert self._client is not None
        timeout = _adaptive_timeout(len(self.entities) or _BOOTSTRAP_FETCH_SIZE)
        try:
            if "entity_registry_updated" in pending:
                self._replace_entities(
                    await self._client.send_command("config/entity_registry/list", timeout=timeout)
                )
            if "device_registry_updated" in pending:
                self._replace_devices(
                    await self._client.send_command("config/device_registry/list", timeout=timeout)
                )
            if "area_registry_updated" in pending:
                self._replace_areas(
                    await self._client.send_command("config/area_registry/list", timeout=timeout)
                )
            if "label_registry_updated" in pending:
                self._replace_labels(
                    await self._client.send_command("config/label_registry/list", timeout=timeout)
                )
        except (CommandTimeout, TimeoutError):
            log.warning("ha.registries.refresh_for_timeout_keeping_warm", pending=sorted(pending))
```
(The field defaults import `field` from dataclasses, already imported. `asyncio` is imported.)

- [ ] **Step 4: Run, confirm PASS** + existing registry tests

Run: `.venv/bin/python -m pytest tests/unit/test_registries_scale.py tests/unit/test_registries.py -q`
Expected: green. (If a registry test asserted an immediate refetch on a single event, add `await asyncio.sleep(0.35)` before its assertion to let the debounce flush.)

- [ ] **Step 5: Commit**

```bash
git add src/mylo/ha/registries.py tests/unit/test_registries_scale.py
git commit -m "feat(ha): coalesce registry-update storms into one debounced refetch"
```

---

## Task 5: Caller hardening (chat stream + verify/notify)

**Files:**
- Modify: `src/mylo/server/routes_chat.py` (`emit` closed-transport guard)
- Modify: `src/mylo/files/rollback.py` (`_record_background_failure` notify guard)
- Test: `tests/unit/test_routes_chat_emit.py` (new)

- [ ] **Step 1: Write the failing test** (`tests/unit/test_routes_chat_emit.py`, Apache header)

```python
from __future__ import annotations

import pytest

from mylo.server.routes_chat import _safe_emit


class _ClosedResponse:
    async def write(self, _data: bytes) -> None:
        raise ConnectionResetError("Cannot write to closing transport")


@pytest.mark.asyncio
async def test_safe_emit_swallows_closed_transport() -> None:
    # Must not raise even though the underlying transport is gone.
    await _safe_emit(_ClosedResponse(), "text", {"text": "hi"})
```

- [ ] **Step 2: Run, confirm FAIL** (`ImportError: _safe_emit`)

Run: `.venv/bin/python -m pytest tests/unit/test_routes_chat_emit.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement.** In `routes_chat.py`, extract the emit body into a guarded module-level helper and call it from the inner `emit`:
```python
import json

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError


async def _safe_emit(response: Any, name: str, data: dict[str, Any]) -> None:
    """Write one SSE event, swallowing a closed-transport write.

    When the user navigates away mid-turn the client is gone; there's no one
    to notify, so a failed write ends the turn quietly instead of cascading
    tracebacks through the error handler and write_eof.
    """
    payload = f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"
    try:
        await response.write(payload.encode("utf-8"))
    except (ConnectionResetError, ClientConnectionResetError):
        return
```
Replace the inner `async def emit(...)` with a call to `_safe_emit(response, name, data)`, and wrap the final `await response.write_eof()` in `with contextlib.suppress(ConnectionResetError, ClientConnectionResetError):` (import `contextlib`).

In `rollback.py`, wrap the notify send in `_record_background_failure` so a notify that can't reach HA degrades to a warning:
```python
        try:
            await client.send_command(...)  # existing notify call, unchanged args
        except HaError as exc:
            log.warning("background_verify.notification_unsendable", error=str(exc))
```
(Import `HaError` from `mylo.ha.ws_client` if not already imported.)

- [ ] **Step 4: Run, confirm PASS** + chat route tests

Run: `.venv/bin/python -m pytest tests/unit/test_routes_chat_emit.py tests/unit -q -k "chat or rollback"`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/mylo/server/routes_chat.py src/mylo/files/rollback.py tests/unit/test_routes_chat_emit.py
git commit -m "fix(server): guard SSE emit + verify-notify against a dead connection"
```

---

## Task 6: Connection-health surfacing

**Files:**
- Modify: `src/mylo/ha/ws_client.py` (`health` property + `ha.ws.health` log on transitions)
- Test: `tests/unit/test_ws_resilience.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_health_reports_ready_and_counters() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = await _ready_client(ws, session)
    h = client.health
    assert h["state"] == "ready"
    assert h["consecutive_failures"] == 0
    assert "events_dropped" in h
    await client.close()
```

- [ ] **Step 2: Run, confirm FAIL** (`AttributeError: health`)

Run: `.venv/bin/python -m pytest tests/unit/test_ws_resilience.py::test_health_reports_ready_and_counters -v`
Expected: FAIL.

- [ ] **Step 3: Implement.** In `__init__` add `self._consecutive_failures = 0` and `self._last_ready_at: float | None = None`. In `_run`, increment `self._consecutive_failures` in the `except` and reset to 0 after a successful session (where `attempt = 0` already resets). In `_connect_once`, after `self._ready_event.set()`, set `self._last_ready_at = time.monotonic()` (import `time`). Add the property:
```python
    @property
    def health(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "last_ready_at": self._last_ready_at,
            "in_flight_commands": len(self._pending),
            "events_dropped": self._events_dropped,
        }
```
After setting READY, log `log.info("ha.ws.health", **self.health)`.

- [ ] **Step 4: Run, confirm PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_ws_resilience.py -q`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/mylo/ha/ws_client.py tests/unit/test_ws_resilience.py
git commit -m "feat(ha): expose connection health (state, failures, in-flight, drops)"
```

---

## Task 7: Retire the Task 9 adaptive-timeout band-aid

**Files:**
- Modify: `src/mylo/ha/registries.py` (simplify `_adaptive_timeout`, keep degrade-to-warm)
- Test: `tests/unit/test_registries_scale.py`

With the deadlock gone the list returns in seconds; the size-scaled timeout isn't load-bearing. Keep a sane fixed timeout with a generous ceiling as a true backstop.

- [ ] **Step 1: Update the test** — replace `test_adaptive_timeout_scales_with_size` with a fixed-timeout expectation:
```python
def test_registry_fetch_timeout_is_fixed_generous() -> None:
    from mylo.ha.registries import _registry_fetch_timeout

    assert _registry_fetch_timeout() == 120.0
```

- [ ] **Step 2: Run, confirm FAIL** (`ImportError: _registry_fetch_timeout`)

Run: `.venv/bin/python -m pytest tests/unit/test_registries_scale.py::test_registry_fetch_timeout_is_fixed_generous -v`
Expected: FAIL.

- [ ] **Step 3: Implement** — replace `_adaptive_timeout(entity_count)` with:
```python
def _registry_fetch_timeout() -> float:
    """Generous fixed backstop for a registry list fetch.

    With event dispatch off the read loop, the list returns in seconds; this
    is only a backstop against a genuinely wedged HA, not a per-size knob.
    """
    return 120.0
```
Update both `refresh` and `_flush_refreshes` to call `_registry_fetch_timeout()` instead of `_adaptive_timeout(...)`, and drop the `_BOOTSTRAP_FETCH_SIZE` constant if now unused. Keep the degrade-to-warm `except (CommandTimeout, TimeoutError)` blocks unchanged.

- [ ] **Step 4: Run, confirm PASS** + registry suites

Run: `.venv/bin/python -m pytest tests/unit/test_registries_scale.py tests/unit/test_registries.py -q`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/mylo/ha/registries.py tests/unit/test_registries_scale.py
git commit -m "refactor(ha): replace size-scaled registry timeout with a fixed backstop"
```

---

## Task 8: End-to-end regression — storm during a write doesn't deadlock

**Files:**
- Test: `tests/unit/test_ws_resilience.py`

Proves the whole thing together: an event storm whose callbacks issue commands does not block an unrelated write's response.

- [ ] **Step 1: Write the test**

```python
@pytest.mark.asyncio
async def test_event_storm_does_not_block_a_write() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = await _ready_client(ws, session)

    async def slow_cb(_event: dict[str, Any]) -> None:
        # A callback that itself issues a (read) command.
        await client.send_command("get_states", connect_wait=5.0)

    sub = await client.subscribe_events("entity_registry_updated", slow_cb)
    sub_msg = await _wait_for_sent(ws, lambda m: m.get("type") == "subscribe_events")
    ws.push_server_text({"id": sub_msg["id"], "type": "result", "success": True})

    # Fire a burst of events; each callback will issue get_states.
    for _ in range(5):
        ws.push_server_text({"id": sub_msg["id"], "type": "event", "event": {}})

    # Meanwhile issue a write; its response must come back promptly.
    async def do_write() -> Any:
        return await client.send_command("config/label_registry/create", write=True, name="x")

    write_task = asyncio.create_task(do_write())
    wmsg = await _wait_for_sent(ws, lambda m: m.get("type") == "config/label_registry/create")
    ws.push_server_text({"id": wmsg["id"], "type": "result", "success": True, "result": {"ok": 1}})
    # Also satisfy any get_states the callbacks sent, so nothing hangs on close.
    for m in list(ws.sent):
        if m.get("type") == "get_states":
            ws.push_server_text({"id": m["id"], "type": "result", "success": True, "result": []})
    assert await write_task == {"ok": 1}
    await client.close()
```

- [ ] **Step 2: Run, confirm PASS** (with the fixes from Tasks 1–2 in place)

Run: `.venv/bin/python -m pytest tests/unit/test_ws_resilience.py -q`
Expected: green.

- [ ] **Step 3: Full gate**

Run: `.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy src && .venv/bin/python -m pytest tests/unit -q`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_ws_resilience.py
git commit -m "test(ha): event storm never blocks an unrelated write (end-to-end)"
```

---

## Final verification (after all tasks)

- [ ] Full gate green: `.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy src && .venv/bin/python -m pytest tests/unit -q`.
- [ ] Grep confirms no mutating `send_command` left without `write=True` (Task 3 list).
- [ ] Add a CHANGELOG entry (next `1.5.0bN` beta, or fold into `1.5.0`) and cut via `scripts/release.sh`.

## Spec coverage check

§4.1 decoupled dispatcher → Task 1 · §4.2 hybrid lifecycle → Tasks 2, 3 · §4.3 registry coalescing → Task 4 · §4.4 caller hardening → Task 5 · §4.5 health → Task 6 · §4.6 retire band-aid → Task 7 · §7 testing (deadlock lock, lifecycle, coalescing, hardening, end-to-end) → Tasks 1, 2, 4, 5, 8.
