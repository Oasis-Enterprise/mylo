# WebSocket Resilience — Design

**Status:** Approved design, ready for implementation planning
**Date:** 2026-06-30
**Part of:** the Robustness pillar (sub-project 2 of 4)

---

## 1. Context

The Home Assistant WebSocket connection has been the single most persistent
source of trouble across the whole project. On the 2,484-entity reference
instance the symptoms are constant:

```
"event_type": "entity_registry_updated",
"error": "command 'config/entity_registry/list' timed out after 60.0s"
```
…repeating for days, plus write commands timing out the same way
(`config/label_registry/create`, `config/entity_registry/update`), plus
`Cannot write to closing transport` traceback cascades, plus background
verify steps dying with `ConnectionClosed: reconnecting`.

### Root cause (the key finding)

It is **not** that HA is slow. The read loop dispatches events by
**awaiting the callback inline** (`ws_client.py`, `_read_loop` → `_dispatch`
→ `await sub.callback(event)`). The `entity_registry_updated` callback calls
`Registries.refresh_for()`, which issues `send_command("config/entity_registry/list")`
and awaits the response — **but that response can only be delivered by the
read loop, which is blocked awaiting the callback.** The command can never
resolve, so it times out at exactly 60s. **Every registry-update event
deadlocks the connection for 60 seconds**, and while it's deadlocked every
*other* in-flight command's response is stuck behind it too — which is why
unrelated writes (label create, entity update) also time out at 60s.

The code already half-knows this: `_post_ready_setup` (the on-reconnect
refetch) is deliberately spawned as a *task* with a comment — "otherwise a
resubscribe awaits a response that nothing will ever deliver." Regular event
dispatch never got the same treatment.

This reframes the sub-project. The reconnect/backoff machinery largely
works; the connection *bootstraps* fine (2,484 entities load). The job is to
**fix the dispatcher concurrency bug** and then harden the command lifecycle
and the callers around a connection that finally behaves.

This is sub-project 2 of the robustness pillar (Scale → **Resilience** →
Diagnostics → Failure-UX); each gets its own spec.

---

## 2. Goals & non-goals

### Goals

1. **The read loop never blocks on anything that needs a frame.** Event
   callbacks run off the read loop, so a callback that issues a command can
   never deadlock the connection. (The headline fix.)
2. **Registry churn stops amplifying.** A burst of `*_registry_updated`
   events collapses into a single refetch, not one 60s-deadlocking refetch
   per event.
3. **Transient blips are invisible to reads, never silently risky for
   writes.** Reads wait briefly for the connection and retry once across a
   reconnect; writes fail fast with a clear error and are never silently
   re-applied.
4. **Callers degrade cleanly.** Background verify/rollback survives a
   reconnect; the chat→browser stream stops double-faulting on a closed
   transport.
5. **Connection health is visible** — an at-a-glance signal that the socket
   is healthy.

### Non-goals

- Deep diagnostics dashboards / one-click bug reports — that is
  **sub-project 3 (Diagnostics)**. This spec surfaces a basic health signal
  only.
- UI-side failure presentation polish — **sub-project 4 (Failure-UX)**.
- Changing HA's own performance. We make Mylo resilient to a slow/busy HA;
  we don't speed up HA.
- LLM-provider retry/backoff — already handled in the provider layer
  (`anthropic_provider.py`); out of scope here.

### Success criteria

On the reference instance:
- **Zero** `*_registry_updated … timed out after 60s`.
- Label/automation **writes complete promptly** (seconds, not 60s timeouts).
- **No** `Cannot write to closing transport` traceback spam.
- The connection **self-heals across HA restarts** with no manual restart of
  the add-on.
- A regression test proves an event-callback-that-issues-a-command no longer
  deadlocks.

---

## 3. Architecture overview

One invariant drives everything: **the read loop reads and routes frames and
nothing else** — it must never `await` anything that could itself require a
frame to make progress.

```
socket → _read_loop → _dispatch ─┬─ result → resolve pending future   (direct, ALWAYS live)
                                 └─ event  → queue.put_nowait((sub,ev)) (non-blocking)
                                                        │
                                                        ▼
                                              _event_worker  → await sub.callback(ev)
                                              (one at a time, in order, OFF the read loop)
```

- **§4.1 Decoupled dispatcher** — the read loop hands events to a bounded
  queue; a single worker awaits callbacks in arrival order. Makes the
  deadlock structurally impossible.
- **§4.2 Hybrid command lifecycle** — reads wait-for-ready + one retry;
  writes fail-fast and are never silently retried.
- **§4.3 Registry coalescing** — the worker debounces `*_registry_updated`
  into one refetch.
- **§4.4 Caller hardening** — verify/rollback + chat-stream resilience.
- **§4.5 Health surfacing** + **§4.6 retire the Task 9 band-aid.**

Everything after §4.1 is hardening *around* a connection that now works.

---

## 4. Components

### 4.1 Decoupled dispatcher (`src/mylo/ha/ws_client.py`)

**Read loop / `_dispatch`:**
- `result` frame → resolve the pending command future **directly**, exactly
  as today. This path must always stay live.
- `event` frame → `self._event_queue.put_nowait((sub, event))` and return
  immediately. **Never awaits a callback.**
- On a full queue, apply the overflow policy (§4.3): coalesce registry
  events; for others, drop-oldest with a logged counter (never silently lose
  unbounded events, never grow memory unbounded).

**New `_event_worker` task:**
- Started in `_connect_once` alongside the read loop; torn down with it (same
  lifecycle as `_post_ready_setup`).
- Drains `_event_queue` and `await`s `sub.callback(event)` **one at a time,
  in arrival order** — preserving today's ordering (the `state_changed`
  transition logger depends on it).
- Each callback wrapped in the existing error guard so one slow/failing
  callback can't kill the worker or block the read loop.

**Queue:** `asyncio.Queue(maxsize=N)` (N sized generously, e.g. a few
thousand). Per-connection: created/drained per `_connect_once`, so stale
events from a dead connection don't leak into the next.

### 4.2 Hybrid command lifecycle (`send_command`)

Signature gains an explicit classifier:
```python
async def send_command(self, type_, *, write: bool = False, timeout=..., **payload)
```
Explicit `write=True` on mutating callers (`manage_labels`, `modify_*`,
`call_service`, registry create/update/delete). Explicit beats inferring
from the command string — write-safety shouldn't ride on a prefix heuristic.

**Reads (`write=False`):**
- If not `READY`, **wait up to `connect_wait` (~10s)** for the next `READY`,
  then send. A transient reconnect is invisible.
- If the connection drops after send but before the response, **retry once**
  on the fresh connection (reads are idempotent).
- Past the window → `ConnectionUnavailable` (clear, structured).

**Writes (`write=True`):**
- If not `READY`, **fail immediately** with a clear structured error ("HA is
  reconnecting — your change was not applied; retry in a moment"). No wait.
- If sent but the response is lost to a disconnect, report **indeterminate**
  — *not* auto-retried. The agent surfaces it as data ("couldn't confirm the
  change landed; re-check before retrying"), so a config edit is never
  silently double-applied.

### 4.3 Registry-storm coalescing (the event worker + `registries.py`)

The worker special-cases the `*_registry_updated` family: instead of a full
refetch per event, it **debounces** — a burst collapses into a single
refetch after a short quiet window (~1–2s). So dozens of
`entity_registry_updated` events become one `config/entity_registry/list`
call once the churn settles. Combined with §4.1 (that list no longer
deadlocks), registry churn goes from "constant 60s stalls" to "one cheap
refetch when it settles." Coalescing lives in the worker (covers the event
path); `Registries.refresh`'s existing 0.5s debounce stays as a second
guard.

### 4.4 Caller hardening

- **Background verify/rollback** (`files/rollback.py`): the verify step is a
  read, so under §4.2 it waits-for-ready instead of throwing
  `ConnectionClosed: reconnecting`. The failure-notification send is wrapped
  so a notify that can't reach HA degrades to a logged warning, never an
  unhandled traceback (your log showed this exact double-failure).
- **Chat→browser stream** (`server/routes_chat.py`): `emit()` swallows a
  closed-transport write (`ClientConnectionResetError` / "Cannot write to
  closing transport") — when the user navigates away mid-turn the client is
  already gone, so there's no one to notify; the turn ends quietly instead of
  cascading tracebacks through the error handler and `write_eof`.

### 4.5 Connection-health surfacing

A real health view on the client: `state`, `last_ready_at`,
`consecutive_failures`, `in_flight_commands`, `events_coalesced` — exposed
via a property and a structured `ha.ws.health` log on state transitions.
Answers "is the socket happy?" at a glance. Deeper dashboards are
sub-project 3; this is just the signal.

### 4.6 Retire the Task 9 timeout band-aid (`registries.py`)

With the deadlock gone, `config/*/list` returns in its normal few seconds, so
the size-scaled adaptive timeout (`_adaptive_timeout`) isn't load-bearing.
Simplify to a sane fixed timeout with a generous ceiling as a true backstop.
**Keep** the degrade-to-warm behavior on a failed refresh (genuinely good);
**drop** the scaling math that never actually helped (at 2,484 entities it
returned exactly 60s).

---

## 5. Data flow

1. **Frame arrives** → read loop routes: `result` resolves a future directly;
   `event` is queued non-blocking.
2. **Event worker** drains the queue in order, awaiting callbacks off the
   read loop. A callback issuing `send_command` gets its response delivered
   normally (no deadlock).
3. **Registry events** are coalesced in the worker → one refetch per burst.
4. **A command** is issued: reads wait-for-ready + retry-once; writes
   fail-fast / report-indeterminate.
5. **On disconnect**, the runner reconnects with existing backoff;
   subscriptions and `on_ready` callbacks re-register as today.

---

## 6. Error handling

| Condition | Behavior |
|---|---|
| Event callback issues a command | Resolves normally — read loop is free (the deadlock is gone). |
| Event queue full (storm) | Coalesce registry events; drop-oldest-with-counter for others. Never unbounded memory, never silent unbounded loss. |
| Read on non-ready connection | Wait up to `connect_wait` for READY, then send; else `ConnectionUnavailable`. |
| Read response lost to disconnect | Retry once on the fresh connection. |
| Write on non-ready connection | Fail fast, clear error, not applied. |
| Write response lost to disconnect | Report **indeterminate**; never auto-retried. |
| Verify during reconnect | Waits-for-ready (it's a read); notify failure degrades to a warning. |
| Browser stream closed mid-turn | `emit()` swallows the closed-transport write; turn ends quietly. |

---

## 7. Testing strategy

- **Deadlock regression lock (headline).** A fake transport delivers an event
  whose callback issues a `send_command`; assert the command **resolves**
  (the read loop keeps delivering results while the callback runs). Fails
  against today's code; proves the bug is dead.
- **Ordering** — events processed in arrival order through the worker.
- **Queue overflow** — registry events coalesce; other events drop-oldest and
  bump a counter.
- **Hybrid lifecycle** — a read mid-reconnect waits then succeeds; a read
  whose response is lost retries once; a **write on a non-ready connection
  fails fast**; a write whose response is lost is reported **indeterminate,
  not retried**.
- **Coalescing** — a burst of N `entity_registry_updated` events triggers
  exactly **one** refetch.
- **Caller hardening** — verify survives a simulated reconnect; `emit()` on a
  closed transport does not raise.

---

## 8. Proposed module layout

- `src/mylo/ha/ws_client.py` — decoupled dispatcher (event queue + worker),
  `send_command(write=...)` lifecycle, health view.
- `src/mylo/ha/registries.py` — registry-event coalescing hook; retire
  `_adaptive_timeout`, keep degrade-to-warm.
- `src/mylo/files/rollback.py` — verify/notify reconnect resilience.
- `src/mylo/server/routes_chat.py` — `emit()` closed-transport guard.
- Write-issuing tool handlers (`manage_labels`, `modify_*`, `call_service`
  path) — pass `write=True`.

---

## 9. Out of scope / follow-on

- **Diagnostics** (sub-project 3): health dashboards, error capture,
  one-click bug report.
- **Failure-UX** (sub-project 4): how degraded states surface in the UI.

Each gets its own spec after this ships.
