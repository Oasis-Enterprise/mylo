# Live Turn — Design

**Status:** approved in conversation 2026-09-22; first of three UX releases
(1.7 Live turn → 1.8 Trust → 1.9 Surface).
**Target release:** 1.7.0.

## 1. Problem

A Mylo turn on a large home runs 30–120 s. Today the model call is
non-streaming (`anthropic_provider.py` docstring: "avoiding streaming keeps
the tool loop a straightforward sequence of turns"), so the panel receives
nothing until the model finishes each round. The user sees a random phrase
cycler (`ThinkingIndicator.tsx` PHRASES) unrelated to what Mylo is doing,
has no way to stop a turn (the fetch layer accepts an `AbortSignal` that
nothing passes), and the UI derives "is there a pending approval / question"
from scratch in three separate places (`detectPendingApproval`,
`findApprovalContexts`, and the `pollUntilTurnCompletes` recovery path).
That last shape produced the missing-Apply-button bug twice.

## 2. Goals

1. Text appears as the model produces it.
2. The indicator tells the truth: what tool Mylo is running, in plain words.
3. The user can stop a turn safely.
4. One function derives turn-dependent UI state, used by both the stream
   path and the recovery path.

## 3. Non-goals

- Changing what any tool does, approval semantics, or backups (1.8).
- Visual redesign, labels, mobile, accessibility (1.9).
- Streaming for OpenAI/Gemini/Ollama (they keep `message()`; §4.1).
- Aborting an in-flight tool call (§4.3).

## 4. Design

### 4.1 Provider streaming

`Provider` protocol (`src/mylo/llm/provider.py`) gains one optional method:

```python
async def stream(
    self, *, system: str, messages: list[ProviderMessage],
    tools: list[dict[str, Any]], model: str, max_tokens: int = 8192,
) -> AsyncIterator[StreamDelta | ProviderResponse]: ...
```

`StreamDelta` is a new frozen dataclass `{"text": str}`. A `stream()`
implementation yields zero or more `StreamDelta`s and then exactly one
`ProviderResponse` as its final item, shaped identically to what
`message()` returns today (content blocks, text, tool calls, stop reason,
usage). Consumers therefore need no second code path for persistence.

`AnthropicProvider.stream()` uses `client.messages.stream(...)` from the
SDK: forwards `text` deltas as `StreamDelta`, then calls
`get_final_message()` and converts it with the same block/usage code as
`message()` (extracted into `_to_response(message)`). Caching blocks
(system, tools, history breakpoint) are identical to `message()`. The
retry/backoff ladder applies to the call that opens the stream; a
connection error mid-stream is not retried (partial text may already be
shown) and propagates as today's `error` event.

Streaming removes the SDK's non-streaming `max_tokens` ceiling
(`_calculate_nonstreaming_timeout`). The tool loop's `max_tokens` stays at
its current default for 1.7; raising it is a separate decision.

`OpenAIProvider` (and its Gemini/Ollama subclasses) do NOT implement
`stream()`. The tool loop checks `hasattr(provider, "stream")` and, when
absent, calls `message()` and treats the whole response as one delta.

### 4.2 Tool loop events

`src/mylo/llm/tool_loop.py`:

- New event `TextDeltaEvent(text: str)`, yielded for every `StreamDelta`.
- New event `StatusEvent(phase: str, tool: str | None, label: str)` where
  `phase ∈ {"thinking", "tool", "cancelled"}`. Yielded:
  - `thinking` immediately before every provider call;
  - `tool` immediately before each tool execution (in addition to the
    existing `ToolCallEvent`), with `label` from §4.4;
  - `cancelled` once, when the loop stops because of §4.3.
- `TextEvent` is still yielded after the model call for each text block,
  exactly as today. CLI and history hydration keep working unchanged; the
  panel uses deltas to paint and the final `TextEvent` to replace them
  (§4.5).
- `DoneEvent.stop_reason` gains the value `"cancelled"`.

Loop shape per iteration:

```
yield StatusEvent("thinking", None, "Thinking")
if cancel_requested(): yield StatusEvent("cancelled"…); stop_reason="cancelled"; break
response = await _call_provider(...)          # streams deltas as TextDeltaEvents
append assistant blocks; yield TextEvents
if no tool calls: break
for call in tool_calls:
    yield ToolCallEvent; yield StatusEvent("tool", call.name, label_for(call.name))
    execute (never aborted); yield ToolResultEvent
append tool_result blocks
if cancel_requested(): yield StatusEvent("cancelled"…); stop_reason="cancelled"; break
```

The provider call itself is cancelled by cancelling the asyncio task that
awaits it: `_call_provider` runs the stream in `asyncio.wait` alongside a
`cancel_event.wait()`; when the cancel event wins, the stream task is
cancelled, the partial text collected so far is persisted as a single
assistant text block (so history stays a valid alternating sequence), and
the loop breaks with `stop_reason="cancelled"`. If cancel arrives while a
tool executes, the tool finishes, its result is appended, and the loop
breaks at the check after the tool batch.

`run_turn` gains `cancel: asyncio.Event | None = None`. `ConversationManager`
gains `cancel_requested: asyncio.Event` (created per turn in the chat
route, cleared on turn start).

### 4.3 Cancel endpoint

`POST /api/chat/cancel` (`routes_chat.py`): if `conv.turn_active`, set
`conv.cancel_requested` and return `{"ok": true, "cancelling": true}`;
otherwise `{"ok": true, "cancelling": false}`. Idempotent. No body.

Guarantees:
- A tool that has started always finishes; the loop never interrupts a
  write. The user is told "Stopping after the current step…" (§4.5).
- Cancelled turns end with a `done` event (`stop_reason: "cancelled"`), so
  `turn_active` clears and `/api/status.last_turn` records usage exactly
  like a finished turn.
- Partial assistant text is kept in history; the next user message
  continues the conversation normally.

### 4.4 Status labels

`src/mylo/llm/status_labels.py`: `label_for(tool_name: str) -> str`, a
dict lookup with a fallback of `"Working"`. Labels are short present-tense
phrases without the tool name:

| tool | label |
|---|---|
| query_entities | Looking at your devices |
| query_devices | Looking at your devices |
| query_automations | Reading your automations |
| query_dashboard | Reading the dashboard |
| query_dashboard_env | Checking themes and cards |
| plan_dashboard | Planning the dashboard change |
| stage_custom_card | Writing the card |
| read_custom_card | Reading the card |
| apply_dashboard_plan | Updating the dashboard |
| apply_custom_card | Installing the card |
| query_logs | Checking the logs |
| query_history | Checking history |
| query_traces | Checking automation runs |
| query_system | Checking system health |
| read_config_file | Reading configuration |
| write_config_file / patch_config_file | Updating configuration |
| modify_automation | Updating the automation |
| modify_script | Updating the script |
| modify_scene | Updating the scene |
| modify_zones | Updating zones |
| modify_areas | Updating areas |
| rename_entities | Renaming |
| manage_helpers | Updating helpers |
| manage_labels | Updating labels |
| manage_monitored | Updating monitoring |
| manage_notification_filters | Updating alert filters |
| call_service | Controlling a device |
| reload_config | Reloading Home Assistant |
| verify_change | Verifying the change |
| memory_note | Taking a note |
| ask_user | Asking you a question |

A test asserts every registered tool has an explicit label (no fallbacks
in production).

### 4.5 SSE protocol and panel

New SSE events from `routes_chat.py`:

- `text_delta` — `{"text": "..."}`
- `status` — `{"phase": "thinking"|"tool"|"cancelled", "tool": str|null, "label": str}`

Existing `text`, `tool_call`, `tool_result`, `done`, `error` unchanged.
The 15 s heartbeat stays (a long tool call still produces silence).

Panel (`ui/src`):

- `api.ts` parses the two new events; `cancelChat()` posts to the cancel
  endpoint.
- `types.ts`: `ChatFragment` gains `{ kind: "draft"; text: string }`.
  `ChatItem` gains `status?: { label: string; phase: string }`.
- `applyEvent`: `text_delta` appends to the trailing `draft` fragment of
  the pending assistant item (creating it if absent); `text` removes any
  trailing `draft` fragment and appends the final `text` fragment (so the
  persisted, exact provider text always wins); `status` sets
  `item.status`; `done` clears it.
- `ThinkingIndicator` takes `label: string` and renders it beside the
  pulsing dots; no timer, no phrase list. `Message.tsx` passes
  `item.status?.label ?? "Thinking"`. A `phase: "cancelled"` status renders
  "Stopping after the current step…" until `done`.
- `Composer`: while `sending`, the send button becomes a Stop button
  (`■`, `title="Stop"`); clicking calls `cancelChat()`, disables the button
  and shows "Stopping…". The textarea stays disabled during a turn as today.
  The Stop button does not abort the fetch; the stream closes when the
  server emits `done`.
- Rendering of `draft` fragments uses the same markdown component as
  `text`.

### 4.6 One derived turn state

New pure module `ui/src/lib/turnState.ts`:

```ts
export interface TurnState {
  pendingApproval: { planIds: string[] } | null;
  approvalContexts: ApprovalContext[];
  pendingQuestion: PendingQuestion | null;
}
export function deriveTurnState(items: ChatItem[]): TurnState;
```

It absorbs `detectPendingApproval`, `findApprovalContexts`,
`findPendingQuestion`, `planIdsFromItems`, `planIdsFromRecords`,
`buildApprovalContext`, and `_callNeedsApproval` from `App.tsx`
(moved, not rewritten). `ApprovalContext` and `PendingQuestion` move to
`types.ts`. `App.tsx` calls it in exactly three places: after
initial hydration, at the end of a streamed turn, and at the end of the
recovery poll. The stream-path `turnSawPreview` flag is deleted; the
end-of-turn call replaces it. Behaviour is identical; the change is that
one function is the only source of truth.

Vitest is added to the UI (`vitest` devDependency, `npm test` script) with
one test file, `turnState.test.ts`, covering: no items; preview in last
assistant turn; preview followed by a user message; confirmation_required;
plan + card ids; ask_user question; question already answered. The
Dockerfile's UI build is unchanged; CI's UI job runs `npm test` after
`typecheck`.

### 4.7 CLI

`src/mylo/scripts/chat.py` prints `TextDeltaEvent` text inline (no newline)
and ignores `StatusEvent`. It already prints `TextEvent`; to avoid double
output it prints a `TextEvent` only when no deltas were received for that
model call (tracked with a boolean reset per `StatusEvent("thinking")`).

### 4.8 Error handling

- Provider stream error after some deltas: the loop persists the partial
  text as an assistant block, then re-raises; the route emits `error` as
  today. History remains valid.
- Cancel during the provider call: handled in §4.2; no `error` event.
- Cancel with no active turn: no-op response.
- SSE drop mid-stream: unchanged recovery (`pollUntilTurnCompletes`);
  hydration produces `text` fragments only, so no `draft` fragment survives
  a reload.

### 4.9 Testing

Python (`tests/unit`):
- `test_anthropic_provider.py`: `stream()` yields deltas then a
  `ProviderResponse` equal to what `message()` builds from the same final
  message (fake SDK stream object with `text_stream`/`get_final_message`).
- `test_tool_loop.py`: fake provider with `stream()` yields
  `TextDeltaEvent`s then `TextEvent`; fake provider without `stream()`
  yields no deltas and one `TextEvent`; `StatusEvent` order
  (thinking → tool → …); cancel before call → `stop_reason="cancelled"`,
  no provider call; cancel during call → partial text persisted as one
  assistant block, `done` cancelled; cancel during tool → tool result
  persisted, loop stops after the batch.
- `test_status_labels.py`: every registered tool has a label.
- `test_routes_chat_emit.py`: `text_delta` and `status` frames emitted;
  cancel endpoint sets the event and returns `cancelling` correctly;
  cancelled turn still emits `done` and clears `turn_active`.

UI: `tsc -b --noEmit` clean; `npm test` green.

## 5. Rollout

Changelog entry `## [1.7.0]`. Release via `scripts/release.sh 1.7.0`
immediately after merge (merge = release). Prompt version unchanged
(0.7.0); no prompt text changes in this release.
