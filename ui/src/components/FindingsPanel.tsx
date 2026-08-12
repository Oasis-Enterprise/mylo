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

import { useState } from "react";
import {
  clearPendingActions,
  dismissPendingAction,
  type PendingActionData,
} from "../api";

interface Props {
  findings: PendingActionData[];
  // Fired after a dismissal mutates the server state so the caller can
  // refresh the header badge count.
  onChanged: () => void;
  onClose: () => void;
}

// Always-available findings list, toggled from the header badge.
// Mirrors the CatchupBanner "Needs attention" block: accent dots,
// per-item ✕ (7-day snooze), dismiss-all. Rendered inline at the top
// of the chat scroll area — no overlay/positioning machinery.
export function FindingsPanel({ findings, onChanged, onClose }: Props) {
  const [dismissedIds, setDismissedIds] = useState<string[]>([]);
  const visible = findings.filter((pa) => !dismissedIds.includes(pa.id));

  const handleDismissItem = (id: string) => {
    setDismissedIds((prev) => [...prev, id]);
    void dismissPendingAction(id).then(onChanged);
  };

  const handleDismissAll = async () => {
    await clearPendingActions();
    onChanged();
    onClose();
  };

  return (
    <div
      className="rounded border px-4 py-3"
      style={{
        borderColor: "var(--color-border-accent)",
        backgroundColor: "var(--color-surface)",
      }}
    >
      <div className="flex items-center justify-between mb-2">
        <span
          className="font-mono text-[10px] uppercase tracking-label"
          style={{ color: "var(--color-accent)" }}
        >
          Findings
        </span>
        <button
          type="button"
          onClick={onClose}
          className="font-mono text-[10px] px-1"
          style={{ color: "var(--color-text-dim)" }}
        >
          close
        </button>
      </div>

      {visible.length === 0 ? (
        <div
          className="font-sans text-[12.5px]"
          style={{ color: "var(--color-text-muted)" }}
        >
          Nothing needs attention right now.
        </div>
      ) : (
        <div className="space-y-1.5">
          {visible.map((pa) => (
            <div
              key={pa.id}
              className="flex items-start gap-2 font-sans text-[12.5px]"
              style={{ color: "var(--color-text)" }}
            >
              <span
                className="mt-1.5 shrink-0 h-[4px] w-[4px] rounded-full dot-glow-accent"
                style={{ backgroundColor: "var(--color-accent)" }}
              />
              <span className="flex-1">{pa.message}</span>
              <button
                type="button"
                onClick={() => handleDismissItem(pa.id)}
                title="Dismiss — won't resurface for a week"
                className="font-mono text-[10px] shrink-0 px-1"
                style={{ color: "var(--color-text-dim)" }}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      {visible.length > 1 ? (
        <div className="mt-3 flex justify-end">
          <button
            type="button"
            onClick={() => void handleDismissAll()}
            className="font-mono text-[9px] uppercase tracking-label"
            style={{ color: "var(--color-text-dim)" }}
          >
            dismiss all
          </button>
        </div>
      ) : null}
    </div>
  );
}
