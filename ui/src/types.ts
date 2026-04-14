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
