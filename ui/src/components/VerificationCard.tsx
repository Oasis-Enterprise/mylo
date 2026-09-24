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

import type { VerificationData } from "../api";
import { formatRelative } from "../lib/format";
import { IconButton } from "./ui/Button";

interface Props {
  item: VerificationData;
  onDismiss: (id: string) => void;
}

const TONE: Record<VerificationData["status"], { label: string; color: string }> = {
  verified: { label: "Verified", color: "var(--color-accent)" },
  failed: { label: "Verification failed", color: "var(--color-error)" },
  rolled_back: { label: "Rolled back", color: "var(--color-warning)" },
  pending: { label: "Verifying", color: "var(--color-text-muted)" },
};

// Outcome of a background verification, shown once in the chat stream
// until dismissed. The model says "applied, verifying" when it writes;
// this card is where the truth lands.
export function VerificationCard({ item, onDismiss }: Props) {
  const tone = TONE[item.status] ?? TONE.pending;
  return (
    <div
      className="rounded border px-3 py-2"
      style={{ borderColor: tone.color, backgroundColor: "var(--color-surface)" }}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className="font-mono text-[10px] font-bold uppercase tracking-label"
          style={{ color: tone.color }}
        >
          {tone.label} · {item.target}
        </span>
        <IconButton onClick={() => onDismiss(item.id)} title="Dismiss" aria-label="Dismiss">
          ✕
        </IconButton>
      </div>
      {item.message ? (
        <div className="mt-1 font-sans text-[12.5px]" style={{ color: "var(--color-text)" }}>
          {item.message}
        </div>
      ) : null}
      <div className="mt-1 font-mono text-[9px]" style={{ color: "var(--color-text-dim)" }}>
        {item.tool} · {formatRelative(item.completed_at ?? item.requested_at)}
      </div>
    </div>
  );
}
