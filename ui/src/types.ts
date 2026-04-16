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
}

export type ChatFragment =
  | { kind: "text"; text: string }
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

export interface MemoryPattern {
  id: string;
  description: string;
  confidence: number;
  first_observed?: string | null;
  last_confirmed?: string | null;
  source?: string;
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
  patterns: MemoryPattern[];
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

export interface ScratchpadEntry {
  type: string;
  content: string;
  scope: Record<string, unknown>;
  recorded: string | null;
  confidence: number | null;
  conversation_id: string | null;
}
