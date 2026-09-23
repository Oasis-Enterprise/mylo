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

export type Role = "user" | "assistant";

export type ToolCallState = "pending" | "ok" | "error";

export interface ToolCallRecord {
  id: string;
  name: string;
  input: Record<string, unknown>;
  state: ToolCallState;
  errorCode?: string | null;
  // Short human-facing summary of the result (e.g. "2 lights (2 on)").
  summary?: string;
  // Raw result data (trimmed) — used to display dry-run previews inline.
  data?: unknown;
  // Client-side timing for the tactical status line. Populated when
  // the tool_call event arrives and finalized on tool_result.
  startedAt?: number;
  durationMs?: number;
}

export interface ChatItem {
  // Role + ordered list of "fragments" — text blocks and tool calls —
  // preserved in the order they arrived from the LLM. Mirrors the shape
  // of Anthropic's content blocks so rendering is deterministic.
  id: string;
  role: Role;
  fragments: ChatFragment[];
  pending: boolean;
  // Live status while the turn runs ("Reading your automations"). Set
  // from `status` events; cleared on `done`. Phase "cancelling" is
  // client-side only (Stop clicked, server not yet finished).
  status?: { phase: string; label: string };
}

export type ChatFragment =
  | { kind: "text"; text: string }
  // Streamed text not yet finalised. Replaced by a `text` fragment when
  // the model call finishes; never persisted.
  | { kind: "draft"; text: string }
  | { kind: "tool"; call: ToolCallRecord };

export interface DoneEvent {
  stopReason: string;
  usage: {
    input_tokens?: number;
    output_tokens?: number;
    cache_read_input_tokens?: number;
    cache_creation_input_tokens?: number;
  };
}

// ─── Memory tab types ──────────────────────────────────────────────────────

export interface MemoryNote {
  id: string;
  content: string;
  entity?: string | null;
  area?: string | null;
  scope?: string | null;
  source?: string;
  added?: string | null;
  metadata?: {
    created?: string | null;
    last_referenced?: string | null;
    reference_count?: number;
    source?: string;
    priority?: string;
    ttl?: string | null;
  };
}

export interface MemoryIssue {
  id: string;
  description: string;
  first_seen?: string | null;
  status: string;
  suggested_fix?: string | null;
  evidence?: string[];
  user_acknowledged?: boolean;
}

export interface MemoryRejection {
  id: string;
  suggestion: string;
  reason?: string | null;
  date?: string | null;
}

export interface MemoryConflict {
  id: string;
  type: string;
  subject: Record<string, unknown>;
  claim_a?: { content: string; source: string; date?: string | null } | null;
  claim_b?: { content: string; source: string; date?: string | null } | null;
  status: string;
  resolution?: Record<string, unknown> | null;
}

export interface MemoryHouseholdMember {
  name: string;
  role: string;
  presence_entity?: string | null;
  notes: string[];
}

export interface MemoryFull {
  version: number;
  last_sync: string | null;
  household: {
    members: MemoryHouseholdMember[];
    shared: Record<string, unknown>;
  };
  preferences: Record<string, unknown>;
  notes: MemoryNote[];
  known_issues: MemoryIssue[];
  rejected: MemoryRejection[];
  conflicts: MemoryConflict[];
  monitored_entities: string[];
}

export interface PruneCandidate {
  section: string;
  id: string;
  reason: string;
  summary: string;
}

export interface SyncResult {
  ok: boolean;
  changed: boolean;
  applied: boolean;
  summary: string;
  conflicts_added: number;
  prune_candidates: PruneCandidate[];
}

export interface AuditEntry {
  timestamp: string;
  conversation_id: string;
  tool_name: string;
  tier: number;
  params: Record<string, unknown>;
  dry_run: boolean;
  user_approved: boolean;
  result: "success" | "failure" | "rolled_back" | "denied";
  details: Record<string, unknown>;
  rollback_performed?: boolean;
  file_backup_path?: string | null;
}

export interface ScratchpadEntry {
  type: string;
  content: string;
  scope: Record<string, unknown>;
  recorded: string | null;
  confidence: number | null;
  conversation_id: string | null;
}

// ─── Dashboard plan (plan_dashboard result) ────────────────────────────────

export interface PlanSectionData {
  heading: string;
  cards: Record<string, unknown>[];
  column_span?: number | null;
}

// Ops are rendered by their `op` tag; other fields are read loosely.
export interface PlanOpData {
  op: string;
  [key: string]: unknown;
}

export interface PlanFingerprint {
  type: string;
  entity?: string | null;
}

export interface PlanResolvedTarget {
  op_index: number;
  section_index?: number | null;
  to_section_index?: number | null;
  card_index?: number | null;
  fingerprint?: PlanFingerprint | null;
}

export interface PlanIssueData {
  severity: "error" | "warning";
  code: string;
  message: string;
  op_index?: number | null;
}

export interface DashboardPlanData {
  plan_id: string;
  dashboard_id: string | null;
  summary: string;
  assumptions: string[];
  operations: PlanOpData[];
  resolved: PlanResolvedTarget[];
  issues: PlanIssueData[];
}

// ─── Staged custom card (stage_custom_card result) ─────────────────────────

export interface StagedCardData {
  card_id: string;
  element: string;
  action: "create" | "update";
  url: string;
  line_count: number;
  byte_count: number;
  warnings: { code: string; message: string; severity: string }[];
  previous_source: string | null;
  source: string;
  config_example: Record<string, unknown> | null;
  description: string;
}
