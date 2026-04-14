import { useCallback, useEffect, useRef, useState } from "react";
import { clearConversation, streamChat, type ServerEvent } from "./api";
import { Composer } from "./components/Composer";
import { Message } from "./components/Message";
import type { ChatFragment, ChatItem, DoneEvent, ToolCallRecord } from "./types";

function randomId() {
  return Math.random().toString(36).slice(2);
}

export default function App() {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [sending, setSending] = useState(false);
  const [lastUsage, setLastUsage] = useState<DoneEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items]);

  const handleClear = useCallback(async () => {
    await clearConversation();
    setItems([]);
    setLastUsage(null);
    setError(null);
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
      const userId = randomId();
      const assistantId = randomId();

      setItems((prev) => [
        ...prev,
        { id: userId, role: "user", fragments: [{ kind: "text", text: message }], pending: false },
        { id: assistantId, role: "assistant", fragments: [], pending: true },
      ]);
      setSending(true);

      const toolCallsById = new Map<string, ToolCallRecord>();

      try {
        for await (const event of streamChat(message)) {
          applyEvent(event, assistantId, toolCallsById, setItems, setLastUsage, setError);
        }
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        setItems((prev) =>
          prev.map((it) => (it.id === assistantId ? { ...it, pending: false } : it)),
        );
        setSending(false);
      }
    },
    [handleClear],
  );

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

      <Composer disabled={sending} onSubmit={handleSubmit} />
    </div>
  );
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
