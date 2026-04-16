import { useCallback, useEffect, useRef, useState } from "react";
import {
  clearConversation,
  fetchConversation,
  streamChat,
  type ServerEvent,
} from "./api";
import { ApprovalCard } from "./components/ApprovalCard";
import { Composer } from "./components/Composer";
import { Header, type Tab } from "./components/Header";
import { MemoryTab } from "./components/MemoryTab";
import { Message } from "./components/Message";
import { hydrateFromMessages, isTurnComplete } from "./hydrate";
import { useSession } from "./store";
import type { ChatFragment, ChatItem, ToolCallRecord } from "./types";

function randomId() {
  return Math.random().toString(36).slice(2);
}

export default function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [items, setItems] = useState<ChatItem[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // When the last turn's tool output included a dry-run preview, we
  // render the ApprovalCard inline at the tail of the conversation
  // and enable Apply. Approval is consumed per-turn.
  const [pendingApproval, setPendingApproval] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const recordTurn = useSession((s) => s.recordTurn);
  const resetSession = useSession((s) => s.reset);

  useEffect(() => {
    if (tab === "chat") endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items, tab]);

  const handleClear = useCallback(async () => {
    await clearConversation();
    setItems([]);
    setError(null);
    setPendingApproval(false);
    resetSession();
  }, [resetSession]);

  const handleSubmit = useCallback(
    async (message: string) => {
      // Slash commands run locally against the server's REST endpoints.
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
            setPendingApproval(true);
          }
          applyEvent(event, assistantId, toolCallsById, setItems, recordTurn, setError);
        }
      } catch (exc) {
        setError(
          `${exc instanceof Error ? exc.message : String(exc)} — waiting for Mylo to come back…`,
        );
        const recovered = await pollUntilTurnCompletes(assistantId);
        if (recovered) setError(null);
      } finally {
        setItems((prev) =>
          prev.map((it) => (it.id === assistantId ? { ...it, pending: false } : it)),
        );
        setSending(false);
        if (turnSawPreview) setPendingApproval(true);
      }
    },
    [handleClear, pendingApproval, recordTurn],
  );

  const pollUntilTurnCompletes = useCallback(
    async (_assistantId: string, timeoutMs = 90_000): Promise<boolean> => {
      const deadline = Date.now() + timeoutMs;
      let attempt = 0;
      while (Date.now() < deadline) {
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
    await handleSubmit("Yes, apply the change.");
  }, [handleSubmit]);

  const handleReject = useCallback(() => {
    setPendingApproval(false);
  }, []);

  // Pull the latest tool call + dry-run data so the ApprovalCard can
  // surface a meaningful diff. Walk backwards looking for the most
  // recent tool fragment with a preview payload.
  const approvalContext = findApprovalContext(items);

  return (
    <div className="flex h-full flex-col bg-bg">
      <Header tab={tab} onChange={setTab} />

      {tab === "chat" ? (
        <>
          <main className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
            {items.length === 0 ? (
              <EmptyState />
            ) : (
              items.map((item) => <Message key={item.id} item={item} />)
            )}
            {pendingApproval && approvalContext ? (
              <div style={{ paddingRight: 40 }}>
                <ApprovalCard
                  description={approvalContext.description}
                  diff={approvalContext.diff}
                  meta={approvalContext.meta}
                  tierLabel={approvalContext.tierLabel}
                  onApprove={() => void handleApply()}
                  onReject={handleReject}
                  disabled={sending}
                />
              </div>
            ) : null}
            <div ref={endRef} />
          </main>

          {error ? (
            <div
              className="border-t px-4 py-2 font-mono text-[10px]"
              style={{
                borderColor: "var(--color-border)",
                backgroundColor: "var(--color-error-soft)",
                color: "var(--color-error)",
              }}
            >
              {error}
            </div>
          ) : null}

          <Composer disabled={sending} onSubmit={handleSubmit} />
        </>
      ) : tab === "memory" ? (
        <MemoryTab />
      ) : (
        <ActivityPlaceholder />
      )}
    </div>
  );
}

interface ApprovalContext {
  description: string;
  diff?: { before: string; after: string };
  meta?: string;
  tierLabel: string;
}

function findApprovalContext(items: ChatItem[]): ApprovalContext | null {
  // Walk the most recent assistant turn's fragments in reverse and
  // pull the first tool fragment we see.
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.role !== "assistant") continue;
    for (let j = item.fragments.length - 1; j >= 0; j--) {
      const frag = item.fragments[j];
      if (frag.kind !== "tool") continue;
      return buildApprovalContext(frag.call);
    }
  }
  return null;
}

function buildApprovalContext(call: ToolCallRecord): ApprovalContext {
  const input = call.input || {};
  const data = (call.data as Record<string, unknown> | undefined) || {};

  // Rename preview — the most common write that benefits from a diff.
  if (call.name === "rename_entities") {
    const renames = (input.renames as Array<Record<string, unknown>> | undefined) ?? [];
    if (renames.length > 0) {
      const r = renames[0];
      const before = String(r.entity_id ?? "");
      const after = String(r.new_entity_id ?? r.new_friendly_name ?? "");
      const references = (data.references_found as Record<string, unknown[]> | undefined) ?? {};
      const totalRefs = Object.values(references).reduce(
        (sum, list) => sum + (Array.isArray(list) ? list.length : 0),
        0,
      );
      return {
        description: "Rename entity",
        diff: { before, after },
        meta: `references: ${totalRefs} file${totalRefs === 1 ? "" : "s"}`,
        tierLabel: "TIER-2",
      };
    }
  }

  return {
    description: `${call.name} · dry run`,
    tierLabel: "TIER-2",
  };
}

function _needsApproval(event: ServerEvent): boolean {
  if (
    event.type === "tool_result" &&
    event.status === "ok" &&
    typeof event.data === "object" &&
    event.data !== null &&
    (event.data as Record<string, unknown>).preview === true
  ) {
    return true;
  }
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
  recordTurn: (u: Record<string, number>) => void,
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
        startedAt: performance.now(),
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
      const durationMs =
        existing.startedAt !== undefined ? performance.now() - existing.startedAt : undefined;
      const updated: ToolCallRecord = {
        ...existing,
        state: event.status === "ok" ? "ok" : "error",
        errorCode: event.error_code,
        summary: extractSummary(event.data),
        data: event.data,
        durationMs,
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
      recordTurn(event.usage || {});
      break;
    case "error":
      setError(`${event.errorType}: ${event.message}`);
      break;
  }
}

function extractSummary(data: unknown): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const d = data as Record<string, unknown>;
  if (d.preview === true) return "dry run";
  for (const key of ["summary", "entities_found", "devices_found", "count"]) {
    if (key in d) return `${String(d[key])} ${key.replace(/_/g, " ")}`;
  }
  return undefined;
}

function EmptyState() {
  return (
    <div
      className="flex h-full items-center justify-center text-center"
      style={{ color: "var(--color-text-muted)" }}
    >
      <div>
        <div
          className="font-mono text-[11px] uppercase tracking-label"
          style={{ color: "var(--color-text-dim)" }}
        >
          Session empty
        </div>
        <div
          className="mt-2 font-sans text-[13px]"
          style={{ color: "var(--color-text-muted)" }}
        >
          Try: <em>what lights are on in the basement?</em>
        </div>
      </div>
    </div>
  );
}

function ActivityPlaceholder() {
  return (
    <div className="flex-1 flex items-center justify-center px-4">
      <div className="text-center">
        <div
          className="font-mono text-[11px] uppercase tracking-label"
          style={{ color: "var(--color-text-dim)" }}
        >
          Activity
        </div>
        <div
          className="mt-2 font-sans text-[13px]"
          style={{ color: "var(--color-text-muted)" }}
        >
          Audit timeline view lands with M12.
        </div>
      </div>
    </div>
  );
}
