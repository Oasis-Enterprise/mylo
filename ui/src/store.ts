// Session-level state for the header status bar and input footer.
// Chat items, SSE plumbing, and approval state still live in App.tsx
// — only the cross-cutting counters (turns, cumulative tokens, cost,
// last-turn-usage) live here so Header and Composer don't need prop-
// drilling.

import { create } from "zustand";
import { estimateCost, type UsageDelta } from "./lib/cost";

interface SessionState {
  turns: number;
  inputTokens: number;
  outputTokens: number;
  cachedReadTokens: number;
  cachedWriteTokens: number;
  costUsd: number;
  lastTurnTokens: number;
  lastContextTokens: number;
  // Model whose rates drive the cost calc. Can be swapped if we ever
  // expose a model switcher in the panel.
  model: string;

  recordTurn(usage: UsageDelta): void;
  reset(): void;
  setModel(model: string): void;
}

export const useSession = create<SessionState>((set, get) => ({
  turns: 0,
  inputTokens: 0,
  outputTokens: 0,
  cachedReadTokens: 0,
  cachedWriteTokens: 0,
  costUsd: 0,
  lastTurnTokens: 0,
  lastContextTokens: 0,
  model: "claude-sonnet-4-6",

  recordTurn(usage) {
    const delta = estimateCost(usage, get().model);
    const inTokens = usage.input_tokens ?? 0;
    const outTokens = usage.output_tokens ?? 0;
    const cacheRead = usage.cache_read_input_tokens ?? 0;
    const cacheWrite = usage.cache_creation_input_tokens ?? 0;
    set((s) => ({
      turns: s.turns + 1,
      inputTokens: s.inputTokens + inTokens,
      outputTokens: s.outputTokens + outTokens,
      cachedReadTokens: s.cachedReadTokens + cacheRead,
      cachedWriteTokens: s.cachedWriteTokens + cacheWrite,
      costUsd: s.costUsd + delta,
      lastTurnTokens: inTokens + outTokens,
      // The "context used" counter in the header tracks how close
      // we are to the model's window. Input tokens on the last turn
      // is the best proxy — includes system prompt + history.
      lastContextTokens: inTokens + cacheRead,
    }));
  },

  reset() {
    set({
      turns: 0,
      inputTokens: 0,
      outputTokens: 0,
      cachedReadTokens: 0,
      cachedWriteTokens: 0,
      costUsd: 0,
      lastTurnTokens: 0,
      lastContextTokens: 0,
    });
  },

  setModel(model) {
    set({ model });
  },
}));
