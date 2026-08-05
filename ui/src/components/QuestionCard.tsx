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

import { StatusDot } from "./StatusDot";

export interface QuestionOption {
  label: string;
  value?: string;
  description?: string;
}

export interface PendingQuestion {
  question: string;
  options: QuestionOption[];
  allowFreeText: boolean;
}

interface Props {
  question: PendingQuestion;
  // Fired with the option label — it's sent as a plain chat message,
  // which is exactly what the model reads as the answer.
  onSelect: (label: string) => void;
  disabled?: boolean;
}

// Inline question card rendered at the tail of the chat when the agent
// paused on ask_user. Mirrors the ApprovalCard visual language: accent
// header strip, then one button per option. Free-text answers go
// through the normal composer, which stays enabled.
export function QuestionCard({ question, onSelect, disabled = false }: Props) {
  return (
    <div
      className="rounded border"
      style={{ borderColor: "var(--color-border-accent)", borderWidth: 1 }}
    >
      <div
        className="flex items-center gap-2 px-3 py-2 border-b"
        style={{ borderColor: "var(--color-border)" }}
      >
        <StatusDot tone="accent" pulse />
        <span
          className="font-mono text-[10px] font-bold uppercase tracking-label"
          style={{ color: "var(--color-accent)" }}
        >
          Mylo needs your input
        </span>
      </div>

      <div className="px-3 py-3 space-y-2.5">
        <div className="font-sans text-[13px]" style={{ color: "var(--color-text)" }}>
          {question.question}
        </div>

        {question.options.length > 0 ? (
          <div className="flex flex-col gap-1.5">
            {question.options.map((opt) => (
              <button
                key={opt.label}
                type="button"
                disabled={disabled}
                onClick={() => onSelect(opt.label)}
                className="rounded border px-3 py-2 text-left hover:brightness-110 disabled:opacity-60"
                style={{
                  borderColor: "var(--color-border)",
                  backgroundColor: "var(--color-surface)",
                }}
              >
                <span
                  className="font-mono text-[11px] font-bold"
                  style={{ color: "var(--color-accent)" }}
                >
                  {opt.label}
                </span>
                {opt.description ? (
                  <span
                    className="ml-2 font-sans text-[11px]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {opt.description}
                  </span>
                ) : null}
              </button>
            ))}
          </div>
        ) : null}

        {question.allowFreeText ? (
          <div
            className="font-mono text-[10px]"
            style={{ color: "var(--color-text-dim)" }}
          >
            {question.options.length > 0
              ? "or type your own answer below"
              : "type your answer below"}
          </div>
        ) : null}
      </div>
    </div>
  );
}
