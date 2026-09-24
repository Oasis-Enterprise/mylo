// Copyright 2026 Maxwell Monson / Oasis Enterprise LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ackVerification,
  cancelChat,
  fetchCatchup,
  fetchConversation,
  fetchFindings,
  fetchStatus,
  newConversation,
  streamChat,
  type CatchupData,
  type PendingActionData,
  type ServerEvent,
  type ServerStatus,
  type VerificationData,
} from "./api";
import { FindingsPanel } from "./components/FindingsPanel";
import { ActivityTab } from "./components/ActivityTab";
import { ApprovalCard } from "./components/ApprovalCard";
import { CatchupBanner } from "./components/CatchupBanner";
import { Composer } from "./components/Composer";
import { DashboardPlanCard } from "./components/DashboardPlanCard";
import { Header, type Tab } from "./components/Header";
import { MemoryTab } from "./components/MemoryTab";
import { Message } from "./components/Message";
import { QuestionCard } from "./components/QuestionCard";
import { VerificationCard } from "./components/VerificationCard";
import { hydrateFromMessages, isTurnComplete } from "./hydrate";
import { describeError } from "./lib/errors";
import { deriveTurnState } from "./lib/turnState";
import { useSession } from "./store";
import type { ChatFragment, ChatItem, ToolCallRecord } from "./types";

function randomId() {
  return Math.random().toString(36).slice(2);
}

interface SubmitOptions {
  approved?: boolean;
  approvedPlanIds?: string[];
}

export default function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [items, setItems] = useState<ChatItem[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<{ type: string; message: string; human?: string } | null>(
    null,
  );
  // Set while the stream has dropped mid-turn and we're polling the
  // server for the finished turn. Rendered as a calm status line, not
  // an error — the server is still working.
  const [reconnecting, setReconnecting] = useState(false);
  // Stop was clicked; cleared when the turn ends.
  const [stopping, setStopping] = useState(false);
  // Set when the last turn included a previewed write. planIds are the
  // plan_dashboard ids in that turn; Apply sends them back so the
  // server can bind approval to exactly those plans.
  const [pendingApproval, setPendingApproval] = useState<{ planIds: string[] } | null>(null);
  const [queuedApply, setQueuedApply] = useState<{ message: string; planIds: string[] } | null>(
    null,
  );
  const [composerDraft, setComposerDraft] = useState<{ text: string; nonce: number } | null>(
    null,
  );
  const [catchup, setCatchup] = useState<CatchupData | null>(null);
  // Inline findings list, toggled from the header badge. statusRefresh
  // bumps force the header to re-poll after a dismissal changes the count.
  const [findings, setFindings] = useState<PendingActionData[] | null>(null);
  const [statusRefresh, setStatusRefresh] = useState(0);
  // Unacknowledged background-verification outcomes from the last
  // status poll — rendered as dismissible cards in the chat stream.
  const [verifications, setVerifications] = useState<VerificationData[]>([]);
  // Ids dismissed locally. A poll can land before the ack round-trip
  // persists server-side (or the ack can fail outright), so we filter
  // dismissed ids out of every status response rather than trusting
  // the server to have caught up — once the user asked for it gone,
  // it stays gone for this session regardless.
  const dismissedVerifications = useRef<Set<string>>(new Set());
  const endRef = useRef<HTMLDivElement>(null);
  const recordTurn = useSession((s) => s.recordTurn);
  const resetSession = useSession((s) => s.reset);
  const sessionCost = useSession((s) => s.costUsd);
  const setModel = useSession((s) => s.setModel);
  const setToolLabels = useSession((s) => s.setToolLabels);

  const handleStatus = useCallback(
    (s: ServerStatus) => {
      if (s.model) setModel(s.model, s.provider);
      if (s.tools) setToolLabels(s.tools);
      setVerifications(
        (s.verifications ?? []).filter((v) => !dismissedVerifications.current.has(v.id)),
      );
    },
    [setModel, setToolLabels],
  );

  const handleDismissVerification = useCallback(async (id: string) => {
    dismissedVerifications.current.add(id);
    setVerifications((prev) => prev.filter((v) => v.id !== id));
    await ackVerification(id);
  }, []);

  useEffect(() => {
    if (tab === "chat") endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items, tab]);

  const handleNewConversation = useCallback(async () => {
    await newConversation();
    setItems([]);
    setError(null);
    setPendingApproval(null);
    setCatchup(null);
    resetSession();
  }, [resetSession]);

  const handleSubmit = useCallback(
    async (message: string, opts: SubmitOptions = {}) => {
      // Slash commands run locally against the server's REST endpoints.
      if (message === "/clear" || message === "/new") {
        await handleNewConversation();
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
      setCatchup(null);
      // Approval comes ONLY from the Apply button. Answering a question
      // card or typing a message never authorizes a write.
      const approved = Boolean(opts.approved);
      const approvedPlanIds = opts.approvedPlanIds ?? [];
      setPendingApproval(null);

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
        for await (const event of streamChat(message, {
          approved,
          approvedPlanIds,
          sessionCostUsd: sessionCost,
        })) {
          applyEvent(event, assistantId, toolCallsById, setItems, recordTurn, setError);
        }
      } catch (exc) {
        const detail = exc instanceof Error ? exc.message : String(exc);
        const errType = exc instanceof Error ? exc.name : "Error";
        if (detail.includes("turn_in_progress")) {
          // The server refused a concurrent turn — the previous one is
          // still running (SSE drop doesn't cancel it). This message was
          // NOT processed; the poll below renders the running turn's
          // result once it lands.
          setError({
            type: errType,
            message: detail,
            human:
              "Mylo is still finishing the previous request — this message wasn't sent. Try again in a moment.",
          });
        } else {
          setReconnecting(true);
        }
        const recovered = await pollUntilTurnCompletes(assistantId);
        setReconnecting(false);
        if (recovered) {
          if (!detail.includes("turn_in_progress")) setError(null);
        } else if (!detail.includes("turn_in_progress")) {
          setError({
            type: errType,
            message: detail,
            human: "Lost the connection and couldn't catch up — reload the panel to see Mylo's reply.",
          });
        }
      } finally {
        setItems((prev) =>
          prev.map((it) =>
            it.id === assistantId
              ? {
                  ...it,
                  pending: false,
                  status: undefined,
                  fragments: it.fragments.map<ChatFragment>((f) =>
                    f.kind === "draft" ? { kind: "text", text: f.text } : f,
                  ),
                }
              : it,
          ),
        );
        setSending(false);
        setStopping(false);
      }
    },
    [handleNewConversation, recordTurn, sessionCost],
  );

  const pollUntilTurnCompletes = useCallback(
    async (_assistantId: string, maxWaitMs = 15 * 60_000): Promise<boolean> => {
      // The server keeps running the turn after the stream drops. Poll
      // /api/status while it reports turn_active, then rehydrate the
      // finished turn. The ceiling is a safety net, not the expected
      // wait — a big dashboard plan can legitimately run for minutes.
      const deadline = Date.now() + maxWaitMs;
      let attempt = 0;
      while (Date.now() < deadline) {
        const delay = Math.min(2000 + attempt * 1000, 10_000);
        await new Promise((r) => setTimeout(r, delay));
        attempt += 1;
        try {
          const status = await fetchStatus();
          if (status.turn_active) continue;
          const messages = await fetchConversation();
          if (!isTurnComplete(messages)) continue;
          const hydrated = hydrateFromMessages(messages);
          setItems(hydrated);
          // The done event never arrived either; credit the turn from
          // the server's record so the budget/cost counters stay right.
          if (status.last_turn?.usage) recordTurn(status.last_turn.usage);
          return true;
        } catch {
          // Server still rebooting — keep trying.
        }
      }
      return false;
    },
    [recordTurn],
  );

  useEffect(() => {
    (async () => {
      try {
        const [messages, catchupData] = await Promise.all([
          fetchConversation(),
          fetchCatchup(),
        ]);
        const hydrated = hydrateFromMessages(messages);
        if (hydrated.length > 0) {
          setItems(hydrated);
          setPendingApproval(deriveTurnState(hydrated).pendingApproval);
        }
        if (catchupData.show_banner) {
          setCatchup(catchupData);
        }
      } catch {
        // Non-fatal — user can still start a new conversation.
      }
    })();
  }, []);

  // The single source of truth for everything derived from the
  // conversation: previewed writes awaiting Apply, and any unanswered
  // ask_user question.
  const turn = useMemo(() => deriveTurnState(items), [items]);
  const approvalContexts = turn.approvalContexts;
  const pendingQuestion = turn.pendingQuestion;

  // Re-derive approval state exactly once per turn, when `sending` flips
  // back to false — the same path whether the stream finished normally
  // or the recovery poll rehydrated the turn.
  const wasSending = useRef(false);
  useEffect(() => {
    if (wasSending.current && !sending) {
      setPendingApproval(deriveTurnState(items).pendingApproval);
    }
    wasSending.current = sending;
  }, [sending, items]);

  // Every previewed change in the latest assistant turn that's awaiting
  // approval. The model can dry-run several writes in one turn; they're
  // all surfaced together and applied with a single click (the approval
  // flag authorizes the whole turn server-side).
  const approvalCount = approvalContexts.length;
  const planContexts = approvalContexts.filter((c) => c.plan !== undefined);
  const cardContexts = approvalContexts.filter((c) => c.card !== undefined);
  const otherContexts = approvalContexts.filter(
    (c) => c.plan === undefined && c.card === undefined,
  );

  const handleApply = useCallback(async (selectedIndices?: number[]) => {
    const all = pendingApproval?.planIds ?? [];
    const chosen = selectedIndices ?? approvalContexts.map((_, i) => i);
    const ids = chosen.map((i) => approvalContexts[i]?.previewId ?? "").filter(Boolean);
    // Plan/card ids are not per-item selectable; always include them.
    const planIds = Array.from(new Set([...all.filter((id) => !id.startsWith("pv_")), ...ids]));
    const total = approvalContexts.length;
    const message =
      total <= 1
        ? "Yes, apply the change."
        : chosen.length === total
          ? `Yes, apply all ${total} changes.`
          : `Yes, apply these ${chosen.length} of ${total} changes: ${chosen.map((i) => approvalContexts[i].description).join("; ")}. Do not apply the others.`;
    if (sending) {
      // Previous stream still closing out — queue the submit so it
      // fires the moment sending clears, and give the button visible
      // feedback.
      setQueuedApply({ message, planIds });
      return;
    }
    await handleSubmit(message, { approved: true, approvedPlanIds: planIds });
  }, [handleSubmit, sending, approvalContexts, pendingApproval]);

  const handleReject = useCallback(() => {
    setPendingApproval(null);
    setQueuedApply(null);
  }, []);

  const handleStop = useCallback(async () => {
    setStopping(true);
    setItems((prev) =>
      prev.map((it) =>
        it.pending
          ? { ...it, status: { phase: "cancelling", label: "Stopping after the current step…" } }
          : it,
      ),
    );
    await cancelChat();
  }, []);

  const handleModify = useCallback(() => {
    setComposerDraft({ text: "Change the plan: ", nonce: Date.now() });
  }, []);

  // Flush a queued apply once the previous turn's stream finishes.
  useEffect(() => {
    if (!sending && queuedApply) {
      const q = queuedApply;
      setQueuedApply(null);
      void handleSubmit(q.message, { approved: true, approvedPlanIds: q.planIds });
    }
  }, [sending, queuedApply, handleSubmit]);

  const handleToggleFindings = useCallback(async () => {
    if (findings !== null) {
      setFindings(null);
      return;
    }
    setFindings(await fetchFindings());
  }, [findings]);


  return (
    <div className="flex h-full flex-col bg-bg">
      <Header
        tab={tab}
        onChange={setTab}
        onNewConversation={() => void handleNewConversation()}
        onToggleFindings={() => void handleToggleFindings()}
        refreshSignal={statusRefresh}
        onStatus={handleStatus}
      />

      {tab === "chat" ? (
        <>
          <main className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
            {findings !== null ? (
              <FindingsPanel
                findings={findings}
                onChanged={() => setStatusRefresh((n) => n + 1)}
                onClose={() => setFindings(null)}
              />
            ) : null}
            {items.length === 0 ? (
              <EmptyState />
            ) : (
              items.map((item) => <Message key={item.id} item={item} />)
            )}
            {!sending
              ? verifications.map((v) => (
                  <div key={v.id} className="sm:pr-10">
                    <VerificationCard
                      item={v}
                      onDismiss={(id) => void handleDismissVerification(id)}
                    />
                  </div>
                ))
              : null}
            {catchup ? (
              <CatchupBanner
                data={catchup}
                onDismiss={() => setCatchup(null)}
              />
            ) : null}
            {pendingQuestion && !sending ? (
              <div className="sm:pr-10">
                <QuestionCard
                  question={pendingQuestion}
                  onSelect={(label) => void handleSubmit(label)}
                  disabled={sending}
                />
              </div>
            ) : null}
            {pendingApproval && approvalCount > 0 ? (
              <div className="sm:pr-10">
                {planContexts.length > 0 || cardContexts.length > 0 ? (
                  <DashboardPlanCard
                    plans={planContexts.map((c) => c.plan!)}
                    cards={cardContexts.map((c) => c.card!)}
                    otherChanges={otherContexts.map((c) => c.description)}
                    onApprove={() => void handleApply()}
                    onReject={handleReject}
                    onModify={handleModify}
                    applying={queuedApply !== null}
                    destructive={approvalContexts.some((c) => c.destructive)}
                  />
                ) : (
                  <ApprovalCard
                    items={approvalContexts}
                    onApprove={(sel) => void handleApply(sel)}
                    onReject={handleReject}
                    applying={queuedApply !== null}
                  />
                )}
              </div>
            ) : null}
            <div ref={endRef} />
          </main>

          {reconnecting ? (
            <div
              role="status"
              aria-live="polite"
              className="border-t px-4 py-2 font-sans text-[13px]"
              style={{
                borderColor: "var(--color-border)",
                backgroundColor: "var(--color-surface)",
                color: "var(--color-text-muted)",
              }}
            >
              Reconnecting — Mylo is still working on your request; the reply will
              appear here when it finishes.
            </div>
          ) : null}
          {error ? (
            <div
              role="alert"
              className="border-t px-4 py-2 font-sans text-[13px]"
              style={{
                borderColor: "var(--color-border)",
                backgroundColor: "var(--color-error-soft)",
                color: "var(--color-text)",
              }}
            >
              <div className="flex items-start justify-between gap-2">
                <span>{error.human ?? describeError(error.type, error.message)}</span>
                <button
                  type="button"
                  aria-label="Dismiss error"
                  onClick={() => setError(null)}
                  className="font-mono text-[10px] px-1"
                  style={{ color: "var(--color-text-dim)" }}
                >
                  ✕
                </button>
              </div>
              <details className="mt-1">
                <summary
                  className="cursor-pointer font-mono text-[10px]"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  Details
                </summary>
                <pre
                  className="mt-1 whitespace-pre-wrap break-words font-mono text-[10px]"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  {`${error.type}: ${error.message}`}
                </pre>
              </details>
            </div>
          ) : null}

          <Composer
            disabled={sending}
            onSubmit={(m) => handleSubmit(m)}
            draft={composerDraft}
            onStop={() => void handleStop()}
            stopping={stopping}
          />
        </>
      ) : tab === "memory" ? (
        <MemoryTab />
      ) : (
        <ActivityTab />
      )}
    </div>
  );
}

function applyEvent(
  event: ServerEvent,
  assistantId: string,
  toolCallsById: Map<string, ToolCallRecord>,
  setItems: React.Dispatch<React.SetStateAction<ChatItem[]>>,
  recordTurn: (u: Record<string, number>) => void,
  setError: (e: { type: string; message: string }) => void,
) {
  switch (event.type) {
    case "text": {
      setItems((prev) =>
        prev.map((it) =>
          it.id === assistantId
            ? {
                ...it,
                fragments: [
                  ...it.fragments.filter((f) => f.kind !== "draft"),
                  { kind: "text", text: event.text },
                ],
              }
            : it,
        ),
      );
      break;
    }
    case "text_delta": {
      setItems((prev) =>
        prev.map((it) => {
          if (it.id !== assistantId) return it;
          const last = it.fragments[it.fragments.length - 1];
          if (last && last.kind === "draft") {
            return {
              ...it,
              fragments: [...it.fragments.slice(0, -1), { kind: "draft", text: last.text + event.text }],
            };
          }
          return { ...it, fragments: [...it.fragments, { kind: "draft", text: event.text }] };
        }),
      );
      break;
    }
    case "status": {
      setItems((prev) =>
        prev.map((it) => {
          if (it.id !== assistantId) return it;
          // Keep "Stopping…" sticky until the server confirms the stop.
          if (it.status?.phase === "cancelling" && event.phase !== "cancelled") return it;
          return { ...it, status: { phase: event.phase, label: event.label } };
        }),
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
      setItems((prev) =>
        prev.map((it) => (it.id === assistantId ? { ...it, status: undefined } : it)),
      );
      break;
    case "error":
      setError({ type: event.errorType, message: event.message });
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
    <div className="flex h-full items-center justify-center px-6">
      <div className="max-w-md space-y-5">
        <div className="text-center">
          <div
            className="font-mono text-[13px] font-extrabold tracking-wordmark"
            style={{ color: "var(--color-accent)" }}
          >
            MYLO
          </div>
          <div
            className="mt-1 font-sans text-[13px]"
            style={{ color: "var(--color-text-muted)" }}
          >
            Your AI home assistant. Ask me anything about your Home Assistant setup.
          </div>
        </div>
        <div
          className="rounded border p-4 space-y-3"
          style={{
            borderColor: "var(--color-border)",
            backgroundColor: "var(--color-surface)",
          }}
        >
          <div
            className="font-mono text-[10px] uppercase tracking-label"
            style={{ color: "var(--color-text-dim)" }}
          >
            Quick starts
          </div>
          <QuickStart
            label="Explore"
            text="What lights are on right now?"
          />
          <QuickStart
            label="Organize"
            text="Help me rename and organize my kitchen entities"
          />
          <QuickStart
            label="Automate"
            text="Create an automation that turns off lights at bedtime"
          />
          <QuickStart
            label="Monitor"
            text="Set up sensor monitoring for my home"
          />
        </div>
      </div>
    </div>
  );
}

function QuickStart({ label, text }: { label: string; text: string }) {
  return (
    <div className="flex items-start gap-2.5">
      <span
        className="mt-0.5 shrink-0 rounded-tag border px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-label"
        style={{
          borderColor: "var(--color-border-accent)",
          color: "var(--color-accent)",
          backgroundColor: "var(--color-accent-soft)",
        }}
      >
        {label}
      </span>
      <span
        className="font-sans text-[12.5px]"
        style={{ color: "var(--color-text)" }}
      >
        {text}
      </span>
    </div>
  );
}

