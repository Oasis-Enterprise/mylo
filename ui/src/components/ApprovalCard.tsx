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

import { useEffect, useState } from "react";
import { StatusDot } from "./StatusDot";
import { Tag } from "./Tag";

// One previewed change awaiting approval.
export interface ApprovalItem {
  description?: string;
  // Optional diff block. When present, rendered as a red minus /
  // green plus pair in mono. Both sides are single lines — wrap at
  // the caller if you need multi-line diffs.
  diff?: { before: string; after: string };
  // Metadata line under the diff — "references: 0 automations · 0 dashboards · 0 scripts".
  meta?: string;
  tierLabel?: string;
  destructive?: boolean;
  target?: string;
}

interface Props {
  // One or more previewed changes. The model can dry-run several writes
  // in a single turn; they're all listed here and approved together.
  items: ApprovalItem[];
  // Indices of the items the user chose to apply (checkbox selection
  // when there's more than one item; all of them otherwise).
  onApprove: (selectedIndices: number[]) => void;
  onReject: () => void;
  // True when an apply has been queued but not yet submitted (prior
  // turn's SSE stream still closing). Disables APPLY to avoid double-
  // send and swaps the label to "Applying…" so the click feels live.
  applying?: boolean;
}

// Inline approval card rendered in the chat stream, not as a bottom
// bar. Pulsing dot + "AWAITING APPROVAL" label in accent mono, then one
// block per previewed change (tier tag + description + diff + metadata),
// and a single Reject / Apply pair that authorizes the whole set.
export function ApprovalCard({ items, onApprove, onReject, applying = false }: Props) {
  const multiple = items.length > 1;
  const [selected, setSelected] = useState<boolean[]>(() => items.map(() => true));
  // eslint-disable-next-line react-hooks/exhaustive-deps -- reset only when the item count changes
  useEffect(() => setSelected(items.map(() => true)), [items.length]);
  const chosen = selected.flatMap((v, i) => (v ? [i] : []));
  const destructive = chosen.some((i) => items[i].destructive);

  return (
    <div
      className="rounded border"
      style={{
        borderColor: "var(--color-border-accent)",
        borderWidth: 1,
      }}
    >
      <div className="flex items-center gap-2 px-3 py-2 border-b"
        style={{ borderColor: "var(--color-border)" }}
      >
        <StatusDot tone="accent" pulse />
        <span
          className="font-mono text-[10px] font-bold uppercase tracking-label"
          style={{ color: "var(--color-accent)" }}
        >
          Awaiting approval
        </span>
        {multiple ? (
          <span
            className="font-mono text-[10px]"
            style={{ color: "var(--color-text-dim)" }}
          >
            · {items.length} changes
          </span>
        ) : null}
      </div>

      {items.map((item, i) => {
        const hasBody = item.description || item.diff || item.meta;
        return (
          <div
            key={i}
            className="px-3 py-3 space-y-2.5"
            style={
              i > 0
                ? { borderTop: "1px solid var(--color-border)" }
                : undefined
            }
          >
            <div className="flex items-center gap-2">
              {multiple ? (
                <input
                  type="checkbox"
                  checked={selected[i]}
                  onChange={() => setSelected((s) => s.map((v, j) => (j === i ? !v : v)))}
                  aria-label={`Include change ${i + 1}`}
                />
              ) : null}
              {multiple ? (
                <span
                  className="font-mono text-[10px]"
                  style={{ color: "var(--color-text-dim)" }}
                >
                  {i + 1}.
                </span>
              ) : null}
              <Tag tone="muted">{item.tierLabel ?? "TIER-2"}</Tag>
            </div>
            {hasBody ? (
              <>
                {item.description ? (
                  <div
                    className="font-sans text-[13px]"
                    style={{ color: "var(--color-text)" }}
                  >
                    {item.description}
                  </div>
                ) : null}
                {item.diff ? (
                  <DiffBlock before={item.diff.before} after={item.diff.after} />
                ) : null}
                {item.meta ? (
                  <div
                    className="font-mono text-[10px]"
                    style={{ color: "var(--color-text-dim)" }}
                  >
                    {item.meta}
                  </div>
                ) : null}
              </>
            ) : null}
          </div>
        );
      })}

      {destructive ? (
        <div
          className="px-3 pb-1 font-mono text-[10px]"
          style={{ color: "var(--color-error)" }}
        >
          Deletes: {chosen.filter((i) => items[i].destructive).map((i) => items[i].target ?? items[i].description).join(", ")}
        </div>
      ) : null}
      <div className="flex items-center justify-end gap-2 px-3 py-2 border-t"
        style={{ borderColor: "var(--color-border)" }}
      >
        <button
          type="button"
          onClick={onReject}
          className="rounded border px-3 py-1 font-mono text-[11px] font-bold uppercase tracking-label hover:opacity-80"
          style={{
            borderColor: "var(--color-border)",
            color: "var(--color-text-muted)",
            background: "transparent",
          }}
        >
          Reject
        </button>
        <button
          type="button"
          onClick={() => onApprove(chosen)}
          disabled={applying || chosen.length === 0}
          className={
            destructive
              ? "rounded px-3 py-1 font-mono text-[11px] font-bold uppercase tracking-label hover:brightness-110 disabled:opacity-60"
              : "btn-glow rounded px-3 py-1 font-mono text-[11px] font-bold uppercase tracking-label hover:brightness-110 disabled:opacity-60"
          }
          style={
            destructive
              ? {
                  backgroundColor: "var(--color-error-soft)",
                  border: "1px solid var(--color-error)",
                  color: "var(--color-error)",
                }
              : {
                  backgroundColor: "var(--color-accent-soft)",
                  border: "1px solid var(--color-accent)",
                  color: "var(--color-accent)",
                }
          }
        >
          {applying
            ? "Applying…"
            : !multiple
              ? "Apply"
              : chosen.length === items.length
                ? `Apply all ${items.length}`
                : `Apply ${chosen.length} of ${items.length}`}
        </button>
      </div>
    </div>
  );
}

function DiffBlock({ before, after }: { before: string; after: string }) {
  return (
    <div
      className="rounded border font-mono text-[11px] leading-[1.55]"
      style={{
        borderColor: "var(--color-border)",
        backgroundColor: "var(--color-surface)",
      }}
    >
      <div
        className="px-3 py-1.5 flex items-start gap-2"
        style={{ color: "var(--color-text-dim)" }}
      >
        <span style={{ color: "var(--color-error)" }}>-</span>
        <span className="line-through break-all">{before}</span>
      </div>
      <div
        className="px-3 py-1.5 flex items-start gap-2 border-t"
        style={{
          borderColor: "var(--color-border)",
          color: "var(--color-text)",
        }}
      >
        <span style={{ color: "var(--color-accent)" }}>+</span>
        <span className="break-all" style={{ color: "var(--color-accent)" }}>
          {after}
        </span>
      </div>
    </div>
  );
}
