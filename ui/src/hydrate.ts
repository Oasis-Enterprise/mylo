// Convert raw conversation messages (from GET /api/conversation) into
// ChatItems the UI renders. Used on page-load rehydrate AND as a polling
// fallback when the SSE stream dies mid-turn (a thing that happens when
// a tier-2 write triggers reload_all and HA's ingress restarts).

import type { RawContentBlock, RawMessage } from "./api";
import type { ChatFragment, ChatItem, ToolCallRecord } from "./types";

function randomId(): string {
  return Math.random().toString(36).slice(2);
}

export function hydrateFromMessages(messages: RawMessage[]): ChatItem[] {
  // First pass: collect tool_result blocks keyed by tool_use_id so we
  // can stitch them back onto the assistant turn's tool_use blocks.
  const resultByUseId = new Map<string, { status: "ok" | "error"; data: unknown; errorCode?: string | null }>();
  for (const msg of messages) {
    if (msg.role !== "user" || !Array.isArray(msg.content)) continue;
    for (const block of msg.content) {
      if (block.type !== "tool_result") continue;
      try {
        const envelope = JSON.parse(block.content) as {
          status?: "ok" | "error";
          data?: unknown;
          error?: { code?: string };
        };
        resultByUseId.set(block.tool_use_id, {
          status: envelope.status === "ok" ? "ok" : "error",
          data: envelope.data,
          errorCode: envelope.error?.code ?? null,
        });
      } catch {
        resultByUseId.set(block.tool_use_id, { status: "error", data: null });
      }
    }
  }

  const items: ChatItem[] = [];

  for (const msg of messages) {
    if (msg.role === "user") {
      // String content = real user message. List content = tool_results
      // (already consumed by the previous assistant render).
      if (typeof msg.content === "string") {
        items.push({
          id: randomId(),
          role: "user",
          fragments: [{ kind: "text", text: msg.content }],
          pending: false,
        });
      }
      continue;
    }

    // Assistant: walk content blocks preserving order of text + tool_use.
    const fragments: ChatFragment[] = [];
    const blocks: RawContentBlock[] = Array.isArray(msg.content)
      ? msg.content
      : [{ type: "text", text: msg.content as string }];

    for (const block of blocks) {
      if (block.type === "text") {
        if (block.text) fragments.push({ kind: "text", text: block.text });
      } else if (block.type === "tool_use") {
        const result = resultByUseId.get(block.id);
        const record: ToolCallRecord = {
          id: block.id,
          name: block.name,
          input: block.input ?? {},
          state: result == null ? "pending" : result.status,
          errorCode: result?.errorCode,
          summary: summarize(result?.data),
          data: result?.data,
        };
        fragments.push({ kind: "tool", call: record });
      }
    }

    if (fragments.length > 0) {
      items.push({
        id: randomId(),
        role: "assistant",
        fragments,
        pending: false,
      });
    }
  }

  return items;
}

function summarize(data: unknown): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const d = data as Record<string, unknown>;
  for (const key of ["summary", "entities_found", "devices_found", "count"]) {
    if (key in d) return `${key}=${String(d[key])}`;
  }
  return undefined;
}
