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

// Rough per-model cost estimator for the session footer. Prices are
// per million tokens in USD and snapshot from Anthropic's published
// pricing at build time; they'll drift — keep close enough for a
// "what did this conversation cost" indicator, not accounting.

interface ModelRates {
  input: number; // $ per 1M input tokens
  output: number; // $ per 1M output tokens
  cacheRead: number; // $ per 1M cached-read tokens
  cacheWrite: number; // $ per 1M cache-write tokens
}

// Defaults keyed to the Sonnet/Opus 4.6 tier. Unknown models use
// Sonnet rates so the surface never shows "$NaN".
const RATES: Record<string, ModelRates> = {
  "claude-sonnet-4-6": { input: 3, output: 15, cacheRead: 0.3, cacheWrite: 3.75 },
  "claude-opus-4-6": { input: 15, output: 75, cacheRead: 1.5, cacheWrite: 18.75 },
  "claude-haiku-4-5-20251001": { input: 1, output: 5, cacheRead: 0.1, cacheWrite: 1.25 },
};

const FALLBACK: ModelRates = RATES["claude-sonnet-4-6"];

export interface UsageDelta {
  input_tokens?: number;
  output_tokens?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
}

export function estimateCost(usage: UsageDelta, model?: string): number {
  const rates = (model && RATES[model]) || FALLBACK;
  const cost =
    ((usage.input_tokens ?? 0) * rates.input) / 1_000_000 +
    ((usage.output_tokens ?? 0) * rates.output) / 1_000_000 +
    ((usage.cache_read_input_tokens ?? 0) * rates.cacheRead) / 1_000_000 +
    ((usage.cache_creation_input_tokens ?? 0) * rates.cacheWrite) / 1_000_000;
  return cost;
}

// Sonnet 4.6 context window — used for the "budget 48k/200k" header.
// If we add a model picker later, compute from the selected model.
export const MODEL_CONTEXT_WINDOW = 200_000;
