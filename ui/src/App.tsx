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

import { useCallback, useEffect, useRef, useState } from "react";
import {
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
import { QuestionCard, type PendingQuestion } from "./components/QuestionCard";
import { hydrateFromMessages, isTurnComplete } from "./hydrate";
import { useSession } from "./store";
import type {
  ChatFragment,
  ChatItem,
  DashboardPlanData,
  StagedCardData,
  ToolCallRecord,
} from "./types";

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
  const [error, setError] = useState<string | null>(null);
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
  const endRef = useRef<HTMLDivElement>(null);
  const recordTurn = useSession((s) => s.recordTurn);
  const resetSession = useSession((s) => s.reset);
  const sessionCost = useSession((s) => s.costUsd);

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
      let turnSawPreview = false;

      try {
        for await (const event of streamChat(message, {
          approved,
          approvedPlanIds,
          sessionCostUsd: sessionCost,
        })) {
          if (_needsApproval(event)) {
            turnSawPreview = true;
            // Don't show the ApprovalCard yet — wait for the turn to
            // finish so the model's trailing text ("please review and
            // click Apply") has rendered before the card appears.
          }
          applyEvent(event, assistantId, toolCallsById, setItems, recordTurn, setError);
        }
      } catch (exc) {
        const detail = exc instanceof Error ? exc.message : String(exc);
        if (detail.includes("turn_in_progress")) {
          // The server refused a concurrent turn — the previous one is
          // still running (SSE drop doesn't cancel it). This message was
          // NOT processed; the poll below renders the running turn's
          // result once it lands.
          setError(
            "Mylo is still finishing the previous request — this message wasn't sent. Try again in a moment.",
          );
        } else {
          setReconnecting(true);
        }
        const recovered = await pollUntilTurnCompletes(assistantId);
        setReconnecting(false);
        if (recovered) {
          if (!detail.includes("turn_in_progress")) setError(null);
        } else if (!detail.includes("turn_in_progress")) {
          setError(
            "Lost the connection and couldn't catch up — reload the panel to see Mylo's reply.",
          );
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
        if (turnSawPreview) {
          setPendingApproval({ planIds: planIdsFromRecords(toolCallsById.values()) });
        }
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
          // The stream died before the tool_result events reached us, so
          // turnSawPreview never fired. Derive the pending approval from
          // the hydrated turn exactly like initial page load does —
          // otherwise a staged plan or dry-run has no Apply button.
          if (detectPendingApproval(hydrated)) {
            setPendingApproval({ planIds: planIdsFromItems(hydrated) });
          }
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
          if (detectPendingApproval(hydrated)) {
            setPendingApproval({ planIds: planIdsFromItems(hydrated) });
          }
        }
        if (catchupData.show_banner) {
          setCatchup(catchupData);
        }
      } catch {
        // Non-fatal — user can still start a new conversation.
      }
    })();
  }, []);

  // Every previewed change in the latest assistant turn that's awaiting
  // approval. The model can dry-run several writes in one turn; they're
  // all surfaced together and applied with a single click (the approval
  // flag authorizes the whole turn server-side).
  const approvalContexts = findApprovalContexts(items);
  const approvalCount = approvalContexts.length;
  const planContexts = approvalContexts.filter((c) => c.plan !== undefined);
  const cardContexts = approvalContexts.filter((c) => c.card !== undefined);
  const otherContexts = approvalContexts.filter(
    (c) => c.plan === undefined && c.card === undefined,
  );

  // A pending ask_user question is derived from the items, not stored:
  // it exists exactly when the latest assistant turn ended on an
  // ask_user result with no user message after it. That makes reload
  // hydration and dismissal (any user message) free.
  const pendingQuestion = findPendingQuestion(items);

  const handleApply = useCallback(async () => {
    const message =
      approvalCount > 1
        ? `Yes, apply all ${approvalCount} changes.`
        : "Yes, apply the change.";
    const planIds = pendingApproval?.planIds ?? [];
    if (sending) {
      // Previous stream still closing out — queue the submit so it
      // fires the moment sending clears, and give the button visible
      // feedback.
      setQueuedApply({ message, planIds });
      return;
    }
    await handleSubmit(message, { approved: true, approvedPlanIds: planIds });
  }, [handleSubmit, sending, approvalCount, pendingApproval]);

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
            {catchup ? (
              <CatchupBanner
                data={catchup}
                onDismiss={() => setCatchup(null)}
              />
            ) : null}
            {pendingQuestion && !sending ? (
              <div style={{ paddingRight: 40 }}>
                <QuestionCard
                  question={pendingQuestion}
                  onSelect={(label) => void handleSubmit(label)}
                  disabled={sending}
                />
              </div>
            ) : null}
            {pendingApproval && approvalCount > 0 ? (
              <div style={{ paddingRight: 40 }}>
                {planContexts.length > 0 || cardContexts.length > 0 ? (
                  <DashboardPlanCard
                    plans={planContexts.map((c) => c.plan!)}
                    cards={cardContexts.map((c) => c.card!)}
                    otherChanges={otherContexts.map((c) => c.description)}
                    onApprove={() => void handleApply()}
                    onReject={handleReject}
                    onModify={handleModify}
                    applying={queuedApply !== null}
                  />
                ) : (
                  <ApprovalCard
                    items={approvalContexts}
                    onApprove={() => void handleApply()}
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
              className="border-t px-4 py-2 font-mono text-[10px]"
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

function detectPendingApproval(items: ChatItem[]): boolean {
  // Walk backwards from the last assistant turn. If any tool fragment
  // has confirmation_required or preview:true, the user hasn't applied
  // yet (otherwise there'd be a follow-up user message with "Yes, apply").
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    // If the most recent message is a user message, the user already
    // responded (either approved or moved on) — no pending approval.
    if (item.role === "user") return false;
    if (item.role !== "assistant") continue;
    for (const frag of item.fragments) {
      if (frag.kind !== "tool") continue;
      if (_callNeedsApproval(frag.call)) return true;
    }
    // Only check the most recent assistant turn.
    return false;
  }
  return false;
}

function findPendingQuestion(items: ChatItem[]): PendingQuestion | null {
  // Walk backwards: a user message means the question (if any) was
  // already answered; otherwise scan the most recent assistant turn
  // for an ask_user result flagged await_user_input.
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.role === "user") return null;
    if (item.role !== "assistant") continue;
    for (let j = item.fragments.length - 1; j >= 0; j--) {
      const frag = item.fragments[j];
      if (frag.kind !== "tool" || frag.call.state !== "ok") continue;
      const data = frag.call.data as Record<string, unknown> | undefined;
      if (!data || data.await_user_input !== true) continue;
      const rawOptions = Array.isArray(data.options) ? data.options : [];
      return {
        question: String(data.question ?? ""),
        options: rawOptions
          .filter((o): o is Record<string, unknown> => !!o && typeof o === "object")
          .map((o) => ({
            label: String(o.label ?? ""),
            value: o.value ? String(o.value) : undefined,
            description: o.description ? String(o.description) : undefined,
          }))
          .filter((o) => o.label),
        allowFreeText: data.allow_free_text !== false,
      };
    }
    return null;
  }
  return null;
}

interface ApprovalContext {
  description: string;
  diff?: { before: string; after: string };
  meta?: string;
  tierLabel: string;
  plan?: DashboardPlanData;
  card?: StagedCardData;
}

function findApprovalContexts(items: ChatItem[]): ApprovalContext[] {
  // Collect EVERY previewed / confirmation-required write in the most
  // recent assistant turn, in the order the model issued them — the model
  // can dry-run several writes per turn and they're approved as a set.
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    // A more recent user message means the user already responded.
    if (item.role === "user") return [];
    if (item.role !== "assistant") continue;
    const contexts: ApprovalContext[] = [];
    for (const frag of item.fragments) {
      if (frag.kind !== "tool") continue;
      if (_callNeedsApproval(frag.call)) {
        contexts.push(buildApprovalContext(frag.call));
      }
    }
    return contexts;
  }
  return [];
}

function _callNeedsApproval(call: ToolCallRecord): boolean {
  if (call.errorCode === "confirmation_required") return true;
  return (
    call.state === "ok" &&
    typeof call.data === "object" &&
    call.data !== null &&
    (call.data as Record<string, unknown>).preview === true
  );
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

  // Service calls — describe "light.turn_on on light.kitchen" so the
  // user can sanity-check target + payload before approving.
  if (call.name === "call_service") {
    const domain = String(input.domain ?? "");
    const service = String(input.service ?? "");
    const target = (input.target as Record<string, unknown> | undefined) ?? {};
    const serviceData = (input.data as Record<string, unknown> | undefined) ?? {};
    const targetEntity = target.entity_id;
    const targetArea = target.area_id;
    const targetDevice = target.device_id;

    const targetLabel = formatTargetList(targetEntity)
      || formatTargetList(targetArea)
      || formatTargetList(targetDevice)
      || "—";

    const description =
      domain && service
        ? `Call ${domain}.${service} on ${targetLabel}`
        : call.name;

    const dataEntries = Object.entries(serviceData);
    const meta =
      dataEntries.length > 0
        ? "data: " + dataEntries.map(([k, v]) => `${k}=${compactValue(v)}`).join(" · ")
        : undefined;

    // call_service is tier-3 when not on the allow-list; we default
    // the label to TIER-3 for the impactful warning.
    const tierLabel = String(data.tier ?? "TIER-3").toUpperCase();

    return { description, meta, tierLabel };
  }

  // Staged custom card — rendered by DashboardPlanCard's StagedCardBlock,
  // not the generic card.
  if (call.name === "stage_custom_card" && data.preview === true && typeof data.element === "string") {
    const card = data as unknown as StagedCardData;
    return {
      description: `Custom card ${card.element} (${card.action === "update" ? "update" : "new"}, ${card.line_count} lines)`,
      card,
      tierLabel: "TIER-2",
    };
  }

  // Dashboard plan — rendered by DashboardPlanCard, not the generic card.
  if (call.name === "plan_dashboard" && data.plan && typeof data.plan === "object") {
    const plan = data.plan as DashboardPlanData;
    return { description: plan.summary, plan, tierLabel: "TIER-2" };
  }

  return {
    description: `${call.name} · dry run`,
    tierLabel: "TIER-2",
  };
}

export function planIdsFromRecords(records: Iterable<ToolCallRecord>): string[] {
  const ids: string[] = [];
  for (const call of records) {
    if (call.state !== "ok") continue;
    const data = call.data as Record<string, unknown> | undefined;
    if (data?.preview !== true) continue;
    if (call.name === "plan_dashboard" && typeof data.plan_id === "string") {
      ids.push(data.plan_id);
    } else if (call.name === "stage_custom_card" && typeof data.card_id === "string") {
      ids.push(data.card_id);
    }
  }
  return ids;
}

export function planIdsFromItems(items: ChatItem[]): string[] {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.role === "user") return [];
    if (item.role !== "assistant") continue;
    const records = item.fragments.flatMap((f) => (f.kind === "tool" ? [f.call] : []));
    return planIdsFromRecords(records);
  }
  return [];
}

function formatTargetList(value: unknown): string {
  if (!value) return "";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function compactValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v.length > 30 ? `${v.slice(0, 27)}…` : v;
  if (typeof v === "object") {
    const s = JSON.stringify(v);
    return s.length > 30 ? `${s.slice(0, 27)}…` : s;
  }
  return String(v);
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

