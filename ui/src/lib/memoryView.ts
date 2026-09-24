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

import type { MemoryNote } from "../types";

export function humanizeKey(key: string): string {
  const spaced = key.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase().trim();
  return spaced ? spaced[0].toUpperCase() + spaced.slice(1) : key;
}

export function stringifyValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return v.map(stringifyValue).join(", ");
  try { return JSON.stringify(v); } catch { return String(v); }
}

export function entriesOf(obj: Record<string, unknown>): Array<{ key: string; label: string; value: string }> {
  return Object.entries(obj ?? {})
    .map(([key, value]) => ({ key, label: humanizeKey(key), value: stringifyValue(value) }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

export function sortNotesNewestFirst(notes: MemoryNote[]): MemoryNote[] {
  const when = (n: MemoryNote) => n.metadata?.created ?? n.added ?? "";
  return [...notes].sort((a, b) => (when(b) > when(a) ? 1 : when(b) < when(a) ? -1 : 0));
}

const KEY = "mylo.memory.advanced";
export function readAdvanced(): boolean { try { return localStorage.getItem(KEY) === "1"; } catch { return false; } }
export function writeAdvanced(v: boolean): void { try { localStorage.setItem(KEY, v ? "1" : "0"); } catch { /* ignore */ } }
