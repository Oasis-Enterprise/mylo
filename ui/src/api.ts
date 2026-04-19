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

// SSE client for /api/chat.
//
// EventSource is read-only GET, but our chat endpoint is POST (message in
// body). We use fetch + ReadableStream to receive `event:/data:` frames
// manually. The parser handles multi-line data and ignores comments.

export type ServerEvent =
  | { type: "text"; text: string }
  | { type: "tool_call"; id: string; name: string; input: Record<string, unknown> }
  | {
      type: "tool_result";
      id: string;
      name: string;
      status: "ok" | "error";
      error_code: string | null;
      data: unknown;
    }
  | {
      type: "done";
      stop_reason: string;
      usage: Record<string, number>;
    }
  | { type: "error"; message: string; errorType: string };

export interface SendOptions {
  approved?: boolean;
  sessionCostUsd?: number;
  signal?: AbortSignal;
}

// Relative URLs so the UI works both at / (localhost dev) AND at
// /api/hassio_ingress/<token>/ (the HA panel iframe). An absolute
// "/api/..." URL under ingress would hit HA's own REST, not ours.
function apiUrl(path: string): string {
  return new URL(path, document.baseURI).toString();
}

export async function* streamChat(
  message: string,
  options: SendOptions = {},
): AsyncGenerator<ServerEvent, void, void> {
  const response = await fetch(apiUrl("api/chat"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      approved: Boolean(options.approved),
      session_cost_usd: options.sessionCostUsd ?? 0,
    }),
    signal: options.signal,
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`chat endpoint returned ${response.status}: ${text}`);
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error("no response body");

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let idx: number;
    // SSE frames are separated by a blank line (\n\n).
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const parsed = parseFrame(frame);
      if (parsed) yield parsed;
    }
  }
}

function parseFrame(frame: string): ServerEvent | null {
  let eventName = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (dataLines.length === 0) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(dataLines.join("\n"));
  } catch {
    return null;
  }
  const obj = payload as Record<string, unknown>;
  switch (eventName) {
    case "text":
      return { type: "text", text: String(obj.text ?? "") };
    case "tool_call":
      return {
        type: "tool_call",
        id: String(obj.id),
        name: String(obj.name),
        input: (obj.input as Record<string, unknown>) ?? {},
      };
    case "tool_result":
      return {
        type: "tool_result",
        id: String(obj.id),
        name: String(obj.name),
        status: (obj.status as "ok" | "error") ?? "error",
        error_code: (obj.error_code as string | null) ?? null,
        data: obj.data,
      };
    case "done":
      return {
        type: "done",
        stop_reason: String(obj.stop_reason ?? ""),
        usage: (obj.usage as Record<string, number>) ?? {},
      };
    case "error":
      return {
        type: "error",
        message: String(obj.message ?? ""),
        errorType: String(obj.type ?? "error"),
      };
    default:
      return null;
  }
}

export async function clearConversation(): Promise<void> {
  await fetch(apiUrl("api/conversation/clear"), { method: "POST" });
}

export interface RawMessage {
  role: "user" | "assistant";
  content: string | RawContentBlock[];
}

export type RawContentBlock =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; tool_use_id: string; content: string };

export interface ServerStatus {
  ok: boolean;
  entities: number;
  automations: number;
  memory: {
    last_sync: string | null;
    pending_conflicts: number;
  };
  has_provider: boolean;
}

export async function fetchStatus(): Promise<ServerStatus> {
  const response = await fetch(apiUrl("api/status"), { method: "GET" });
  if (!response.ok) {
    throw new Error(`status returned ${response.status}`);
  }
  return (await response.json()) as ServerStatus;
}

export interface CatchupData {
  show_banner: boolean;
  gap_label?: string;
  lines?: string[];
}

export async function fetchCatchup(): Promise<CatchupData> {
  const response = await fetch(apiUrl("api/catchup"), { method: "GET" });
  if (!response.ok) return { show_banner: false };
  return (await response.json()) as CatchupData;
}

export async function newConversation(): Promise<void> {
  await fetch(apiUrl("api/conversation/new"), { method: "POST" });
}

export async function fetchConversation(): Promise<RawMessage[]> {
  const response = await fetch(apiUrl("api/conversation"), { method: "GET" });
  if (!response.ok) {
    throw new Error(`conversation endpoint returned ${response.status}`);
  }
  const body = (await response.json()) as { messages: RawMessage[] };
  return body.messages || [];
}

// ─── Memory API ───────────────────────────────────────────────────────────

import type { AuditEntry, MemoryFull, ScratchpadEntry, SyncResult } from "./types";

export async function fetchMemoryFull(): Promise<MemoryFull> {
  const response = await fetch(apiUrl("api/memory/full"), { method: "GET" });
  if (!response.ok) {
    throw new Error(`memory/full returned ${response.status}`);
  }
  return (await response.json()) as MemoryFull;
}

export async function fetchScratchpad(): Promise<ScratchpadEntry[]> {
  const response = await fetch(apiUrl("api/memory/scratchpad"), { method: "GET" });
  if (!response.ok) {
    throw new Error(`memory/scratchpad returned ${response.status}`);
  }
  const body = (await response.json()) as { entries: ScratchpadEntry[] };
  return body.entries || [];
}

export async function syncMemory(options: { applyPrune?: boolean } = {}): Promise<SyncResult> {
  const response = await fetch(apiUrl("api/memory/sync"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ apply_prune: Boolean(options.applyPrune) }),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`memory/sync ${response.status}: ${text}`);
  }
  return (await response.json()) as SyncResult;
}

export async function applyPrune(options: { ids?: string[] } = {}): Promise<SyncResult> {
  const body: Record<string, unknown> = {};
  if (options.ids) body.ids = options.ids;
  const response = await fetch(apiUrl("api/memory/prune"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`memory/prune ${response.status}`);
  }
  return (await response.json()) as SyncResult;
}

export async function deleteMemoryItem(section: string, id: string): Promise<void> {
  const response = await fetch(apiUrl("api/memory/item"), {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ section, id }),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`memory/item DELETE ${response.status}: ${text}`);
  }
}

export async function fetchActivity(options: {
  limit?: number;
  tool?: string;
  result?: string;
} = {}): Promise<AuditEntry[]> {
  const params = new URLSearchParams();
  if (options.limit) params.set("limit", String(options.limit));
  if (options.tool) params.set("tool", options.tool);
  if (options.result) params.set("result", options.result);
  const qs = params.toString();
  const response = await fetch(apiUrl(`api/activity${qs ? `?${qs}` : ""}`), {
    method: "GET",
  });
  if (!response.ok) {
    throw new Error(`activity returned ${response.status}`);
  }
  const body = (await response.json()) as { entries: AuditEntry[] };
  return body.entries || [];
}

export async function resolveConflict(
  conflictId: string,
  choice: "a" | "b" | "dismiss",
): Promise<void> {
  const response = await fetch(
    apiUrl(`api/memory/conflict/${encodeURIComponent(conflictId)}/resolve`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ choice }),
    },
  );
  if (!response.ok) {
    throw new Error(`conflict resolve returned ${response.status}`);
  }
}
