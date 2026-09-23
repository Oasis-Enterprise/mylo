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

import type {
  ApprovalContext,
  ChatItem,
  DashboardPlanData,
  PendingQuestion,
  StagedCardData,
  ToolCallRecord,
} from "../types";

export function detectPendingApproval(items: ChatItem[]): boolean {
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
      if (callNeedsApproval(frag.call)) return true;
    }
    // Only check the most recent assistant turn.
    return false;
  }
  return false;
}

export function findPendingQuestion(items: ChatItem[]): PendingQuestion | null {
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

export function findApprovalContexts(items: ChatItem[]): ApprovalContext[] {
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
      if (callNeedsApproval(frag.call)) {
        contexts.push(buildApprovalContext(frag.call));
      }
    }
    return contexts;
  }
  return [];
}

export function callNeedsApproval(call: ToolCallRecord): boolean {
  if (call.errorCode === "confirmation_required") return true;
  return (
    call.state === "ok" &&
    typeof call.data === "object" &&
    call.data !== null &&
    (call.data as Record<string, unknown>).preview === true
  );
}

export function buildApprovalContext(call: ToolCallRecord): ApprovalContext {
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

export function formatTargetList(value: unknown): string {
  if (!value) return "";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

export function compactValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v.length > 30 ? `${v.slice(0, 27)}…` : v;
  if (typeof v === "object") {
    const s = JSON.stringify(v);
    return s.length > 30 ? `${s.slice(0, 27)}…` : s;
  }
  return String(v);
}

export interface TurnState {
  pendingApproval: { planIds: string[] } | null;
  approvalContexts: ApprovalContext[];
  pendingQuestion: PendingQuestion | null;
}

// The single source of truth for everything the UI derives from the
// conversation: is there a previewed write awaiting Apply, what are its
// contexts, is there an unanswered question. Called after hydration and
// whenever a turn ends (streamed or recovered) — never re-parsed ad hoc.
export function deriveTurnState(items: ChatItem[]): TurnState {
  const approvalContexts = findApprovalContexts(items);
  const pendingApproval = detectPendingApproval(items)
    ? { planIds: planIdsFromItems(items) }
    : null;
  return { pendingApproval, approvalContexts, pendingQuestion: findPendingQuestion(items) };
}
