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

import type { ReactNode } from "react";
import { Tag, type TagTone } from "./Tag";

export type Severity = "critical" | "high" | "medium" | "low" | "info";

interface Props {
  severity: Severity;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}

// Issue card with a 3px left accent border in the severity color.
// Used in MemoryTab for conflicts and active issues, and available
// as a reusable primitive for future proactive-notification flows.
export function SeverityCard({ severity, title, children, action }: Props) {
  const meta = severityMeta(severity);
  return (
    <div
      className="rounded bg-surface border"
      style={{
        borderLeftWidth: 3,
        borderLeftColor: meta.border,
        borderColor: "var(--color-border)",
      }}
    >
      <div className="flex items-start justify-between gap-3 px-3 pt-2.5 pb-1">
        <div className="flex items-center gap-2">
          <Tag tone={meta.tagTone}>{meta.label}</Tag>
          <div className="font-sans text-[12.5px] font-semibold text-text leading-tight">
            {title}
          </div>
        </div>
        {action}
      </div>
      {children ? (
        <div
          className="px-3 pb-3 font-sans text-[12px] leading-[1.5]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {children}
        </div>
      ) : null}
    </div>
  );
}

function severityMeta(severity: Severity): {
  label: string;
  tagTone: TagTone;
  border: string;
} {
  switch (severity) {
    case "critical":
      return { label: "CRIT", tagTone: "error", border: "var(--color-error)" };
    case "high":
      return { label: "HIGH", tagTone: "warning", border: "var(--color-warning)" };
    case "medium":
      return { label: "MED", tagTone: "info", border: "var(--color-info)" };
    case "low":
      return { label: "LOW", tagTone: "muted", border: "var(--color-text-muted)" };
    case "info":
      return { label: "INFO", tagTone: "default", border: "var(--color-accent)" };
  }
}
