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

import { useEffect, useRef, useState } from "react";
import { contextWindowFor } from "../lib/cost";
import { formatDollars, formatTokens } from "../lib/format";
import { useSession } from "../store";
import { PrimaryButton } from "./ui/Button";

interface Props {
  disabled?: boolean;
  onSubmit: (message: string) => void | Promise<void>;
  // Set by the plan card's Modify button: pre-fill and focus. The
  // nonce changes on every request so the same text can be re-applied.
  draft?: { text: string; nonce: number } | null;
  // While a turn runs the send button becomes Stop. `stopping` is true
  // after the click until the server ends the turn.
  onStop?: () => void;
  stopping?: boolean;
}

// Composer with the tactical status row above the input: budget
// (last-turn context vs model window) and session cost. Sits inside
// a surface-background container with a top border that aligns with
// the ApprovalCard border weight. The input itself is dark on dark
// with a muted border and accent focus — deliberately uncluttered
// so the status row reads as the ambient telemetry.
export function Composer({ disabled, onSubmit, draft, onStop, stopping }: Props) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const lastContext = useSession((s) => s.lastContextTokens);
  const cost = useSession((s) => s.costUsd);
  const model = useSession((s) => s.model);

  useEffect(() => {
    if (!draft) return;
    setText(draft.text);
    const el = textareaRef.current;
    if (el) {
      el.focus();
      el.setSelectionRange(draft.text.length, draft.text.length);
    }
  }, [draft]);

  async function submit() {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    setText("");
    await onSubmit(trimmed);
    textareaRef.current?.focus();
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      className="border-t bg-surface px-4 pt-2 pb-3"
      style={{ borderColor: "var(--color-border)" }}
    >
      <div
        className="mb-1.5 flex items-center gap-3 font-mono text-[10px] sm:text-[9px] leading-none"
        style={{ color: "var(--color-text-muted)" }}
      >
        <span>
          <span style={{ color: "var(--color-text-dim)" }}>budget: </span>
          <span style={{ color: "var(--color-text)" }}>
            {formatTokens(lastContext)}/{formatTokens(contextWindowFor(model))}
          </span>{" "}
          tokens
        </span>
        <span style={{ color: "var(--color-text-dim)" }}>·</span>
        <span>
          <span style={{ color: "var(--color-text-dim)" }}>cost: </span>
          <span style={{ color: "var(--color-text)" }}>{formatDollars(cost)}</span>{" "}
          this session
        </span>
      </div>
      <div className="flex items-stretch gap-2">
        <div
          className="flex-1 rounded border"
          style={{
            backgroundColor: "var(--color-bg)",
            borderColor: "var(--color-border)",
          }}
        >
          <textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit();
              }
            }}
            placeholder={
              disabled ? "Waiting for response…" : "Ask Mylo about your home…"
            }
            disabled={disabled}
            rows={1}
            aria-label="Message Mylo"
            className="w-full resize-none bg-transparent border-0 font-sans text-[12.5px] px-3 py-2.5 disabled:opacity-60"
            style={{ color: "var(--color-text)" }}
          />
        </div>
        {disabled ? (
          <PrimaryButton
            tone="error"
            glow={false}
            aria-label="Stop"
            title="Stop"
            onClick={onStop}
            disabled={stopping || !onStop}
            className="px-4"
          >
            {stopping ? "Stopping…" : "■"}
          </PrimaryButton>
        ) : (
          <PrimaryButton
            type="submit"
            aria-label="Send"
            title="Send"
            disabled={!text.trim()}
            className="px-4"
          >
            →
          </PrimaryButton>
        )}
      </div>
    </form>
  );
}
