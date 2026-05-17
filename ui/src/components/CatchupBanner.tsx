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

import { clearPendingActions, type CatchupData } from "../api";

interface Props {
  data: CatchupData;
  onDismiss: () => void;
}

// Injected as a divider between old messages and the input area.
// Not a message from Mylo — a lightweight status block that gives
// context without pretending the old conversation didn't happen.
// Pending actions from the proactive engine show here with accent
// styling so the user knows Mylo has something to discuss.
export function CatchupBanner({ data, onDismiss }: Props) {
  if (!data.show_banner) return null;

  const hasPending = (data.pending_actions?.length ?? 0) > 0;
  const regularLines = (data.lines || []).filter(
    (line) =>
      !data.pending_actions?.some((pa) => pa.message === line),
  );

  const handleDismiss = async () => {
    if (hasPending) {
      await clearPendingActions();
    }
    onDismiss();
  };

  return (
    <div className="my-4">
      <div
        className="flex items-center gap-3 text-center"
        style={{ color: "var(--color-text-dim)" }}
      >
        <div
          className="flex-1 h-px"
          style={{ backgroundColor: "var(--color-border)" }}
        />
        <span className="font-mono text-[9px] uppercase tracking-label shrink-0">
          {data.gap_label}
        </span>
        <div
          className="flex-1 h-px"
          style={{ backgroundColor: "var(--color-border)" }}
        />
      </div>
      <div
        className="mt-3 rounded border px-4 py-3"
        style={{
          borderColor: hasPending
            ? "var(--color-border-accent)"
            : "var(--color-border)",
          backgroundColor: "var(--color-surface)",
        }}
      >
        <div
          className="font-mono text-[10px] uppercase tracking-label mb-2"
          style={{ color: "var(--color-text-dim)" }}
        >
          Since we last talked
        </div>

        {/* Regular activity lines */}
        {regularLines.length > 0 ? (
          <ul className="space-y-1 mb-2">
            {regularLines.map((line, i) => (
              <li
                key={i}
                className="flex items-start gap-2 font-sans text-[12.5px]"
                style={{ color: "var(--color-text-muted)" }}
              >
                <span
                  className="mt-1.5 shrink-0 h-[4px] w-[4px] rounded-full"
                  style={{ backgroundColor: "var(--color-text-muted)" }}
                />
                {line}
              </li>
            ))}
          </ul>
        ) : null}

        {/* Pending actions — accent-styled, these are actionable */}
        {hasPending ? (
          <div className="space-y-1.5">
            <div
              className="font-mono text-[10px] uppercase tracking-label mt-2"
              style={{ color: "var(--color-accent)" }}
            >
              Needs attention
            </div>
            {data.pending_actions!.map((pa) => (
              <div
                key={pa.id}
                className="flex items-start gap-2 font-sans text-[12.5px]"
                style={{ color: "var(--color-text)" }}
              >
                <span
                  className="mt-1.5 shrink-0 h-[4px] w-[4px] rounded-full dot-glow-accent"
                  style={{ backgroundColor: "var(--color-accent)" }}
                />
                {pa.message}
              </div>
            ))}
          </div>
        ) : null}

        <div className="mt-3 flex items-center justify-between">
          <span
            className="font-sans text-[12.5px]"
            style={{ color: "var(--color-text)" }}
          >
            {hasPending
              ? "Tell me what to do about these, or just start chatting."
              : "What are you working on?"}
          </span>
          <button
            type="button"
            onClick={() => void handleDismiss()}
            className="font-mono text-[9px] uppercase tracking-label"
            style={{ color: "var(--color-text-dim)" }}
          >
            dismiss
          </button>
        </div>
      </div>
    </div>
  );
}
