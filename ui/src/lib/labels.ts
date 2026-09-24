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

// Plain-language labels for tool names and tiers. The server can supply
// a lookup table (from /api/status) mapping raw tool names to a friendly
// label + tier; when it hasn't (or a name is missing from it), we
// humanize the raw snake_case name instead of showing it verbatim.

export type ToolLabelTable = Record<string, { label: string; tier: number }>;

export function toolLabel(name: string, table?: ToolLabelTable): string {
  const hit = table?.[name]?.label;
  if (hit) return hit;
  const words = name.replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : name;
}

const TIER_LABELS: Record<number, string> = { 1: "Read-only", 2: "Changes config", 3: "Controls devices" };

export function tierLabel(tier: number | string): string {
  const n = typeof tier === "number" ? tier : Number(String(tier).match(/(\d)/)?.[1] ?? 2);
  return TIER_LABELS[n] ?? TIER_LABELS[2];
}
