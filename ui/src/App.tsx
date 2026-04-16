import { useCallback, useEffect, useRef, useState } from "react";
import {
  clearConversation,
  fetchConversation,
  streamChat,
  type ServerEvent,
} from "./api";
import { Composer } from "./components/Composer";
import { Message } from "./components/Message";
import { hydrateFromMessages, isTurnComplete } from "./hydrate";
import type { ChatFragment, ChatItem, DoneEvent, ToolCallRecord } from "./types";

function randomId() {
  return Math.random().toString(36).slice(2);
}

export default function App() {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [sending, setSending] = useState(false);
  const [lastUsage, setLastUsage] = useState<DoneEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  // When the last turn's tool output included a dry-run preview, we offer
  // an Apply button that sends the user's next message with approved=true.
  const [pendingApproval, setPendingApproval] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items]);

  const handleClear = useCallback(async () => {
    await clearConversation();
    setItems([]);
    setLastUsage(null);
    setError(null);
    setPendingApproval(false);
  }, []);

  const handleSubmit = useCallback(
    async (message: string) => {
      // Slash commands run locally against the server's REST endpoints
      // instead of being sent to the LLM.
      if (message === "/clear") {
        await handleClear();
        return;
      }
      if (message === "/help") {
        setItems((prev) => [
          ...prev,
          {
            id: randomId(),
            role: "assistant",
            fragments: [
              {
                kind: "text",
                text:
                  "**Slash commands**\n\n" +
                  "- `/clear` — wipe the conversation\n" +
                  "- `/help` — show this help",
              },
            ],
            pending: false,
          },
        ]);
        return;
      }

      setError(null);
      // Approved is consumed per-turn: if the user hit Apply on the last
      // preview, this turn gets the flag; after that it resets.
      const approvedForThisTurn = pendingApproval;
      setPendingApproval(false);

      const userId = randomId();
      const assistantId = randomId();

      setItems((prev) => [
        ...prev,
        { id: userId, role: "user", fragments: [{ kind: "text", text: message }], pending: false },
        { id: assistantId, role: "assistant", fragments: [], pending: true },
      ]);
      setSending(true);

      const toolCallsById = new Map<string, ToolCallRecord>();
      let turnSawPreview = false;

      try {
        for await (const event of streamChat(message, { approved: approvedForThisTurn })) {
          if (_needsApproval(event)) {
            turnSawPreview = true;
          }
          applyEvent(event, assistantId, toolCallsById, setItems, setLastUsage, setError);
        }
      } catch (exc) {
        // SSE connection error during a turn — could be legitimate
        // (network) or expected (reload_all restarted HA's ingress,
        // killing the tunnel). Fall back to polling /api/conversation
        // until the completed turn appears server-side. The reload
        // finishes in ~10-30s typically.
        setError(
          `${exc instanceof Error ? exc.message : String(exc)} — waiting for Mylo to come back…`,
        );
        const recovered = await pollUntilTurnCompletes(assistantId);
        if (recovered) {
          setError(null);
        }
      } finally {
        setItems((prev) =>
          prev.map((it) => (it.id === assistantId ? { ...it, pending: false } : it)),
        );
        setSending(false);
        if (turnSawPreview) setPendingApproval(true);
      }
    },
    [handleClear, pendingApproval],
  );

  // On SSE error during a turn, wait for the server to come back and
  // rehydrate from whatever it has. Returns true if we recovered to a
  // genuinely-complete state (assistant turn with no trailing tool_use).
  // Premature recoveries on a still-pending tool_use would leave the
  // user staring at a half-rendered state and miss the actual answer.
  const pollUntilTurnCompletes = useCallback(
    async (_assistantId: string, timeoutMs = 90_000): Promise<boolean> => {
      const deadline = Date.now() + timeoutMs;
      let attempt = 0;
      while (Date.now() < deadline) {
        // Back off: 3s, 3s, 5s, 5s, 8s, ... up to 15s.
        const delay = Math.min(3000 + attempt * 1000, 15_000);
        await new Promise((r) => setTimeout(r, delay));
        attempt += 1;
        try {
          const messages = await fetchConversation();
          if (!isTurnComplete(messages)) continue;
          setItems(hydrateFromMessages(messages));
          return true;
        } catch {
          // Server still rebooting — keep trying.
        }
      }
      return false;
    },
    [],
  );

  // Rehydrate conversation on initial mount so a refresh doesn't lose
  // prior turns.
  useEffect(() => {
    (async () => {
      try {
        const messages = await fetchConversation();
        const hydrated = hydrateFromMessages(messages);
        if (hydrated.length > 0) setItems(hydrated);
      } catch {
        // Non-fatal — user can still start a new conversation.
      }
    })();
  }, []);

  const handleApply = useCallback(async () => {
    // The user clicks Apply without typing — send a short confirmation
    // message with approved=true. The model's retry with dry_run=false
    // is what actually writes.
    await handleSubmit("Yes, apply the change.");
  }, [handleSubmit]);

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-border bg-elevated px-4 py-3">
        <div>
          <h1 className="text-base font-semibold">Mylo</h1>
          <div className="text-xs text-mute">
            {lastUsage ? (
              <UsageSummary usage={lastUsage} />
            ) : (
              "Ready. Ask about your Home Assistant setup."
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={() => void handleClear()}
          disabled={sending}
          className="text-xs text-mute hover:text-gray-200 disabled:opacity-40"
        >
          Clear conversation
        </button>
      </header>

      <main className="flex-1 overflow-y-auto p-4 space-y-3">
        {items.length === 0 ? (
          <EmptyState />
        ) : (
          items.map((item) => <Message key={item.id} item={item} />)
        )}
        <div ref={endRef} />
      </main>

      {error ? (
        <div className="border-t border-rose-900/40 bg-rose-950/40 px-4 py-2 text-xs text-rose-300">
          {error}
        </div>
      ) : null}

      {pendingApproval ? (
        <div className="flex items-center justify-between gap-3 border-t border-indigo-500/30 bg-indigo-950/30 px-4 py-2 text-sm">
          <div className="text-indigo-200">
            Mylo is waiting for you to approve the change above.
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setPendingApproval(false)}
              disabled={sending}
              className="rounded border border-border px-3 py-1 text-xs text-mute hover:text-gray-200 disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void handleApply()}
              disabled={sending}
              className="rounded bg-indigo-600 px-3 py-1 text-xs font-medium hover:bg-indigo-500 disabled:opacity-40"
            >
              Apply
            </button>
          </div>
        </div>
      ) : null}

      <Composer disabled={sending} onSubmit={handleSubmit} />
    </div>
  );
}

function _needsApproval(event: ServerEvent): boolean {
  // Tier-2 dry-run preview completed successfully.
  if (
    event.type === "tool_result" &&
    event.status === "ok" &&
    typeof event.data === "object" &&
    event.data !== null &&
    (event.data as Record<string, unknown>).preview === true
  ) {
    return true;
  }
  // Tier-2/3 blocked without approval.
  if (
    event.type === "tool_result" &&
    event.status === "error" &&
    event.error_code === "confirmation_required"
  ) {
    return true;
  }
  return false;
}

function applyEvent(
  event: ServerEvent,
  assistantId: string,
  toolCallsById: Map<string, ToolCallRecord>,
  setItems: React.Dispatch<React.SetStateAction<ChatItem[]>>,
  setLastUsage: (d: DoneEvent) => void,
  setError: (s: string) => void,
) {
  switch (event.type) {
    case "text": {
      setItems((prev) =>
        prev.map((it) =>
          it.id === assistantId
            ? { ...it, fragments: [...it.fragments, { kind: "text", text: event.text }] }
            : it,
        ),
      );
      break;
    }
    case "tool_call": {
      const record: ToolCallRecord = {
        id: event.id,
        name: event.name,
        input: event.input,
        state: "pending",
      };
      toolCallsById.set(event.id, record);
      setItems((prev) =>
        prev.map((it) =>
          it.id === assistantId
            ? { ...it, fragments: [...it.fragments, { kind: "tool", call: record }] }
            : it,
        ),
      );
      break;
    }
    case "tool_result": {
      const existing = toolCallsById.get(event.id);
      if (!existing) break;
      const updated: ToolCallRecord = {
        ...existing,
        state: event.status === "ok" ? "ok" : "error",
        errorCode: event.error_code,
        summary: extractSummary(event.data),
        data: event.data,
      };
      toolCallsById.set(event.id, updated);
      setItems((prev) =>
        prev.map((it) =>
          it.id === assistantId
            ? {
                ...it,
                fragments: it.fragments.map<ChatFragment>((f) =>
                  f.kind === "tool" && f.call.id === event.id ? { kind: "tool", call: updated } : f,
                ),
              }
            : it,
        ),
      );
      break;
    }
    case "done":
      setLastUsage({ stopReason: event.stop_reason, usage: event.usage });
      break;
    case "error":
      setError(`${event.errorType}: ${event.message}`);
      break;
  }
}

function extractSummary(data: unknown): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const d = data as Record<string, unknown>;
  for (const key of ["summary", "entities_found", "devices_found", "count"]) {
    if (key in d) return `${key}=${String(d[key])}`;
  }
  return undefined;
}

function UsageSummary({ usage }: { usage: DoneEvent }) {
  const { input_tokens = 0, output_tokens = 0, cache_read_input_tokens, cache_creation_input_tokens } =
    usage.usage;
  const parts = [`in=${input_tokens}`, `out=${output_tokens}`];
  if (cache_read_input_tokens) parts.push(`cache_read=${cache_read_input_tokens}`);
  if (cache_creation_input_tokens) parts.push(`cache_write=${cache_creation_input_tokens}`);
  return <span>last turn · {parts.join(" ")}</span>;
}

function EmptyState() {
  return (
    <div className="flex h-full items-center justify-center text-center text-mute">
      <div>
        <div className="text-base text-gray-300">Nothing here yet.</div>
        <div className="mt-2 text-xs">
          Try: <em>what lights are on in the basement?</em>
        </div>
      </div>
    </div>
  );
}
